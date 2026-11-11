"""Engine + web server: keeps the feeds running and serves the heatmap to browsers.

usage: .venv/bin/python -m deep_market.server ALTUSDT@BNF [ALTUSDT@BB ...] [--port 8740]

An instrument (a tab) is one coin on one exchange: @BNF = Binance USDT-M
futures, @BB = Bybit linear. The default rolling history window is 4 hours.

Protocol on /ws
  client -> server (JSON text):
    {"type": "view", "req": n, "symbol": "ALTUSDT@BNF", "dt": 100, "ncols": 500,
     "nrows": 200, "row_ticks": 1, "tick_lo": null | int, "t_end": null | ms}
        tick_lo null = centre on the current price; t_end null = live. Replaces the previous
        view; the server answers with a "grid", "cob", "svp" and then pushes updates.
        orderflow=1 opts into ordered events [ms,tick,base_qty,side] and "cols"
        batches. Their times label column starts; replace=true also refreshes the
        unfinished last column. Sampling is end-of-column (or the latest state
        for the current column), delivery every 50ms, independent of column width.
        heat_source_rows optionally requests fixed-point price-row compression;
        row_ticks remains the source level size, output row width is
        heat_source_rows*row_ticks/nrows. Legacy clients retain their old contract.
  server -> client:
    text   {"type": "meta", "symbols": {instrument: {symbol, exchange, tick_size, decimals}}}
    binary [uint32 header length][header JSON, padded to 4 bytes][float32 payload]
      grid: header {type, view, req, t_end, dt, ncols, nrows, tick_lo, row_ticks, live, bid[],
            ask[], buy_vol[], buy_px[], sell_vol[], sell_px[], trades, last_trade},
            payload ncols*nrows sizes, oldest column first, row 0 = tick_lo
      col:  live view, every dt: header {t, bid, ask, buy_vol, buy_px, sell_vol, sell_px,
            trades, last_trade}, payload nrows sizes (the book of now)
      cob:  live AND history views, every 100 ms, independently of dt: current book.
            Payload starts with nrows totals (legacy readers); with book_sides=true,
            another nrows bids and nrows asks follow. All quantities are base units.
      svp:  every second, independently of dt: payload 2*nrows = buys, then sells
"""

import argparse
import asyncio
import logging
import os
import pathlib
import re
import secrets
import signal
import struct
import time
from decimal import Decimal

import aiohttp
import numpy as np
import orjson
from aiohttp import web

from . import __version__, binance, bybit
from .book import ASK, BID
from .store import BookHistory, _levels, filter_trades

log = logging.getLogger("deep_market.server")
WEB_DIR = pathlib.Path(__file__).resolve().parent.parent / "web"
DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
SETTINGS_FILE = DATA_DIR / "settings.json"

TABS_FILE = DATA_DIR / "tabs.json"
HISTORY_DIR = DATA_DIR / "history"
MAX_TABS = 5
SAVE_EVERY_S = 300

# Display settings, global for all coins, stored on the server. key -> (min, max, default).
SETTINGS_NUM = {
    "volumeDotSize": (0.0, 100.0, 40.0),
    "clustering": (0.0, 100.0, 52.0),
    "transparency": (0.0, 100.0, 41.0),
    "minVolume": (0.0, 1e12, 0.0),       # absolute cluster delta, in coins
    "minTradeSize": (0.0, 1e12, 0.0),    # source record quantity, in coins
    "lowerCutoff": (0.0, 99.0, 15.0),
    "upperCutoff": (1.0, 100.0, 96.0),
    "heatmapIntensity": (0.0, 100.0, 60.0),  # 100 = undimmed
    "brightness": (0.0, 100.0, 100.0),
    "contrast": (-100.0, 100.0, 0.0),
    "adjustInterval": (0.25, 3600.0, 10.0),
    "nowFrac": (0.3, 0.95, 0.7),   # position of the now-line as a fraction of the heatmap width
}
SETTINGS_BOOL = {"adjustOnRecenter": True, "adjustByTime": True}
# Expand/contract bridge: distinct canonical keys avoid reinterpreting values sent
# by an already-open old browser. Keep derived aliases for rollback/old readers.
# legacy key -> (canonical key, offset, legacy units per canonical unit)
SETTINGS_ALIASES = {
    "dotSize": ("volumeDotSize", 0.3, 0.027),
    "bubbleDensity": ("clustering", 0.5, 0.025),
    "cutoff": ("lowerCutoff", -1.0, 0.025),
    "heatmapDimming": ("heatmapIntensity", 100.0, -1.0),
    "dimming": ("heatmapIntensity", 1.0, -0.01),
}
SETTINGS_CHOICE = {
    "units": (("base", "usdt"), "base"),   # sizes in coins or in USDT
}
TZ_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")


def load_settings():
    try:
        data = orjson.loads(SETTINGS_FILE.read_bytes())
    except (FileNotFoundError, orjson.JSONDecodeError):
        data = {}
    return clean_settings(data)


def expand_settings(data):
    data = dict(data)
    for old, (key, offset, scale) in SETTINGS_ALIASES.items():
        if old in data and key not in data:
            try:
                data[key] = (float(data[old]) - offset) / scale
            except (TypeError, ValueError):
                pass
    return data


def clean_settings(data):
    data = expand_settings(data)
    out = {}
    for k, (lo, hi, default) in SETTINGS_NUM.items():
        try:
            value = float(data.get(k, default))
            out[k] = min(hi, max(lo, value)) if np.isfinite(value) else default
        except (TypeError, ValueError):
            out[k] = default
    out["lowerCutoff"] = min(out["lowerCutoff"], out["upperCutoff"] - 1)
    for old, (key, offset, scale) in SETTINGS_ALIASES.items():
        out[old] = offset + scale * out[key]
    for k, default in SETTINGS_BOOL.items():
        out[k] = data[k] if isinstance(data.get(k), bool) else default
    for k, (choices, default) in SETTINGS_CHOICE.items():
        out[k] = data.get(k) if data.get(k) in choices else default
    tz = data.get("tz")
    out["tz"] = tz if isinstance(tz, str) and len(tz) <= 64 and TZ_RE.match(tz) else "Etc/GMT-1"  # UTC+1
    return out


def _write_json(path, obj):
    DATA_DIR.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(orjson.dumps(obj, option=orjson.OPT_INDENT_2))
    tmp.replace(path)  # atomic: never a half-written file


def save_settings(settings):
    _write_json(SETTINGS_FILE, settings)


def load_tabs():
    """[{"inst", "gran", "sizeGran"}] in tab order."""
    try:
        data = orjson.loads(TABS_FILE.read_bytes())
    except (FileNotFoundError, orjson.JSONDecodeError):
        return None
    return [t for t in data if isinstance(t, dict) and isinstance(t.get("inst"), str)][:MAX_TABS]


MAX_COLS = 4000
MAX_ROWS = 2000
MAX_ORDERFLOW_ROWS = 8192  # visible 2H rows plus same-density off-screen padding
# Exchange suffixes used in instrument identifiers (ALTUSDT@BNF).
SUFFIX = {"BNF": ("binance", binance, binance.BinanceFeed), "BB": ("bybit", bybit, bybit.BybitFeed)}


def now_ms():
    return int(time.time() * 1000)


def _aggregate(recs_list):
    """Trade records (one array per exchange) -> (buy_vol, buy_px, sell_vol, sell_px);
    px = volume-weighted tick."""
    out = []
    for side_val in (1, -1):
        vol = pxw = 0.0
        for recs in recs_list:
            m = recs["side"] == side_val
            q = recs["qty"][m].astype(np.float64)
            vol += float(q.sum())
            pxw += float((q * recs["tick"][m]).sum())
        out += [vol, round(pxw / vol, 2) if vol > 0 else None]
    return tuple(out)


def _merge_levels(parts):
    """Merge (col, tick, buy, sell) arrays of several exchanges into one flat JSON list
    [col, tick, buy, sell, col, tick, ...], summing equal (col, tick)."""
    parts = [p for p in parts if len(p[0])]
    if not parts:
        return []
    col = np.concatenate([p[0] for p in parts])
    tick = np.concatenate([p[1] for p in parts])
    buy = np.concatenate([p[2] for p in parts])
    sell = np.concatenate([p[3] for p in parts])
    if len(parts) > 1:
        key = col * (1 << 32) + (tick - tick.min())
        uniq, inv = np.unique(key, return_inverse=True)
        buy = np.bincount(inv, weights=buy, minlength=len(uniq))
        sell = np.bincount(inv, weights=sell, minlength=len(uniq))
        tick = (uniq & ((1 << 32) - 1)) + tick.min()
        col = uniq >> 32
    out = np.empty((len(col), 4))
    out[:, 0], out[:, 1], out[:, 2], out[:, 3] = col, tick, buy, sell
    return [round(v, 6) for v in out.ravel().tolist()]


def _live_levels(recs_list, min_trade_size=0):
    """Trades since the last column, per price tick: flat list [tick, buy, sell, ...]."""
    recs_list = [filtered for r in recs_list if len(filtered := filter_trades(r, min_trade_size))]
    if not recs_list:
        return []
    recs = np.concatenate(recs_list) if len(recs_list) > 1 else recs_list[0]
    _c, tick, buy, sell = _levels(np.zeros(len(recs), np.int64), recs)
    out = np.empty((len(tick), 3))
    out[:, 0], out[:, 1], out[:, 2] = tick, buy, sell
    return [round(v, 6) for v in out.ravel().tolist()]


def _round_list(a, nd=6):
    """JSON-friendly list: NaN -> None, rounded to keep the message small."""
    return [None if v != v else round(v, nd) for v in a.tolist()]


def _trade_events(parts, minimum=0):
    """Ordered events, flat [timestamp_ms, native_tick, base_qty, aggressor].

    Stable ordering preserves arrival order for equal timestamps. Unlike levels,
    this never combines two visits to a price. Opt-in wire field, no storage change.
    """
    parts = [r for p in parts if len(r := filter_trades(p, minimum))]
    if not parts:
        return []
    records = np.concatenate(parts) if len(parts) > 1 else parts[0]
    if len(parts) > 1:
        records = records[np.argsort(records['t'], kind='stable')]
    return [v for r in records for v in (int(r['t']), int(r['tick']), float(r['qty']), int(r['side']))]


def _best(values, pick):
    """Combine best bid/ask ticks of several exchanges (-1 / None = unknown)."""
    vals = [v for v in values if v is not None and v >= 0]
    return pick(vals) if vals else -1


def pack(header, payload=None):
    h = orjson.dumps(header)
    h += b" " * (-(len(h) + 4) % 4)  # pad so the float32 payload is 4-byte aligned
    body = payload.astype("<f4", copy=False).tobytes() if payload is not None else b""
    return struct.pack("<I", len(h)) + h + body


class Coin:
    def __init__(self, symbol, tick_size, feeds):
        self.symbol = symbol
        self.tick_size = tick_size  # common grid (Decimal)
        self.feeds = feeds          # exchange name -> feed, in EXCHANGES order
        self.tasks = []
        self.last_trade = None      # over all exchanges, with a cross-exchange streak

    def on_trade(self, lt):
        prev = self.last_trade
        streak = prev["streak"] + lt["qty"] if prev and prev["tick"] == lt["tick"] and prev["side"] == lt["side"] else lt["qty"]
        self.last_trade = dict(lt, streak=streak)


def history_path(inst):
    return HISTORY_DIR / f"{inst.replace('@', '_')}.npz"


class Engine:
    def __init__(self, session, retention_ms):
        self.session = session
        self.retention_ms = retention_ms
        self.coins = {}             # instrument -> Coin, in tab order
        self.listeners = set()      # callables notified when the tabs change

    @staticmethod
    def parse(instrument):
        symbol, _, suffix = instrument.upper().partition("@")
        suffix = suffix or "BNF"
        if suffix not in SUFFIX:
            raise ValueError(f"unknown exchange @{suffix} (use @BNF or @BB)")
        return symbol, suffix

    async def add(self, instrument, gran=1, size_gran=None):
        """Start tracking one instrument: "ALTUSDT@BNF" (Binance Futures) or "ALTUSDT@BB"
        (Bybit). A plain symbol means Binance Futures. If a saved
        history of this instrument exists (server restart), it is loaded."""
        symbol, suffix = self.parse(instrument)
        inst = f"{symbol}@{suffix}"
        if inst in self.coins:
            raise ValueError(f"{inst} is already open")
        if len(self.coins) >= MAX_TABS:
            raise ValueError(f"at most {MAX_TABS} tabs; close a tab first")
        name, mod, cls = SUFFIX[suffix]
        info = await mod.symbol_info(self.session, symbol)
        if not info:
            raise ValueError(f"{symbol} is not a perpetual on {name}")
        history, profile = self._restore(inst)
        feed = cls(self.session, symbol, info["tick_size"], None, history)
        if profile:
            feed.profile = profile
        coin = Coin(inst, Decimal(info["tick_size"]), {name: feed})
        coin.base_symbol = symbol
        coin.exchange = name
        coin.suffix = suffix
        coin.step_size = info["step_size"]
        coin.gran = max(1, int(gran or 1))
        step = Decimal(info["step_size"])
        coin.size_gran = Decimal(str(size_gran)) if size_gran else step
        feed.trade_listener = coin.on_trade
        coin.tasks = [asyncio.create_task(feed.run()), asyncio.create_task(self._maintain(coin))]
        self.coins[inst] = coin
        log.info("tracking %s (%s, tick %s)%s", inst, name, info["tick_size"],
                 " with restored history" if profile is not None else "")
        return coin

    def _restore(self, inst):
        path = history_path(inst)
        if not path.exists():
            return BookHistory(self.retention_ms), None
        try:
            h, profile = BookHistory.load(path, self.retention_ms)
            if not h.keyframes or now_ms() - h.keyframe_times[-1] > self.retention_ms:
                log.info("%s: saved history too old, starting fresh", inst)
                return BookHistory(self.retention_ms), None
            return h, profile
        except Exception as e:
            log.warning("%s: could not load saved history (%s), starting fresh", inst, e)
            return BookHistory(self.retention_ms), None

    async def remove(self, inst):
        """Close a tab: stop tracking and throw the history away (also on disk)."""
        coin = self.coins.pop(inst)
        for t in coin.tasks:
            t.cancel()
        history_path(inst).unlink(missing_ok=True)
        log.info("stopped tracking %s", inst)

    async def save(self, coin):
        """Write the history of one instrument to disk (in a thread: big arrays)."""
        feed = next(iter(coin.feeds.values()))
        if not feed.history.keyframes:
            return
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        parts = feed.history.export_parts()
        profile = {k: list(v) for k, v in feed.profile.items()}
        await asyncio.to_thread(BookHistory.save_parts, history_path(coin.symbol), parts, profile)

    async def save_all(self):
        for coin in list(self.coins.values()):
            try:
                t0 = time.perf_counter()
                await self.save(coin)
                log.info("saved history of %s (%.1f s)", coin.symbol, time.perf_counter() - t0)
            except Exception as e:
                log.warning("saving %s failed: %s", coin.symbol, e)

    async def autosave(self):
        while True:
            await asyncio.sleep(SAVE_EVERY_S)
            await self.save_all()

    def tabs(self):
        return [{"inst": i, "gran": c.gran, "sizeGran": str(c.size_gran)} for i, c in self.coins.items()]

    def save_tabs(self):
        _write_json(TABS_FILE, self.tabs())

    def changed(self):
        """Tabs were added, closed or changed: store them and tell every browser."""
        self.save_tabs()
        for fn in list(self.listeners):
            fn()

    async def _maintain(self, coin):
        next_refresh = {n: time.monotonic() + 30 + 10 * i for i, n in enumerate(coin.feeds)}
        while True:
            await asyncio.sleep(1)
            for name, feed in coin.feeds.items():
                feed.check_book_freshness()
                if feed.synced and feed.last_event_time:
                    feed.history.prune(feed.last_event_time)
                if feed.synced and time.monotonic() >= next_refresh[name]:
                    next_refresh[name] = time.monotonic() + 60
                    try:
                        r = await feed.refresh()
                        lvl = logging.INFO if r.get("ok") else logging.WARNING
                        log.log(lvl, "%s %s refresh: %s", coin.symbol, name, r)
                    except Exception as e:
                        log.warning("%s %s refresh failed: %s", coin.symbol, name, e)

    def meta(self):
        out = {}
        for inst, c in self.coins.items():
            d = c.tick_size.normalize()
            sg = c.size_gran.normalize()
            out[inst] = {"symbol": c.base_symbol, "exchange": c.exchange, "suffix": c.suffix,
                         "tick_size": float(d), "decimals": max(0, -d.as_tuple().exponent),
                         "step_size": c.step_size, "gran": c.gran, "size_gran": float(sg),
                         "size_decimals": max(0, -sg.as_tuple().exponent)}
        return out

    def feeds(self, symbol, exchanges):
        coin = self.coins[symbol]
        return [coin.feeds[n] for n in coin.feeds if n in exchanges]

    def data_now(self, feeds):
        """'Now' on the exchange clock as far as our data goes: wall time minus the network
        delay of the slowest exchange, so every live column is complete for all of them.
        Live columns and the stored history both use exchange time, so they line up."""
        lat = max((f.latency_ms or 0.0) for f in feeds)
        return now_ms() - int(lat)

    @staticmethod
    def best_bid_ask(feeds):
        return (_best([f.book.best_bid() for f in feeds], max),
                _best([f.book.best_ask() for f in feeds], min))

    def last_trade(self, symbol, feeds):
        coin = self.coins[symbol]
        if len(feeds) == len(coin.feeds):
            return coin.last_trade
        lts = [f.last_trade for f in feeds if f.last_trade]
        return max(lts, key=lambda lt: lt["t"]) if lts else None

    @staticmethod
    def live_column(feeds, tick_lo, nrows, row_ticks=1, outward=False):
        sides, bb, ba = Engine.cob_rows(feeds, tick_lo, nrows, row_ticks, outward)
        return sides[:nrows] + sides[nrows:], bb, ba

    @staticmethod
    def cob_rows(feeds, tick_lo, nrows, row_ticks=1, outward=False):
        """Current book: bids then asks, even when both occupy the same price row."""
        out = np.zeros(2 * nrows, np.float32)
        span = nrows * row_ticks
        for f in feeds:
            for side in (BID, ASK):
                levels = f.book.sides[side]
                origin = tick_lo - (row_ticks - 1 if outward and side == ASK else 0)
                col = out[side * nrows:(side + 1) * nrows]
                if len(levels) < span:
                    for tick, size in levels.items():
                        i = tick - origin
                        if 0 <= i < span:
                            col[i // row_ticks] += size
                else:
                    get = levels.get
                    for i in range(span):
                        s = get(origin + i)
                        if s:
                            col[i // row_ticks] += s
        bb, ba = Engine.best_bid_ask(feeds)
        return out, bb, ba

    @staticmethod
    def svp_rows(feeds, tick_lo, nrows, row_ticks=1):
        """Session volume per row: float32 [2*nrows] = buy volumes, then sell volumes."""
        out = np.zeros(2 * nrows, np.float32)
        span = nrows * row_ticks
        for f in feeds:
            for tick, (b, s) in f.profile.items():
                i = tick - tick_lo
                if 0 <= i < span:
                    r = i // row_ticks
                    out[r] += b
                    out[nrows + r] += s
        return out

    @staticmethod
    def profile_levels(feeds, tick_lo, tick_hi, row_ticks, book=False, outward=False):
        """Occupied selected-price levels, independent of heatmap zoom aggregation.

        Sparse ticks keep a deeply zoomed-out view bounded by actual book/profile
        entries, not by the number of empty ticks across its entire price range.
        """
        rows = {}
        for feed in feeds:
            if book:
                entries = ((tick, side, qty) for side in (BID, ASK)
                           for tick, qty in feed.book.sides[side].items())
            else:
                entries = ((tick, side, qty) for tick, quantities in feed.profile.items()
                           for side, qty in enumerate(quantities))
            for tick, side, qty in entries:
                offset = row_ticks - 1 if book and outward and side == ASK else 0
                level = (tick + offset) // row_ticks * row_ticks
                if qty > 0 and tick_lo <= level < tick_hi:
                    rows.setdefault(level, [0., 0.])[side] += qty
        ticks = sorted(rows)
        sides = np.array([[rows[t][side] for t in ticks] for side in (0, 1)], np.float32).ravel()
        return ticks, sides

    @staticmethod
    def grid(feeds, times, dt, tick_lo, nrows, row_ticks, outward=False, min_trade_size=0, source_rows=None, exact=False):
        grid = None
        bbs, bas, trades = [], [], []
        for f in feeds:
            g, bb, ba = f.history.sample_grid(times, tick_lo, nrows, row_ticks, outward, source_rows, exact)
            grid = g if grid is None else grid + g
            bbs.append(bb)
            bas.append(ba)
            trades.append(f.history.trade_columns(times, dt))
        bb = np.max(np.stack(bbs), axis=0)  # -1 = unknown, so max() ignores it
        ba_stack = np.stack(bas).astype(np.float64)
        ba_stack[ba_stack < 0] = np.inf
        ba = np.min(ba_stack, axis=0)
        ba = np.where(np.isfinite(ba), ba, -1).astype(np.int64)
        # Trades: sum volumes, combine volume-weighted prices.
        bv = sum(t[0] for t in trades)
        sv = sum(t[2] for t in trades)
        with np.errstate(invalid="ignore", divide="ignore"):
            bp = sum(np.nan_to_num(t[0] * t[1]) for t in trades) / bv
            sp = sum(np.nan_to_num(t[2] * t[3]) for t in trades) / sv
        bp = np.where(bv > 0, bp, np.nan)
        sp = np.where(sv > 0, sp, np.nan)
        levels = _merge_levels([f.history.trade_levels(times, dt, min_trade_size) for f in feeds])
        return grid, bb, ba, bv, bp, sv, sp, levels


class Client:
    """One browser connection: at most one active view, with a pusher task."""

    SVP_EVERY_MS = 1000
    COB_EVERY_MS = 100

    def __init__(self, engine, ws):
        self.engine = engine
        self.ws = ws
        self.view_id = 0
        self.pusher = None

    def _exchanges(self, msg, symbol):
        avail = list(self.engine.coins[symbol].feeds)
        ex = [e for e in (msg.get("exchanges") or avail) if e in avail]
        return ex or avail

    async def handle_view(self, msg):
        symbol = msg.get("symbol")
        if symbol not in self.engine.coins:
            await self.ws.send_json({"type": "error", "error": f"unknown symbol {symbol}"})
            return
        exchanges = self._exchanges(msg, symbol)
        feeds = self.engine.feeds(symbol, exchanges)
        ordered = msg.get("orderflow") == 1
        dt = max(1 if ordered else 10, int(msg.get("dt", 100)))
        ncols = max(1, min(MAX_COLS, int(msg.get("ncols", 500))))
        nrows = max(1, min(MAX_ORDERFLOW_ROWS if ordered else MAX_ROWS, int(msg.get("nrows", 200))))
        row_ticks = max(1, min(100_000, int(msg.get("row_ticks", 1))))
        source_rows = max(nrows, min(2_000_000, int(msg.get("heat_source_rows", nrows)))) if ordered else nrows
        # Opt-in keeps already-open/cached browsers on their original dense format.
        profile_ticks = max(1, min(row_ticks, int(msg["profile_row_ticks"]))) if "profile_row_ticks" in msg else (row_ticks if ordered else 0)
        outward = msg.get("book_rounding") == "outward"  # old bundles retain their row coordinates
        min_trade_size = float(msg.get("min_trade_size", 0))
        if not np.isfinite(min_trade_size) or min_trade_size < 0:
            raise ValueError("invalid minimum trade size")
        tick_lo = msg.get("tick_lo")
        if tick_lo is None:
            bb, ba = self.engine.best_bid_ask(feeds)
            if bb < 0 or ba < 0:
                await self.ws.send_json({"type": "error", "error": "book not synced yet"})
                return
            tick_lo = (bb + ba) // 2 - (source_rows - int(outward)) * row_ticks // 2
        tick_lo = int(tick_lo) // row_ticks * row_ticks  # rows on a fixed grid: stable binning
        req_end = msg.get("t_end")  # None = live

        if self.pusher:
            self.pusher.cancel()
            self.pusher = None
        self.view_id += 1
        view = self.view_id

        clock = lambda: self.engine.data_now(feeds)  # noqa: E731
        live = req_end is None or int(req_end) >= clock()
        t_end = (clock() if live else int(req_end)) // dt * dt
        times = t_end - dt * np.arange(ncols - 1, -1, -1, dtype=np.int64)
        sample_times = times + dt - 1 if ordered else times
        t0 = time.perf_counter()
        grid, bb, ba, bv, bp, sv, sp, levels = self.engine.grid(
            feeds, sample_times, dt, tick_lo, nrows, row_ticks, outward, min_trade_size, source_rows, ordered)
        flow = {}
        if ordered:
            flow = {"orderflow": 1, "heat_source_rows": source_rows,
                    "events": _trade_events([f.history.trades.slice_time(int(times[0])-1, clock() if live else t_end+dt-1)
                                              for f in feeds], min_trade_size)}
        # Capture the cursor before sending: late trades can arrive while the socket
        # drains the initial grid/profiles and must still enter the next column.
        trade_pos = [f.history.trades.pos_after(clock() if ordered else t_end) for f in feeds] if live else None
        log.debug("grid %s %dx%d in %.1f ms", symbol, ncols, nrows, 1000 * (time.perf_counter() - t0))
        starts = [f.history.keyframe_times[0] for f in feeds if f.history.keyframe_times]
        await self.ws.send_bytes(pack({
            "type": "grid", "view": view, "req": msg.get("req"), "symbol": symbol,
            "exchanges": exchanges, "t_end": t_end, "dt": dt,
            "ncols": ncols, "nrows": nrows, "tick_lo": tick_lo, "row_ticks": row_ticks, "live": live,
            "book_rounding": "outward" if outward else "floor", "min_trade_size": min_trade_size,
            "history_start": min(starts) if starts else None,
            "bid": bb.tolist(), "ask": ba.tolist(),
            "buy_vol": _round_list(bv), "buy_px": _round_list(bp, 2),
            "sell_vol": _round_list(sv), "sell_px": _round_list(sp, 2),
            "trades": levels,  # flat [col, tick, buy, sell, ...]: trades per price level
            **flow,
            "last_trade": self.engine.last_trade(symbol, feeds),
            "server_time": clock(),
        }, grid))
        await self._send_cob(view, symbol, feeds, tick_lo, source_rows, row_ticks, outward, profile_ticks)
        await self._send_svp(view, feeds, tick_lo, source_rows, row_ticks, profile_ticks)
        self.pusher = asyncio.create_task(
            self._push(view, symbol, feeds, dt, t_end if live else None,
                       tick_lo, nrows, row_ticks, trade_pos, outward, min_trade_size, profile_ticks,
                       ordered, source_rows))

    async def _send_cob(self, view, symbol, feeds, tick_lo, nrows, row_ticks, outward=False, profile_ticks=0):
        geometry = {}
        if profile_ticks:
            ticks, sides = self.engine.profile_levels(feeds, tick_lo, tick_lo + nrows * row_ticks,
                                                      profile_ticks, book=True, outward=outward)
            geometry = {"price_ticks": ticks, "row_ticks": profile_ticks}
            nrows = len(ticks)
            bb, ba = self.engine.best_bid_ask(feeds)
        else:
            sides, bb, ba = self.engine.cob_rows(feeds, tick_lo, nrows, row_ticks, outward)
        total = sides[:nrows] + sides[nrows:]
        await self.ws.send_bytes(pack({
            "type": "cob", "view": view, "bid": bb, "ask": ba, "book_sides": True,
            **geometry,
            "last_trade": self.engine.last_trade(symbol, feeds),
            "server_time": self.engine.data_now(feeds),
        }, np.concatenate((total, sides))))

    async def _send_svp(self, view, feeds, tick_lo, nrows, row_ticks, profile_ticks=0):
        geometry = {}
        if profile_ticks:
            ticks, values = self.engine.profile_levels(feeds, tick_lo, tick_lo + nrows * row_ticks, profile_ticks)
            geometry = {"price_ticks": ticks, "row_ticks": profile_ticks}
        else:
            values = self.engine.svp_rows(feeds, tick_lo, nrows, row_ticks)
        await self.ws.send_bytes(pack({"type": "svp", "view": view,
                                      **geometry, "server_time": self.engine.data_now(feeds)}, values))

    async def _push(self, view, symbol, feeds, dt, t_last, tick_lo, nrows, row_ticks, trade_pos,
                    outward=False, min_trade_size=0, profile_ticks=0, ordered=False, source_rows=None):
        """One cancellable view task, with separate deadlines for profiles and heatmap.
        t_last=None freezes the heatmap in history mode; the profiles stay current."""
        clock = lambda: self.engine.data_now(feeds)  # noqa: E731
        next_svp = time.monotonic() + self.SVP_EVERY_MS / 1000
        next_cob = time.monotonic() + self.COB_EVERY_MS / 1000
        next_flow = time.monotonic() + 0.05
        source_rows = source_rows or nrows
        try:
            while True:
                now = time.monotonic()
                waits = [next_cob - now, next_svp - now]
                if t_last is not None:
                    waits.append(next_flow-now if ordered else (t_last + dt - clock()) / 1000)
                await asyncio.sleep(max(0.0, min(waits)))
                now = time.monotonic()
                if now >= next_cob:
                    next_cob = now + self.COB_EVERY_MS / 1000
                    await self._send_cob(view, symbol, feeds, tick_lo, source_rows, row_ticks, outward, profile_ticks)
                if now >= next_svp:
                    next_svp = now + self.SVP_EVERY_MS / 1000
                    await self._send_svp(view, feeds, tick_lo, source_rows, row_ticks, profile_ticks)
                if ordered and t_last is not None:
                    if now < next_flow:
                        continue
                    next_flow = now + 0.05
                    end = clock() // dt * dt
                    if end < t_last:  # latency estimator can move the data clock backwards
                        continue
                    times = np.arange(t_last, min(end, t_last+(MAX_COLS-1)*dt)+1, dt, dtype=np.int64)
                    grid, bb, ba, bv, bp, sv, sp, _ = self.engine.grid(
                        feeds, times+dt-1, dt, tick_lo, nrows, row_ticks, outward, min_trade_size, source_rows, True)
                    recs = []
                    for i, f in enumerate(feeds):
                        stop = f.history.trades.pos_after(min(clock(),int(times[-1])+dt-1))
                        recs.append(f.history.trades.since(trade_pos[i])[:max(0,stop-trade_pos[i])])
                        trade_pos[i] = max(trade_pos[i],stop)
                    await self.ws.send_bytes(pack({
                        "type":"cols", "view":view, "times":times.tolist(), "replace":True,
                        "bid":bb.tolist(), "ask":ba.tolist(),
                        "buy_vol":_round_list(bv), "buy_px":_round_list(bp,2),
                        "sell_vol":_round_list(sv), "sell_px":_round_list(sp,2),
                        "events":_trade_events(recs,min_trade_size),
                        "last_trade":self.engine.last_trade(symbol,feeds), "server_time":clock(),
                    },grid))
                    t_last = int(times[-1])
                    continue
                if t_last is None or clock() < t_last + dt:
                    continue
                t_last = max(t_last + dt, clock() // dt * dt)  # skip ahead if we fell behind
                col, bb, ba = self.engine.live_column(feeds, tick_lo, nrows, row_ticks, outward)
                # Every trade that arrived since the previous column, whatever its timestamp,
                # so late arrivals are not lost.
                recs = []
                for i, f in enumerate(feeds):
                    recs.append(f.history.trades.since(trade_pos[i]))
                    trade_pos[i] = f.history.trades.total
                bv, bp, sv, sp = _aggregate(recs)
                await self.ws.send_bytes(pack({
                    "type": "col", "view": view, "t": t_last, "bid": bb, "ask": ba,
                    "buy_vol": bv, "buy_px": bp, "sell_vol": sv, "sell_px": sp,
                    "trades": _live_levels(recs, min_trade_size),  # flat [tick, buy, sell, ...]
                    "last_trade": self.engine.last_trade(symbol, feeds),
                    "server_time": clock(),
                }, col))
        except (ConnectionResetError, asyncio.CancelledError):
            pass

    def close(self):
        if self.pusher:
            self.pusher.cancel()


async def ws_handler(request):
    engine = request.app["engine"]
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=1 << 20)
    connection = secrets.token_hex(4)
    started = time.monotonic()
    try:
        await ws.prepare(request)
    except Exception:
        log.warning("browser_ws_upgrade_failed connection=%s", connection, exc_info=True)
        raise
    log.info("browser_ws_connected connection=%s", connection)
    client = Client(engine, ws)
    send_meta = lambda: ws.send_json({"type": "meta", "symbols": engine.meta(),  # noqa: E731
                                      "order": list(engine.coins), "server_time": now_ms(),
                                      "version": __version__,
                                      "revision": os.environ.get("DEEP_MARKET_REVISION", "source")})

    def on_tabs_changed():
        if not ws.closed:
            asyncio.create_task(send_meta())

    engine.listeners.add(on_tabs_changed)
    request.app["sockets"].add(ws)
    try:
        await send_meta()
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                m = orjson.loads(msg.data)
            except orjson.JSONDecodeError:
                continue
            try:
                if m.get("type") == "view":
                    await client.handle_view(m)
            except (KeyError, TypeError, ValueError) as e:
                await ws.send_json({"type": "error", "error": f"bad request: {e}"})
    except Exception:
        log.exception("browser_ws_error connection=%s", connection)
        raise
    finally:
        log.log(logging.WARNING if ws.exception() else logging.INFO,
                "browser_ws_disconnected connection=%s duration_s=%.1f code=%s error=%s",
                connection, time.monotonic() - started, ws.close_code, ws.exception())
        request.app["sockets"].discard(ws)
        engine.listeners.discard(on_tabs_changed)
        client.close()
    return ws


async def index(_request):
    return web.FileResponse(WEB_DIR / "index.html")


async def get_settings(request):
    return web.json_response(request.app["settings"])


async def put_settings(request):
    """Merge a partial settings update from either generation of browser."""
    try:
        body = await request.json()
    except ValueError:
        raise web.HTTPBadRequest(text="invalid JSON")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="expected an object")
    cur = dict(request.app["settings"])
    body = expand_settings(body)
    for k in (*SETTINGS_NUM, *SETTINGS_CHOICE, *SETTINGS_BOOL, "tz"):
        if k in body:
            cur[k] = body[k]
    request.app["settings"] = clean_settings(cur)
    save_settings(request.app["settings"])
    return web.json_response(request.app["settings"])


async def get_debug_memory(request):
    """Diagnostics: process memory next to what the stores hold, per tab."""
    import gc
    import resource
    engine = request.app["engine"]
    trimmed = None
    if request.query.get("trim"):
        before = rss_mb()
        malloc_trim()
        trimmed = {"before_mb": round(before, 1), "after_mb": round(rss_mb(), 1)}
    with open("/proc/self/statm") as f:
        rss = int(f.read().split()[1]) * 4096
    tabs = {}
    for inst, c in engine.coins.items():
        f = next(iter(c.feeds.values()))
        nb = f.history.nbytes()
        tabs[inst] = {
            "store_mb": round(sum(nb.values()) / 2**20, 1),
            "parts_mb": {k: round(v / 2**20, 1) for k, v in nb.items()},
            "changes": len(f.history.changes), "chunks": len(f.history.changes.chunks),
            "keyframes": len(f.history.keyframes), "trades": len(f.history.trades),
            "book_levels": len(f.book.bids) + len(f.book.asks),
            "recent_events": len(getattr(f, "recent", ())),
            "profile_levels": len(f.profile),
        }
    # Optional allocation tracking (start the engine with DM_TRACEMALLOC=1): top growth
    # per source line since the previous call.
    growth = None
    import tracemalloc
    if tracemalloc.is_tracing():
        snap = tracemalloc.take_snapshot().filter_traces(
            [tracemalloc.Filter(False, tracemalloc.__file__), tracemalloc.Filter(False, "<frozen*")])
        prev = request.app.get("tm_prev")
        if prev is not None:
            growth = [f"{st.size_diff / 2**20:+.1f} MB {st.traceback[0].filename.split('/')[-1]}:{st.traceback[0].lineno}"
                      for st in snap.compare_to(prev, "lineno")[:12]]
        request.app["tm_prev"] = snap
        growth = {"traced_mb": round(tracemalloc.get_traced_memory()[0] / 2**20, 1), "top_growth": growth}
    return web.json_response({
        "trim": trimmed,
        "tracemalloc": growth,
        "rss_mb": round(rss / 2**20, 1),
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1),
        "gc_objects": len(gc.get_objects()),
        "clients": len(request.app["sockets"]),
        "tabs": tabs,
    })


# ---- tabs and symbol lists ------------------------------------------------------------

SYMBOL_CACHE_S = 600


async def get_symbols(request):
    """All perpetuals of one platform, for the symbol search in the "+" dialog."""
    suffix = request.query.get("platform", "BNF").upper()
    if suffix not in SUFFIX:
        raise web.HTTPBadRequest(text="platform must be BNF or BB")
    cache = request.app["symbol_cache"]
    hit = cache.get(suffix)
    if not hit or time.monotonic() - hit[0] > SYMBOL_CACHE_S:
        _name, mod, _cls = SUFFIX[suffix]
        hit = (time.monotonic(), await mod.list_symbols(request.app["engine"].session))
        cache[suffix] = hit
    return web.json_response(hit[1])


async def get_tabs(request):
    return web.json_response(request.app["engine"].tabs())


def _bad(msg):
    return web.json_response({"error": msg}, status=400)


def _size_gran(value):
    try:
        d = Decimal(str(value))
    except Exception:
        raise ValueError("invalid size granularity")
    if d <= 0 or d > 1_000_000:
        raise ValueError("invalid size granularity")
    return d


async def post_tab(request):
    """Open a tab: {"platform": "BNF"|"BB", "symbol": "ALTUSDT", "gran": 5, "sizeGran": "1"}."""
    engine = request.app["engine"]
    try:
        body = await request.json()
        inst = f"{str(body['symbol']).upper().strip()}@{str(body.get('platform', 'BNF')).upper()}"
        coin = await engine.add(inst, int(body.get("gran") or 1),
                                _size_gran(body["sizeGran"]) if body.get("sizeGran") else None)
    except (KeyError, TypeError, ValueError) as e:
        return _bad(str(e))
    engine.changed()
    return web.json_response({"inst": coin.symbol})


async def patch_tab(request):
    """Change Tick size / size granularity of an open tab."""
    engine = request.app["engine"]
    inst = request.match_info["inst"]
    coin = engine.coins.get(inst)
    if not coin:
        return _bad(f"{inst} is not open")
    try:
        body = await request.json()
        if "gran" in body:
            coin.gran = max(1, min(100_000, int(body["gran"])))
        if "sizeGran" in body:
            coin.size_gran = _size_gran(body["sizeGran"])
    except (TypeError, ValueError) as e:
        return _bad(str(e))
    engine.changed()
    return web.json_response({"inst": inst})


async def delete_tab(request):
    engine = request.app["engine"]
    inst = request.match_info["inst"]
    if inst not in engine.coins:
        return _bad(f"{inst} is not open")
    await engine.remove(inst)
    engine.changed()
    return web.json_response({"inst": inst})


def malloc_trim():
    """Give freed heap memory back to the OS (glibc keeps it otherwise)."""
    import ctypes
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except OSError:
        pass


def rss_mb():
    with open("/proc/self/statm") as f:
        return int(f.read().split()[1]) * 4096 / 2**20


async def memory_guard(stop_requested, max_rss_mb):
    """Safety net (the VM has no swap and crashed once, 2026-10-23): above max_rss_mb the
    engine saves its histories and stops cleanly instead of taking the whole VM down."""
    n = 0
    while True:
        await asyncio.sleep(5)
        n += 1
        if n % 12 == 0:      # every minute: hand freed heap memory back to the OS
            malloc_trim()
        rss = rss_mb()
        if n % 120 == 0:     # every 10 minutes: memory in the log, to watch long-term growth
            log.info("memory: rss %.0f MB", rss)
        if rss > max_rss_mb:
            log.error("MEMORY GUARD: rss %.0f MB > limit %d MB, saving and stopping", rss, max_rss_mb)
            stop_requested.set()
            return


async def main(args):
    if os.environ.get("DM_TRACEMALLOC"):
        import tracemalloc
        tracemalloc.start(1)
    logging.Formatter.converter = time.gmtime
    fmt = "%(asctime)sZ %(levelname)s %(name)s %(message)s"
    if args.log_file:
        # Own rotating log file (the system journal is not readable for this user).
        from logging.handlers import RotatingFileHandler
        pathlib.Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(args.log_file, maxBytes=5 * 2**20, backupCount=3)
        logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%Y-%m-%dT%H:%M:%S", handlers=[handler])
    else:
        logging.basicConfig(level=logging.INFO, format=fmt, datefmt="%Y-%m-%dT%H:%M:%S")
    log.info("starting version=%s revision=%s retention_min=%s max_rss_mb=%s",
             __version__, os.environ.get("DEEP_MARKET_REVISION", "source"),
             args.retention_min, args.max_rss_mb)
    session = aiohttp.ClientSession()
    engine = Engine(session, retention_ms=int(args.retention_min * 60_000))
    # Tabs from the last run; the command line only seeds the very first start.
    tabs = load_tabs()
    if tabs is None:
        tabs = [{"inst": s} for s in args.symbols]
    for t in tabs:
        try:
            await engine.add(t["inst"], t.get("gran", 1), t.get("sizeGran"))
        except Exception as e:
            log.warning("could not open %s: %s", t.get("inst"), e)
    engine.save_tabs()

    app = web.Application()
    app["engine"] = engine
    app["settings"] = load_settings()
    app["symbol_cache"] = {}
    app["sockets"] = set()
    app.router.add_get("/", index)
    app.router.add_get("/api/settings", get_settings)
    app.router.add_put("/api/settings", put_settings)
    app.router.add_get("/api/symbols", get_symbols)
    app.router.add_get("/api/debug/memory", get_debug_memory)
    app.router.add_get("/api/tabs", get_tabs)
    app.router.add_post("/api/tabs", post_tab)
    app.router.add_patch("/api/tabs/{inst}", patch_tab)
    app.router.add_delete("/api/tabs/{inst}", delete_tab)
    app.router.add_get("/ws", ws_handler)
    # The local upload module is absent from public distributions.
    try:
        from ._private_upload import register_upload_routes
    except ModuleNotFoundError as exc:
        if exc.name != f"{__package__}._private_upload":
            raise
    else:
        register_upload_routes(app, DATA_DIR)
    app.router.add_static("/static/", WEB_DIR)
    runner = web.AppRunner(app, access_log=None)  # no log line per request
    await runner.setup()
    await web.TCPSite(runner, args.host, args.port).start()
    log.info("serving on http://%s:%d", args.host, args.port)
    autosave = asyncio.create_task(engine.autosave())
    guard = asyncio.create_task(memory_guard(stop_requested := asyncio.Event(), args.max_rss_mb))
    # Stop cleanly on SIGTERM/SIGINT: save every open tab's history first.
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    # The memory guard can also ask for a clean stop.
    await asyncio.wait([asyncio.create_task(stop.wait()), asyncio.create_task(stop_requested.wait())],
                       return_when=asyncio.FIRST_COMPLETED)
    guard.cancel()
    log.info("shutting down: saving histories")
    autosave.cancel()
    await engine.save_all()
    # Close browser connections ourselves (they reconnect to the new server); otherwise the
    # shutdown waits until every browser goes away.
    for ws in list(app["sockets"]):
        await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"server restart")
    for coin in engine.coins.values():
        for t in coin.tasks:
            t.cancel()
    await runner.cleanup()
    await session.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("symbols", nargs="*", default=["ALTUSDT@BNF"],
                    help="instruments for the very first start (afterwards data/tabs.json is used)")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8740)
    ap.add_argument("--retention-min", type=float, default=240)
    ap.add_argument("--max-rss-mb", type=int, default=1500, help="memory guard: stop cleanly above this")
    ap.add_argument("--log-file", help="write logs to this rotating file (3 x 5 MB)")
    asyncio.run(main(ap.parse_args()))
