"""Bybit linear (USDT perpetual) feed: orderbook.1000 + publicTrade for one symbol.

Bybit sends a full snapshot over the WebSocket, then deltas. The update id `u` grows by
exactly 1 per message (checked 2026-10-23), so a jump means a lost message -> resync. A
snapshot can also arrive unannounced (u == 1 after a Bybit restart): reset the book.
"""

import asyncio
import collections
import time

import aiohttp
import orjson

from .book import ASK, BID
from .feed import FeedBase, Resync

REST = "https://api.bybit.com"
WS = "wss://stream.bybit.com/v5/public/linear"
DEPTH = 1000

__all__ = ["BybitFeed", "symbol_info", "list_symbols"]


async def symbol_info(session, symbol):
    """{"tick_size", "step_size"} or None if Bybit has no such trading linear perpetual."""
    params = {"category": "linear", "symbol": symbol}
    async with session.get(f"{REST}/v5/market/instruments-info", params=params) as r:
        r.raise_for_status()
        data = orjson.loads(await r.read())
    for s in (data.get("result") or {}).get("list") or []:
        if s["symbol"] == symbol and s.get("status") == "Trading" and s.get("contractType") == "LinearPerpetual":
            return {"tick_size": s["priceFilter"]["tickSize"], "step_size": s["lotSizeFilter"]["qtyStep"]}
    return None


async def list_symbols(session):
    """All trading USDT linear perpetuals: [{"symbol", "tick", "step"}] (non-ASCII left out)."""
    out, cursor = [], ""
    for _ in range(20):  # paginated, 1000 per page
        params = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        async with session.get(f"{REST}/v5/market/instruments-info", params=params) as r:
            r.raise_for_status()
            data = orjson.loads(await r.read())
        res = data.get("result") or {}
        for s in res.get("list") or []:
            if (s.get("status") == "Trading" and s.get("contractType") == "LinearPerpetual"
                    and s.get("quoteCoin") == "USDT" and s["symbol"].isascii()):
                out.append({"symbol": s["symbol"], "tick": s["priceFilter"]["tickSize"],
                            "step": s["lotSizeFilter"]["qtyStep"]})
        cursor = res.get("nextPageCursor") or ""
        if not cursor:
            break
    return sorted(out, key=lambda x: x["symbol"])


async def fetch_snapshot(session, symbol, limit=DEPTH):
    params = {"category": "linear", "symbol": symbol, "limit": limit}
    async with session.get(f"{REST}/v5/market/orderbook", params=params) as r:
        r.raise_for_status()
        data = orjson.loads(await r.read())
    if data.get("retCode") != 0:
        raise RuntimeError(f"bybit orderbook: {data.get('retMsg')}")
    return data["result"]


class BybitFeed(FeedBase):
    exchange = "bybit"

    def __init__(self, session, symbol, native_tick, common_tick=None, history=None):
        super().__init__(session, symbol, native_tick, common_tick, history)
        self.last_u = None
        # Recent applied messages for verification: (seq, t, [(side, tick, old, new)]).
        self.recent = collections.deque(maxlen=1000)

    async def run(self):
        await self._reconnecting("ws", self._session)

    async def _session(self):
        self.synced = False
        self.last_u = None
        topics = [f"orderbook.{DEPTH}.{self.symbol}", f"publicTrade.{self.symbol}"]
        async with self.session.ws_connect(WS, heartbeat=None, max_msg_size=0) as ws:
            self.connection_opened("ws")
            await ws.send_json({"op": "subscribe", "args": topics})
            pinger = asyncio.create_task(self._ping(ws))
            try:
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        if msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                            break
                        continue
                    d = orjson.loads(msg.data)
                    topic = d.get("topic")
                    if topic is None:
                        if d.get("op") == "subscribe" and not d.get("success", True):
                            raise RuntimeError(f"bybit subscribe failed: {d}")
                        continue  # pong / ack
                    if topic.startswith("orderbook."):
                        self._on_book(d)
                    elif topic.startswith("publicTrade."):
                        for tr in d["data"]:
                            # S = side of the taker (the aggressor).
                            self.on_trade(int(tr["T"]), tr["p"], float(tr["v"]), 1 if tr["S"] == "Buy" else -1)
            finally:
                pinger.cancel()
        raise ConnectionError("websocket closed")

    async def _ping(self, ws):
        # Bybit drops connections without an application-level ping (every 20 s advised).
        while True:
            await asyncio.sleep(20)
            await ws.send_json({"op": "ping"})

    def _on_book(self, d):
        data = d["data"]
        t = int(d["ts"])
        u = data["u"]
        if d["type"] == "snapshot" or u == 1:
            self.reset_book(t, data["b"], data["a"])
            self.recent.clear()
            self.synced = True
            self.stats["snapshots"] += 1
        else:
            if not self.synced:
                return
            if u != self.last_u + 1:
                self.stats["gaps"] += 1
                raise Resync(f"gap: u={u} after {self.last_u}")
            applied = []
            for side, levels in ((BID, data["b"]), (ASK, data["a"])):
                for p, q in levels:
                    tick, old, new = self.set_level(t, side, p, float(q))
                    applied.append((side, tick, old, new))
            self.recent.append((data["seq"], t, applied))
            self.stats["depth_events"] += 1
            self.stats["level_updates"] += len(applied)
        self.last_u = u
        self.after_event(t)

    # ---- verification -------------------------------------------------------------

    async def refresh(self):
        """Compare the live book with a REST snapshot. The REST book is matched to our stream
        by `seq`; levels touched by the first stream message after it are excluded (the REST
        book may already contain part of that message)."""
        snap = await fetch_snapshot(self.session, self.symbol)
        S = snap["seq"]
        for _ in range(30):  # wait until the stream has passed the snapshot
            if self.synced and self.recent and self.recent[-1][0] > S:
                break
            await asyncio.sleep(0.1)
        events = list(self.recent)
        if not events or S < events[0][0]:
            return {"ok": False, "reason": f"seq {S} not in recent ring"}
        later = [ev for ev in events if ev[0] > S]
        book = self.book.copy()
        for _seq, _t, applied in reversed(later):
            for side, tick, old, _new in reversed(applied):
                book.apply(side, tick, old)
        excluded = {(side, tick) for side, tick, _o, _n in later[0][2]} if later else set()
        snap_sides = (self.aggregate(snap["b"])[0], self.aggregate(snap["a"])[0])
        # Both sides hold the nearest 1000 levels: compare where both windows overlap.
        # With several native levels per common tick the outermost common tick may be cut off.
        e = 1 if self.native is not None else 0
        bid_floor = max(min(snap_sides[BID], default=0), min(book.sides[BID], default=0)) + e
        ask_ceil = min(max(snap_sides[ASK], default=0), max(book.sides[ASK], default=0)) - e
        compared, mismatches = self.compare_books(book.sides, snap_sides, excluded, bid_floor, ask_ceil,
                                                  self.tick_to_price)
        return {"ok": not mismatches, "compared": compared, "excluded": len(excluded),
                "mismatches": len(mismatches), "examples": mismatches[:5]}
