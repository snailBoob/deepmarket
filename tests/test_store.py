"""Randomised tests for Book + BookHistory. Run: .venv/bin/python -m tests.test_store"""

import random

from deep_market.book import ASK, BID, Book
from deep_market.store import BookHistory


def random_walk(seed, steps=20000, resync_every=3000):
    rnd = random.Random(seed)
    book = Book()
    hist = BookHistory(retention_ms=300_000, keyframe_ms=1_000)
    hist.changes.chunk = 500  # small chunks so pruning really happens
    truth = {}  # t -> (bids, asks) copies at some sample times
    t = 1_000_000
    mid = 10_000
    for i in range(steps):
        t += rnd.choice((0, 10, 100, 100, 250))
        mid += rnd.choice((-1, 0, 0, 1))
        if i and i % resync_every == 0:
            # Simulated resync: a fresh "snapshot" replaces the book.
            bids = {mid - k: float(rnd.randint(1, 100)) for k in range(1, 50) if rnd.random() < 0.8}
            asks = {mid + k: float(rnd.randint(1, 100)) for k in range(1, 50) if rnd.random() < 0.8}
            for side, tick, size in book.reset(bids, asks):
                hist.record_change(t, side, tick, size)
        else:
            for _ in range(rnd.randint(1, 6)):
                side = rnd.choice((BID, ASK))
                tick = mid - rnd.randint(1, 60) if side == BID else mid + rnd.randint(1, 60)
                size = 0.0 if rnd.random() < 0.3 else float(rnd.randint(1, 1000))
                if book.apply(side, tick, size) != size:
                    hist.record_change(t, side, tick, size)
        hist.maybe_keyframe(t, book)
        if rnd.random() < 0.02:
            truth[t] = (dict(book.bids), dict(book.asks))
            t += 1  # book_at(t) includes every change at t, so close this timestamp
        hist.prune(t)
    return book, hist, truth, t


def test_rebuild_matches_truth():
    for seed in range(5):
        book, hist, truth, now = random_walk(seed)
        oldest = hist.keyframes[0].t
        checked = 0
        for t, (bids, asks) in truth.items():
            got = hist.book_at(t)
            if t < oldest:
                continue  # outside retention
            assert got is not None, (seed, t)
            assert got[0] == bids and got[1] == asks, (seed, t)
            checked += 1
        assert checked > 10, checked
        assert hist.book_at(now) == (book.bids, book.asks)


def test_retention_is_enforced():
    _book, hist, _truth, now = random_walk(7)
    oldest_change = min(int(c["t"][0]) for c in hist.changes.chunks) + hist.t0
    # Keep at most retention + one keyframe interval + one chunk's span.
    assert now - hist.keyframes[0].t <= hist.retention_ms + hist.keyframe_ms
    assert now - oldest_change < hist.retention_ms * 2
    assert hist.book_at(now - hist.retention_ms) is not None


def test_sample_grid_matches_book_at():
    import numpy as np

    _book, hist, _truth, now = random_walk(3, steps=6000)
    start = hist.keyframes[0].t
    times = np.arange(start - 5_000, now, 777, dtype=np.int64)  # some columns before history
    tick_lo, nrows = 9_950, 120
    grid, bb, ba = hist.sample_grid(times, tick_lo, nrows)
    for j, t in enumerate(times.tolist()):
        got = hist.book_at(t)
        if got is None:
            assert not grid[j].any() and bb[j] == -1
            continue
        bids, asks = got
        want = np.zeros(nrows, np.float32)
        for side in (bids, asks):
            for tick, size in side.items():
                if 0 <= tick - tick_lo < nrows:
                    want[tick - tick_lo] += size
        assert np.array_equal(grid[j], want), j
        in_b = [k for k in bids if 0 <= k - tick_lo < nrows]
        in_a = [k for k in asks if 0 <= k - tick_lo < nrows]
        # Column 0 (and columns without changes) may still carry the full-book value.
        if in_b and j > 0:
            assert bb[j] in (max(in_b), max(bids)), (j, bb[j], max(in_b))
        if in_a and j > 0:
            assert ba[j] in (min(in_a), min(asks)), (j, ba[j], min(in_a))


def test_sample_grid_row_aggregation():
    import numpy as np

    _book, hist, _truth, now = random_walk(5, steps=6000)
    hist.changes.chunk = 300  # force many chunks for the streaming path
    start = hist.keyframes[0].t
    times = np.arange(start, now, 2_345, dtype=np.int64)
    fine, _, _ = hist.sample_grid(times, 9_940, 150)
    for k in (2, 3, 5):
        nrows = 150 // k
        coarse, _, _ = hist.sample_grid(times, 9_940, nrows, row_ticks=k)
        want = fine[:, : nrows * k].reshape(len(times), nrows, k).sum(2)
        assert np.allclose(coarse, want), k


def test_trade_columns_and_cursor():
    import numpy as np

    rnd = random.Random(11)
    hist = BookHistory()
    hist.trades.chunk = 64
    trades = []
    t = 10_000
    for _ in range(2000):
        t += rnd.choice((0, 5, 40, 130))
        tr = (t, rnd.randint(100, 110), float(rnd.randint(1, 50)), rnd.choice((1, -1)))
        trades.append(tr)
        hist.record_trade(*tr)
        if rnd.random() < 0.05:
            hist.trades.flush()  # mixed flushed/unflushed state
    dt = 100
    times = np.arange(10_500, t, dt, dtype=np.int64)
    bv, bp, sv, sp = hist.trade_columns(times, dt)
    for j in rnd.sample(range(len(times)), 50):
        lo, hi = times[j] - dt, times[j]
        for side, vol, px in ((1, bv, bp), (-1, sv, sp)):
            sel = [(k, q) for (tt, k, q, s) in trades if lo < tt <= hi and s == side]
            v = sum(q for _, q in sel)
            assert abs(vol[j] - v) < 1e-6, (j, side)
            if v:
                assert abs(px[j] - sum(k * q for k, q in sel) / v) < 1e-6
            else:
                assert np.isnan(px[j])
    # Per price level: must match a brute-force sum and add up to the column totals.
    cols, ticks, buys, sells = hist.trade_levels(times, dt)
    want = {}
    for (tt, k, q, s) in trades:
        j = int(np.searchsorted(times, tt, side="left"))
        if j < len(times) and times[j] - dt < tt:
            b, sl = want.get((j, k), (0.0, 0.0))
            want[(j, k)] = (b + (q if s > 0 else 0), sl + (q if s < 0 else 0))
    got = {(int(c), int(k)): (b, sl) for c, k, b, sl in zip(cols, ticks, buys, sells)}
    assert got.keys() == want.keys()
    for key in got:
        assert np.allclose(got[key], want[key])
    assert np.allclose(np.bincount(cols, weights=buys, minlength=len(times))[: len(times)], bv)
    cut = trades[700][0]
    pos = hist.trades.pos_after(cut)
    rest = hist.trades.since(pos)
    assert len(rest) == sum(1 for tr in trades if tr[0] > cut)
    assert (rest["t"] > cut).all()


def test_outward_aggregation_edges_updates_and_coarse_keyframes():
    import numpy as np

    for keyframe_ms in (1000, 100):
        book = Book()
        hist = BookHistory(keyframe_ms=keyframe_ms)
        # Asks below the first anchor and bids above the last anchor still belong
        # to those rows. Neighbouring excluded levels must not leak into the view.
        for side, levels in [(BID, {99:99,100:1,104:2,105:4,109:8,110:99}),
                             (ASK, {95:99,96:16,100:32,101:64,105:128,106:99})]:
            for tick, size in levels.items():
                book.apply(side, tick, size)
                hist.record_change(1000, side, tick, size)
        hist.maybe_keyframe(1000, book)
        for t, size in [(1100, 0), (1200, 256)]:
            book.apply(ASK, 96, size)
            hist.record_change(t, ASK, 96, size)
            hist.maybe_keyframe(t, book)
        grid, _, _ = hist.sample_grid([1000,1100,1200], 100, 2, 5, outward=True)
        np.testing.assert_array_equal(grid, [[51,204],[35,204],[291,204]])


def test_outward_random_books_match_independent_price_assignment():
    import numpy as np

    _, hist, _, now = random_walk(21, steps=3000)
    times = np.arange(hist.keyframe_times[0], now, 333, dtype=np.int64)
    for width in (1, 5, 200):
        lo, nrows = 9800, max(3, 400 // width)
        grid, _, _ = hist.sample_grid(times, lo, nrows, width, outward=True)
        for j, t in enumerate(times):
            want = np.zeros(nrows)
            for side, levels in enumerate(hist.book_at(t)):
                for tick, size in levels.items():
                    anchor = -(-tick // width) * width if side == ASK else tick // width * width
                    row = (anchor - lo) // width
                    if 0 <= row < nrows: want[row] += size
            np.testing.assert_allclose(grid[j], want, rtol=1e-6)


def test_save_and_load_roundtrip():
    import pathlib
    import tempfile

    import numpy as np

    book, hist, truth, now = random_walk(9, steps=4000)
    for i in range(50):
        hist.record_trade(now - 1000 + i, 10_000 + i % 7, float(i + 1), 1 if i % 2 else -1)
    profile = {10_000: [5.0, 6.0], 10_003: [1.5, 0.0]}
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "x.npz"
        BookHistory.save_parts(path, hist.export_parts(), profile)
        h2, prof2 = BookHistory.load(path, hist.retention_ms, hist.keyframe_ms)
    assert prof2 == profile
    for t in list(truth)[-20:]:
        if t >= hist.keyframes[0].t:
            assert h2.book_at(t) == hist.book_at(t), t
    times = np.arange(hist.keyframes[0].t, now, 999, dtype=np.int64)
    assert np.array_equal(h2.sample_grid(times, 9_950, 100)[0], hist.sample_grid(times, 9_950, 100)[0])
    assert np.array_equal(h2.trade_columns(times, 999)[0], hist.trade_columns(times, 999)[0])
    # After the save time the book is empty (unknown while the server was down).
    after = h2.book_at(now + 5)
    assert after == ({}, {}), after


def test_load_memory_is_bounded():
    """Loading a history must cost about its own size, not a copy per keyframe."""
    import pathlib
    import resource
    import tempfile

    import numpy as np

    from deep_market.book import Book
    from deep_market.store import Keyframe

    hist = BookHistory(retention_ms=24 * 3600 * 1000, keyframe_ms=1)
    big = Book()
    big.reset({k: 1.0 for k in range(20_000)}, {k: 1.0 for k in range(20_000, 40_000)})
    for i in range(300):  # 300 keyframes of 40k levels = ~96 MB of keyframe data
        hist.record_change(i, 0, i % 20_000, float(i + 2))
        hist.keyframes.append(Keyframe(i, hist.changes.total, big))
        hist.keyframe_times.append(i)
    hist._last_t = 299
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "big.npz"
        BookHistory.save_parts(path, hist.export_parts(), {})
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        h2, _ = BookHistory.load(path, hist.retention_ms, hist.keyframe_ms)
        grown_mb = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - before) / 1024
    assert len(h2.keyframes) == 301  # 300 + the gap keyframe
    assert grown_mb < 400, f"loading grew peak memory by {grown_mb:.0f} MB"
    assert np.array_equal(h2.keyframes[150].bid_ticks, hist.keyframes[150].bid_ticks)


def test_rebase_keeps_history_exact():
    """A service running for weeks: the int32 change times get a new base (t0)."""
    from deep_market.book import Book

    hist = BookHistory(retention_ms=24 * 3600 * 1000, keyframe_ms=1000)
    book = Book()
    t = 1_000_000_000_000
    truth = {}
    rnd = random.Random(4)
    for i in range(4000):
        # Every 1000 steps jump 9 days ahead (27 days in total): crosses the 20-day rebase point.
        t += 9 * 24 * 3600 * 1000 if i and i % 1000 == 0 else rnd.choice((10, 100, 250))
        side = rnd.choice((BID, ASK))
        tick = 1000 - rnd.randint(1, 50) if side == BID else 1000 + rnd.randint(1, 50)
        size = 0.0 if rnd.random() < 0.3 else float(rnd.randint(1, 99))
        if book.apply(side, tick, size) != size:
            hist.record_change(t, side, tick, size)
        hist.maybe_keyframe(t, book)
        if rnd.random() < 0.05:
            truth[t] = (dict(book.bids), dict(book.asks))
            t += 1
        hist.prune(t)  # old keyframes expire, so t0 can move up
    assert hist.t0 >= 1_000_000_000_000 + 8 * 24 * 3600 * 1000  # t0 moved (rebased at least once)
    for tt, (bids, asks) in truth.items():
        if tt >= hist.keyframe_times[0]:
            assert hist.book_at(tt) == (bids, asks), tt


def test_load_old_format_file():
    """History files written before the int32 change times (absolute int64 times)."""
    import pathlib
    import tempfile

    import numpy as np

    _book, hist, truth, now = random_walk(8, steps=3000)
    parts = hist.export_parts()
    old_dt = np.dtype([("t", "<i8"), ("tick", "<i4"), ("size", "<f4"), ("side", "i1")])
    chunks = []
    for c in parts["changes"][0]:
        o = np.empty(len(c), old_dt)
        o["t"] = c["t"].astype(np.int64) + hist.t0
        for f in ("tick", "size", "side"):
            o[f] = c[f]
        chunks.append(o)
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / "old.npz"
        BookHistory.save_parts(path, parts, {})
        z = dict(np.load(path))
        z["changes"] = np.concatenate(chunks)
        del z["changes_t0"]
        np.savez(path, **z)
        h2, _ = BookHistory.load(path, hist.retention_ms, hist.keyframe_ms)
    for t in list(truth)[-15:]:
        if t >= hist.keyframes[0].t:
            assert h2.book_at(t) == hist.book_at(t), t


def test_reset_changes_are_exact():
    b = Book()
    b.reset({1: 1.0, 2: 2.0}, {5: 1.0})
    ch = b.reset({2: 3.0, 3: 1.0}, {})
    assert sorted(ch) == [(BID, 1, 0.0), (BID, 2, 3.0), (BID, 3, 1.0), (ASK, 5, 0.0)]


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
