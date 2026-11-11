"""In-memory rolling history: book change log + periodic keyframes + trades.

The heatmap is never stored as full snapshots. Every level change is one compact record
(time, side, tick, size); keyframes (full book copies every few seconds) make it cheap to
rebuild the book at any moment: start at the last keyframe before t and replay the log.
"""

import bisect

import numpy as np

from .book import ASK, BID

# Book changes: time as int32 ms relative to the history's t0 (13 bytes per record instead
# of 17; busy coins log ~1000 changes/s). t0 is moved forward every ~20 days (see _rebase).
CHANGE_DT = np.dtype([("t", "<i4"), ("tick", "<i4"), ("size", "<f4"), ("side", "i1")])
_I32_MIN, _I32_MAX = -(2**31), 2**31 - 1
REBASE_AFTER_MS = 20 * 24 * 3600 * 1000
TRADE_DT = np.dtype([("t", "<i8"), ("tick", "<i4"), ("qty", "<f4"), ("side", "i1")])  # side +1 buy, -1 sell

# Rough per-entry cost of the not-yet-flushed Python tuple buffer, for memory accounting.
_TUPLE_BYTES = 120


class ChunkedLog:
    """Append-only log of fixed-dtype records in numpy chunks, addressed by a global position."""

    def __init__(self, dtype, chunk=65536):
        self.dtype = dtype
        self.chunk = chunk
        self.buf = []
        self.chunks = []
        self.chunk_starts = []  # global position of chunks[i][0]
        self.flushed_end = 0  # global position just past the last flushed record

    def append(self, rec):
        self.buf.append(rec)
        if len(self.buf) >= self.chunk:
            self.flush()

    def flush(self):
        if self.buf:
            self.chunk_starts.append(self.flushed_end)
            self.chunks.append(np.array(self.buf, dtype=self.dtype))
            self.flushed_end += len(self.buf)
            self.buf = []
            self._merge_small()

    def _merge_small(self):
        # Flushes for queries create small chunks; fold them together to keep the list short.
        while len(self.chunks) >= 2 and len(self.chunks[-1]) + len(self.chunks[-2]) <= self.chunk:
            b = self.chunks.pop()
            self.chunk_starts.pop()
            self.chunks[-1] = np.concatenate((self.chunks[-1], b))

    @property
    def total(self):
        """Global position just past the last record ever appended."""
        return self.flushed_end + len(self.buf)

    @property
    def start_pos(self):
        """Global position of the first retained record."""
        return self.chunk_starts[0] if self.chunks else self.flushed_end

    def prune_before(self, t_cut):
        """Drop whole chunks whose last record is older than t_cut."""
        while self.chunks and self.chunks[0]["t"][-1] < t_cut:
            self.chunks.pop(0)
            self.chunk_starts.pop(0)

    def slice(self, pos_from, t_to):
        """Records from global position pos_from up to and including time t_to."""
        self.flush()
        parts = []
        for start, arr in zip(self.chunk_starts, self.chunks):
            end = start + len(arr)
            if end <= pos_from:
                continue
            a = arr[max(0, pos_from - start):]
            cut = np.searchsorted(a["t"], t_to, side="right")
            parts.append(a[:cut])
            if cut < len(a):
                break
        if not parts:
            return np.empty(0, self.dtype)
        return np.concatenate(parts)

    def iter_range(self, pos_from, t_from_excl, t_to):
        """Yield per-chunk views of records from global position pos_from with
        t_from_excl < t <= t_to (no copying of the whole range)."""
        self.flush()
        for start, arr in zip(self.chunk_starts, self.chunks):
            if start + len(arr) <= pos_from or arr["t"][-1] <= t_from_excl:
                continue
            if arr["t"][0] > t_to:
                break
            a = arr[max(0, pos_from - start):]
            lo = np.searchsorted(a["t"], t_from_excl, side="right")
            hi = np.searchsorted(a["t"], t_to, side="right")
            if hi > lo:
                yield a[lo:hi]
            if hi < len(a):
                break

    def pos_after(self, t):
        """Global position of the first record with time > t."""
        self.flush()
        for start, arr in zip(self.chunk_starts, self.chunks):
            if arr["t"][-1] > t:
                return start + int(np.searchsorted(arr["t"], t, side="right"))
        return self.total

    def since(self, pos):
        """All records from global position pos (clamped to what is retained)."""
        self.flush()
        pos = max(pos, self.start_pos)
        parts = [arr[max(0, pos - start):] for start, arr in zip(self.chunk_starts, self.chunks)
                 if start + len(arr) > pos]
        if not parts:
            return np.empty(0, self.dtype)
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def slice_time(self, t_from_excl, t_to):
        """Records with t_from_excl < t <= t_to (the log must be time-sorted)."""
        self.flush()
        parts = []
        for arr in self.chunks:
            if arr["t"][-1] <= t_from_excl:
                continue
            if arr["t"][0] > t_to:
                break
            ts = arr["t"]
            a = np.searchsorted(ts, t_from_excl, side="right")
            b = np.searchsorted(ts, t_to, side="right")
            parts.append(arr[a:b])
        if not parts:
            return np.empty(0, self.dtype)
        return parts[0] if len(parts) == 1 else np.concatenate(parts)

    def nbytes(self):
        return sum(a.nbytes for a in self.chunks) + len(self.buf) * _TUPLE_BYTES

    def __len__(self):
        return self.total - self.start_pos


def _levels(col, tr):
    """Sum trade records per (col, tick) and side -> (col, tick, buy_vol, sell_vol)."""
    tick = tr["tick"].astype(np.int64)
    key = col.astype(np.int64) * (1 << 32) + (tick - tick.min())
    uniq, inv = np.unique(key, return_inverse=True)
    qty = tr["qty"].astype(np.float64)
    buy = np.bincount(inv, weights=np.where(tr["side"] > 0, qty, 0.0), minlength=len(uniq))
    sell = np.bincount(inv, weights=np.where(tr["side"] < 0, qty, 0.0), minlength=len(uniq))
    return uniq >> 32, (uniq & ((1 << 32) - 1)) + tick.min(), buy, sell


def _rows(bid, ask, nrows, row_ticks, source_rows=None):
    row = bid + ask
    source_rows = source_rows or nrows
    row = row.reshape(source_rows, row_ticks).sum(1) if row_ticks > 1 else row
    if source_rows == nrows:
        return row
    # Bookmap tF: price-descending indices, truncated 32-bit fixed-point ratio.
    ratio = (nrows << 32) // source_rows
    indices = nrows - 1 - ((np.arange(source_rows - 1, -1, -1, dtype=np.int64) * ratio) >> 32)
    return np.bincount(indices, weights=row, minlength=nrows).astype(np.float32)


def _best_in_range(bid, ask, tick_lo, ask_shift=0):
    """Best bid/ask ticks from dense arrays (-1 when the side is empty in the range; the
    client keeps the price centred, so that is the rare case)."""
    nzb = np.flatnonzero(bid)
    nza = np.flatnonzero(ask)
    return (tick_lo + int(nzb[-1]) if len(nzb) else -1,
            tick_lo - ask_shift + int(nza[0]) if len(nza) else -1)


def filter_trades(tr, min_trade_size=0):
    """Filter source records in coins, before grouping; the store uses float32 lots."""
    return tr[tr["qty"] >= np.float32(min_trade_size)] if min_trade_size > 0 else tr


def _changes_to_current_format(z):
    """(changes array, t0) from a saved file; files from before 2026-10-24 stored absolute
    int64 times."""
    arr = z["changes"]
    if "changes_t0" in z and arr.dtype == CHANGE_DT:
        return arr, int(z["changes_t0"])
    t0 = int(arr["t"][0]) if len(arr) else (int(z["kf_t"][0]) if len(z["kf_t"]) else 0)
    new = np.empty(len(arr), CHANGE_DT)
    new["t"] = (arr["t"].astype(np.int64) - t0).clip(_I32_MIN, _I32_MAX)
    for f in ("tick", "size", "side"):
        new[f] = arr[f]
    return new, t0


class Keyframe:
    __slots__ = ("t", "pos", "bid_ticks", "bid_sizes", "ask_ticks", "ask_sizes")

    def __init__(self, t, pos, book):
        self.t = t
        self.pos = pos
        self.bid_ticks = np.fromiter(book.bids.keys(), np.int32, len(book.bids))
        self.bid_sizes = np.fromiter(book.bids.values(), np.float32, len(book.bids))
        self.ask_ticks = np.fromiter(book.asks.keys(), np.int32, len(book.asks))
        self.ask_sizes = np.fromiter(book.asks.values(), np.float32, len(book.asks))

    def nbytes(self):
        return sum(a.nbytes for a in (self.bid_ticks, self.bid_sizes, self.ask_ticks, self.ask_sizes))


class BookHistory:
    """Rolling history for one coin on one exchange."""

    def __init__(self, retention_ms=4 * 3600 * 1000, keyframe_ms=30_000):
        if retention_ms > REBASE_AFTER_MS // 2:
            # Change times are int32 ms from t0; t0 can only move up to the oldest kept data.
            raise ValueError("retention too long for int32 change times (max ~10 days)")
        self.retention_ms = retention_ms
        self.keyframe_ms = keyframe_ms
        self.t0 = None              # base time of the change log (ms); set by the first record
        self.changes = ChunkedLog(CHANGE_DT)
        self.trades = ChunkedLog(TRADE_DT, chunk=16384)
        self.keyframes = []
        self.keyframe_times = []
        self._last_t = 0
        self._last_trade_t = 0

    def record_change(self, t, side, tick, size):
        # Keep the log time-sorted even if a source time steps back slightly (e.g. a REST
        # snapshot time after buffered stream events).
        if t < self._last_t:
            t = self._last_t
        self._last_t = t
        if self.t0 is None:
            self.t0 = int(t)
        elif t - self.t0 > REBASE_AFTER_MS:
            self._rebase()
        self.changes.append((t - self.t0, tick, size, side))

    def _q(self, t):
        """An absolute time as a change-log time (clipped to int32 for comparisons)."""
        if self.t0 is None:
            return 0
        return min(_I32_MAX, max(_I32_MIN, int(t) - self.t0))

    def _rebase(self):
        """Move t0 up to the oldest time still needed, so int32 times never overflow on a
        server that runs for weeks. Rewrites the retained chunks (once every ~20 days)."""
        new_t0 = self.keyframe_times[0] if self.keyframe_times else self._last_t
        delta = int(new_t0) - self.t0
        if delta <= 0:
            return
        self.changes.flush()
        rebased = []
        for arr in self.changes.chunks:
            a = arr.copy()
            a["t"] = (a["t"].astype(np.int64) - delta).clip(_I32_MIN, _I32_MAX)
            rebased.append(a)
        self.changes.chunks = rebased
        self.t0 = int(new_t0)

    def record_trade(self, t, tick, qty, side):
        if t < self._last_trade_t:
            t = self._last_trade_t
        self._last_trade_t = t
        self.trades.append((t, tick, qty, side))

    def maybe_keyframe(self, t, book):
        """Call after applying all changes of time t."""
        t = max(t, self._last_t)
        if self.t0 is None:
            self.t0 = int(t)
        if not self.keyframes or t - self.keyframes[-1].t >= self.keyframe_ms:
            self.keyframes.append(Keyframe(t, self.changes.total, book))
            self.keyframe_times.append(t)

    def prune(self, now):
        cut = now - self.retention_ms
        # Keep the newest keyframe at or before the cut so `cut` itself stays reconstructible.
        i = bisect.bisect_right(self.keyframe_times, cut) - 1
        if i > 0:
            del self.keyframes[:i]
            del self.keyframe_times[:i]
        if self.keyframes:
            # Changes before the oldest keyframe are no longer needed.
            self.changes.prune_before(self._q(self.keyframes[0].t))
        self.trades.prune_before(cut)

    def book_at(self, t):
        """(bids, asks) dicts as of time t (inclusive), or None if t is before the history."""
        i = bisect.bisect_right(self.keyframe_times, t) - 1
        if i < 0:
            return None
        kf = self.keyframes[i]
        bids = dict(zip(kf.bid_ticks.tolist(), kf.bid_sizes.tolist()))
        asks = dict(zip(kf.ask_ticks.tolist(), kf.ask_sizes.tolist()))
        sides = (bids, asks)
        recs = self.changes.slice(kf.pos, self._q(t))
        for tick, size, side in zip(recs["tick"].tolist(), recs["size"].tolist(), recs["side"].tolist()):
            if size > 0.0:
                sides[side][tick] = size
            else:
                sides[side].pop(tick, None)
        return bids, asks

    def sample_grid(self, times, tick_lo, nrows, row_ticks=1, outward=False, source_rows=None, exact=False):
        """Sample the book at each time in `times` (sorted int64 ms). Row r covers ticks
        tick_lo + r*row_ticks .. + row_ticks-1. With outward rounding, asks cover
        anchor-row_ticks+1 .. anchor instead: bids floor, asks ceil.

        Returns (grid, best_bid, best_ask):
          grid      float32 [len(times), nrows], bid size + ask size per row (0 = empty)
          best_bid  int64 [len(times)] best bid tick per column (-1 if unknown/out of range)
          best_ask  int64 [len(times)] best ask tick per column (-1 if unknown/out of range)
        Columns before the start of the history are all zero. The change log is walked
        chunk by chunk, so a 2-hour request does not copy the whole log."""
        times = np.asarray(times, dtype=np.int64)
        ncols = len(times)
        source_rows = source_rows or nrows
        span = source_rows * row_ticks
        ask_shift = row_ticks - 1 if outward else 0
        grid = np.zeros((ncols, nrows), np.float32)
        best_bid = np.full(ncols, -1, np.int64)
        best_ask = np.full(ncols, -1, np.int64)
        if not self.keyframes or ncols == 0:
            return grid, best_bid, best_ask
        first = int(np.searchsorted(times, self.keyframe_times[0], side="left"))
        if first >= ncols:
            return grid, best_bid, best_ask
        dt = int(times[1] - times[0]) if ncols > 1 else 0
        if dt >= self.keyframe_ms and source_rows == nrows and not exact:
            self._grid_from_keyframes(times, first, tick_lo, nrows, row_ticks, grid, best_bid, best_ask, ask_shift)
            return grid, best_bid, best_ask
        t0 = int(times[first])
        start = self.book_at(t0)
        # One dense array for both sides: [0, span) bids, [span, 2*span) asks.
        both = np.zeros(2 * span, np.float32)
        for off, levels in ((0, start[0]), (span, start[1])):
            for tick, size in levels.items():
                i = tick - tick_lo + (ask_shift if off else 0)
                if 0 <= i < span:
                    both[off + i] = size
        bid, ask = both[:span], both[span:]
        # Full-book best bid/ask at t0 (the price may sit outside the visible range).
        state = {"bb": max(start[0]) if start[0] else -1, "ba": min(start[1]) if start[1] else -1,
                 "dirty": False, "next": first}

        def emit(upto):
            """Write columns state['next'] .. upto-1 with the current book."""
            nxt = state["next"]
            if upto <= nxt:
                return
            if state["dirty"]:
                bb, ba = _best_in_range(bid, ask, tick_lo, ask_shift)
                state["bb"], state["ba"] = bb, ba
                state["dirty"] = False
            grid[nxt:upto] = _rows(bid, ask, nrows, row_ticks, source_rows)
            best_bid[nxt:upto] = state["bb"]
            best_ask[nxt:upto] = state["ba"]
            state["next"] = upto

        sel_times = times[first:]
        for part in self._iter_changes(t0, int(times[-1])):
            idx = part["tick"].astype(np.int64) - tick_lo + part["side"].astype(np.int64) * ask_shift
            m = (idx >= 0) & (idx < span)
            if not m.any():
                continue
            key = idx[m] + part["side"][m].astype(np.int64) * span
            sz = part["size"][m]
            # A record with times[c-1] < t <= times[c] is applied before column c is sampled.
            col = np.searchsorted(sel_times, part["t"][m].astype(np.int64) + self.t0, side="left") + first
            # Per (column, level) only the last write matters: sort by column, level, position
            # and keep the last of each run. Then every group has unique keys.
            n = len(key)
            order = np.lexsort((np.arange(n), key, col))
            ks, cs = key[order], col[order]
            last = np.ones(n, bool)
            last[:-1] = (ks[1:] != ks[:-1]) | (cs[1:] != cs[:-1])
            keep = order[last]
            key, sz, col = key[keep], sz[keep], col[keep]
            bounds = np.flatnonzero(np.diff(col)) + 1
            starts = [0] + bounds.tolist()
            ends = bounds.tolist() + [len(col)]
            for a, b in zip(starts, ends):
                emit(int(col[a]))
                both[key[a:b]] = sz[a:b]
                state["dirty"] = True
        emit(ncols)
        return grid, best_bid, best_ask

    def _grid_from_keyframes(self, times, first, tick_lo, nrows, row_ticks, grid, best_bid, best_ask, ask_shift=0):
        """Coarse columns (>= keyframe interval): each column shows the last keyframe at or
        before its time. At most one keyframe interval stale, i.e. about one column."""
        span = nrows * row_ticks
        prev_i = -1
        row = None
        bb = ba = -1
        for c in range(first, len(times)):
            i = bisect.bisect_right(self.keyframe_times, int(times[c])) - 1
            if i < 0:
                continue
            if i != prev_i:
                kf = self.keyframes[i]
                bid = np.zeros(span, np.float32)
                ask = np.zeros(span, np.float32)
                for arr, ticks, sizes, shift in ((bid, kf.bid_ticks, kf.bid_sizes, 0),
                                                 (ask, kf.ask_ticks, kf.ask_sizes, ask_shift)):
                    idx = ticks.astype(np.int64) - tick_lo + shift
                    m = (idx >= 0) & (idx < span)
                    arr[idx[m]] = sizes[m]
                row = _rows(bid, ask, nrows, row_ticks)
                bb, ba = _best_in_range(bid, ask, tick_lo, ask_shift)
                prev_i = i
            grid[c] = row
            best_bid[c] = bb
            best_ask[c] = ba

    def trade_columns(self, times, dt):
        """Aggregate trades into columns: column j covers (times[j] - dt, times[j]].
        Returns float arrays (buy_vol, buy_px, sell_vol, sell_px); px = volume-weighted
        mean tick (NaN when there was no volume)."""
        times = np.asarray(times, dtype=np.int64)
        n = len(times)
        out_vol = [np.zeros(n), np.zeros(n)]
        out_px = [np.full(n, np.nan), np.full(n, np.nan)]
        if n == 0:
            return out_vol[0], out_px[0], out_vol[1], out_px[1]
        tr = self.trades.slice_time(int(times[0]) - dt, int(times[-1]))
        if len(tr):
            col = np.searchsorted(times, tr["t"], side="left")  # times[col-1] < t <= times[col]
            qty = tr["qty"].astype(np.float64)
            for k, side_val in enumerate((1, -1)):
                m = tr["side"] == side_val
                vol = np.bincount(col[m], weights=qty[m], minlength=n)[:n]
                pxw = np.bincount(col[m], weights=qty[m] * tr["tick"][m], minlength=n)[:n]
                out_vol[k] = vol
                with np.errstate(invalid="ignore", divide="ignore"):
                    out_px[k] = np.where(vol > 0, pxw / vol, np.nan)
        return out_vol[0], out_px[0], out_vol[1], out_px[1]

    def trade_levels(self, times, dt, min_trade_size=0):
        """Trades per (column, price tick): column j covers (times[j] - dt, times[j]].
        Returns int/float arrays (col, tick, buy_vol, sell_vol), sorted by column then tick.
        Unlike trade_columns this keeps every price a sweep went through, so a fast dump
        becomes a row of bubbles instead of one bubble at the average price."""
        times = np.asarray(times, dtype=np.int64)
        empty = (np.empty(0, np.int64), np.empty(0, np.int64), np.empty(0), np.empty(0))
        if len(times) == 0:
            return empty
        tr = filter_trades(self.trades.slice_time(int(times[0]) - dt, int(times[-1])), min_trade_size)
        if not len(tr):
            return empty
        return _levels(np.searchsorted(times, tr["t"], side="left"), tr)

    def _iter_changes(self, t_from_excl, t_to):
        """Yield change records with t_from_excl < t <= t_to, one chunk at a time."""
        i = bisect.bisect_right(self.keyframe_times, t_from_excl) - 1
        pos = self.keyframes[max(i, 0)].pos
        yield from self.changes.iter_range(pos, self._q(t_from_excl), self._q(t_to))

    # ---- persistence (survive server restarts while a tab stays open) ----------------

    def export_parts(self):
        """Grab references to everything needed to save this history. Call from the event
        loop; the returned parts are immutable arrays, so save_parts() can run in a thread."""
        self.changes.flush()
        self.trades.flush()
        return {
            "changes": (list(self.changes.chunks), self.changes.start_pos),
            "trades": (list(self.trades.chunks), self.trades.start_pos),
            "keyframes": list(self.keyframes),
            "last_t": self._last_t,
            "last_trade_t": self._last_trade_t,
            "t0": self.t0 if self.t0 is not None else 0,
        }

    @staticmethod
    def save_parts(path, parts, profile, extra=None):
        """Write a history (from export_parts) plus the session volume profile to `path`."""
        ch_chunks, ch_start = parts["changes"]
        tr_chunks, tr_start = parts["trades"]
        kfs = parts["keyframes"]
        cat = lambda xs, dt: np.concatenate(xs) if xs else np.empty(0, dt)  # noqa: E731
        prof_ticks = np.fromiter(profile.keys(), np.int64, len(profile))
        prof_vals = np.array(list(profile.values()), np.float64).reshape(-1, 2)
        arrays = {
            "changes": cat(ch_chunks, CHANGE_DT), "changes_start": np.int64(ch_start),
            "changes_t0": np.int64(parts["t0"]),
            "trades": cat(tr_chunks, TRADE_DT), "trades_start": np.int64(tr_start),
            "kf_t": np.array([k.t for k in kfs], np.int64),
            "kf_pos": np.array([k.pos for k in kfs], np.int64),
            "kf_nb": np.array([len(k.bid_ticks) for k in kfs], np.int64),
            "kf_na": np.array([len(k.ask_ticks) for k in kfs], np.int64),
            "kf_bid_ticks": cat([k.bid_ticks for k in kfs], np.int32),
            "kf_bid_sizes": cat([k.bid_sizes for k in kfs], np.float32),
            "kf_ask_ticks": cat([k.ask_ticks for k in kfs], np.int32),
            "kf_ask_sizes": cat([k.ask_sizes for k in kfs], np.float32),
            "last_t": np.int64(parts["last_t"]), "last_trade_t": np.int64(parts["last_trade_t"]),
            "prof_ticks": prof_ticks, "prof_vals": prof_vals,
            "extra": np.frombuffer(repr(extra or {}).encode(), np.uint8),
        }
        tmp = path.with_name(path.name + ".tmp")
        with open(tmp, "wb") as f:
            np.savez(f, **arrays)
        tmp.replace(path)  # atomic

    @classmethod
    def load(cls, path, retention_ms, keyframe_ms=30_000):
        """Read a saved history. Returns (history, profile). The book is cleared right after
        the save time, so the time the server was down shows as empty, not as a frozen book."""
        # Read every array exactly once: each z["name"] access re-reads the whole array from
        # the file, and slicing such a fresh copy per keyframe kept hundreds of full copies
        # alive (2.9 GB for one busy coin; this crashed the VM on 2026-10-23).
        with np.load(path) as zf:
            z = {k: zf[k] for k in zf.files}
        h = cls(retention_ms, keyframe_ms)
        z["changes"], h.t0 = _changes_to_current_format(z)
        for log, key in ((h.changes, "changes"), (h.trades, "trades")):
            arr, start = z[key], int(z[key + "_start"])
            log.flushed_end = start
            if len(arr):
                log.chunks = [arr]
                log.chunk_starts = [start]
                log.flushed_end = start + len(arr)
        ob = oa = 0
        for t, pos, nb, na in zip(z["kf_t"].tolist(), z["kf_pos"].tolist(), z["kf_nb"].tolist(), z["kf_na"].tolist()):
            kf = Keyframe.__new__(Keyframe)
            kf.t, kf.pos = t, pos
            kf.bid_ticks, kf.bid_sizes = z["kf_bid_ticks"][ob:ob + nb], z["kf_bid_sizes"][ob:ob + nb]
            kf.ask_ticks, kf.ask_sizes = z["kf_ask_ticks"][oa:oa + na], z["kf_ask_sizes"][oa:oa + na]
            ob += nb
            oa += na
            h.keyframes.append(kf)
            h.keyframe_times.append(t)
        h._last_t = int(z["last_t"])
        h._last_trade_t = int(z["last_trade_t"])
        profile = {int(t): [float(b), float(s)] for t, (b, s) in zip(z["prof_ticks"].tolist(), z["prof_vals"].tolist())}
        h.mark_gap(h._last_t + 1)
        return h, profile

    def mark_gap(self, t):
        """From time t on we know nothing: remove every level (the next snapshot refills)."""
        last = self.book_at(self._last_t) if self.keyframes else None
        if last:
            for side in (BID, ASK):
                for tick in last[side]:
                    self.record_change(t, side, tick, 0.0)
        from .book import Book  # local import: store must not depend on feeds
        self.keyframes.append(Keyframe(max(t, self._last_t), self.changes.total, Book()))
        self.keyframe_times.append(self.keyframes[-1].t)

    def nbytes(self):
        return {
            "changes": self.changes.nbytes(),
            "keyframes": sum(k.nbytes() for k in self.keyframes),
            "trades": self.trades.nbytes(),
        }


__all__ = ["BookHistory", "ChunkedLog", "BID", "ASK"]
