// Numeric rendering rules recovered from Bookmap 7.9.0 b12.
const priceTolerance = margin => Math.min(margin, Math.max(2, Math.round(margin * 20 / 100)));

// sp live recenter trigger: keep the viewport still inside the tolerance zone.
export function shouldRecenterPrice(latest, history, upper, rows, margin) {
  margin = Math.max(3, Math.min(4000, Math.round(margin)));
  const tolerance = priceTolerance(margin), inner = margin - tolerance;
  if (latest.some(p => upper-p < inner || p-(upper-rows) < inner)) return true;
  return history.length > 0 && Math.max(upper-Math.max(...history),
    Math.min(...history)-(upper-rows)-1) > margin+tolerance;
}

// qy / qA / qs: bounds in selected price ticks, oldest sample first. The
// current sample is added AFTER trimming, so the latest price cannot disappear.
export function autoPriceRange(samples, margin = 25) {
  margin = Math.max(3, Math.min(4000, Math.round(margin)));
  const tolerance = priceTolerance(margin);
  const lows = [], highs = [];
  let latest;
  for (let i = 0; i < samples.length; i++) {
    const prices = samples[i].filter(Number.isFinite);
    if (!prices.length) continue;
    const progress = samples.length === 1 ? 1 : i / (samples.length - 1);
    const pad = Math.floor((1 - 2 * Math.sqrt(1 - progress)) * tolerance + 0.5);
    const pair = [Math.min(...prices) - pad, Math.max(...prices) + pad];
    if (latest) { lows.push(latest[0]); highs.push(latest[1]); }
    latest = pair;
  }
  if (!latest) return null;
  lows.sort((a,b)=>a-b); highs.sort((a,b)=>b-a);
  const count = lows.length + 1;
  const trim = count > 2 ? Math.ceil(count * 0.04 / 2) : 0;
  const lower = Math.min(latest[0], lows[trim] ?? Infinity) - margin;
  const upper = Math.max(latest[1], highs[trim] ?? -Infinity) + margin;
  const rows = Math.min(36000, upper - lower + 1);
  return { lower: upper - rows + 1, upper, rows, margin };
}

export function smartParameter(slider) {
  const x = slider / 100 * 2.1465;
  return (Math.tan((x - 1) * 1.37) / 5 + 1) * x ** 0.3 + 10 * Math.max(0, x - 1.8) ** 2;
}

export const dotTargetRadius = (slider) => Math.trunc((0.2 + 0.008 * slider) ** 2 * 100);

// Ordered tr/tp preprocessing. An event opens a group only after processing the
// previous tail; a price visited again is not a dictionary bucket. Gross weight
// survives delta cancellation, except that a completely cancelled group is removed.
export function appendTradeEvents(groups, events, { clustering = 52, ppt = 1, sameColumn = false } = {}) {
  const distance = 6 * smartParameter(clustering);
  const merge = (a,b) => {
    const weight = a.weight+b.weight;
    return {x:Math.round((a.x*a.weight+b.x*b.weight)/weight),
      tick:(a.tick*a.weight+b.tick*b.weight)/weight,
      buy:a.buy+b.buy,sell:a.sell+b.sell,weight};
  };
  for (const event of events) {
    const last = groups.at(-1);
    if (last && last.x === event.x && Math.trunc(last.tick) === Math.trunc(event.tick)) {
      const weight = event.buy+event.sell;
      last.tick = (last.tick*last.weight+event.tick*weight)/(last.weight+weight);
      last.weight += weight; last.buy += event.buy; last.sell += event.sell;
      if (last.buy === last.sell) groups.pop();
      continue;
    }
    while (groups.length > 1) {
      const tail = groups.at(-1);
      let found = -1;
      for (let i = groups.length-2; i >= Math.max(0,groups.length-1-(sameColumn ? 1 : 10)); i--) {
        const other = groups[i];
        if (sameColumn ? tail.x === other.x :
          (tail.x-other.x)**2+((tail.tick-other.tick)*ppt)**2 < distance**2) { found=i; break; }
      }
      if (found < 0) break;
      const combined = merge(groups[found],groups.pop());
      groups.splice(found,1);
      if (combined.buy !== combined.sell) groups.push(combined);
    }
    if (event.buy !== event.sell) groups.push({...event,weight:event.buy+event.sell});
  }
  return groups;
}

// tF uses a truncated 32-bit fixed-point ratio (not floating-point division).
export function reducedRow(index, sourceRows, rows) {
  return Math.max(0,Math.min(rows-1,Math.floor(index*Math.floor(rows*2**32/sourceRows)/2**32)));
}

export function reduceHeatRows(input, rows) {
  const out = new Float64Array(rows);
  for (let i=0;i<input.length;i++) out[reducedRow(i,input.length,rows)] += input[i];
  return out;
}

export function heatPrecision(sigma) {
  if (sigma < 0.01) return 1;
  const width = Math.max(1,Math.round(sigma*1.66));
  return 2**Math.min(6,Math.max(1,Math.floor(Math.log2(width))+1));
}

// Ascending price rows: extend into each side at the BBO, zero-pad the outer
// viewport boundaries. Reverse for the original filter's top-to-bottom passes.
export function filterBookColumn(input, sigma, bid, ask, clipDepth = false) {
  const precision = heatPrecision(sigma), n=input.length;
  const out=Float64Array.from(input,v=>Math.round(v)*precision);
  const apply=(lo,hi,left,right)=>{
    if(hi<=lo) return;
    const filtered=filterHeatColumn(out.slice(lo,hi).reverse(),sigma,left,right,n-hi).reverse();
    out.set(filtered,lo);
  };
  apply(Math.max(0,ask),n,false,true);
  apply(0,Math.min(n,Math.max(0,bid+1)),true,false);
  if(clipDepth) {
    const [lo,hi]=activeHeatBounds(input);
    out.fill(0,0,lo); out.fill(0,hi+1);
  }
  return out;
}

export function activeHeatBounds(values) {
  let lo=0,hi=values.length-1;
  while(lo<=hi && !values[lo]) lo++;
  while(hi>=lo && !values[hi]) hi--;
  return [lo,hi];
}

// nD's COB uses the native-price lazy column, then removes fixed-point bits.
// nv's SVP filters each side without those bits or edge replication. Inputs and
// outputs are integer exchange lots; callers retain the originals for numbers.
export function profileVolumes(first, second, {sigma, book = false, bid = -1, ask = first.length}) {
  if (book) {
    const total = Float64Array.from(first,(v,i)=>v+second[i]);
    const filtered = filterBookColumn(total,sigma,bid,ask,true);
    const precision = heatPrecision(sigma);
    return {
      first: Float64Array.from(filtered,(v,i)=>i<=bid ? Math.trunc(v/precision) : 0),
      second: Float64Array.from(filtered,(v,i)=>i>bid ? Math.trunc(v/precision) : 0),
    };
  }
  const filter = values => filterHeatColumn(Float64Array.from(values).reverse(),sigma,false,false).reverse();
  return {first:filter(first),second:filter(second)};
}

// Executed Dz/Do Java renderer rules: 60% shrink threshold, 20% headroom,
// followed by DS's rounded scale. Native OpenGL scaling is not independently run.
const PROFILE_STEPS = [2,5,10,25,50,100,200,500,1000,2000,5000,10000,
  ...Array.from({length:14},(_,i)=>[5*10**(i+4),10**(i+5)]).flat()];
export function profileScale(maximum, previous = 5) {
  if (maximum >= previous*.6 && maximum <= previous) return previous;
  const value = Math.max(5,Math.trunc(maximum*1.2));
  const target = Math.fround(Math.fround(value)*Math.fround(.1));
  const step = PROFILE_STEPS.find(v=>target<Math.fround(v));
  return step ? Math.ceil(value/step)*step : value;
}

export function filterHeatColumn(input, sigma, extendLeft = true, extendRight = true, offset = 0) {
  const values = Float64Array.from(input);
  if (sigma < 0.01 || !values.length) return values;
  const left = extendLeft ? values[0] : 0, right = extendRight ? values[values.length - 1] : 0;
  const at = i => i < 0 ? left : i >= values.length ? right : values[i];
  if (sigma < 5) {
    const f = Math.fround, s = f(sigma), radius = Math.round(2.5 * s);
    const weights = new Float32Array(2 * radius + 1);
    let total = 0;
    for (let i = 0; i < weights.length; i++) {
      weights[i] = Math.exp(-0.5 * (radius - i) ** 2 / f(s * s));
      total = f(total + weights[i]);
    }
    for (let i = 0; i < weights.length; i++) weights[i] = weights[i] / total;
    const out = new Float64Array(values.length);
    for (let i = 0; i < out.length; i++) {
      let sum = 0;
      for (let j = 0; j < weights.length; j++) sum += f(weights[j] * f(at(i - j + radius)));
      out[i] = Math.round(sum);
    }
    return out;
  }
  const width = Math.max(1, Math.round(sigma * 1.66)), ring = new Float64Array(width);
  for (let pass = 0; pass < 4; pass++) {
    let next = -Math.trunc(width / 2) + (width % 2 === 0 && pass % 2 === 0 ? 1 : 0), sum = 0;
    for (let j = 0; j < width; j++) { ring[j] = at(next++); sum += ring[j]; }
    let cursor = offset % width;
    for (let i = 0; i < values.length; i++) {
      values[i] = Math.trunc(sum / width);
      cursor = (cursor + 1) % width;
      const value = at(next++);
      sum += value - ring[cursor]; ring[cursor] = value;
    }
  }
  return values;
}

class Heap {
  constructor(compare) { this.items = []; this.compare = compare; }
  push(value) {
    const a = this.items; let i = a.length; a.push(value);
    while (i) { const p = (i - 1) >> 1; if (this.compare(a[p], value) <= 0) break; a[i] = a[p]; i = p; }
    a[i] = value;
  }
  pop() {
    const a = this.items, first = a[0], last = a.pop();
    if (a.length) {
      let i = 0;
      while (2 * i + 1 < a.length) {
        let c = 2 * i + 1;
        if (c + 1 < a.length && this.compare(a[c + 1], a[c]) < 0) c++;
        if (this.compare(last, a[c]) <= 0) break;
        a[i] = a[c]; i = c;
      }
      a[i] = last;
    }
    return first;
  }
  get first() { return this.items[0]; }
}

// Exact nearest neighbours; lazy deletion and periodic rebuilding keep merging
// from turning this into an all-pairs scan. Tie breaks are stable, unlike the
// reference's shuffled tree insertion order.
class Neighbours {
  constructor(points) { this.points = points; this.rebuild(); }
  rebuild() {
    this.points = this.points.filter(p => p.active);
    const build = (points, axis = 'x') => {
      if (!points.length) return null;
      points.sort((a, b) => a[axis] - b[axis] || a.id - b.id);
      const mid = points.length >> 1, next = axis === 'x' ? 'y' : 'x';
      return { p: points[mid], axis, left: build(points.slice(0, mid), next), right: build(points.slice(mid + 1), next) };
    };
    this.root = build([...this.points]); this.added = 0;
  }
  add(p) {
    this.points.push(p);
    let n = this.root;
    if (!n) { this.root = { p, axis: 'x', left: null, right: null }; return; }
    while (true) {
      const side = p[n.axis] < n.p[n.axis] ? 'left' : 'right';
      if (n[side]) n = n[side];
      else { n[side] = { p, axis: n.axis === 'x' ? 'y' : 'x', left: null, right: null }; break; }
    }
    if (++this.added >= 128) this.rebuild();
  }
  nearest(p, count) {
    const best = [];
    const visit = n => {
      if (!n) return;
      const delta = p[n.axis] - n.p[n.axis];
      visit(delta < 0 ? n.left : n.right);
      if (n.p.active && n.p !== p) {
        const d = (p.x - n.p.x) ** 2 + (p.y - n.p.y) ** 2;
        const worst = best[best.length - 1];
        if (best.length < count || d < worst.d || (d === worst.d && n.p.id < worst.p.id)) {
          let i = best.length;
          while (i && (best[i-1].d > d || (best[i-1].d === d && best[i-1].p.id > n.p.id))) i--;
          best.splice(i,0,{p:n.p,d});
          if (best.length > count) best.pop();
        }
      }
      if (best.length < count || delta * delta <= best[best.length - 1].d) visit(delta < 0 ? n.right : n.left);
    };
    visit(this.root); return best.map(v => v.p);
  }
}

export function smartClusters(input, { size = 40, clustering = 52, volume = p => Math.abs(p.buy - p.sell) } = {}) {
  let id = 0;
  const make = p => {
    const out = { ...p, id: id++, active: true };
    out.weight = p.weight ?? p.buy + p.sell;
    out.volume = volume(out);
    out.unitRadius = Math.cbrt(out.volume) / 2;
    return out;
  };
  const points = input.map(make), index = new Neighbours(points);
  const merge = (a, b) => {
    a.active = b.active = false;
    const weight = a.weight + b.weight;
    const p = make({ x: Math.round((a.x * a.weight + b.x * b.weight) / weight),
      y: (a.y * a.weight + b.y * b.weight) / weight,
      tick: ((a.tick ?? 0) * a.weight + (b.tick ?? 0) * b.weight) / weight,
      buy: a.buy + b.buy, sell: a.sell + b.sell, weight });
    index.add(p); return p;
  };
  const parameter = smartParameter(clustering), minimumDistance = 6 * parameter;
  const queue = [];
  const addNear = p => {
    const q = index.nearest(p, 1)[0];
    if (q && Math.hypot(p.x - q.x, p.y - q.y) < minimumDistance) queue.push([p, q]);
  };
  [...points].sort((a, b) => a.x - b.x || a.y - b.y).forEach(addNear);
  for (let i = 0; i < queue.length; i++) {
    const [a, b] = queue[i]; if (a.active && b.active) addNear(merge(a, b));
  }
  const candidates = new Heap((a, b) => a.score - b.score || a.a.id - b.a.id || a.b.id - b.b.id);
  const largest = new Heap((a, b) => b.volume - a.volume || a.id - b.id);
  const target = dotTargetRadius(size);
  const currentScale = () => {
    while (largest.first && !largest.first.active) largest.pop();
    return largest.first?.volume > 0 ? target / largest.first.unitRadius : Infinity;
  };
  const score = (a, b) => {
    let large = Math.max(0.1, a.unitRadius * parameter), small = Math.max(0.1, b.unitRadius * parameter);
    if (large < small) [large, small] = [small, large];
    const distance = Math.hypot(a.x - b.x, a.y - b.y), minimum = 3 * parameter;
    if (distance < 2 * minimum) return -1 / distance;
    return distance < minimum * (large + small) / small ? (distance - minimum) / large : distance / (large + small);
  };
  const addPairs = a => index.nearest(a, 10).forEach(b => candidates.push({ a, b, score: score(a, b) }));
  for (const p of index.points) if (p.active) { largest.push(p); addPairs(p); }
  while (candidates.first) {
    const pair = candidates.pop();
    if (!pair.a.active || !pair.b.active) continue;
    if (currentScale() < pair.score) break;
    const p = merge(pair.a, pair.b); largest.push(p); addPairs(p);
  }
  const scale = currentScale();
  const clusters = index.points.filter(p => p.active).map(p => ({ ...p,
    radius: p.volume > 0 ? Math.max(3, p.unitRadius * scale) : 0 }));
  return { clusters, scale: Number.isFinite(scale) ? scale : 0 };
}

// Bookmap's histogram: exact integer bins through 4095, then 256 bins/doubling.
export function heatBin(value) {
  if (value < 4096) return Math.max(0, Math.floor(value));
  const exponent = Math.floor(Math.log2(value));
  return 4096 + (exponent - 12) * 256 + Math.floor((value - 2 ** exponent) / 2 ** (exponent - 8));
}
export function heatBinValue(index) {
  if (index < 4096) return index;
  const n = index - 4096, exponent = 12 + Math.floor(n / 256);
  return 2 ** exponent + n % 256 * 2 ** (exponent - 8);
}

export function heatTransfer(values, { lower = 15, upper = 96, contrast = 0, brightness = 100, quantum = 1 } = {}) {
  let max = 0;
  for (const v of values) max = Math.max(max, v / quantum);
  const length = heatBin(max) + 1, counts = new Float64Array(length);
  for (const v of values) counts[heatBin(v / quantum)]++;
  let distinct = 0; for (const c of counts) if (c) distinct++;
  let lo = 0, hi = length - 1;
  let lowRemainder = Math.trunc(values.length * lower / 100), highRemainder = Math.trunc(values.length * (100 - upper) / 100);
  if (distinct > 5) {
    while (lowRemainder >= 0 && lo < length) { lowRemainder -= counts[lo]; if (lowRemainder >= 0) lo++; }
    lo = Math.min(lo, length - 1);
    while (highRemainder >= 0 && hi >= 0) { highRemainder -= counts[hi]; if (highRemainder >= 0) hi--; }
  }
  hi = Math.max(hi, lo + 1);
  const clipped = new Float64Array(hi + 1);
  for (let i = lo; i <= Math.min(hi, length - 1); i++) clipped[i] = counts[i];
  // Endpoint count corrections also apply when sparse histograms skip trimming.
  clipped[lo] -= lowRemainder; clipped[hi] -= highRemainder;
  let first = 0; while (first < clipped.length && !clipped[first]) first++;
  if (first === clipped.length) first = 0;
  const last = clipped.length - 1, cdf = new Float64Array(clipped.length + 1);
  let total = 0; for (const c of clipped) total += c;
  let cumulative = 0;
  for (let i = 0; i < clipped.length; i++) {
    cdf[i] = total > 0 ? cumulative / total : 0;
    cumulative += clipped[i];
  }
  cdf[clipped.length] = 1;
  const cmin = cdf[first] + cdf[first + 1], cmax = cdf[last] + cdf[last + 1];
  const min = heatBinValue(first), maxValue = heatBinValue(last), mix = Math.max(0, Math.min(1, contrast / 100));
  const lut = new Float32Array(Math.max(length, clipped.length));
  for (let i = 0; i < lut.length; i++) {
    let value;
    if (i >= clipped.length) {
      const span = heatBinValue(lut.length - 1) - maxValue;
      value = brightness / 100 + (1 - brightness / 100) * (span > 0 ? (heatBinValue(i) - maxValue) / span : 1);
    } else if (i < first) value = 0;
    else {
      const linear = maxValue === min ? 0.5 : Math.min(1, (heatBinValue(i) - min) / (maxValue - min));
      const rank = cmax === cmin ? 0.5 : (cdf[i] + cdf[i + 1] - cmin) / (cmax - cmin);
      value = (linear * (1 - mix) + rank * mix) * (brightness / 100);
    }
    lut[i] = Math.trunc(Math.max(0, Math.min(1, value)) * 255) / 255;
  }
  return { lut, quantum, lo: min * quantum, hi: maxValue * quantum };
}
