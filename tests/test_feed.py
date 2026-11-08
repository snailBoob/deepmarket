"""Tests for FeedBase price-grid aggregation. Run: .venv/bin/python -m tests.test_feed"""

import random

from deep_market.book import ASK, BID
from deep_market.feed import FeedBase


def brute(native, k):
    out = ({}, {})
    for side in (BID, ASK):
        for n, q in native[side].items():
            out[side][n // k] = out[side].get(n // k, 0.0) + q
    return out


def check(feed, truth, k):
    want = brute(truth, k)
    for side in (BID, ASK):
        got = feed.book.sides[side]
        assert got.keys() == want[side].keys(), side
        for tk in got:
            assert abs(got[tk] - want[side][tk]) < 1e-6 * max(1.0, want[side][tk])
    rebuilt = feed.history.book_at(feed.history._last_t)
    for side in (BID, ASK):
        assert rebuilt[side].keys() == want[side].keys()


def test_aggregation_matches_bruteforce():
    for seed, k in ((1, 1), (2, 5), (3, 10)):
        rnd = random.Random(seed)
        f = FeedBase(None, "TEST", "0.000001", f"{k}e-6")
        assert f.k == k
        truth = ({}, {})
        t = 1000
        # snapshot
        bids = [(f"{(7000 - i) * 1e-6:.6f}", str(rnd.randint(1, 100))) for i in range(1, 200)]
        asks = [(f"{(7000 + i) * 1e-6:.6f}", str(rnd.randint(1, 100))) for i in range(1, 200)]
        f.reset_book(t, bids, asks)
        for p, q in bids:
            truth[BID][f.native_key(p)] = float(q)
        for p, q in asks:
            truth[ASK][f.native_key(p)] = float(q)
        check(f, truth, k)
        for _ in range(5000):
            t += rnd.choice((0, 100))
            side = rnd.choice((BID, ASK))
            n = 7000 - rnd.randint(1, 250) if side == BID else 7000 + rnd.randint(1, 250)
            q = 0.0 if rnd.random() < 0.3 else float(rnd.randint(1, 500))
            f.set_level(t, side, f"{n * 1e-6:.6f}", q)
            if q > 0:
                truth[side][n] = q
            else:
                truth[side].pop(n, None)
            f.after_event(t)
        check(f, truth, k)


if __name__ == "__main__":
    test_aggregation_matches_bruteforce()
    print("ok test_aggregation_matches_bruteforce")
