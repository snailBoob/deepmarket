"""Histogram wire/scheduling regressions. Run: .venv/bin/python -m tests.test_server"""

import asyncio
import struct
import time
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import orjson

from deep_market.book import BID
from deep_market.feed import FeedBase
from deep_market.server import Client, Coin, Engine, _live_levels, clean_settings, put_settings


START = 1_800_000_000_000  # aligned to every tested heatmap interval
REAL_SLEEP = asyncio.sleep


def market():
    feed = FeedBase(None, "TEST", "0.01")
    feed.reset_book(START - 60_000, [("100.01", "40")], [("100.02", "60")])
    engine = Engine(None, 14_400_000)
    coin = Coin("TEST@BNF", Decimal("0.01"), {"binance": feed})
    engine.coins[coin.symbol] = coin
    feed.trade_listener = coin.on_trade
    return engine, feed


class Wire:
    def __init__(self, clock, on_send=None):
        self.clock = clock
        self.messages = []
        self.on_send = on_send

    async def send_bytes(self, blob):
        length = struct.unpack_from("<I", blob)[0]
        header = orjson.loads(blob[4:4 + length])
        payload = np.frombuffer(blob, "<f4", offset=4 + length)
        self.messages.append((self.clock(), header, payload))
        if self.on_send:
            self.on_send(header)


def view(dt=30_000, **kw):
    return dict(type="view", symbol="TEST@BNF", req=1, dt=dt, ncols=3,
                nrows=3, row_ticks=10, tick_lo=9990, **kw)


async def trace(dt, historical=False, duration=3.1, trade_during_grid=False, **view_options):
    engine, feed = market()
    clock = [0.0]
    changed = False

    async def advance(seconds):
        nonlocal changed
        assert seconds >= 0
        clock[0] += max(seconds, 1e-9)
        if clock[0] > duration:
            raise asyncio.CancelledError
        if clock[0] >= 0.35 and not changed:
            feed.set_level(START + 350, BID, "100.01", 80)
            # Late arrivals must reach both SVP and the next heatmap trade bucket.
            feed.on_trade(START - 100, "100.01", 7, 1)
            feed.on_trade(START - 50, "100.02", 3, -1)
            changed = True
        await REAL_SLEEP(0)

    engine.data_now = lambda feeds: START + round(clock[0] * 1000)
    def on_send(header):
        if trade_during_grid and header["type"] == "grid":
            feed.on_trade(START - 1, "100.01", 2, 1)

    wire = Wire(lambda: clock[0], on_send)
    client = Client(engine, wire)
    fake_time = SimpleNamespace(monotonic=lambda: clock[0], perf_counter=time.perf_counter)
    with patch("deep_market.server.time", fake_time), patch("deep_market.server.asyncio.sleep", advance):
        await client.handle_view(view(dt, t_end=START - 60_000 if historical else None, **view_options))
        await client.pusher
    return wire.messages


class OrderedFlowTests(unittest.TestCase):
    def test_ordered_row_budget_keeps_padding_without_changing_legacy_limit(self):
        async def run():
            for ordered, requested, expected in [(1,3220,3220),(1,100_000,8192),(0,3220,2000)]:
                engine, feed = market()
                engine.data_now = lambda _: START
                wire = Wire(lambda: 0)
                client = Client(engine,wire)
                await client.handle_view(dict(view(50),orderflow=ordered,nrows=requested,
                                              row_ticks=1,tick_lo=8500))
                client.close()
                h, data = wire.messages[0][1:]
                self.assertEqual(h['nrows'],expected)
                self.assertEqual(data.size,h['ncols']*expected)
                self.assertEqual(float(data[-expected:].sum()),100)
        asyncio.run(run())

    def test_ordered_grid_keeps_same_timestamp_revisits_and_filters_individually(self):
        async def run():
            engine, feed = market()
            for tick, qty, side in [(10001,10,1),(10002,5,-1),(10001,20,1),(10001,.05,1)]:
                feed.on_trade(START-2, str(Decimal(tick)/100), qty, side)
            engine.data_now = lambda _: START
            wire = Wire(lambda: 0)
            client = Client(engine,wire)
            await client.handle_view(view(6, orderflow=1, min_trade_size=.1))
            client.close()
            h = wire.messages[0][1]
            self.assertEqual(h['dt'],6)
            self.assertEqual(h['events'][1::4],[10001,10002,10001])
            self.assertEqual(h['events'][2::4],[10,5,20])
            self.assertEqual(h['events'][::4],[START-2]*3)
        asyncio.run(run())

    def test_batched_columns_sample_their_own_book_and_deliver_late_trades_once(self):
        messages = asyncio.run(trace(10,duration=.6,trade_during_grid=True,orderflow=1))
        batches = [h for _,h,_ in messages if h['type']=='cols']
        events = [v for h in batches for v in h['events']]
        self.assertEqual(events[2::4],[2,7,3])
        self.assertEqual(len(set(t for h in batches for t in h['times'])),61)
        for _,h,data in messages:
            if h['type']!='cols': continue
            for i,t in enumerate(h['times']):
                self.assertEqual(data[i*3+1],100 if t<START+350 else 140)

    def test_reduced_grid_matches_price_descending_jvm_rows(self):
        from deep_market.store import _rows
        bids = np.arange(10,0,-1,dtype=np.float32)
        for rows,expected in [(3,[27,18,10]),(4,[19,15,15,6]),(6,[10,17,7,11,7,3])]:
            np.testing.assert_array_equal(_rows(bids,np.zeros(10),rows,1,10),expected)

    def test_current_wide_column_refreshes_before_rollover(self):
        messages=asyncio.run(trace(30_000,duration=.6,orderflow=1))
        batches=[(h,data) for _,h,data in messages if h['type']=='cols']
        self.assertGreater(len(batches),1)
        self.assertTrue(all(h['times']==[START] and h['replace'] for h,_ in batches))
        self.assertEqual(batches[0][1][1],100)
        self.assertEqual(batches[-1][1][1],140)
        self.assertEqual([q for h,_ in batches for q in h['events'][2::4]],[7,3])

    def test_ordered_coarse_grid_uses_last_update_not_last_keyframe(self):
        engine,feed=market()
        feed.set_level(START+350,BID,'100.01',80)
        result=engine.grid([feed],np.array([START-1,START+29999]),30000,9990,3,10,exact=True)
        np.testing.assert_array_equal(result[0][:,1],[100,140])

    def test_reduced_history_replays_updates_in_native_rows_before_compression(self):
        engine,feed=market()
        feed.reset_book(START,[(str(Decimal(10000+i)/100),str(10-i)) for i in range(10)], [('100.10','1')])
        feed.set_level(START+10,BID,'100.00',100)
        feed.set_level(START+20,BID,'100.01',0)
        result=engine.grid([feed],np.array([START,START+10,START+20]),10,10000,3,1,
                           source_rows=10,exact=True)
        np.testing.assert_array_equal(result[0],[[27,18,10],[117,18,10],[108,18,10]])


class SettingsTests(unittest.TestCase):
    def test_legacy_settings_convert_without_changing_render_factors(self):
        legacy = dict(dotSize=1.15, bubbleDensity=1.8, cutoff=0, dimming=0.27, units="base", tz="UTC")
        new = clean_settings(legacy)
        self.assertAlmostEqual(new["volumeDotSize"], (1.15-0.3)/2.7*100)
        self.assertAlmostEqual(new["clustering"], 52)
        self.assertEqual(new["lowerCutoff"], 40)
        self.assertEqual(new["heatmapIntensity"], 73)
        self.assertEqual(new["heatmapDimming"], 27)  # previous browser's opposite scale
        for key in legacy:
            if isinstance(legacy[key], (float, int)):
                self.assertAlmostEqual(new[key], legacy[key])
            else:
                self.assertEqual(new[key], legacy[key])
        self.assertEqual(clean_settings(new), new)

    def test_dimming_migration_preserves_intensity_for_both_old_formats(self):
        for old in (dict(dimming=0.4), dict(heatmapDimming=40, dimming=0.4)):
            migrated = clean_settings(old)
            self.assertEqual(migrated["heatmapIntensity"], 60)
            self.assertAlmostEqual(migrated["dimming"], 0.4)
            self.assertEqual(clean_settings(migrated), migrated)
        for value, fade in ((0, 1), (60, 0.4), (100, 0)):
            current = clean_settings(dict(heatmapIntensity=value))
            self.assertAlmostEqual(current["dimming"], fade)
            self.assertEqual(current["heatmapDimming"], 100-value)

    def test_settings_partial_writes_from_old_and_new_browsers(self):
        async def run():
            app = {"settings": clean_settings({})}
            async def write(body):
                async def json(): return body
                request = SimpleNamespace(app=app, json=json)
                with patch("deep_market.server.save_settings") as save:
                    response = await put_settings(request)
                    save.assert_called_once_with(app["settings"])
                return orjson.loads(response.body)
            values = dict(volumeDotSize=40, clustering=52, transparency=41, minVolume=0.1,
                          minTradeSize=0.1, lowerCutoff=15, upperCutoff=96, heatmapIntensity=60,
                          brightness=100, contrast=0, adjustOnRecenter=True, adjustByTime=True,
                          adjustInterval=10)
            new = await write(values)
            for key, value in values.items(): self.assertEqual(new[key], value)
            self.assertAlmostEqual(new["cutoff"], -0.625)
            self.assertAlmostEqual(new["dimming"], 0.4)
            for legacy in (dict(heatmapDimming=20), dict(dimming=0.2)):
                cached_write = await write(legacy)
                self.assertEqual(cached_write["heatmapIntensity"], 80)
            canonical = await write(dict(heatmapIntensity=60, heatmapDimming=90, dimming=0.9))
            self.assertEqual(canonical["heatmapIntensity"], 60)
            self.assertAlmostEqual(canonical["dimming"], 0.4)
            old_write = await write(dict(dotSize=2, cutoff=0.5))
            self.assertAlmostEqual(old_write["volumeDotSize"], (2-0.3)/2.7*100)
            self.assertEqual(old_write["lowerCutoff"], 60)
            self.assertEqual(old_write["upperCutoff"], 96)
            self.assertEqual(old_write["transparency"], 41)
            # Explicit canonical values win when a cached client sends both shapes.
            both = await write(dict(clustering=52, bubbleDensity=3))
            self.assertAlmostEqual(both["bubbleDensity"], 1.8)
            bounded = await write(dict(lowerCutoff=99, upperCutoff=20, transparency=999,
                                       minTradeSize=-1, adjustInterval="bad"))
            self.assertLess(bounded["lowerCutoff"], bounded["upperCutoff"])
            self.assertEqual(bounded["transparency"], 100)
            self.assertEqual(bounded["minTradeSize"], 0)
            self.assertEqual(bounded["adjustInterval"], 10)
        asyncio.run(run())


class HistogramTests(unittest.TestCase):
    def test_selected_profile_levels_ignore_heatmap_aggregation(self):
        async def run():
            engine, feed = market()
            engine.data_now = lambda feeds: START
            feed.on_trade(START - 100, "100.01", 7, 1)
            feed.on_trade(START - 50, "100.02", 3, -1)
            wire = Wire(lambda: 0)
            client = Client(engine, wire)
            for row_ticks in (1, 10, 100):
                await client.handle_view(dict(view(t_end=START-60_000), tick_lo=9800,
                    row_ticks=row_ticks, nrows=400//row_ticks, profile_row_ticks=1,
                    book_rounding="outward"))
                for kind, want in (("cob", [40,60,40,0,0,60]), ("svp", [7,0,0,3])):
                    header, payload = next((h,p) for _,h,p in reversed(wire.messages) if h["type"] == kind)
                    self.assertEqual(header.get("price_ticks"), [10001,10002])
                    self.assertEqual(header.get("row_ticks"), 1)
                    np.testing.assert_array_equal(payload, want)
            # Selected tick size still aggregates deliberately, with outward book rounding.
            await client.handle_view(dict(view(t_end=START-60_000), profile_row_ticks=5, book_rounding="outward"))
            h,p = next((h,p) for _,h,p in reversed(wire.messages) if h["type"] == "cob")
            self.assertEqual(h["price_ticks"], [10000,10005])
            np.testing.assert_array_equal(p, [40,60,40,0,0,60])
            h,p = next((h,p) for _,h,p in reversed(wire.messages) if h["type"] == "svp")
            self.assertEqual(h["price_ticks"], [10000])
            np.testing.assert_array_equal(p, [7,3])
            client.close()
            await REAL_SLEEP(0)
        asyncio.run(run())

    def test_selected_profile_levels_follow_live_updates(self):
        messages = asyncio.run(trace(30_000, historical=True, profile_row_ticks=1, book_rounding="outward"))
        profiles = [(h,p) for _,h,p in messages if h["type"] == "cob"]
        self.assertTrue(all(h.get("price_ticks") == [10001,10002] for h,p in profiles))
        np.testing.assert_array_equal(profiles[-1][1], [80,60,80,0,0,60])
        h,p = next((h,p) for _,h,p in reversed(messages) if h["type"] == "svp")
        self.assertEqual(h["price_ticks"], [10001,10002])
        np.testing.assert_array_equal(p, [7,0,0,3])

    def test_filter_changes_rebuild_history_from_individual_records(self):
        async def run():
            engine, feed = market()
            engine.data_now = lambda feeds: START
            for qty in (0.4, 0.4, 0.7): feed.on_trade(START - 50, "100.01", qty, 1)
            wire = Wire(lambda: 0)
            client = Client(engine, wire)
            for minimum, visible in ((0.7, 0.7), (0, 1.5), (2, 0)):
                await client.handle_view(view(100, min_trade_size=minimum, book_rounding="outward"))
                h = next(h for _, h, _ in reversed(wire.messages) if h["type"] == "grid")
                self.assertEqual(sum(h["buy_vol"]), 1.5)
                self.assertEqual(sum(h["trades"][2::4]), visible)
                self.assertEqual(h["min_trade_size"], minimum)
            client.close()
            await REAL_SLEEP(0)
        asyncio.run(run())

    def test_outward_and_filter_follow_live_updates_without_filtering_totals(self):
        messages = asyncio.run(trace(100, duration=0.7, book_rounding="outward", min_trade_size=5))
        cols = [(h, p) for _, h, p in messages if h["type"] == "col"]
        changed = next((h, p) for h, p in cols if h["buy_vol"] > 0)
        h, p = changed
        self.assertEqual((h["buy_vol"], h["sell_vol"]), (7, 3))
        self.assertEqual(h["trades"], [10001, 7, 0])
        np.testing.assert_array_equal(p, [0, 80, 60])
        profiles = [p for _, h, p in messages if h["type"] == "cob"]
        np.testing.assert_array_equal(profiles[-1], [0, 80, 60, 0, 80, 0, 0, 0, 60])

    def test_synthetic_outward_rows_preserve_visible_volume(self):
        # Invented prices and distinct quantities: no exchange or reference data.
        # Exact grid boundaries (1.05, 1.10, 1.15) must stay on their own row.
        feed = FeedBase(None, "SYNTHETIC", "0.01")
        feed.reset_book(START,
            [("1.01", "2"), ("1.04", "3"), ("1.05", "5"), ("1.08", "7")],
            [("1.09", "11"), ("1.10", "13"), ("1.11", "17"),
             ("1.14", "19"), ("1.15", "23"), ("1.19", "29")])
        # Independently calculated rows: bids round down, asks round up.
        # The cropped view excludes the first bid row and the final ask row,
        # rather than clamping their volume into a visible edge row.
        cases = [
            (100, 5, [5, 12, 0, 0, 0], [0, 0, 24, 59, 29], (17, 112)),
            (100, 10, [17, 0, 0], [0, 24, 88], (17, 112)),
            (105, 5, [12, 0, 0], [0, 24, 59], (12, 83)),
        ]
        for tick_lo, row_ticks, bids, asks, totals in cases:
            with self.subTest(tick_lo=tick_lo, row_ticks=row_ticks):
                nrows = len(bids)
                sides, bb, ba = Engine.cob_rows([feed], tick_lo, nrows, row_ticks, outward=True)
                self.assertEqual((bb, ba), (108, 109))
                np.testing.assert_array_equal(sides, bids + asks)
                self.assertEqual((float(sides[:nrows].sum()), float(sides[nrows:].sum())), totals)
                expected = np.array(bids) + np.array(asks)
                live, _, _ = Engine.live_column([feed], tick_lo, nrows, row_ticks, outward=True)
                history, _, _ = feed.history.sample_grid([START], tick_lo, nrows, row_ticks, outward=True)
                np.testing.assert_array_equal(live, expected)
                np.testing.assert_array_equal(history[0], expected)

    def test_outward_wire_is_opt_in_for_cached_clients(self):
        async def run():
            engine, _ = market()
            engine.data_now = lambda feeds: START
            wire = Wire(lambda: 0)
            client = Client(engine, wire)
            for mode in (None, "outward"):
                req = view(t_end=START-60_000)
                if mode:
                    req["book_rounding"] = mode
                await client.handle_view(req)
                header, payload = next((h, p) for _, h, p in reversed(wire.messages) if h["type"] == "grid")
                self.assertEqual(header.get("book_rounding", "floor"), mode or "floor")
                np.testing.assert_array_equal(payload.reshape(3, 3)[-1], [0, 40, 60] if mode else [0, 100, 0])
            client.close()
            await REAL_SLEEP(0)
        asyncio.run(run())

    def test_minimum_trade_size_filters_before_price_column_aggregation(self):
        _, feed = market()
        for q, side in [(0.4, 1), (0.4, 1), (0.7, 1), (0.8, -1)]:
            feed.on_trade(START, "100.01", q, side)
        levels = feed.history.trade_levels([START], 100, min_trade_size=0.7)
        np.testing.assert_allclose(levels[2:], [[0.7], [0.8]])
        live = _live_levels([feed.history.trades.since(0)], min_trade_size=0.7)
        self.assertEqual(live, [10001.0, 0.7, 0.8])
        total = feed.history.trade_columns([START], 100)
        np.testing.assert_allclose([total[0][0], total[2][0]], [1.5, 0.8])

    def test_initial_snapshot_preserves_both_sides_and_legacy_total(self):
        for historical in (False, True):
            with self.subTest(historical=historical):
                messages = asyncio.run(trace(30_000, historical))
                snapshots = [(t, h, p) for t, h, p in messages if h["type"] == "cob"]
                self.assertTrue(snapshots, "COB must arrive without waiting for a heatmap column")
                t, header, payload = snapshots[0]
                self.assertEqual(t, 0, "COB must be available with the initial grid")
                self.assertTrue(header.get("book_sides"))
                # Prefix remains readable by already-open browser bundles.
                np.testing.assert_array_equal(payload, [0, 100, 0, 0, 40, 0, 0, 60, 0])
                self.assertEqual((header["bid"], header["ask"]), (10001, 10002))

    def test_refresh_does_not_follow_time_zoom(self):
        for dt in (100, 1000, 5000, 30_000):
            for historical in (False, True):
                with self.subTest(dt=dt, historical=historical):
                    messages = asyncio.run(trace(dt, historical))
                    for kind, max_gap in (("cob", 0.101), ("svp", 1.001)):
                        times = [t for t, h, p in messages if h["type"] == kind]
                        self.assertGreaterEqual(len(times), 4, (kind, times))
                        self.assertLessEqual(max(b - a for a, b in zip(times, times[1:])), max_gap)
                        self.assertLessEqual(3.1 - times[-1], max_gap)
                    latest = {h["type"]: p for t, h, p in messages}
                    np.testing.assert_array_equal(latest["cob"][:3], [0, 140, 0])
                    np.testing.assert_array_equal(latest["svp"], [0, 7, 0, 0, 3, 0])
                    if historical or dt > 3100:
                        self.assertNotIn("col", latest)

    def test_heatmap_trade_bucket_is_not_consumed_by_histogram_updates(self):
        messages = asyncio.run(trace(30_000, duration=60.1))
        cols = [(h, p) for t, h, p in messages if h["type"] == "col"]
        self.assertEqual(len(cols), 2)
        h, payload = cols[0]
        self.assertEqual(h["t"], START + 30_000)
        self.assertEqual((h["buy_vol"], h["sell_vol"]), (7, 3))
        self.assertEqual(h["trades"], [10001.0, 7.0, 0.0, 10002.0, 0.0, 3.0])
        np.testing.assert_array_equal(payload, [0, 140, 0])
        self.assertEqual((cols[1][0]["buy_vol"], cols[1][0]["sell_vol"]), (0, 0))

    def test_view_replacement_and_close_stop_the_previous_pusher(self):
        async def run():
            engine, _ = market()
            engine.data_now = lambda feeds: START
            wire = Wire(lambda: 0)
            client = Client(engine, wire)
            await client.handle_view(view())
            old = client.pusher
            await REAL_SLEEP(0)
            await client.handle_view(view(t_end=START - 60_000))
            current = client.pusher
            await REAL_SLEEP(0)
            self.assertTrue(old.done())
            self.assertIsNot(old, current)
            self.assertEqual(client.view_id, 2)
            client.close()
            await REAL_SLEEP(0)
            self.assertTrue(current.done())
            self.assertEqual({h["view"] for _, h, _ in wire.messages}, {1, 2})
        asyncio.run(run())

    def test_late_trade_during_initial_grid_delivery_reaches_next_column(self):
        messages = asyncio.run(trace(100, duration=0.2, trade_during_grid=True))
        cols = [h for _, h, _ in messages if h["type"] == "col"]
        self.assertEqual(sum(h["buy_vol"] for h in cols), 2)
        self.assertEqual(cols[0]["trades"], [10001.0, 2.0, 0.0])


if __name__ == "__main__":
    unittest.main()
