"""Binance USDT-M futures feed: diff depth + aggTrades for one symbol.

Sync follows Binance's documented procedure for a local order book:
  1. open the stream and buffer events,
  2. fetch a REST snapshot (lastUpdateId = L),
  3. drop events with u < L; the first applied event must have U <= L <= u,
  4. each following event must have pu == previous u, otherwise resync.
"""

import asyncio
import collections
import time

import aiohttp
import orjson

from .book import ASK, BID
from .feed import FeedBase, Resync

REST = "https://fapi.binance.com"
# Binance split its futures streams (2026): depth on /public, trades on /market. The legacy
# /stream endpoint still sends depth but no longer sends aggTrade.
WS_PUBLIC = "wss://fstream.binance.com/public/stream"
WS_MARKET = "wss://fstream.binance.com/market/stream"

__all__ = ["BinanceFeed", "Resync", "symbol_info", "list_symbols", "fetch_snapshot"]


async def symbol_info(session, symbol):
    """{"tick_size", "step_size"} or None if Binance has no such trading perpetual."""
    async with session.get(f"{REST}/fapi/v1/exchangeInfo") as r:
        r.raise_for_status()
        data = orjson.loads(await r.read())
    for s in data["symbols"]:
        if s["symbol"] == symbol and s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL":
            f = {x["filterType"]: x for x in s["filters"]}
            return {"tick_size": f["PRICE_FILTER"]["tickSize"], "step_size": f["LOT_SIZE"]["stepSize"]}
    return None


async def list_symbols(session):
    """All trading USDT-M perpetuals: [{"symbol", "tick", "step"}] (non-ASCII names left out)."""
    async with session.get(f"{REST}/fapi/v1/exchangeInfo") as r:
        r.raise_for_status()
        data = orjson.loads(await r.read())
    out = []
    for s in data["symbols"]:
        if (s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT" and s["symbol"].isascii()):
            f = {x["filterType"]: x for x in s["filters"]}
            out.append({"symbol": s["symbol"], "tick": f["PRICE_FILTER"]["tickSize"], "step": f["LOT_SIZE"]["stepSize"]})
    return sorted(out, key=lambda x: x["symbol"])


async def fetch_snapshot(session, symbol, limit=1000):
    async with session.get(f"{REST}/fapi/v1/depth", params={"symbol": symbol, "limit": limit}) as r:
        r.raise_for_status()
        return orjson.loads(await r.read())


class BinanceFeed(FeedBase):
    exchange = "binance"
    SNAPSHOT_LIMIT = 1000

    def __init__(self, session, symbol, native_tick, common_tick=None, history=None):
        super().__init__(session, symbol, native_tick, common_tick, history)
        self.last_u = None
        # Recent applied events for verification: (U, u, E, [(side, tick, old, new)]).
        self.recent = collections.deque(maxlen=1000)

    # ---- main loop ----------------------------------------------------------------

    async def run(self):
        await asyncio.gather(
            self._reconnecting("depth", self._depth_session),
            self._reconnecting("trades", self._trade_session, resets_book=False),
        )

    async def _messages(self, url, stream):
        async with self.session.ws_connect(url, heartbeat=30, max_msg_size=0) as ws:
            self.connection_opened(stream)
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    yield orjson.loads(msg.data)["data"]
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        raise ConnectionError("websocket closed")

    async def _trade_session(self):
        async for d in self._messages(f"{WS_MARKET}?streams={self.symbol.lower()}@aggTrade", "trades"):
            # m = buyer is maker -> the aggressor sold.
            self.on_trade(d["T"], d["p"], float(d["q"]), -1 if d["m"] else 1)

    async def _depth_session(self):
        self.synced = False
        self.last_u = None
        buffered = []
        snap_task = asyncio.create_task(fetch_snapshot(self.session, self.symbol))
        attempts = 0
        try:
            async for data in self._messages(f"{WS_PUBLIC}?streams={self.symbol.lower()}@depth@100ms", "depth"):
                if not self.synced:
                    buffered.append(data)
                    if snap_task.done():
                        snap = snap_task.result()
                        if snap["lastUpdateId"] < buffered[0]["U"]:
                            # The REST snapshot lags the stream: keep buffering, fetch again.
                            attempts += 1
                            self.stats["stale_snapshots"] += 1
                            if attempts > 20:
                                raise Resync("REST snapshot keeps lagging the stream")
                            snap_task = asyncio.create_task(self._delayed_snapshot(0.5 * attempts))
                            continue
                        self._start_from_snapshot(snap, buffered)
                        buffered = []
                    continue
                self._on_depth(data)
        finally:
            snap_task.cancel()

    async def _delayed_snapshot(self, delay):
        await asyncio.sleep(delay)
        return await fetch_snapshot(self.session, self.symbol)

    # ---- depth --------------------------------------------------------------------

    def _start_from_snapshot(self, snap, buffered):
        L = snap["lastUpdateId"]
        t = snap.get("E") or int(time.time() * 1000)
        bids, asks = self.reset_book(t, snap["bids"], snap["asks"])
        self._set_known_range(snap, bids, asks)
        self.recent.clear()
        self.last_u = None
        self.snapshot_id = L
        self.synced = True
        self.stats["snapshots"] += 1
        for ev in buffered:
            if ev["u"] < L:
                continue
            self._on_depth(ev)

    def _on_depth(self, ev):
        U, u, pu = ev["U"], ev["u"], ev["pu"]
        if self.last_u is None:
            if u < self.snapshot_id:
                return  # older than the snapshot
            if not (U <= self.snapshot_id <= u):
                raise Resync(f"first event U={U} u={u} does not cover snapshot {self.snapshot_id}")
        elif pu != self.last_u:
            self.stats["gaps"] += 1
            raise Resync(f"gap: pu={pu} != last u={self.last_u}")
        t = ev["E"]
        applied = []
        for side, levels in ((BID, ev["b"]), (ASK, ev["a"])):
            for p, q in levels:
                tick, old, new = self.set_level(t, side, p, float(q))
                applied.append((side, tick, old, new))
        self.after_event(t)
        self.recent.append((U, u, t, applied))
        self.last_u = u
        self.stats["depth_events"] += 1
        self.stats["level_updates"] += len(applied)

    # ---- verification -------------------------------------------------------------

    def book_at_update(self, L):
        """Rebuild the live book as it was at update id L, using the recent-event ring.
        Returns (book, excluded_ticks) or None if L is outside the ring. Levels touched by the
        event that straddles L are excluded: the snapshot may hold either value for them."""
        events = list(self.recent)
        if not events or L < events[0][0] or L > events[-1][1]:
            return None
        k = max(i for i, ev in enumerate(events) if ev[0] <= L)
        book = self.book.copy()
        for _U, _u, _t, applied in reversed(events[k + 1:]):
            for side, tick, old, _new in reversed(applied):
                book.apply(side, tick, old)
        U, u, _t, applied = events[k]
        excluded = {(side, tick) for side, tick, _o, _n in applied} if u > L else set()
        return book, excluded

    def _set_known_range(self, snap, bids, asks):
        """A snapshot only shows the nearest 1000 levels per side. Levels further out are
        unknown until they change. Track the range we know exactly."""
        full_b = len(snap["bids"]) < self.SNAPSHOT_LIMIT
        full_a = len(snap["asks"]) < self.SNAPSHOT_LIMIT
        e = self._edge()
        self.known_bid_floor = float("-inf") if full_b or not bids else min(bids) + e
        self.known_ask_ceil = float("inf") if full_a or not asks else max(asks) - e

    def _edge(self):
        """With several native levels per common tick, the outermost common tick of a
        snapshot may be cut off halfway: treat it as unknown."""
        return 1 if self.native is not None else 0

    async def refresh(self):
        """Fetch a REST snapshot, compare it with the live book inside the range we know
        exactly, then fill in deep levels the snapshot shows beyond that range (the snapshot
        reaches further once the price has moved). Returns the comparison result."""
        snap = await fetch_snapshot(self.session, self.symbol)
        L = snap["lastUpdateId"]
        for _ in range(50):  # wait until the stream has caught up with the snapshot
            if self.synced and self.last_u is not None and self.last_u >= L:
                break
            await asyncio.sleep(0.1)
        # From here on no awaits: the book cannot change under us.
        got = self.book_at_update(L)
        if got is None:
            return {"ok": False, "reason": f"update {L} not in recent ring"}
        book, excluded = got
        snap_sides = (self.aggregate(snap["bids"])[0], self.aggregate(snap["asks"])[0])
        e = self._edge()
        bid_floor = max(min(snap_sides[BID], default=0) + e, self.known_bid_floor)
        ask_ceil = min(max(snap_sides[ASK], default=0) - e, self.known_ask_ceil)
        compared, mismatches = self.compare_books(book.sides, snap_sides, excluded, bid_floor, ask_ceil,
                                                  self.tick_to_price)
        filled = self._extend_known(snap, snap_sides, L, excluded)
        return {
            "ok": not mismatches,
            "compared": compared,
            "excluded": len(excluded),
            "mismatches": len(mismatches),
            "examples": mismatches[:5],
            "filled_deep": filled,
        }

    def _extend_known(self, snap, snap_sides, L, excluded):
        """Adopt snapshot values for levels outside the known range, unless the stream has
        touched them since the snapshot (then the live value is newer)."""
        touched = set(excluded)
        for U, _u, _t, applied in self.recent:
            if U > L:
                touched.update((side, tick) for side, tick, _o, _n in applied)
        t = self.last_event_time
        filled = 0
        for side, raw in ((BID, snap["bids"]), (ASK, snap["asks"])):
            snap_lv = snap_sides[side]
            if not snap_lv:
                continue
            if side == BID:
                lo, hi = min(snap_lv), self.known_bid_floor
                if lo >= hi:
                    continue
                region = lambda tk, lo=lo, hi=hi: lo <= tk < hi  # noqa: E731
            else:
                lo, hi = self.known_ask_ceil, max(snap_lv)
                if hi <= lo:
                    continue
                region = lambda tk, lo=lo, hi=hi: lo < tk <= hi  # noqa: E731
            # Native levels: from the snapshot, and ours that the snapshot no longer has.
            snap_native = {self.native_key(p): float(q) for p, q in raw if float(q) > 0}
            ours_native = self.native[side].keys() if self.native is not None else self.book.sides[side].keys()
            for n in set(ours_native) | snap_native.keys():
                tick = self.tick_of_native(n)
                if not region(tick) or (side, tick) in touched:
                    continue
                _tk, old, new = self.set_native(t, side, n, snap_native.get(n, 0.0))
                if old != new:
                    filled += 1
        # Widen the known range (a side with fewer than 1000 levels is fully known).
        e = self._edge()
        new_b = float("-inf") if len(snap["bids"]) < self.SNAPSHOT_LIMIT else min(snap_sides[BID], default=self.known_bid_floor) + e
        new_a = float("inf") if len(snap["asks"]) < self.SNAPSHOT_LIMIT else max(snap_sides[ASK], default=self.known_ask_ceil) - e
        self.known_bid_floor = min(self.known_bid_floor, new_b)
        self.known_ask_ceil = max(self.known_ask_ceil, new_a)
        return filled
