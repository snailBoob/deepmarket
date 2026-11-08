"""Shared parts of the exchange feeds: price grid, book updates, history, trades, SVP.

Every exchange keeps its own book and history in *common* ticks: the coarsest tick size of
the exchanges that list the coin, so books can be summed row by row. When an exchange has a
finer native tick, several native levels fall into one common tick; the feed then keeps the
native levels too and maintains the common-tick sums incrementally.
"""

import asyncio
import collections
import logging
import math
import time
from decimal import Decimal

from .book import BID, Book
from .store import BookHistory

log = logging.getLogger(__name__)


class Resync(Exception):
    pass


class FeedBase:
    exchange = "?"

    def __init__(self, session, symbol, native_tick, common_tick=None, history=None):
        self.session = session
        self.symbol = symbol.upper()
        self.native_tick = Decimal(native_tick)
        self.tick_size = Decimal(common_tick or native_tick)  # the common grid
        ratio = self.tick_size / self.native_tick
        if ratio < 1:
            raise ValueError("common tick must not be finer than the native tick")
        self.k = int(ratio) if ratio == ratio.to_integral_value() else None
        self._inv_native = 1.0 / float(self.native_tick)
        self._native_per_common = float(self.native_tick) / float(self.tick_size)
        # Native levels per side, only needed when several native levels share a common tick.
        self.native = ({}, {}) if self.k != 1 else None
        self.book = Book()
        self.history = history or BookHistory()
        self.synced = False
        self.stats = collections.Counter()
        self.last_trade = None
        self.latency_ms = None
        self.last_event_time = None
        self.last_book_received = None
        self._book_wait_since = time.monotonic()
        self._book_state = "starting"
        # Session volume profile (SVP) since this coin is tracked: tick -> [buy, sell].
        self.profile = {}
        self.trade_listener = None  # called with last_trade after every trade (cross-exchange streak)

    # ---- price grid ---------------------------------------------------------------

    def native_key(self, price_str):
        return round(float(price_str) * self._inv_native)

    def tick_of_native(self, n):
        if self.k:
            return n // self.k
        return math.floor(n * self._native_per_common + 1e-9)

    def to_tick(self, price_str):
        return self.tick_of_native(self.native_key(price_str))

    def tick_to_price(self, tick):
        return float(tick * self.tick_size)

    # ---- book ---------------------------------------------------------------------

    def set_native(self, t, side, n, size):
        """Set one native level (n = native tick number) to `size` (0 = remove) and record
        the change of its common tick. Returns (tick, old, new) in common-tick sizes."""
        if self.native is None:  # native grid == common grid
            old = self.book.apply(side, n, size)
            if old != size:
                self.history.record_change(t, side, n, size)
            return n, old, size
        tick = self.tick_of_native(n)
        nat = self.native[side]
        old_n = nat.pop(n, 0.0)
        if size > 0:
            nat[n] = size
        old = self.book.sides[side].get(tick, 0.0)
        new = old - old_n + size
        if new <= 1e-9 * max(1.0, old):  # float dust after removing the last native level
            new = 0.0
        if new != old:
            self.book.apply(side, tick, new)
            self.history.record_change(t, side, tick, new)
        return tick, old, new

    def set_level(self, t, side, price_str, size):
        return self.set_native(t, side, self.native_key(price_str), size)

    def aggregate(self, levels):
        """[(price, qty), ...] -> (common tick -> summed size, native tick -> size)."""
        agg, nat = {}, {}
        for p, q in levels:
            q = float(q)
            if q > 0:
                n = self.native_key(p)
                nat[n] = q
                tk = self.tick_of_native(n)
                agg[tk] = agg.get(tk, 0.0) + q
        return agg, nat

    def reset_book(self, t, bid_levels, ask_levels):
        """Replace the book from a snapshot; the change log stays continuous."""
        (ab, nb), (aa, na) = self.aggregate(bid_levels), self.aggregate(ask_levels)
        if self.native is not None:
            self.native = (nb, na)
        for side, tick, size in self.book.reset(ab, aa):
            self.history.record_change(t, side, tick, size)
        self.history.maybe_keyframe(t, self.book)
        return ab, aa

    def after_event(self, t):
        """Call after all levels of one exchange message (time t) are applied."""
        self.history.maybe_keyframe(t, self.book)
        # Network delay (arrival - exchange time), smoothed. Used to run the live view on
        # exchange time, so live columns line up with the stored history.
        delay = time.time() * 1000 - t
        self.latency_ms = delay if self.latency_ms is None else self.latency_ms * 0.95 + delay * 0.05
        self.last_event_time = t
        self.last_book_received = time.monotonic()
        if self.synced and self._book_state != "live":
            log.info("book_live exchange=%s symbol=%s previous=%s latency_ms=%.0f",
                     self.exchange, self.symbol, self._book_state, self.latency_ms)
            self._book_state = "live"

    def check_book_freshness(self):
        """Diagnostic only: warn once after 30s without an applied book event."""
        since = self.last_book_received if self.last_book_received is not None else self._book_wait_since
        age = time.monotonic() - since
        if age >= 30 and self._book_state != "stale":
            log.warning("book_stale exchange=%s symbol=%s age_s=%.1f synced=%s threshold_s=30",
                        self.exchange, self.symbol, age, self.synced)
            self._book_state = "stale"

    def connection_opened(self, stream):
        log.info("feed_connected exchange=%s symbol=%s stream=%s reconnects=%d",
                 self.exchange, self.symbol, stream, self.stats[f"reconnects_{stream}"])

    # ---- trades -------------------------------------------------------------------

    def on_trade(self, t, price_str, qty, side):
        """side +1 = buy aggressor, -1 = sell aggressor."""
        tick = self.to_tick(price_str)
        self.history.record_trade(t, tick, qty, side)
        # Streak: consecutive trades on the same side at the same price (shown in brackets
        # next to the last trade size, e.g. "14094 (14094)").
        lt = self.last_trade
        streak = lt["streak"] + qty if lt and lt["tick"] == tick and lt["side"] == side else qty
        self.last_trade = {"t": t, "tick": tick, "qty": qty, "side": side, "streak": streak}
        pv = self.profile.get(tick)
        if pv is None:
            pv = self.profile[tick] = [0.0, 0.0]
        pv[0 if side > 0 else 1] += qty
        self.stats["trades"] += 1
        if self.trade_listener:
            self.trade_listener(self.last_trade)

    # ---- connection helpers -------------------------------------------------------

    async def _reconnecting(self, name, session_fn, resets_book=True):
        backoff = 1
        while True:
            t0 = time.monotonic()
            try:
                await session_fn()
                raise ConnectionError("feed session ended")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # network errors, Resync, server-side disconnects
                self.stats[f"reconnects_{name}"] += 1
                log.warning("feed_disconnected exchange=%s symbol=%s stream=%s error=%s: %s reconnects=%d retry_s=%d",
                            self.exchange, self.symbol, name, type(e).__name__, e,
                            self.stats[f"reconnects_{name}"],
                            1 if time.monotonic() - t0 > 60 else backoff)
            if resets_book:
                self.synced = False
                if self._book_state != "stale":
                    self._book_state = "disconnected"
            if time.monotonic() - t0 > 60:
                backoff = 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    # ---- verification -------------------------------------------------------------

    def verify_store(self):
        """Rebuild the book from the stored history and compare with the live book."""
        t = self.last_event_time
        if t is None:
            return {"ok": False, "reason": "no events yet"}
        got = self.history.book_at(t)
        if got is None:
            return {"ok": False, "reason": "no keyframe"}
        bad = 0
        for side in (BID, 1 - BID):
            ours, rebuilt = self.book.sides[side], got[side]
            for tick in ours.keys() | rebuilt.keys():
                a, b = ours.get(tick, 0.0), rebuilt.get(tick, 0.0)
                if abs(a - b) > 1e-6 * max(abs(a), abs(b), 1e-12):  # store keeps float32
                    bad += 1
        return {"ok": bad == 0, "levels": len(self.book.bids) + len(self.book.asks), "mismatches": bad}

    @staticmethod
    def compare_books(ours, snap_sides, excluded, bid_floor, ask_ceil, tick_to_price):
        """Compare two (bids, asks) dicts inside [bid_floor, ask_ceil], skipping excluded
        (side, tick) pairs. Returns (compared, mismatches)."""
        mismatches = []
        compared = 0
        for side in (BID, 1 - BID):
            snap_lv = snap_sides[side]
            mine = ours[side]
            if not snap_lv:
                continue
            if side == BID:
                ticks = {t for t in mine.keys() | snap_lv.keys() if t >= bid_floor}
            else:
                ticks = {t for t in mine.keys() | snap_lv.keys() if t <= ask_ceil}
            for tick in ticks:
                if (side, tick) in excluded:
                    continue
                compared += 1
                a, b = mine.get(tick, 0.0), snap_lv.get(tick, 0.0)
                # Relative tolerance: summed native levels can differ in the last float bits.
                if abs(a - b) > 1e-7 * max(abs(a), abs(b), 1.0):
                    mismatches.append((side, tick_to_price(tick), a, b))
        return compared, mismatches
