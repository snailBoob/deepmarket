// deepmarket front-end: live heatmap, bid/ask, trades, zoom and scroll-back for one coin.
import { Heatmap, smoothingKernel } from "./heatmap.js";

// Show start-up failures (e.g. a shader that does not compile) on the page instead of
// silently leaving it blank.
function showFatal(msg) {
  const el = document.getElementById("fatal");
  el.textContent = `Error during start-up: ${msg}`;
  el.style.display = "block";
}
window.addEventListener("error", (e) => showFatal(e.message));
window.addEventListener("unhandledrejection", (e) => showFatal(String(e.reason)));

const PRICE_AXIS_W = 96;
const COB_W = 125;         // current order book column (= #colheads widths in style.css)
const SVP_W = 125;         // session volume profile column
const SIDE_W = PRICE_AXIS_W + COB_W + SVP_W;
const TIME_AXIS_H = 24;
// Thin monospace font (IBM Plex Mono Light, shipped in web/fonts).
const FONT = "300 14px 'Plex Mono', Menlo, 'DejaVu Sans Mono', monospace";
const FONT_BOLD = "400 14px 'Plex Mono', Menlo, 'DejaVu Sans Mono', monospace";
const FONT_NUM = "300 13px 'Plex Mono', Menlo, 'DejaVu Sans Mono', monospace";   // COB/SVP numbers
const BASE_DT = 100;       // finest column duration (Binance sends the book every 100 ms)
const MIN_COL_PX = 2;      // columns are at least this wide (CSS px)
const MIN_ROW_PX = 2;      // below ~2px per level, levels are summed (only below visibility);
                           // this also keeps the 2000-level grid covering the visible range
const LABEL_STEP_PX = 22;  // price-axis labels; profile numbers have their own spacing
const PROFILE_LABEL_PX = 16;
const PROFILE_WIDTH_FRAC = 0.9; // leave 10% of the bar area free before the right divider
const MAX_AHEAD = 6;
const MARGIN_FRAC = 0.5;   // extra grid rows above and below the screen, as a fraction of it
const MAX_ROWS = 2000;     // = server limit       // columns the live view may run ahead of the newest data
const LINES_FROM_PX = 6;   // lines between price levels when levels are at least this tall
// Time zones: fixed UTC offsets only (no summer/winter time), as the user asked.
// IANA "Etc/GMT" zones have the sign reversed: UTC+1 is "Etc/GMT-1".
const TZ_DEFAULT = "Etc/GMT-1";
const TZ_CHOICES = [];
for (let h = -12; h <= 14; h++) {
  const id = h === 0 ? "UTC" : `Etc/GMT${h > 0 ? "-" : "+"}${Math.abs(h)}`;
  TZ_CHOICES.push([id, h === 0 ? "UTC" : `UTC${h > 0 ? "+" : "\u2212"}${Math.abs(h)}`]);
}
// Display settings: global for all coins, stored on the server (/api/settings).
const DISPLAY_DEFAULTS = {
  volumeDotSize: 40, clustering: 52, transparency: 41,
  lowerCutoff: 15, upperCutoff: 96, heatmapIntensity: 60,
  brightness: 100, contrast: 0,
};
const DEFAULT_SETTINGS = {
  ...DISPLAY_DEFAULTS, minVolume: 0, minTradeSize: 0,
  adjustOnRecenter: true, adjustByTime: true, adjustInterval: 10,
  units: "base", tz: TZ_DEFAULT, nowFrac: 0.7,
};
// Convert legacy factor-based settings to the current numeric control scales.
function normalizeSettings(data) {
  const settings = { ...data };
  for (const [old, key, offset, scale] of [
    ["dotSize", "volumeDotSize", 0.3, 0.027], ["bubbleDensity", "clustering", 0.5, 0.025],
    ["cutoff", "lowerCutoff", -1, 0.025], ["heatmapDimming", "heatmapIntensity", 100, -1],
    ["dimming", "heatmapIntensity", 1, -0.01],
  ]) {
    if (!(key in settings) && Number.isFinite(data[old])) settings[key] = (data[old] - offset) / scale;
  }
  return { ...DEFAULT_SETTINGS, ...settings };
}
const dotScale = () => 0.3 + 0.027 * S.set.volumeDotSize;
const clusteringScale = () => 0.5 + 0.025 * S.set.clustering;
// Time zoom levels in ms per CSS px. 50 = 100 ms columns of 2 px (the default).
// Time zoom is continuous (ms per CSS px), like the price zoom. 50 = 100 ms columns of 2 px.
const MS_PER_PX_MIN = 6.25;
const MS_PER_PX_MAX = 15000;   // ~4 h across a wide screen
const MS_PER_PX_DEFAULT = 50;
const NICE = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000];

const $ = (id) => document.getElementById(id);
const chart = $("chart");
const heatCanvas = $("heat");
const ov = $("overlay");
const ctx = ov.getContext("2d");
let heat;
try {
  heat = new Heatmap(heatCanvas);
} catch (e) {
  showFatal(e.message);
  throw e;
}

const S = {
  set: { ...DEFAULT_SETTINGS },  // display settings (server-side, global)
  meta: null,
  symbol: null,
  ws: null,
  clockOffset: 0,        // data clock (exchange time) - local time (ms)
  // What the user asked for:
  msPerPx: MS_PER_PX_DEFAULT,
  pxPerTick: 4,          // CSS px per native price tick (continuous)
  gran: 1,               // price level size in native ticks, per coin
  live: true,
  tRight: 0,             // history mode: time at the right edge of the heat area
  autoCenter: true,      // live mode: keep the price in the middle
  centerTick: null,      // manual price centre (when not auto-centring)
  // What is on screen (from the last grid):
  view: null,            // {id, dt, ncols, nrows, tick_lo, rowTicks, colPx, rowPx, live}
  pendingView: false,
  requestTimer: 0,
  reqSeq: 0,
  reqGeom: new Map(),    // request id -> {colPx, rowPx}
  data: null,            // Float32Array ring, ncols * nrows
  bid: null,             // Int32 ticks per ring slot
  ask: null,
  buyVol: null,          // Float32 per ring slot: aggressive buy volume in the column
  buyPx: null,           // volume-weighted tick (NaN = none)
  sellVol: null,
  sellPx: null,
  tr: null,              // per ring slot: flat [tick, buy, sell, ...] trades per price level
  lastTrade: null,       // {t, tick, qty, side, streak}
  cob: null,             // current book, independent of heatmap time zoom: {data, bids, asks, bid, ask}
  svp: null,             // selected-price levels: {buy, sell, ticks, rowTicks}
  volRef: 0,             // bubble size reference in the selected display units
  bubbleReference: null, // initial mean per instrument/filter, retained across live updates and zoom
  head: 0,               // ring slot of the oldest column
  valid: 0,
  tNewest: 0,
  historyStart: null,
  scale: 0,
  layout: null,
  mouse: null,
  drag: null,            // {kind: "pan" | "price", x0, y0, dx, dy}
};

// ---------- formatting ------------------------------------------------------------------

function makeTimeFmt(tz) {
  const opts = { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" };
  try {
    return new Intl.DateTimeFormat("en-GB", { ...opts, timeZone: tz });
  } catch {
    return new Intl.DateTimeFormat("en-GB", { ...opts, timeZone: TZ_DEFAULT });
  }
}
let fmtTime = makeTimeFmt(TZ_DEFAULT);
function timeLabel(t, withMs) {
  const s = fmtTime.format(new Date(t));
  return withMs ? `${s}.${String(((t % 1000) + 1000) % 1000).padStart(3, "0")}` : s;
}
function priceStr(tick) {
  const m = S.meta[S.symbol];
  let s = (tick * m.tick_size).toFixed(m.decimals);
  if (s.includes(".")) s = s.replace(/0+$/, "").replace(/\.$/, "");
  return s;
}
function fmtQty(q) {
  if (q >= 100) return String(Math.round(q));
  return String(Number(q.toPrecision(4)));
}

// Sizes are shown in coins rounded to the tab's size granularity,
// or in USDT (size x price) when the units toggle says so.
const usdt = () => S.set.units === "usdt";
function sizeValue(size, tick) {
  return usdt() ? size * tick * S.meta[S.symbol].tick_size : size;
}
function fmtSize(size, tick) {
  const m = S.meta[S.symbol];
  if (usdt()) {
    const v = size * tick * m.tick_size;
    return v >= 100 ? String(Math.round(v)) : v.toFixed(2);
  }
  const g = m.size_gran || 1;
  return (Math.round(size / g) * g).toFixed(m.size_decimals || 0);
}
// A value that is already in display units (e.g. a volume scale computed in USDT).
function fmtValue(v) {
  if (usdt()) return v >= 100 ? String(Math.round(v)) : v.toFixed(2);
  const m = S.meta[S.symbol];
  const g = m.size_gran || 1;
  return (Math.round(v / g) * g).toFixed(m.size_decimals || 0);
}
const dataNow = () => Date.now() + S.clockOffset;

// ---------- zoom geometry ---------------------------------------------------------------

function timeGeometry() {
  const msPerPx = S.msPerPx;
  if (msPerPx * MIN_COL_PX <= BASE_DT) return { dt: BASE_DT, colPx: BASE_DT / msPerPx, msPerPx };
  return { dt: Math.round(msPerPx * MIN_COL_PX), colPx: MIN_COL_PX, msPerPx };
}

// Rows are whole price levels (gran native ticks); when a level gets thinner than
// MIN_ROW_PX, several levels are summed per row.
function priceGeometry() {
  const G = S.gran;
  const mult = NICE.find((k) => k * G * S.pxPerTick >= MIN_ROW_PX) || NICE[NICE.length - 1];
  return { rowTicks: mult * G, rowPx: mult * G * S.pxPerTick };
}

// Price level helpers (ticks are native exchange ticks).
const levelOf = (t) => Math.floor(t / S.gran) * S.gran;          // lowest tick of the level
const levelMid = (t) => levelOf(t) + (S.view?.outward ? 0 : (S.gran - 1) / 2);
const bookLevel = (t, ask) => S.view.outward
  ? (ask ? Math.ceil(t / S.gran) : Math.floor(t / S.gran)) * S.gran : levelMid(t);

// ---------- connection ------------------------------------------------------------------

function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.binaryType = "arraybuffer";
  S.ws = ws;
  setConn("connecting…");
  ws.onclose = () => {
    setConn("connection lost, reconnecting…");
    S.view = null;
    S.pendingView = false;
    setTimeout(connect, 2000);
  };
  ws.onmessage = (ev) => {
    if (typeof ev.data === "string") onText(JSON.parse(ev.data));
    else onBinary(ev.data);
  };
}

function setConn(text) { $("conn").textContent = text; }

function onText(m) {
  if (m.type === "meta") {
    $("app-version").textContent = m.version ? `v${m.version}` : "";
    $("app-version").title = m.revision || "";
    // Also sent again whenever a tab is opened, closed or changed (in any browser).
    // No syncClock here: meta carries wall time, grids/columns carry exchange time.
    onMeta(m);
  } else if (m.type === "error") {
    $("status").textContent = m.error;
    S.pendingView = false;
    setTimeout(requestView, 1000);
  }
}

// Clock: the offset that comes from the fastest messages is the best estimate (network delay
// only ever makes messages late). Jump up to faster ones, drift down very slowly otherwise,
// so the scroll speed stays perfectly even.
function syncClock(serverTime) {
  const off = serverTime - Date.now();
  if (!S.clockSet || off > S.clockOffset) {
    S.clockOffset = off;
    S.clockSet = true;
  } else {
    S.clockOffset -= 0.2;  // ~2 ms/s at 10 messages/s: follows real clock drift
  }
}

function onBinary(buf) {
  const hlen = new DataView(buf).getUint32(0, true);
  const h = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, hlen)));
  const payload = new Float32Array(buf, 4 + hlen);
  if (Number.isFinite(h.server_time)) syncClock(h.server_time);
  if (h.type === "grid") onGrid(h, payload);
  else if (h.type === "col") onCol(h, payload);
  else if (h.type === "svp") {
    if (S.view && h.view === S.view.id) {
      const n = h.price_ticks ? h.price_ticks.length : S.view.nrows, data = new Float32Array(payload);
      S.svp = { buy: data.subarray(0, n), sell: data.subarray(n, 2 * n),
        ticks: h.price_ticks, rowTicks: h.row_ticks || S.view.rowTicks };
    }
  }
  else if (h.type === "cob") {
    if (S.view && h.view === S.view.id) {
      const n = h.price_ticks ? h.price_ticks.length : S.view.nrows, data = new Float32Array(payload);
      S.cob = { data: data.subarray(0, n), bid: h.bid, ask: h.ask,
        ticks: h.price_ticks, rowTicks: h.row_ticks || S.view.rowTicks,
        bids: h.book_sides ? data.subarray(n, 2 * n) : null,
        asks: h.book_sides ? data.subarray(2 * n, 3 * n) : null };
      if (h.last_trade) S.lastTrade = h.last_trade;
    }
  }
}

// Debounced: wheel/drag can fire many times; only the last state is requested.
// Throttled: during a continuous gesture (trackpad zoom, drag) a fresh grid is requested at
// most every VIEW_EVERY_MS, plus one at the end with the final state.
const VIEW_EVERY_MS = 200;
function scheduleView(delay = 80) {
  clearTimeout(S.requestTimer);
  const now = performance.now();
  if (!S.pendingView && now - (S.lastRequest || 0) >= VIEW_EVERY_MS) requestView();
  else S.requestTimer = setTimeout(requestView, delay);
}

function requestView(reason = "view") {
  if (!S.ws || S.ws.readyState !== WebSocket.OPEN || !S.layout || !S.symbol) return;
  const L = S.layout;
  const tg = timeGeometry();
  const pg = priceGeometry();
  if (!S.live && S.tRight >= dataNow()) S.live = true; // scrolled into the present
  const nowX = S.live ? L.heatW * S.set.nowFrac : L.heatW;
  // Retain trades beyond the left edge while their merged bubble is still visible.
  // Include the live clock's permitted run-ahead, not just the current viewport.
  const ncols = Math.ceil((nowX + BUBBLE_HISTORY_PX) / tg.colPx) + MAX_AHEAD + 1;
  // Extra rows above and below the visible part, so zooming out or scrolling vertically
  // shows data right away instead of an empty edge.
  const visRows = Math.ceil(L.heatH / pg.rowPx);
  const margin = Math.ceil(visRows * MARGIN_FRAC);
  const nrows = Math.min(MAX_ROWS, visRows + 2 * margin);
  let tickLo = null;
  if (!(S.live && S.autoCenter) && S.centerTick !== null) {
    tickLo = Math.round(S.centerTick - ((nrows - 1) * pg.rowTicks) / 2);
  }
  S.pendingView = true;
  const req = ++S.reqSeq;
  S.reqGeom.set(req, { colPx: tg.colPx, rowPx: pg.rowPx, msPerPx: tg.msPerPx, reason });
  S.lastRequest = performance.now();
  S.ws.send(JSON.stringify({
    type: "view", req, symbol: S.symbol, dt: tg.dt, ncols, nrows, row_ticks: pg.rowTicks,
    tick_lo: tickLo, t_end: S.live ? null : Math.round(S.tRight),
    book_rounding: "outward", min_trade_size: S.set.minTradeSize, profile_row_ticks: S.gran,
  }));
  updateButtons();
}

function onGrid(h, payload) {
  if (h.symbol !== S.symbol) return;   // answer for a tab we already left
  // Pixel sizes belong to the zoom the grid was requested with.
  const geom = S.reqGeom.get(h.req) || { colPx: MIN_COL_PX, rowPx: h.row_ticks * S.pxPerTick };
  // Keep the existing preview until data for the current gesture arrives. Drawing
  // an obsolete coarse grid between finer previews makes dots disappear/reappear.
  if (h.req < S.reqSeq ||
      (geom.msPerPx && Math.abs(geom.msPerPx - S.msPerPx) > 1e-9) ||
      Math.abs(geom.rowPx / h.row_ticks - S.pxPerTick) > 1e-9) {
    S.reqGeom.delete(h.req);
    return;
  }
  for (const k of S.reqGeom.keys()) if (k <= h.req) S.reqGeom.delete(k);
  S.view = {
    id: h.view, dt: h.dt, ncols: h.ncols, nrows: h.nrows, tick_lo: h.tick_lo,
    rowTicks: h.row_ticks, live: h.live, colPx: geom.colPx, rowPx: geom.rowPx,
    outward: h.book_rounding === "outward", minTradeSize: h.min_trade_size || 0,
    yOff: 0,               // CSS px: keeps the picture in place during a price zoom/pan
    // The grid is taller than the screen (margins above and below): shift it up so its
    // middle is at the middle of the heat area.
    baseYOff: (S.layout.heatH - h.nrows * geom.rowPx) / 2,
    xOff: 0,               // CSS px: same for a time pan, until the new grid arrives
  };
  S.pendingView = h.req !== S.reqSeq;
  S.live = h.live;
  S.historyStart = h.history_start;
  $("status").textContent = "";
  S.data = new Float32Array(payload); // copy: the message buffer is not ours to keep
  if (geom.reason !== "recenter" || S.set.adjustOnRecenter) scaleAt = -Infinity;
  S.bid = Int32Array.from(h.bid);
  S.ask = Int32Array.from(h.ask);
  const f32 = (a) => Float32Array.from(a, (x) => (x === null ? NaN : x));
  S.buyVol = f32(h.buy_vol);
  S.buyPx = f32(h.buy_px);
  S.sellVol = f32(h.sell_vol);
  S.sellPx = f32(h.sell_px);
  if (h.live) S.lastTrade = h.last_trade;
  S.tr = new Array(h.ncols).fill(null);
  const T = h.trades || [];
  for (let i = 0; i < T.length; i += 4) {
    const c = T[i];
    (S.tr[c] || (S.tr[c] = [])).push(T[i + 1], T[i + 2], T[i + 3]);
  }
  S.head = 0;
  S.valid = h.ncols;
  S.tNewest = h.t_end;
  if (!h.live) S.tRight = h.t_end;
  // Keep a manually chosen centre across grid resolutions. Replacing it with
  // each snapped grid's midpoint accumulates half-row drift on zoom round trips.
  if ((S.live && S.autoCenter) || S.centerTick === null) S.centerTick = tickOf(frameGeom(), S.layout.heatH / 2);
  S.svp = null;
  S.cob = null;
  heat.setGrid(h.ncols, h.nrows, S.data);
  updateButtons();
  if (autoZoomPrice(h)) requestView();
}

function onCol(h, col) {
  const v = S.view;
  if (!v || !v.live || h.view !== v.id || h.t <= S.tNewest) return;
  const steps = Math.round((h.t - S.tNewest) / v.dt);
  if (steps > v.ncols) { requestView(); return; }
  for (let s = 0; s < steps; s++) {   // fill missed columns with the latest state
    const slot = S.head;
    S.data.set(col, slot * v.nrows);
    heat.setColumn(slot, col);
    S.bid[slot] = h.bid;
    S.ask[slot] = h.ask;
    const last = s === steps - 1;  // trades go into the newest column only
    S.buyVol[slot] = last ? h.buy_vol : 0;
    S.buyPx[slot] = last && h.buy_px !== null ? h.buy_px : NaN;
    S.sellVol[slot] = last ? h.sell_vol : 0;
    S.sellPx[slot] = last && h.sell_px !== null ? h.sell_px : NaN;
    S.tr[slot] = last && h.trades && h.trades.length ? h.trades : null;
    S.head = (S.head + 1) % v.ncols;
  }
  S.tNewest = h.t;
  if (h.last_trade) S.lastTrade = h.last_trade;
  // The picture stays still; only when the price is far from the middle (more than 30% of the
  // screen height) is a centred grid fetched. (Continuous gliding was tried and made people
  // seasick, 2026-10-24.)
  if (S.autoCenter && !S.pendingView && !S.drag && h.bid >= 0 && h.ask >= 0) {
    const g = frameGeom();
    const off = (yOf(g, (h.bid + h.ask) / 2) - S.layout.heatH / 2) / S.layout.heatH;
    if (Math.abs(off) > 0.3) requestView("recenter");
  }
}



// Percentile cut-offs determine quantity bounds, not individual colour ranks.
// Estimate the bounds from visible, smoothed cells on zoom,
// recenter (when enabled), and at the configured live adjustment interval.
// Log bins keep this linear-time; use their actual mean sizes rather than bin edges.
const HIST_BINS = 512;
const cutOf = () => S.set.lowerCutoff / 100;
const hist = new Uint32Array(HIST_BINS);
const histSizes = new Float64Array(HIST_BINS);
let scaleAt = -Infinity;
let scaleValues = new Float32Array(0);
let scaleColumn = new Float64Array(0);

function heatQuantile(count, fraction) {
  const target = Math.max(1, Math.ceil(count * fraction));
  let acc = 0;
  for (let b = 0; b < HIST_BINS; b++) {
    acc += hist[b];
    if (acc >= target) return Math.fround(histSizes[b] / hist[b]);
  }
}

function buildHeatScale(L, v, g, smoothing) {
  S.heatScale = { lo: 0, hi: 1 };
  if (!S.data) return;
  const [r0, r1] = visibleRows(L, v, g);
  const ncols = v.ncols, nrows = v.nrows;
  const j0 = Math.max(0, ncols - S.valid);
  const usdtOn = usdt();
  const { weights } = smoothing;
  const radius = weights.length - 1;
  if (scaleValues.length < S.data.length) scaleValues = new Float32Array(S.data.length);
  if (scaleColumn.length < nrows) scaleColumn = new Float64Array(nrows);
  const p0 = rowMid(v, 0) * S.meta[S.symbol].tick_size;
  const pstep = v.rowTicks * S.meta[S.symbol].tick_size;
  let max = 0, count = 0;
  for (let j = j0; j < ncols; j++) {
    const right = g.xr - (ncols - 1 - j) * v.colPx;
    const future = v.live && j === ncols - 1 && g.xr < L.heatW;
    if (!future && (right <= 0 || right - v.colPx >= L.heatW)) continue;
    const base = ((S.head + j) % ncols) * nrows;
    for (let r = Math.max(0, r0 - radius); r <= Math.min(nrows - 1, r1 + radius); r++) {
      scaleColumn[r] = S.data[base + r] * (usdtOn ? p0 + r * pstep : 1);
    }
    for (let r = r0; r <= r1; r++) {
      const top = rowTop(v, g, r);
      if (top >= L.heatH || top + v.rowPx <= 0) continue;
      let sum = weights[0] * scaleColumn[r], total = weights[0];
      for (let d = 1; d <= radius; d++) {
        const w = weights[d];
        if (r - d >= 0) { sum += w * scaleColumn[r - d]; total += w; }
        if (r + d < nrows) { sum += w * scaleColumn[r + d]; total += w; }
      }
      const x = Math.fround(sum / total);
      if (x > 0) { scaleValues[count++] = x; max = Math.max(max, x); }
    }
  }
  if (!count) return;
  const lmax = Math.log(max);
  const k = (HIST_BINS - 1) / Math.log(1e6);
  hist.fill(0); histSizes.fill(0);
  for (let i = 0; i < count; i++) {
    const x = scaleValues[i];
    const b = Math.max(0, Math.min(HIST_BINS - 1, Math.floor((Math.log(x) - lmax) * k + HIST_BINS - 1)));
    hist[b]++; histSizes[b] += x;
  }
  let lo = cutOf() > 0 ? heatQuantile(count, cutOf()) : 0;
  let hi = heatQuantile(count, S.set.upperCutoff / 100);
  // Repeated sizes can put both percentiles at one quantity. Use the actual
  // maximum in that case; an entirely uniform book gets a zero-based scale.
  if (hi <= lo) hi = max;
  if (hi <= lo) lo = 0;
  S.heatScale = { lo, hi };
}

// ---------- layout ----------------------------------------------------------------------

function resize() {
  const dpr = window.devicePixelRatio || 1;
  const W = chart.clientWidth;
  const H = chart.clientHeight;
  for (const c of [heatCanvas, ov]) {
    c.width = Math.round(W * dpr);
    c.height = Math.round(H * dpr);
  }
  const heatW = W - SIDE_W;
  const heatH = H - TIME_AXIS_H;
  const old = S.layout;
  S.layout = { dpr, W, H, heatW, heatH };
  $("btn-back").style.top = `${heatH / 2 - 12}px`;
  $("btn-live").style.top = `${heatH / 2 - 12}px`;
  $("btn-live").style.right = `${SIDE_W + 8}px`;
  if (!old || old.heatW !== heatW || old.heatH !== heatH) scheduleView(50);
}
new ResizeObserver(resize).observe(chart);

// ---------- coordinates -----------------------------------------------------------------

// Geometry of what is drawn this frame, including drag offsets.
function frameGeom() {
  const L = S.layout;
  const v = S.view;
  const nowX = v.live ? L.heatW * S.set.nowFrac : L.heatW;
  // Run ahead of the newest column for up to MAX_AHEAD columns when one is late: the future
  // area already shows the current book, so there is no gap, and no stop-and-jump.
  const shift = v.live ? Math.min(MAX_AHEAD, Math.max(0, (dataNow() - S.tNewest) / v.dt)) : 0;
  const d = S.drag && S.drag.kind === "pan" ? S.drag : null;
  const dx = (d ? d.dx : 0) + (v.xOff || 0);
  const dy = (d ? d.dy : 0) + (v.yOff || 0) + (v.baseYOff || 0);
  const xr = nowX - shift * v.colPx + dx;   // right edge of the newest column
  const pxPerMs = v.colPx / v.dt;
  const ppt = v.rowPx / v.rowTicks;          // CSS px per tick of the grid on screen
  return { nowX, shift, xr, pxPerMs, ppt, dx, dy };
}

function xOf(g, t) { return g.xr + (t - S.tNewest) * g.pxPerMs; }
function tOf(g, x) { return S.tNewest + (x - g.xr) / g.pxPerMs; }
function rowHalf(v) { return v.outward ? v.rowTicks / 2 : 0.5; }
function rowOf(v, tick) { return Math.floor((tick + rowHalf(v) - v.tick_lo) / v.rowTicks); }
function yOf(g, tick) {    // outward rows are centred on their displayed price anchor
  const v = S.view;
  return (v.nrows * v.rowTicks - (tick - v.tick_lo) - rowHalf(v)) * g.ppt + g.dy;
}
function tickOf(g, y) {
  const v = S.view;
  return v.tick_lo + v.nrows * v.rowTicks - (y - g.dy) / g.ppt - rowHalf(v);
}

// ---------- drawing ---------------------------------------------------------------------

function frame() {
  requestAnimationFrame(frame);
  const L = S.layout;
  const v = S.view;
  if (!L) return;
  const dpr = L.dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, L.W, L.H);
  if (!v) {
    heat.draw({ x: 0, y: 0, w: 0, h: 0, valid: 0 });
    return;
  }
  const g = frameGeom();
  const tg = visibleTimes(L, g);
  // Auto uses sigma 5 CSS pixels. Zoom changes the distance between discrete
  // levels, not sigma; the shared weights are independent of devicePixelRatio.
  const smoothing = smoothingKernel(v.rowPx, v.nrows, 5);
  // Measure the same smoothed quantities that the shader will colour.
  const nowMs = performance.now();
  if (!S.heatScale || scaleAt === -Infinity ||
      (S.set.adjustByTime && nowMs - scaleAt >= S.set.adjustInterval * 1000)) {
    scaleAt = nowMs;
    buildHeatScale(L, v, g, smoothing);
  }
  const gridT = tg.times.length ? tg.times[0] : 0;
  // The heatmap shader has no drag offset; emulate it by moving the viewport.
  heat.draw({
    dpr, gridStep: tg.times.length ? tg.step * g.pxPerMs * dpr : 0,
    gridX0: (Math.round(xOf(g, gridT)) - g.dx) * dpr,
    x: Math.round(g.dx * dpr), y: Math.round(g.dy * dpr),
    w: Math.round(L.heatW * dpr), h: Math.round(v.nrows * v.rowPx * dpr),
    clip: { x: 0, y: 0, w: Math.round(L.heatW * dpr), h: Math.round(L.heatH * dpr) },
    nowX: g.nowX * dpr, colPx: v.colPx * dpr, rowPx: v.rowPx * dpr, shift: g.shift,
    head: S.head, valid: S.valid, future: v.live, fade: 1 - S.set.heatmapIntensity / 100,
    brightness: S.set.brightness / 100, contrast: S.set.contrast,
    smoothing, scaleLo: S.heatScale.lo, scaleHi: S.heatScale.hi,
    lines: v.rowPx >= 12,
    usdt: usdt(), p0: rowMid(v, 0) * S.meta[S.symbol].tick_size, pstep: v.rowTicks * S.meta[S.symbol].tick_size,
  });

  ctx.save();
  ctx.beginPath();
  ctx.rect(0, 0, L.heatW, L.heatH);
  ctx.clip();
  drawHistoryStart(L, g);
  drawBidAsk(L, v, g);
  const buckets = tradeBuckets(v, g);
  drawVolume(L, buckets);
  drawBubbles(v, g);     // translucent bubbles on top of the price lines
  if (v.live) {
    drawLastTradeLabel(L, g);
    ctx.strokeStyle = "rgba(235,240,245,0.75)";  // now-line
    ctx.lineWidth = 1;
    ctx.beginPath();
    // The line stays put while panning; only the chart moves under it.
    ctx.moveTo(g.nowX + 0.5, 0);
    ctx.lineTo(g.nowX + 0.5, L.heatH);
    ctx.stroke();
  }
  ctx.restore();

  drawPriceAxis(L, v, g);
  drawSideColumns(L, v, g);
  drawTimeAxis(L, g);
  drawHover(L, v, g);
}

function drawHistoryStart(L, g) {
  // Shade the part before the engine started recording.
  if (!S.historyStart) return;
  const x = xOf(g, S.historyStart);
  if (x <= 0) return;
  ctx.fillStyle = "rgba(0,0,0,0.35)";
  ctx.fillRect(0, 0, Math.min(x, L.heatW), L.heatH);
  if (x > 160) {
    ctx.fillStyle = "#6f7d8a";
    ctx.font = FONT;
    ctx.textAlign = "right";
    ctx.textBaseline = "top";
    ctx.fillText("no data before the start", x - 8, 8);
  }
}

const TIME_STEPS = [100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000,
  600000, 900000, 1800000, 3600000];

function visibleTimes(L, g) {
  const step = TIME_STEPS.find((s) => s * g.pxPerMs >= 90) || TIME_STEPS[TIME_STEPS.length - 1];
  const tLeft = tOf(g, 0);
  const tRight = tOf(g, L.heatW);
  const out = [];
  for (let t = Math.ceil(tLeft / step) * step; t <= tRight; t += step) out.push(t);
  return { step, times: out };
}

function drawTimeAxis(L, g) {
  const { step, times } = visibleTimes(L, g);
  ctx.fillStyle = "#1b232c";
  ctx.fillRect(0, L.heatH, L.W, TIME_AXIS_H);
  ctx.fillStyle = "#aab6c2";
  ctx.font = FONT;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  for (const t of times) {
    const x = xOf(g, t);
    if (x < 30 || x > L.heatW - 30) continue;
    ctx.fillText(timeLabel(t, step < 1000), x, L.heatH + TIME_AXIS_H / 2);
  }
}

function drawBidAsk(L, v, g) {
  const n = v.ncols;
  const newest = (S.head + n - 1) % n;
  const lw = v.colPx >= 4 ? 2.5 : 2;
  for (const [arr, color, ask] of [[S.ask, RED, true], [S.bid, GREEN, false]]) {
    ctx.strokeStyle = color;
    ctx.lineWidth = lw;
    ctx.beginPath();
    let pen = false;
    for (let j = n - S.valid; j < n; j++) {
      const tick = arr[(S.head + j) % n];
      const x1 = g.xr - (n - 1 - j) * v.colPx;
      if (tick < 0 || x1 < 0) { pen = false; continue; }
      const y = yOf(g, bookLevel(tick, ask));
      if (!pen) { ctx.moveTo(x1 - v.colPx, y); pen = true; } else ctx.lineTo(x1 - v.colPx, y);
      ctx.lineTo(x1, y);
    }
    ctx.stroke();
    const last = arr[newest];
    if (v.live && last >= 0) {  // dashed extension into the future area
      ctx.setLineDash([6, 5]);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(g.xr, yOf(g, bookLevel(last, ask)));
      ctx.lineTo(L.heatW, yOf(g, bookLevel(last, ask)));
      ctx.stroke();
      ctx.setLineDash([]);
    }
  }
}

const SIDE_BG = "#2e3b46";   // lighter slate background for the price axis, COB and SVP
function drawPriceAxis(L, v, g) {
  const x0 = L.heatW;
  ctx.fillStyle = SIDE_BG;
  ctx.fillRect(x0, 0, PRICE_AXIS_W, L.heatH);
  ctx.strokeStyle = "#2f3b47";
  ctx.lineWidth = 1;
  ctx.beginPath();
  ctx.moveTo(x0 + 0.5, 0);
  ctx.lineTo(x0 + 0.5, L.heatH);
  ctx.stroke();
  const every = priceStep(g);
  ctx.font = FONT;
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";
  ctx.fillStyle = "#aab6c2";
  const top = Math.ceil(tickOf(g, 0));
  const bottom = Math.floor(tickOf(g, L.heatH));
  for (let t = Math.ceil(bottom / every) * every; t <= top; t += every) {
    const y = yOf(g, levelMid(t));
    if (y < 8 || y > L.heatH - 8) continue;
    ctx.fillText(priceStr(t), x0 + 8, y);
  }
  // Current level boxed: the last trade price, box coloured by the aggressor side.
  const lt = S.lastTrade;
  if (v.live && lt) {
    const y = yOf(g, levelMid(lt.tick));
    if (y > 0 && y < L.heatH) {
      ctx.fillStyle = SIDE_BG;
      ctx.fillRect(x0 + 2, y - 10, PRICE_AXIS_W - 4, 20);
      ctx.strokeStyle = lt.side > 0 ? GREEN : RED;
      ctx.strokeRect(x0 + 2.5, y - 9.5, PRICE_AXIS_W - 5, 19);
      ctx.fillStyle = "#e6edf3";
      ctx.fillText(priceStr(levelOf(lt.tick)), x0 + 8, y);
    }
  }
}

// ---------- COB and SVP -----------------------------------------------------------------

// Current book snapshots arrive independently of heatmap columns in both view modes.
// The newest heatmap column remains a fallback for an older server during rollout/rollback.
function cobSource(v) {
  if (S.cob) return S.cob;
  if (v.live) {
    const newest = (S.head + v.ncols - 1) % v.ncols;
    return { data: S.data.subarray(newest * v.nrows, (newest + 1) * v.nrows), bid: S.bid[newest], ask: S.ask[newest] };
  }
  return null;
}

function rowTop(v, g, r) { return (v.nrows - 1 - r) * v.rowPx + g.dy; }

function visibleRows(L, v, g) {
  const r0 = Math.max(0, Math.floor(v.nrows - 1 - (L.heatH - g.dy) / v.rowPx));
  const r1 = Math.min(v.nrows - 1, Math.ceil(v.nrows - 1 - (0 - g.dy) / v.rowPx) + 1);
  return [r0, r1];
}


// Middle tick of row r (used to turn sizes into USDT for that row).
function rowMid(v, r) { return v.tick_lo + r * v.rowTicks + (v.outward ? 0 : (v.rowTicks - 1) / 2); }

function rowSide(v, r, bid, ask) {
  const lo = v.tick_lo + r * v.rowTicks;
  const hi = lo + (v.outward ? 0 : v.rowTicks - 1);
  if (ask >= 0 && lo >= ask) return 1;   // ask
  if (bid >= 0 && hi <= bid) return -1;  // bid
  return 0;                              // the row holds the spread
}

const COB_ASK = "rgb(226,74,64)";
const COB_BID = "rgb(46,204,113)";

// Old total-only snapshots cannot identify both sides inside a shared row.
function cobAsk(v, cob, r) {
  return cob.asks ? cob.asks[r] : rowSide(v, r, cob.bid, cob.ask) > 0 ? cob.data[r] : 0;
}

// Price-axis label step in ticks.
function priceStep(g) {
  return NICE.map((k) => k * S.gran).find((k) => k * g.ppt >= LABEL_STEP_PX) || NICE[NICE.length - 1] * S.gran;
}



// Label actual, unsmoothed row quantities at their own price. Do not silently sum
// extra levels just to make text fit. Prioritize large levels, then omit collisions.
function drawProfileNumbers(rows, x) {
  const candidates = [];
  for (const row of rows) {
    const { y } = row;
    const val = sizeValue(row.first + row.second, row.tick);
    if (val > 0 && y >= PROFILE_LABEL_PX / 2 && y <= S.layout.heatH - PROFILE_LABEL_PX / 2) {
      candidates.push({ val, y });
    }
  }
  candidates.sort((a, b) => b.val - a.val);
  const placed = [];
  ctx.font = FONT_NUM;
  ctx.fillStyle = "#d9dee3";   // plain light numbers over the bars, no outline
  for (const { val, y } of candidates) {
    if (placed.some((other) => Math.abs(y - other) < PROFILE_LABEL_PX)) continue;
    ctx.fillText(fmtValue(val), x, y);
    placed.push(y);
  }
}

// Keep selected price levels separate from the heatmap's coarser zoom grid.
function profileRows(v, g, source, firstOf, secondOf) {
  const rt = source.rowTicks || v.rowTicks;
  const n = source.ticks ? source.ticks.length : v.nrows;
  return Array.from({ length: n }, (_, r) => {
    const tick = source.ticks ? source.ticks[r] + (v.outward ? 0 : (rt - 1) / 2) : rowMid(v, r);
    return { tick, y: yOf(g, tick), first: firstOf(r), second: secondOf(r) };
  });
}

// Discrete selected levels shrink with zoom instead of being repeatedly summed
// into 2–5px heatmap rows. Once subpixel, sample their Gaussian density at device
// pixels. Wide levels stay rectangular; dense levels form a smooth wave.
// The isolated QNT reference profiles support sigma ~5 CSS px. Use identical
// positive weights for total and red segment, converting each source to USDT first.
function barGroups(v, g, rows, rowTicks) {
  const rowPx = rowTicks * g.ppt, pixel = 1 / S.layout.dpr;
  const step = Math.max(rowPx, pixel), sigma = 5, reach = 4 * sigma;
  let top = 0;
  if (rowPx >= pixel) {
    const offset = v.outward ? 0 : (rowTicks - 1) / 2;
    const tick = Math.ceil((tickOf(g, 0) - offset) / rowTicks) * rowTicks + offset;
    top = yOf(g, tick) - rowPx / 2;
  }
  const list = Array.from({ length: Math.max(0, Math.ceil((S.layout.heatH - top) / step)) }, (_, i) =>
    ({ top: top + i * step, bottom: top + (i + 1) * step, val: 0, second: 0 }));
  for (const row of rows) {
    const total = sizeValue(row.first + row.second, row.tick);
    if (total <= 0) continue;
    const second = sizeValue(row.second, row.tick);
    const first = Math.max(0, Math.ceil((row.y - reach - top) / step - 0.5));
    const last = Math.min(list.length - 1, Math.floor((row.y + reach - top) / step - 0.5));
    for (let i = first; i <= last; i++) {
      const weight = Math.exp(-0.5 * ((top + (i + 0.5) * step - row.y) / sigma) ** 2);
      list[i].val += weight * total;
      list[i].second += weight * second;
    }
  }
  return { list };
}

function drawBars(groups, x0, widthOf, color) {
  ctx.fillStyle = color;
  const dpr = S.layout.dpr;
  // Whole device-pixel boundaries prevent dark seams between adjacent thin bars.
  for (const gr of groups.list) {
    const w = widthOf(gr);
    const top = Math.round(gr.top * dpr) / dpr, bottom = Math.round(gr.bottom * dpr) / dpr;
    if (w > 0 && bottom > top) ctx.fillRect(x0, top, w, bottom - top);
  }
}

// Each profile has its own linear scale. Heatmap contrast, past liquidity and
// fully off-screen rows must not change the proportions of the current bars.
function barMax(height, ...groups) {
  let max = 0;
  for (const { list } of groups) {
    for (const gr of list) {
      if (gr.bottom > 0 && gr.top < height) max = Math.max(max, gr.val);
    }
  }
  return max;
}

function drawSideColumns(L, v, g) {
  const xc = L.heatW + PRICE_AXIS_W;
  const xs = xc + COB_W;
  ctx.fillStyle = SIDE_BG;
  ctx.fillRect(xc, 0, COB_W + SVP_W, L.heatH);
  ctx.save();
  ctx.beginPath();
  ctx.rect(xc, 0, COB_W + SVP_W, L.heatH);
  ctx.clip();
  // Highlight the row of the current price across both columns.
  const lt = S.lastTrade;
  if (lt) {
    const y = yOf(g, levelMid(lt.tick));
    const h = Math.max(v.rowPx, 12);
    ctx.fillStyle = "rgba(230,237,243,0.10)";
    ctx.fillRect(xc, y - h / 2, COB_W + SVP_W, h);
  }
  ctx.textAlign = "left";
  ctx.textBaseline = "middle";

  // COB: one shared volume scale for both sides of the current book.
  const cob = cobSource(v);
  if (cob) {
    const rows = profileRows(v, g, cob, (r) => cob.data[r] - cobAsk(v, cob, r), (r) => cobAsk(v, cob, r));
    const tot = barGroups(v, g, rows, cob.rowTicks || v.rowTicks);
    const max = barMax(L.heatH, tot);
    const width = (gr) => max > 0 ? gr.val / max * (COB_W - 2) * PROFILE_WIDTH_FRAC : 0;
    // Shared rows retain both colours: red on the left, green after it, as in SVP.
    drawBars(tot, xc + 1, width, COB_BID);
    drawBars(tot, xc + 1, (gr) => width({ val: gr.second }), COB_ASK);
    drawProfileNumbers(rows, xc + 3);
  }

  // SVP: its own scale from total session volume. Applying the SAME linear scale
  // to the red segment preserves the actual sell/buy split (also for USDT).
  const svp = S.svp;
  if (svp) {
    const rows = profileRows(v, g, svp, (r) => svp.buy[r], (r) => svp.sell[r]);
    const tot = barGroups(v, g, rows, svp.rowTicks);
    const max = barMax(L.heatH, tot);
    const width = (gr) => max > 0 ? gr.val / max * (SVP_W - 2) * PROFILE_WIDTH_FRAC : 0;
    drawBars(tot, xs + 1, width, COB_BID);
    drawBars(tot, xs + 1, (gr) => width({ val: gr.second }), COB_ASK);
    drawProfileNumbers(rows, xs + 3);
  }
  ctx.restore();

  // Separators and headers.
  ctx.strokeStyle = "#1f2932";
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (const x of [xc, xs]) { ctx.moveTo(x + 0.5, 0); ctx.lineTo(x + 0.5, L.heatH); }
  ctx.stroke();
  // Column headers live in the HTML bar above the chart (#colheads).
}

function sideHoverText(L, v, g, m) {
  const tickF = tickOf(g, m.y);
  const isCob = m.x < L.heatW + PRICE_AXIS_W + COB_W;
  const source = isCob ? cobSource(v) : S.svp;
  const rt = source?.rowTicks || v.rowTicks;
  const lo = source?.ticks ? Math.floor((tickF + (v.outward ? rt / 2 : 0.5)) / rt) * rt
    : v.tick_lo + rowOf(v, tickF) * rt;
  const r = source?.ticks ? source.ticks.indexOf(lo) : rowOf(v, tickF);
  const mt = lo + (v.outward ? 0 : (rt - 1) / 2);
  const priceTxt = !v.outward && rt > S.gran ? `${priceStr(lo)}–${priceStr(lo + rt - S.gran)}` : priceStr(lo);
  if (isCob) {
    const cob = source;
    if (!cob) return `COB  ${priceTxt}`;
    if (cob.bids && cob.asks) {
      const b = cob.bids[r] || 0, a = cob.asks[r] || 0;
      if (b > 0 && a > 0) return `COB  BID ${fmtSize(b, mt)}  ASK ${fmtSize(a, mt)} at ${priceTxt}`;
      return `COB  ${a > 0 ? "ASK" : b > 0 ? "BID" : ""} ${fmtSize(b + a, mt)} at ${priceTxt}`;
    }
    const side = rowSide(v, r, cob.bid, cob.ask);
    const size = cob.data[r] || 0;
    return `COB  ${side > 0 ? "ASK" : side < 0 ? "BID" : ""} ${fmtSize(size, rowMid(v, r))} at ${priceTxt}`;
  }
  const svp = source;
  if (!svp) return `SVP  ${priceTxt}`;
  const b = svp.buy[r] || 0;
  const sl = svp.sell[r] || 0;
  return `SVP  ${priceTxt}  koop ${fmtSize(b, mt)}  verkoop ${fmtSize(sl, mt)}  totaal ${fmtSize(b + sl, mt)}`;
}

// ---------- trades ----------------------------------------------------------------------

const GREEN = "#2fd07a";
const RED = "#ef4f4a";
// Rounded histogram durations keep volume bars stable during a continuous time zoom.
const BUCKET_SPANS = [100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000, 120000, 300000, 600000, 900000, 1800000];
function bucketSpan(v, minPx) {
  const want = minPx * v.dt / v.colPx;   // ms that cover minPx pixels
  return BUCKET_SPANS.find((s) => s >= want) || BUCKET_SPANS[BUCKET_SPANS.length - 1];
}
const VOL_BUCKET_PX = 15;  // volume bars approximately 15 px wide
const VOL_BAR_FILL = 0.85; // with a small gap between bars
const BUBBLE_MIN_DRAW_R = 3; // smaller bubbles are not drawn (their volume still counts in merges)
const VOL_H = 60;          // volume histogram height (CSS px)

// Group columns into time buckets aligned to absolute time, so they do not jump while scrolling.
function tradeBuckets(v, g) {
  const n = v.ncols;
  const span = bucketSpan(v, VOL_BUCKET_PX);
  const out = [];
  let cur = null;
  for (let j = n - S.valid; j < n; j++) {
    const slot = (S.head + j) % n;
    const t = S.tNewest - (n - 1 - j) * v.dt;
    const id = Math.floor((t - 1) / span); // column t covers (t - dt, t]
    if (!cur || cur.id !== id) {
      cur = { id, x0: g.xr - (n - j) * v.colPx, x1: 0, bv: 0, bpx: 0, sv: 0, spx: 0 };
      out.push(cur);
    }
    cur.x1 = g.xr - (n - 1 - j) * v.colPx;
    const bv = S.buyVol[slot];
    const sv = S.sellVol[slot];
    if (bv > 0) { cur.bv += bv; cur.bpx += bv * S.buyPx[slot]; }
    if (sv > 0) { cur.sv += sv; cur.spx += sv * S.sellPx[slot]; }
  }
  return out;
}

// Delta bubbles: keep both sides through every grouping/merge. Colour and area
// represent buy minus sell, but placement uses total volume so cancellation cannot
// put a bubble outside its trades (or divide by zero). A balanced cluster is kept
// for further merging but draws no sphere.
function updateCluster(c) {
  c.vol = c.buy + c.sell;
  c.x = c.xw / c.vol;
  c.tick = c.pxw / c.vol;
  c.delta = c.buy - c.sell;
  // Decimal lots such as 0.1 + 0.2 versus 0.3 must not become a full-size
  // sphere when their floating-point residue is the only nonzero delta.
  if (Math.abs(c.delta) <= Number.EPSILON * c.vol) c.delta = 0;
  c.side = Math.sign(c.delta);
}

// Internally the delta stays in coins. USDT changes its displayed size at the
// gross-volume-weighted trade price, never the aggressor colour.
function bubbleVolume(c) { return sizeValue(Math.abs(c.delta), c.tick); }

// Fixed for this page lifetime: live scroll, ring eviction and grid refreshes
// must not perturb distances or tie-breaking between unchanged trades.
const BUBBLE_TIME_ORIGIN = Date.now();
function bubbleXOffset(g) {
  return g.xr - (S.tNewest - BUBBLE_TIME_ORIGIN) * g.pxPerMs;
}

function tradeClusters(v, g) {
  // Start at screen-pixel resolution, then let Smart overlap merging do the
  // clustering. Source records retain native prices but are time-aggregated into
  // heat columns; never invent sub-column timing. Absolute anchors avoid pan jitter.
  const n = v.ncols;
  // Whole source columns/native ticks keep bin boundaries anchored while zoom
  // varies continuously. Fractional widths slide every boundary over the tape.
  const span = Math.max(1, Math.ceil(1 / (g.pxPerMs * v.dt))) * v.dt;
  const bandTicks = Math.max(1, Math.ceil(1 / g.ppt));
  const out = [];
  const open = new Map();   // "band:bucket" -> cluster
  for (let j = n - S.valid; j < n; j++) {
    const slot = (S.head + j) % n;
    const lv = S.tr[slot];  // flat [tick, buy, sell, ...]: trades per price level
    if (!lv) continue;
    const time = S.tNewest - (n - 1 - j) * v.dt;
    const xc = (time - BUBBLE_TIME_ORIGIN - v.dt / 2) * g.pxPerMs;
    const bucket = Math.floor((S.tNewest - (n - 1 - j) * v.dt - 1) / span);
    for (let i = 0; i < lv.length; i += 3) {
      const px = lv[i];
      const band = Math.floor(px / bandTicks);
      const buy = lv[i + 1], sell = lv[i + 2], vol = buy + sell;
      if (!(vol > 0)) continue;
      const key = `${band}:${bucket}`;
      let c = open.get(key);
      if (!c) {
        c = { buy: 0, sell: 0, pxw: 0, xw: 0 };
        open.set(key, c);
        out.push(c);
      }
      c.buy += buy; c.sell += sell;
      c.pxw += vol * px;
      c.xw += vol * xc;
    }
  }
  for (const c of out) updateCluster(c);
  return out;
}

function updateVolRef(clusters, xOffset = 0) {
  // Calibrate once from the first non-empty tape, not from each moving viewport.
  // Otherwise an unrelated new trade (or a zoom/grid replacement) resizes every
  // old dot and even changes its overlap partners. Keep both unit references from
  // that same sample so switching coins/USDT and back is reversible.
  const minTradeSize = S.view.minTradeSize;
  let ref = S.bubbleReference;
  if (!ref || ref.symbol !== S.symbol || ref.minTradeSize !== minTradeSize) {
    const visible = clusters.filter((c) => c.delta !== 0 && c.x + xOffset >= 0);
    // The off-screen overlap margin must not change the initial visible scale.
    const nonzero = visible.length ? visible : clusters.filter((c) => c.delta !== 0);
    if (!nonzero.length) { S.bubbleReference = null; S.volRef = 0; return; }
    const tickSize = S.meta[S.symbol].tick_size;
    ref = S.bubbleReference = {
      symbol: S.symbol, minTradeSize,
      base: nonzero.reduce((sum, c) => sum + Math.abs(c.delta), 0) / nonzero.length,
      quote: nonzero.reduce((sum, c) => sum + Math.abs(c.delta) * c.tick * tickSize, 0) / nonzero.length,
    };
  }
  S.volRef = usdt() ? ref.quote : ref.base;
}

// Area follows absolute delta relative to the average pre-merge dot. The pixel
// radius is calibrated empirically.
const BUBBLE_MIN_R = 1.5;
const BUBBLE_REF_R = 6;
const BUBBLE_MAX_R = 20;
// Two maximum overlap distances at Size 100 / Clustering 100. This context stays
// outside the canvas clip and survives ring eviction until edge dots have left.
const BUBBLE_HISTORY_PX = 400;
function bubbleRadius(vol) {
  if (!(vol > 0)) return 0;
  const ref = S.volRef || vol;
  const k = dotScale();
  return Math.min(BUBBLE_MAX_R * k, Math.max(BUBBLE_MIN_R, BUBBLE_REF_R * k * Math.sqrt(vol / ref)));
}

// Overlapping clusters combine both sides, then recalculate the delta. The radius
// may shrink and the colour may flip; gross buy/sell volume is never discarded.
// Merge candidates are processed in deterministic overlap passes.
const BUBBLE_OVERLAP = 1 / 1.8; // touching spheres merge at clustering setting 52
function mergeOverlapping(list, g, xOffset = 0) {
  // Growing/moving a merged dot can make two previously separate hosts overlap.
  // Repeat until stable; each non-final pass removes at least one cluster.
  while (true) {
    list.sort((a, b) => bubbleVolume(b) - bubbleVolume(a) || a.x - b.x || a.tick - b.tick || b.buy - a.buy);
    const kept = [];
    // Spatial hash: only bubbles in neighbouring cells can overlap.
    const overlap = BUBBLE_OVERLAP * clusteringScale();
    const cell = 2 * overlap * BUBBLE_MAX_R * dotScale();
    const cells = new Map();
    const cellKey = (cx, cy) => `${cx}:${cy}`;
    for (const p of list) {
      p.y = p.tick * g.ppt;
      p.r = bubbleRadius(bubbleVolume(p));
      const cx = Math.floor(p.x / cell);
      const cy = Math.floor(p.y / cell);
      let host = null, nearest = Infinity;
      for (let ix = cx - 1; ix <= cx + 1; ix++) {
        for (let iy = cy - 1; iy <= cy + 1; iy++) {
          const bucket = cells.get(cellKey(ix, iy));
          if (!bucket) continue;
          for (const k of bucket) {
            const dx = k.x - p.x;
            const dy = k.y - p.y;
            const lim = overlap * (k.r + p.r);
            const distance = dx * dx + dy * dy;
            if (distance < lim * lim && (distance < nearest ||
                (distance === nearest && (k.x < host.x || (k.x === host.x && k.tick < host.tick))))) {
              host = k; nearest = distance;
            }
          }
        }
      }
      if (!host) {
        kept.push(p);
        const key = cellKey(cx, cy);
        (cells.get(key) || cells.set(key, []).get(key)).push(p);
        continue;
      }
      const oldKey = cellKey(Math.floor(host.x / cell), Math.floor(host.y / cell));
      host.buy += p.buy; host.sell += p.sell;
      host.xw += p.xw; host.pxw += p.pxw;
      updateCluster(host);
      host.y = host.tick * g.ppt;
      host.r = bubbleRadius(bubbleVolume(host));
      const newKey = cellKey(Math.floor(host.x / cell), Math.floor(host.y / cell));
      // Weighted placement can move the host across a cell boundary. Keep its
      // lookup position current so later dots find it regardless of screen offset.
      if (newKey !== oldKey) {
        const bucket = cells.get(oldKey);
        bucket.splice(bucket.indexOf(host), 1);
        if (!bucket.length) cells.delete(oldKey);
        (cells.get(newKey) || cells.set(newKey, []).get(newKey)).push(host);
      }
    }
    if (kept.length === list.length) {
      // Projection happens only after merging, so subpixel scroll/recentering
      // cannot change which of two equally near bubbles receives a trade.
      for (const p of kept) {
        p.x += xOffset;
        p.y = yOf(g, p.tick);
      }
      return kept;
    }
    list = kept;
  }
}

// Pre-rendered shaded spheres, one per side and half-pixel radius (much cheaper than a
// radial gradient per bubble per frame).
const spriteCache = new Map();
function sphereSprite(side, r, dpr) {
  const rq = Math.max(1, Math.round(r));
  const key = `${side}:${rq}:${dpr}`;
  let c = spriteCache.get(key);
  if (c) return c;
  const size = Math.ceil((rq * 2 + 2) * dpr);
  c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  g.scale(dpr, dpr);
  const m = size / dpr / 2;
  const [hi, mid, shade, dark] = SPHERE[side];
  // Light centre on the right, slightly up: the right half stays flat, the left edge goes dark.
  const lx = m + rq * 0.5, ly = m - rq * 0.12;
  const gr = g.createRadialGradient(lx, ly, 0, lx, ly, rq * 1.5);
  gr.addColorStop(0, hi);
  gr.addColorStop(0.4, mid);
  gr.addColorStop(0.68, shade);
  gr.addColorStop(0.95, dark);
  g.fillStyle = gr;
  g.beginPath();
  g.arc(m, m, rq, 0, Math.PI * 2);
  g.fill();
  c.cssSize = size / dpr;
  spriteCache.set(key, c);
  return c;
}

// Bubble shading uses a matte colour on the right and a dark crescent on the
// left edge, without a highlight. Transparency lets lines and grid show through.
const SPHERE = {
  [1]: ["#42dc8b", "#3dd585", "#2b9a5e", "#0c2a1b"],
  [-1]: ["#e85648", "#e25143", "#a23a31", "#261214"],
};

function drawBubbles(v, g) {
  const clusters = tradeClusters(v, g);
  const xOffset = bubbleXOffset(g);
  updateVolRef(clusters, xOffset);
  const list = mergeOverlapping(clusters, g, xOffset);
  list.sort((a, b) => a.r - b.r); // small first, so the biggest delta bubble is on top
  const dpr = S.layout.dpr;
  ctx.globalAlpha = 1 - S.set.transparency / 100;
  for (const p of list) {
    // Clip only complete clusters: an off-screen trade can belong to a sphere
    // whose centre or right edge is still visible. Leave one pixel for rounding.
    if (p.x + p.r + 1 <= 0) continue;
    // Filtering follows ALL clustering/merging. Hidden dots still contribute
    // both sides and weighted placement; histogram and SVP remain unfiltered.
    if (p.r < BUBBLE_MIN_DRAW_R || Math.abs(p.delta) + Number.EPSILON * p.vol < S.set.minVolume) continue;
    const spr = sphereSprite(p.side, p.r, dpr);
    const h = spr.cssSize / 2;
    ctx.drawImage(spr, Math.round(p.x - h), Math.round(p.y - h), spr.cssSize, spr.cssSize);
  }
  ctx.globalAlpha = 1;
}

function niceCeil(x) {
  const e = Math.pow(10, Math.floor(Math.log10(x)));
  for (const m of [1, 2, 2.5, 3, 5, 10]) if (m * e >= x) return m * e;
  return 10 * e;
}

function drawVolume(L, buckets) {
  // In USDT mode each bucket is volume x its trade prices (bpx/spx hold sum(vol * tick)).
  const ts = S.meta[S.symbol].tick_size;
  const vals = (b) => (usdt() ? [b.bpx * ts, b.spx * ts] : [b.bv, b.sv]);
  // One bar per time bucket, its height the total volume, its colour the side
  // with the most volume.
  let max = 0;
  for (const b of buckets) if (b.x1 > 0) { const [vb, vs] = vals(b); max = Math.max(max, vb + vs); }
  if (max <= 0) return;
  const scale = niceCeil(max);
  const base = L.heatH;
  for (const b of buckets) {
    const [vb, vs] = vals(b);
    if (!(vb + vs > 0)) continue;
    const w = Math.max(1, (b.x1 - b.x0) * VOL_BAR_FILL);
    const xb = b.x0 + (b.x1 - b.x0 - w) / 2;   // centred in its bucket
    const h = ((vb + vs) / scale) * VOL_H;
    ctx.fillStyle = vb >= vs ? "rgba(70,222,146,0.85)" : "rgba(234,98,88,0.85)";
    ctx.fillRect(xb, base - h, w, h);
  }
  ctx.font = FONT;
  ctx.textAlign = "left";
  ctx.textBaseline = "bottom";
  const label = fmtValue(scale);
  ctx.fillStyle = "rgba(19,26,34,0.75)";
  ctx.fillRect(2, base - VOL_H - 17, ctx.measureText(label).width + 6, 17);
  ctx.fillStyle = "#e6edf3";
  ctx.fillText(label, 5, base - VOL_H);
}

function drawLastTradeLabel(L, g) {
  const lt = S.lastTrade;
  if (!lt) return;
  const y = yOf(g, levelMid(lt.tick));
  ctx.font = FONT_BOLD;
  ctx.textBaseline = "middle";
  ctx.textAlign = "left";
  const a = fmtSize(lt.qty, lt.tick);
  const b = ` (${fmtSize(lt.streak, lt.tick)})`;
  const wa = ctx.measureText(a).width;
  const wb = ctx.measureText(b).width;
  const x = L.heatW - wa - wb - 6;
  ctx.fillStyle = "rgba(19,26,34,0.8)";
  ctx.fillRect(x - 3, y - 9, wa + wb + 8, 18);
  ctx.fillStyle = "#ffffff";
  ctx.fillText(a, x, y);
  ctx.fillStyle = lt.side > 0 ? GREEN : RED;
  ctx.fillText(b, x + wa, y);
}

function drawHover(L, v, g) {
  const m = S.mouse;
  if (m && !S.drag && m.x >= L.heatW + PRICE_AXIS_W && m.y >= 0 && m.y < L.heatH) {
    $("hover").textContent = sideHoverText(L, v, g, m);
    return;
  }
  if (!m || S.drag || m.x < 0 || m.x >= L.heatW || m.y < 0 || m.y >= L.heatH) {
    $("hover").textContent = "";
    return;
  }
  const n = v.ncols;
  const t = Math.round(tOf(g, m.x));
  const tickF = tickOf(g, m.y);
  const row = rowOf(v, tickF);
  const rowTick = v.tick_lo + row * v.rowTicks;            // lowest tick of the row
  const j = m.x >= g.xr ? (v.live ? n - 1 : -1) : n - 1 - Math.floor((g.xr - m.x) / v.colPx);
  const priceTxt = !v.outward && v.rowTicks > S.gran
    ? `${priceStr(rowTick)}–${priceStr(rowTick + v.rowTicks - S.gran)}` : priceStr(rowTick);
  if (j < n - S.valid || j < 0 || row < 0 || row >= v.nrows) {
    $("hover").textContent = `${timeLabel(t, true)}  ${priceTxt}`;
    return;
  }
  const slot = (S.head + j) % n;
  const size = S.data[slot * v.nrows + row] || 0;
  const side = rowSide(v, row, S.bid[slot], S.ask[slot]);
  const sizeTxt = size ? `${side > 0 ? "ASK" : side < 0 ? "BID" : ""} ${fmtSize(size, rowMid(v, row))} at ${priceTxt}` : priceTxt;
  $("hover").textContent = `${timeLabel(t, true)}  ${sizeTxt}`;
}

// ---------- interaction -----------------------------------------------------------------

function inPriceAxis(x, y) { return S.layout && x >= S.layout.heatW && y < S.layout.heatH; }
function localXY(e) {
  const r = ov.getBoundingClientRect();
  return { x: e.clientX - r.left, y: e.clientY - r.top };
}

function zoomPrice(factor) {
  S.pxPerTick = Math.min(20, Math.max(0.002, S.pxPerTick * factor));
  rescaleViewY();
  scheduleView();
}

// Make the current grid follow a price zoom immediately (until the new grid arrives).
function rescaleViewY() {
  const v = S.view;
  if (!v || !S.layout) return;
  scaleAt = -Infinity;
  const mid = S.layout.heatH / 2;
  const center = tickOf(frameGeom(), mid);
  v.rowPx = v.rowTicks * S.pxPerTick;
  // Keep the centre tick at the same height: solve yOf(center) == mid for yOff.
  const drag = S.drag && S.drag.kind === "pan" ? S.drag.dy : 0;
  v.baseYOff = (S.layout.heatH - v.nrows * v.rowPx) / 2;
  v.yOff = mid - drag - v.baseYOff - (v.nrows * v.rowTicks - (center - v.tick_lo) - rowHalf(v)) * S.pxPerTick;
}

// Wheel: a mouse sends a few big steps, a trackpad sends many tiny ones (plus momentum).
// Both are turned into a continuous zoom factor, so the trackpad zooms smoothly.

function wheelPixels(e, d) {
  return e.deltaMode === 1 ? d * 16 : e.deltaMode === 2 ? d * 400 : d;
}

ov.addEventListener("wheel", (e) => {
  e.preventDefault();
  if (!S.view) return;
  const { x, y } = localXY(e);
  const dx = wheelPixels(e, e.deltaX);
  const dy = wheelPixels(e, e.deltaY);
  // Two-finger horizontal swipe on a trackpad: scroll through time.
  if (!e.ctrlKey && Math.abs(dx) > Math.abs(dy) && !inPriceAxis(x, y)) {
    wheelPan(-dx);
    return;
  }
  if (inPriceAxis(x, y) || e.shiftKey) {
    // Price zoom is continuous, so it can follow the trackpad smoothly.
    zoomPrice(Math.exp(-dy / 300));
    return;
  }
  // Pinch on a trackpad arrives as wheel + ctrlKey with small deltas: make it more sensitive.
  const next = Math.min(MS_PER_PX_MAX, Math.max(MS_PER_PX_MIN,
    S.msPerPx * Math.exp((e.ctrlKey ? dy * 4 : dy) / 300)));
  if (next === S.msPerPx) return;
  if (!S.live) {
    // History: keep the time under the mouse in place.
    const g = frameGeom();
    const tMouse = tOf(g, x);
    S.tRight = tMouse + (S.layout.heatW - x) * next;
  }
  previewTimeZoom(S.msPerPx / next, x);
  S.msPerPx = next;
  scheduleView(120);
}, { passive: false });

// Stretch the current picture right away; the sharp grid follows when it arrives.
function previewTimeZoom(factor, mouseX) {
  const v = S.view;
  if (!v) return;
  scaleAt = -Infinity;
  const g0 = frameGeom();
  // Live: the now-line is the anchor. History: the time under the mouse.
  const anchorX = v.live ? g0.nowX : mouseX;
  const tA = tOf(g0, anchorX);
  v.colPx *= factor;
  const g1 = frameGeom();
  v.xOff += anchorX - xOf(g1, tA);
}

// Horizontal trackpad pan: show it as a drag offset, commit when the gesture pauses.
let wheelPanTimer = 0;
function wheelPan(dxPx) {
  if (!S.drag) S.drag = { kind: "pan", x0: 0, y0: 0, dx: 0, dy: 0, wheel: true };
  if (!S.drag.wheel) return;
  S.drag.dx += dxPx;
  clearTimeout(wheelPanTimer);
  wheelPanTimer = setTimeout(() => {
    const d = S.drag;
    S.drag = null;
    if (d) commitPan(d.dx, 0);
  }, 150);
}

ov.addEventListener("mousedown", (e) => {
  if (e.button !== 0 || !S.view) return;
  const { x, y } = localXY(e);
  if (y >= S.layout.heatH) return;
  if (S.drag && S.drag.wheel) return;
  const kind = nearNowLine(x, y) ? "now" : inPriceAxis(x, y) ? "price" : "pan";
  S.drag = { kind, x0: x, y0: y, dx: 0, dy: 0, ppt0: S.pxPerTick };
  e.preventDefault();
});

// The now-line can be grabbed and moved left/right (how much room the future area gets).
const NOW_GRAB_PX = 5;
function nearNowLine(x, y) {
  const v = S.view;
  if (!v || !v.live || !S.layout || y >= S.layout.heatH) return false;
  return Math.abs(x - S.layout.heatW * S.set.nowFrac) <= NOW_GRAB_PX;
}

window.addEventListener("mousemove", (e) => {
  const p = localXY(e);
  S.mouse = p;
  const d = S.drag;
  ov.style.cursor = (d && d.kind === "now") || (!d && nearNowLine(p.x, p.y)) ? "ew-resize" : "";
  if (!d || d.wheel) return;
  if (d.kind === "now") {
    S.set.nowFrac = Math.min(0.95, Math.max(0.3, p.x / S.layout.heatW));
  } else if (d.kind === "price") {
    // Drag up to zoom in on the price axis.
    S.pxPerTick = Math.min(20, Math.max(0.002, d.ppt0 * Math.exp((d.y0 - p.y) / 120)));
    rescaleViewY();
    scheduleView(150);
  } else {
    d.dx = p.x - d.x0;
    d.dy = p.y - d.y0;
  }
});

window.addEventListener("mouseup", () => {
  const d = S.drag;
  if (!d || d.wheel) return;
  S.drag = null;
  if (d.kind === "pan") commitPan(d.dx, d.dy);
  if (d.kind === "now") {
    saveSettings({ nowFrac: S.set.nowFrac });
    requestView();   // more or fewer columns fit left of the line now
  }
});

function commitPan(dx, dy) {
  const d = { dx, dy };
  if ((Math.abs(d.dx) < 3 && Math.abs(d.dy) < 3) || !S.view) return;
  const v = S.view;
  const g = frameGeom(); // without drag now
  // Time: the time that was at the right edge moves by dx.
  const tRightNow = tOf(g, S.layout.heatW);
  const newRight = tRightNow - d.dx / g.pxPerMs;
  if (Math.abs(d.dx) >= 3) {
    if (newRight < dataNow()) { S.live = false; S.tRight = newRight; }
    else S.live = true;
    v.xOff += d.dx;
  }
  if (Math.abs(d.dy) >= 3) {
    S.autoCenter = false;
    S.centerTick = tickOf(g, S.layout.heatH / 2 - d.dy);
    v.yOff += d.dy; // keep the picture where it was dropped until the new grid arrives
  }
  if (!S.live) clampHistory();
  requestView();
}

ov.addEventListener("mouseleave", () => { S.mouse = null; });

function clampHistory() {
  if (S.historyStart === null) return;
  const spanMs = S.layout.heatW * S.msPerPx;
  S.tRight = Math.max(S.tRight, S.historyStart + spanMs * 0.25);
}

function goBack() {
  if (!S.view) return;
  const g = frameGeom();
  const spanMs = S.layout.heatW / g.pxPerMs;
  const right = S.live ? tOf(g, S.layout.heatW * S.set.nowFrac) : S.tRight;
  S.live = false;
  S.tRight = right - spanMs / 2;
  clampHistory();
  requestView();
}

function goLive() {
  S.live = true;
  S.autoCenter = true;
  requestView("recenter");
}

function updateButtons() {
  $("btn-live").style.display = S.live && S.autoCenter ? "none" : "block";
  setConn(S.live ? "Data: live" : "History");
}

$("btn-back").addEventListener("click", goBack);
$("btn-live").addEventListener("click", goLive);
window.addEventListener("keydown", (e) => {
  if ($("display-settings").matches(":popover-open") || $("dlg").classList.contains("open") ||
      e.target.matches("input, select, textarea")) return;
  if (e.key === "End" || e.key === "Escape") goLive();
});

// ---------- tabs (instruments) ---------------------------------------------------------

const GRAN_CHOICES = [1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000];
const PLATFORM_NAME = { BNF: "Binance Futures", BB: "Bybit" };
const zoomByTab = new Map();   // per tab: {msPerPx, pxPerTick}, kept while the page is open

function onMeta(m) {
  const prevGran = S.symbol && S.meta && S.meta[S.symbol] ? S.meta[S.symbol].gran : null;
  S.meta = m.symbols;
  S.order = m.order || Object.keys(m.symbols);
  renderTabs();
  if (!S.order.length) {
    S.symbol = null;
    S.view = null;
    $("empty").style.display = "block";
    document.title = "deepmarket";
    return;
  }
  $("empty").style.display = "none";
  if (!S.symbol || !S.meta[S.symbol]) {
    selectTab(S.pendingSelect && S.meta[S.pendingSelect] ? S.pendingSelect : S.order[0]);
  } else if (S.pendingSelect && S.meta[S.pendingSelect]) {
    selectTab(S.pendingSelect);
  } else if (S.meta[S.symbol].gran !== prevGran || !S.view) {
    S.gran = S.meta[S.symbol].gran;
    requestView();
  }
  S.pendingSelect = null;
}

function renderTabs() {
  const box = $("tablist");
  box.innerHTML = "";
  for (const inst of S.order) {
    const t = document.createElement("div");
    t.className = "tab" + (inst === S.symbol ? " active" : "");
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = inst;
    name.title = inst === S.symbol ? "Change tick size / size granularity" : `Go to ${inst}`;
    name.addEventListener("click", () => (inst === S.symbol ? openDialog(inst) : selectTab(inst)));
    const close = document.createElement("span");
    close.className = "close";
    close.textContent = "×";
    close.title = "Close tab";
    close.addEventListener("click", (e) => { e.stopPropagation(); closeTab(inst); });
    t.append(name, close);
    box.appendChild(t);
  }
  $("btn-add").style.display = S.order.length >= 5 ? "none" : "";
}

function selectTab(inst) {
  if (S.symbol) zoomByTab.set(S.symbol, { msPerPx: S.msPerPx, pxPerTick: S.pxPerTick });
  S.symbol = inst;
  S.bubbleReference = null;
  const z = zoomByTab.get(inst);
  S.msPerPx = z ? z.msPerPx : MS_PER_PX_DEFAULT;
  S.pxPerTick = z ? z.pxPerTick : 4;
  S.autoZoom = !z;        // first view of this tab: pick a sensible price zoom
  S.gran = S.meta[inst].gran;
  S.view = null;
  S.data = null;
  S.live = true;
  S.autoCenter = true;
  S.centerTick = null;
  S.lastTrade = null;
  S.svp = null;
  S.cob = null;
  document.title = `${inst} · deepmarket`;
  renderTabs();
  requestView();
}

async function closeTab(inst) {
  const r = await fetch(`/api/tabs/${encodeURIComponent(inst)}`, { method: "DELETE" });
  if (!r.ok) alert((await r.json().catch(() => ({}))).error || "Closing failed");
  // onMeta updates the selection and view together, regardless of whether the
  // broadcast arrives before or after this HTTP response.
}

// First view of a tab: zoom the price axis so roughly 3% of the price fits on screen.
function autoZoomPrice(h) {
  if (!S.autoZoom || !S.layout || h.bid.length === 0) return false;
  S.autoZoom = false;
  const bid = h.bid[h.bid.length - 1];
  if (!(bid > 0)) return false;
  const want = Math.min(20, Math.max(0.002, S.layout.heatH / (0.03 * bid)));
  if (want / S.pxPerTick < 1.5 && S.pxPerTick / want < 1.5) return false;
  S.pxPerTick = want;
  return true;
}

// ---------- instrument subscription dialog --------------------------------------------

const symbolLists = {};   // platform -> [{symbol, tick, step}]
let dlgEdit = null;       // instrument being edited, or null when adding

// ---------- custom dropdown lists (native selects/datalists break the dark theme) ------

let openListEl = null;    // the one open .dd-list, so only one is open at a time
function closeOpenList() {
  if (openListEl) { openListEl.classList.remove("open"); openListEl = null; }
}
document.addEventListener("mousedown", (e) => {
  // Close when clicking outside; the parent of the list is the control that owns it, so
  // opening a second dropdown while one is open swaps them instead of closing both.
  if (openListEl && !openListEl.parentElement.contains(e.target)) closeOpenList();
});

// A dropdown inside `root`: a button showing the current label and a themed list under it.
// The value lives in the hidden input `hiddenId`, so form handling stays unchanged.
function dropdown(root, hiddenId, onChange) {
  const hidden = $(hiddenId);
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "dd-btn";
  const lbl = document.createElement("span");
  lbl.className = "dd-lbl";
  const list = document.createElement("span");
  list.className = "dd-list";
  btn.append(lbl);
  root.append(btn, list);
  let opts = [];
  let hl = -1;
  function sync() {
    const cur = opts.find((o) => o.value === hidden.value);
    lbl.textContent = cur ? cur.label : "";
  }
  function paint() {
    list.innerHTML = "";
    opts.forEach((o, i) => {
      const it = document.createElement("span");
      it.className = "dd-item" + (o.value === hidden.value ? " on" : "") + (i === hl ? " hl" : "");
      it.textContent = o.label;
      it.addEventListener("mousedown", (e) => { e.preventDefault(); pick(o.value); });
      list.appendChild(it);
    });
  }
  function scrollHl() {
    if (list.children[hl]) list.children[hl].scrollIntoView({ block: "nearest" });
  }
  function openList() {
    closeOpenList();
    hl = opts.findIndex((o) => o.value === hidden.value);
    paint();
    list.classList.add("open");
    openListEl = list;
    scrollHl();
  }
  function pick(v) {
    hidden.value = v;
    sync();
    closeOpenList();
    onChange();
  }
  function move(d) {
    hl = Math.max(0, Math.min(opts.length - 1, hl + d));
    [...list.children].forEach((c, i) => c.classList.toggle("hl", i === hl));
    scrollHl();
  }
  btn.addEventListener("mousedown", (e) => {
    if (root.classList.contains("off")) return;
    e.preventDefault();     // no native focus change: we focus the button ourselves
    btn.focus();
    if (openListEl === list) closeOpenList(); else openList();
  });
  btn.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && openListEl === list) { closeOpenList(); e.stopPropagation(); return; }
    if (openListEl !== list) {
      if (e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "Enter" || e.key === " ") { openList(); e.preventDefault(); }
      return;
    }
    if (e.key === "ArrowDown") { move(1); e.preventDefault(); }
    else if (e.key === "ArrowUp") { move(-1); e.preventDefault(); }
    else if (e.key === "Enter" && hl >= 0) { pick(opts[hl].value); e.preventDefault(); }
    else if (e.key === "Escape") closeOpenList();
  });
  return {
    set(newOpts, value) { opts = newOpts; hidden.value = value ?? ""; sync(); },
    sync,
    disable(off) { root.classList.toggle("off", off); },
  };
}

const ddPlatform = dropdown($("dd-platform"), "dlg-platform", refreshSymbolChoice);
ddPlatform.set([{ value: "BNF", label: "Binance Futures" }, { value: "BB", label: "Bybit" }], "BNF");
const ddTick = dropdown($("dd-tick"), "dlg-tick", () => {});
const ddSize = dropdown($("dd-size"), "dlg-sizegran", () => {});

// Symbol suggestions: a themed autocomplete list fed by the platform's symbol list. It only
// opens while typing (from the first letter) or via the caret button, not on focus/click.
let symHl = -1;
function symListOpen() { return $("sym-list").classList.contains("open"); }
function hideSymList() { $("sym-list").classList.remove("open"); symHl = -1; }
function showSymList(force = false) {
  if ($("dlg-symbol").disabled) return;
  const list = symbolLists[$("dlg-platform").value] || [];
  const q = $("dlg-symbol").value.trim().toUpperCase();
  if (!q && !force) { hideSymList(); return; }
  const starts = q ? list.filter((x) => x.symbol.startsWith(q)) : list;
  const rest = q ? list.filter((x) => !x.symbol.startsWith(q) && x.symbol.includes(q)) : [];
  const box = $("sym-list");
  box.innerHTML = "";
  symHl = -1;
  for (const x of starts.concat(rest).slice(0, 60)) {
    const it = document.createElement("span");
    it.className = "dd-item";
    it.textContent = x.symbol;
    it.addEventListener("mousedown", (e) => {
      e.preventDefault();     // keep focus in the input
      $("dlg-symbol").value = x.symbol;
      hideSymList();
      onSymbolTyped();
    });
    box.appendChild(it);
  }
  if (box.children.length) {
    closeOpenList();
    box.classList.add("open");
    openListEl = box;
  }
}
function symMove(d) {
  const box = $("sym-list");
  symHl = Math.max(0, Math.min(box.children.length - 1, symHl + d));
  [...box.children].forEach((c, i) => c.classList.toggle("hl", i === symHl));
  if (box.children[symHl]) box.children[symHl].scrollIntoView({ block: "nearest" });
}

async function loadSymbols(platform) {
  if (!symbolLists[platform]) {
    const r = await fetch(`/api/symbols?platform=${platform}`);
    symbolLists[platform] = r.ok ? await r.json() : [];
  }
  return symbolLists[platform];
}

function decimalsOf(str) {
  const s = String(str);
  if (s.includes("e-")) return Number(s.split("e-")[1]);
  const t = s.includes(".") ? s.replace(/0+$/, "") : s;
  return t.includes(".") ? t.length - t.indexOf(".") - 1 : 0;
}
function trimNum(s) { return s.includes(".") ? s.replace(/0+$/, "").replace(/\.$/, "") : s; }

function fillTickChoices(tickStr, selected) {
  const dec = decimalsOf(tickStr);
  const tick = Number(tickStr);
  ddTick.set(GRAN_CHOICES.map((k) => ({ value: String(k), label: trimNum((k * tick).toFixed(dec)) })), String(selected || 1));
  ddTick.disable(false);
}

function fillSizeChoices(stepStr, selected) {
  const dec = decimalsOf(stepStr);
  const step = Number(stepStr);
  const opts = [];
  for (let i = 0; i <= 5; i++) {
    const val = step * 10 ** i;
    if (val > 100000) break;
    const txt = trimNum(val.toFixed(Math.max(0, dec - i)));
    opts.push({ value: txt, label: txt });
  }
  const want = selected != null ? trimNum(String(selected)) : opts[0]?.value;
  ddSize.set(opts, opts.some((o) => o.value === want) ? want : opts[0]?.value);
  ddSize.disable(false);
}

async function refreshSymbolChoice() {
  await loadSymbols($("dlg-platform").value);
  onSymbolTyped();
}

function onSymbolTyped() {
  const input = $("dlg-symbol");
  const upper = input.value.toUpperCase();
  if (input.value !== upper) {
    const { selectionStart, selectionEnd, selectionDirection } = input;
    input.value = upper;
    input.setSelectionRange(selectionStart, selectionEnd, selectionDirection);
  }
  const list = symbolLists[$("dlg-platform").value] || [];
  const sym = input.value.trim();
  const hit = list.find((x) => x.symbol === sym);
  $("dlg-ok").disabled = !hit;
  $("dlg-err").textContent = sym && !hit && list.length ? "Unknown coin on this platform" : "";
  if (hit) {
    fillTickChoices(hit.tick, Number($("dlg-tick").value) || 1);
    fillSizeChoices(hit.step, null);
  } else if (!dlgEdit) {
    ddTick.set([], "");
    ddSize.set([], "");
    ddTick.disable(true);
    ddSize.disable(true);
  }
  if (document.activeElement === $("dlg-symbol")) showSymList();
}

async function openDialog(editInst = null) {
  dlgEdit = editInst;
  const m = editInst ? S.meta[editInst] : null;
  $("dlg-title").textContent = editInst ? `Edit ${editInst}` : "Add coin";
  $("dlg-ok").textContent = editInst ? "Save" : "Subscribe";
  ddPlatform.disable(!!editInst);
  $("dlg-symbol").disabled = !!editInst;
  $("dlg-err").textContent = "";
  $("dlg").classList.add("open");
  if (editInst) {
    $("dlg-platform").value = m.suffix;
    ddPlatform.sync();
    $("dlg-symbol").value = m.symbol;
    const tickStr = trimNum(m.tick_size.toFixed(m.decimals));
    fillTickChoices(tickStr, m.gran);
    fillSizeChoices(m.step_size, m.size_gran.toFixed(m.size_decimals));
    $("dlg-ok").disabled = false;
  } else {
    $("dlg-symbol").value = "";
    ddTick.set([], "");
    ddSize.set([], "");
    ddTick.disable(true);
    ddSize.disable(true);
    $("dlg-ok").disabled = true;
    $("dlg-symbol").focus();
    await refreshSymbolChoice();
  }
}

function closeDialog() { $("dlg").classList.remove("open"); }

$("btn-add").addEventListener("click", () => openDialog(null));
$("empty-add").addEventListener("click", () => openDialog(null));
$("dlg-cancel").addEventListener("click", closeDialog);
$("dlg").addEventListener("mousedown", (e) => { if (e.target === $("dlg")) closeDialog(); });
$("dlg-symbol").addEventListener("input", onSymbolTyped);
$("sym-caret").addEventListener("mousedown", (e) => {
  e.preventDefault();         // keep focus in the input
  if (symListOpen()) hideSymList(); else showSymList(true);
  $("dlg-symbol").focus();
});
$("dlg-symbol").addEventListener("blur", () => setTimeout(hideSymList, 100));
$("dlg-symbol").addEventListener("keydown", (e) => {
  const box = $("sym-list");
  if (!symListOpen()) {
    if (e.key === "ArrowDown" && box.children.length) { showSymList(); e.preventDefault(); }
    return;
  }
  if (e.key === "ArrowDown") { symMove(1); e.preventDefault(); }
  else if (e.key === "ArrowUp") { symMove(-1); e.preventDefault(); }
  else if (e.key === "Enter" && symHl >= 0) {
    e.preventDefault();     // pick the highlighted suggestion instead of submitting
    $("dlg-symbol").value = box.children[symHl].textContent;
    hideSymList();
    onSymbolTyped();
  } else if (e.key === "Escape") {
    e.stopPropagation();    // first Esc closes the list, only the second closes the dialog
    hideSymList();
  }
});
$("dlg-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { gran: Number($("dlg-tick").value), sizeGran: $("dlg-sizegran").value };
  let r;
  if (dlgEdit) {
    r = await fetch(`/api/tabs/${encodeURIComponent(dlgEdit)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  } else {
    body.platform = $("dlg-platform").value;
    body.symbol = $("dlg-symbol").value.trim().toUpperCase();
    $("dlg-ok").disabled = true;
    $("dlg-err").textContent = "Opening…";
    r = await fetch("/api/tabs", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  }
  const res = await r.json().catch(() => ({}));
  if (!r.ok) {
    $("dlg-err").textContent = res.error || "Failed";
    $("dlg-ok").disabled = false;
    return;
  }
  if (!dlgEdit) S.pendingSelect = res.inst;   // switch to it when the tab list arrives
  closeDialog();
});
window.addEventListener("keydown", (e) => { if (e.key === "Escape" && $("dlg").classList.contains("open")) closeDialog(); });

// ---------- display settings (sliders), stored on the server --------------------------

let saveTimer = 0;
let pendingSave = {};
let saving = false;
function saveSettings(part) {
  pendingSave = { ...pendingSave, ...part };
  settingsSaveStatus("Saving…");
  clearTimeout(saveTimer);
  saveTimer = setTimeout(flushSettings, 400);
}

function settingsSaveStatus(message, error = false) {
  $("settings-save-status").textContent = message;
  $("settings-save-status").classList.toggle("error", error);
  $("settings-retry").hidden = !error;
  $("btn-settings").textContent = error ? "Settings !" : "Settings";
}

async function flushSettings() {
  clearTimeout(saveTimer);
  if (saving || !Object.keys(pendingSave).length) return;
  saving = true;
  const body = pendingSave;
  pendingSave = {};
  try {
    const r = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    settingsSaveStatus("");
  } catch (e) {
    pendingSave = { ...body, ...pendingSave }; // newer edits take precedence on retry
    settingsSaveStatus(`Not saved (${e.message}). Retry to keep these changes.`, true);
    saving = false;
    return;
  }
  saving = false;
  if (Object.keys(pendingSave).length) await flushSettings();
}

const SLIDERS = [["s-dot", "volumeDotSize"], ["s-cut", "lowerCutoff"], ["s-dim", "heatmapIntensity"]];
function syncSettingsControls() {
  for (const el of document.querySelectorAll("[data-setting]")) {
    const value = S.set[el.dataset.setting];
    if (el.type === "checkbox") el.checked = value;
    else el.value = Number(value.toFixed(6));
  }
  for (const [id, key] of SLIDERS) {
    document.querySelector(`output[for="${id}"]`).textContent = Number(S.set[key].toFixed(1));
  }
  $("set-interval").disabled = !S.set.adjustByTime;
}

function changeSetting(key, value) {
  S.set[key] = value;
  const part = { [key]: value };
  if (key === "lowerCutoff" || key === "upperCutoff") {
    if (S.set.lowerCutoff >= S.set.upperCutoff) {
      const other = key === "lowerCutoff" ? "upperCutoff" : "lowerCutoff";
      part[other] = S.set[other] = value + (key === "lowerCutoff" ? 1 : -1);
    }
    scaleAt = -Infinity;
  }
  if (key === "minTradeSize") scheduleView();
  syncSettingsControls();
  saveSettings(part);
}

function restoreSettings() {
  Object.assign(S.set, DISPLAY_DEFAULTS);
  scaleAt = -Infinity;
  syncSettingsControls();
  saveSettings(DISPLAY_DEFAULTS);
}

function setupSliders() {
  // Units toggle: coins <-> USDT.
  const units = $("units");
  const showUnits = () => {
    units.textContent = "USDT";
    units.classList.toggle("on", usdt());
    units.title = usdt() ? "Sizes in USDT (click: in the coin itself)" : "Sizes in the coin itself (click: in USDT)";
  };
  showUnits();
  units.addEventListener("click", () => {
    S.set.units = usdt() ? "base" : "usdt";
    showUnits();
    scaleAt = -Infinity;   // rebuild the quantity bounds in USDT / coins on the next frame
    saveSettings({ units: S.set.units });
  });
  // Time zone.
  const tz = $("tz");
  for (const [id, label] of TZ_CHOICES) {
    const o = document.createElement("option");
    o.value = id;
    o.textContent = label;
    tz.appendChild(o);
  }
  if (!TZ_CHOICES.some(([id]) => id === S.set.tz)) {   // old city setting -> UTC+1
    S.set.tz = TZ_DEFAULT;
    saveSettings({ tz: S.set.tz });
  }
  tz.value = S.set.tz;
  fmtTime = makeTimeFmt(S.set.tz);
  tz.addEventListener("change", () => {
    S.set.tz = tz.value;
    fmtTime = makeTimeFmt(S.set.tz);
    saveSettings({ tz: S.set.tz });
  });
  syncSettingsControls();
  for (const el of document.querySelectorAll("[data-setting]")) {
    el.addEventListener(el.type === "range" ? "input" : "change", () => {
      if (el.type === "checkbox") changeSetting(el.dataset.setting, el.checked);
      else {
        if (el.value === "" || !Number.isFinite(el.valueAsNumber)) { syncSettingsControls(); return; }
        changeSetting(el.dataset.setting, Math.min(Number(el.max), Math.max(Number(el.min), el.valueAsNumber)));
      }
    });
  }
  const panel = $("display-settings");
  // Native auto popover: outside click, Escape and a second invoker click all
  // dismiss it, while the chart and toolbar remain available for comparison.
  panel.addEventListener("beforetoggle", (e) => {
    if (e.newState === "open") syncSettingsControls();
    else flushSettings();
  });
  $("settings-done").addEventListener("click", () => panel.hidePopover());
  $("settings-restore").addEventListener("click", restoreSettings);
  $("settings-retry").addEventListener("click", flushSettings);
}

async function start() {
  try {
    await Promise.all([document.fonts.load(FONT), document.fonts.load(FONT_NUM), document.fonts.load(FONT_BOLD)]);
  } catch (e) {
    console.warn("font not loaded, using fallback", e);
  }
  try {
    const r = await fetch("/api/settings");
    if (r.ok) S.set = normalizeSettings(await r.json());
  } catch (e) {
    console.warn("settings not loaded, using defaults", e);
  }
  setupSliders();
  connect();
  requestAnimationFrame(frame);
}
start();
