// WebGL2 heatmap. Data lives in an R32F texture used as a ring buffer of columns:
// texture width = price rows, height = time columns. The colour mapping happens in the shader,
// so brightness/contrast/gradient changes cost nothing.

const GAUSSIAN_EXTENT = 4; // keep discrete samples through at least four sigma

const VS = `#version 300 es
in vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }`;

const FS = `#version 300 es
precision highp float;
precision highp int;
uniform highp sampler2D u_tex;   // R32F, width = nrows, height = ncols
uniform sampler2D u_grad;        // 256x1 colour gradient
uniform vec2 u_origin;           // heat area origin in device px (bottom-left, GL coords)
uniform vec2 u_size;             // heat area size in device px
uniform float u_nowX;            // now-line x in device px from the left of the heat area
uniform float u_colPx;
uniform float u_rowPx;
uniform float u_shift;           // 0..1: how far time has moved past the newest column
uniform int u_head;              // ring index of the oldest column
uniform int u_ncols;
uniform int u_nrows;
uniform int u_valid;             // number of valid columns (newest ones)
uniform float u_scaleLo;         // lower percentile expressed as a quantity
uniform float u_scaleHi;         // upper percentile expressed as a quantity
uniform vec3 u_bg;
uniform bool u_future;           // extend the newest column to the right (live view)
uniform float u_fade;            // 0 = full colour; 1 = background only
uniform float u_brightness;      // 0..1, separate from the percentile cut-offs
uniform float u_contrast;        // -100..100; 0 leaves the quantity transfer unchanged
uniform float u_gridX0;          // x of one vertical time grid line (device px, viewport coords)
uniform float u_gridStep;        // distance between grid lines (device px); 0 = none
uniform float u_dpr;
uniform highp sampler2D u_kernel; // exact Float32 weights also used by the CPU scale
uniform int u_radius;
uniform bool u_lines;            // thin lines between price levels
uniform bool u_usdt;             // colour by size x price (USDT) instead of size in coins
uniform float u_p0;              // price of row 0 (its middle)
uniform float u_pstep;           // price difference between rows
out vec4 outColor;

vec3 levelLines(vec3 c, float yTop) {
  if (u_lines && mod(yTop, u_rowPx) < u_dpr) return mix(c, vec3(0.13, 0.16, 0.19), 0.35);
  return c;
}


// Dotted vertical time grid, drawn here instead of on the 2D canvas (much cheaper).
vec3 grid(vec3 c, float x, float yTop) {
  if (u_gridStep <= 0.0) return c;
  float d = mod(x - u_gridX0, u_gridStep);
  if (d < u_dpr && mod(yTop, 6.0 * u_dpr) < 2.0 * u_dpr) return mix(c, vec3(0.82, 0.86, 0.9), 0.45);
  return c;
}

// Level lines first, then the time grid on top. (Declared after grid(): GLSL needs a function
// to be declared before it is used.)
vec3 finish(vec3 c, float x, float yTop) { return grid(levelLines(c, yTop), x, yTop); }

// Filter discrete levels, then colour the entire destination row uniformly.
// Wide rows keep sharp edges; closer levels share more weight at fixed sigma.
// Renormalize at the texture boundary so uniform liquidity remains uniform.
float smoothRow(int center, int tr) {
  int first = max(0, center - u_radius);
  int last = min(u_nrows - 1, center + u_radius);
  float acc = 0.0, total = 0.0;
  for (int r = first; r <= last; r++) {
    float weight = texelFetch(u_kernel, ivec2(abs(r - center), 0), 0).r;
    float value = texelFetch(u_tex, ivec2(r, tr), 0).r;
    if (u_usdt) value *= u_p0 + float(r) * u_pstep;
    acc += weight * value;
    total += weight;
  }
  return acc / total;
}

void main() {
  float x = gl_FragCoord.x - u_origin.x;
  float yTop = u_size.y - (gl_FragCoord.y - u_origin.y);
  int rowFromTop = int(floor(yTop / u_rowPx));
  int r = u_nrows - 1 - rowFromTop;
  if (r < 0 || r >= u_nrows || u_valid == 0) { outColor = vec4(finish(u_bg, x, yTop), 1.0); return; }

  float xr = u_nowX - u_shift * u_colPx;   // right edge of the newest column
  int j;
  if (x >= xr) {
    if (!u_future) { outColor = vec4(finish(u_bg, x, yTop), 1.0); return; }
    j = u_ncols - 1;                       // future area: extend the current book
  } else {
    int k = int(floor((xr - x) / u_colPx));
    j = u_ncols - 1 - k;
  }
  if (j < u_ncols - u_valid) { outColor = vec4(finish(u_bg, x, yTop), 1.0); return; }
  int tr = (u_head + j) % u_ncols;
  float v = smoothRow(r, tr);
  if (v <= u_scaleLo) { outColor = vec4(finish(u_bg, x, yTop), 1.0); return; }
  // Percentiles choose the quantity bounds, not each cell's colour rank. Linear
  // interpolation preserves size differences even when small sizes are repeated.
  // Normalize quantities within the selected bounds.
  float n = clamp((v - u_scaleLo) / (u_scaleHi - u_scaleLo), 0.0, 1.0);
  // Symmetric contrast curve: keeps both endpoints, neutral at zero.
  float power = exp2(u_contrast / 50.0);
  float a = pow(n, power);
  n = a / (a + pow(1.0 - n, power));
  vec3 c = texture(u_grad, vec2(n * (255.0 / 256.0) + 0.5 / 256.0, 0.5)).rgb;
  // Dimming applies to every band, including the largest walls. Grid and overlays
  // remain readable; at the maximum no liquidity colour remains above the background.
  c = mix(u_bg, c, u_brightness * (1.0 - u_fade));
  outColor = vec4(finish(c, x, yTop), 1.0);
}`;

// Ordered sRGB samples for the heatmap texture. Preserve each colour's position;
// quantity normalization, contrast and dimming are applied separately by the shader.
export const GRADIENT = [
  [26, 34, 38], [26, 34, 38], [26, 35, 39], [26, 35, 41], [26, 36, 41], [26, 37, 43],
  [26, 37, 43], [25, 38, 45], [26, 39, 46], [26, 40, 47], [26, 41, 48], [26, 41, 49],
  [26, 41, 50], [25, 42, 51], [25, 42, 52], [26, 43, 53], [26, 44, 54], [26, 44, 55],
  [25, 46, 56], [27, 45, 56], [27, 45, 58], [26, 47, 58], [26, 48, 59], [26, 49, 61],
  [26, 49, 62], [27, 50, 63], [27, 50, 64], [26, 51, 65], [26, 52, 66], [26, 52, 67],
  [25, 53, 69], [26, 54, 70], [26, 54, 70], [26, 55, 71], [26, 55, 72], [26, 56, 72],
  [27, 57, 73], [27, 57, 74], [26, 58, 76], [26, 59, 77], [26, 59, 78], [27, 60, 79],
  [26, 61, 80], [26, 61, 81], [26, 62, 82], [27, 63, 83], [27, 63, 84], [26, 64, 85],
  [26, 65, 85], [26, 65, 87], [26, 65, 87], [27, 66, 89], [26, 67, 89], [26, 68, 91],
  [26, 68, 92], [27, 69, 93], [26, 70, 94], [26, 70, 95], [25, 71, 96], [27, 72, 97],
  [27, 72, 98], [27, 72, 98], [26, 73, 100], [25, 74, 100], [25, 74, 101], [26, 75, 102],
  [26, 76, 105], [26, 76, 105], [26, 78, 106], [26, 78, 107], [25, 79, 108], [27, 80, 109],
  [27, 80, 110], [27, 80, 111], [26, 81, 112], [26, 81, 113], [25, 82, 114], [26, 83, 115],
  [26, 83, 115], [26, 84, 117], [25, 85, 117], [25, 85, 119], [26, 86, 119], [25, 87, 121],
  [27, 88, 123], [27, 88, 123], [26, 89, 124], [26, 89, 125], [25, 90, 126], [26, 91, 127],
  [26, 91, 128], [25, 92, 129], [27, 93, 130], [27, 93, 131], [26, 94, 132], [26, 94, 132],
  [25, 95, 134], [26, 96, 134], [26, 96, 135], [25, 97, 136], [27, 98, 137], [27, 98, 138],
  [26, 99, 139], [26, 99, 141], [26, 99, 142], [27, 100, 143], [26, 101, 144], [26, 101, 145],
  [27, 102, 146], [27, 104, 146], [27, 106, 147], [26, 107, 147], [26, 109, 149], [31, 111, 150],
  [34, 113, 149], [37, 114, 150], [41, 115, 150], [45, 117, 150], [47, 119, 151], [52, 120, 152],
  [54, 122, 152], [57, 123, 152], [61, 124, 153], [65, 126, 153], [67, 127, 154], [71, 128, 154],
  [75, 130, 155], [79, 131, 156], [86, 134, 156], [89, 135, 157], [92, 137, 157], [96, 138, 158],
  [99, 140, 158], [103, 141, 159], [106, 143, 159], [110, 144, 160], [112, 145, 160], [117, 146, 161],
  [121, 148, 160], [124, 150, 162], [127, 151, 162], [131, 152, 163], [134, 154, 163], [141, 158, 164],
  [144, 158, 165], [148, 159, 165], [151, 161, 164], [156, 163, 166], [159, 164, 167], [161, 165, 167],
  [162, 166, 164], [162, 166, 159], [163, 167, 156], [163, 167, 152], [163, 167, 147], [163, 167, 143],
  [164, 168, 139], [164, 168, 135], [165, 169, 125], [165, 169, 122], [165, 169, 118], [166, 170, 112],
  [166, 170, 110], [167, 171, 106], [167, 171, 102], [167, 171, 97], [167, 171, 93], [168, 172, 89],
  [168, 172, 84], [170, 172, 80], [170, 172, 76], [169, 173, 72], [169, 173, 67], [170, 174, 60],
  [172, 174, 55], [172, 174, 52], [171, 175, 46], [171, 175, 42], [172, 176, 38], [172, 176, 38],
  [174, 176, 38], [174, 176, 38], [173, 177, 37], [173, 177, 37], [173, 177, 37], [175, 175, 38],
  [174, 172, 38], [175, 171, 38], [175, 166, 39], [175, 163, 38], [176, 160, 39], [175, 159, 37],
  [175, 156, 39], [174, 154, 38], [175, 152, 39], [175, 149, 38], [174, 147, 39], [175, 145, 38],
  [175, 143, 38], [175, 140, 38], [175, 138, 39], [175, 135, 38], [174, 133, 38], [175, 128, 38],
  [175, 126, 37], [175, 124, 38], [175, 121, 37], [175, 119, 38], [174, 118, 38], [175, 115, 37],
  [175, 111, 38], [174, 107, 38], [175, 101, 38], [174, 97, 37], [175, 92, 39], [175, 87, 38],
  [175, 82, 38], [175, 78, 38], [175, 68, 39], [175, 63, 38], [175, 57, 39], [174, 53, 38],
  [175, 48, 38], [175, 44, 37], [175, 40, 38], [175, 34, 38], [175, 34, 38], [175, 34, 38],
  [175, 34, 38], [175, 34, 38], [172, 35, 38], [165, 35, 38], [159, 34, 39], [146, 34, 39],
  [139, 34, 38], [133, 34, 38], [126, 34, 38], [121, 33, 38], [113, 34, 37], [107, 34, 38],
].map((rgb, i, colours) => [i / (colours.length - 1), rgb]);
export const BG = [19, 26, 34];

// Sigma is in CSS pixels, converted to distance between discrete (aggregated)
// levels. No rectangle convolution or interpolation across a level's interior.
// Zero disables smoothing; the same parameter supports a future manual control.
// Cache stable geometry; zoom previews may have subpixel rows, so do not cap sigma.
let lastKernel;
export function smoothingKernel(rowPx, nrows, sigmaPx) {
  if (lastKernel?.rowPx === rowPx && lastKernel.nrows === nrows && lastKernel.sigmaPx === sigmaPx) return lastKernel;
  const sigma = sigmaPx / rowPx;
  const radius = Math.min(nrows - 1, Math.ceil(GAUSSIAN_EXTENT * sigma));
  const weights = new Float32Array(radius + 1);
  weights[0] = 1;
  for (let d = 1; d <= radius; d++) {
    weights[d] = Math.exp(-0.5 * (d / sigma) ** 2);
  }
  return lastKernel = { rowPx, nrows, sigmaPx, weights };
}

function gradientPixels(stops) {
  const px = new Uint8Array(256 * 4);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let k = 0;
    while (k < stops.length - 2 && t > stops[k + 1][0]) k++;
    const [t0, c0] = stops[k];
    const [t1, c1] = stops[k + 1];
    const f = Math.min(1, Math.max(0, (t - t0) / (t1 - t0)));
    for (let ch = 0; ch < 3; ch++) px[i * 4 + ch] = Math.round(c0[ch] + (c1[ch] - c0[ch]) * f);
    px[i * 4 + 3] = 255;
  }
  return px;
}

export class Heatmap {
  constructor(canvas) {
    const gl = canvas.getContext("webgl2", { antialias: false, premultipliedAlpha: false });
    if (!gl) throw new Error("WebGL2 is niet beschikbaar in deze browser");
    this.gl = gl;
    this.canvas = canvas;
    this.prog = this._program(VS, FS);
    this.loc = {};
    for (const n of ["u_tex", "u_grad", "u_origin", "u_size", "u_nowX", "u_colPx", "u_rowPx", "u_shift",
      "u_head", "u_ncols", "u_nrows", "u_valid", "u_scaleLo", "u_scaleHi", "u_bg", "u_future", "u_fade", "u_brightness", "u_contrast", "u_gridX0", "u_gridStep", "u_dpr", "u_kernel", "u_radius", "u_lines", "u_usdt", "u_p0", "u_pstep"]) {
      this.loc[n] = gl.getUniformLocation(this.prog, n);
    }
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
    this.vao = gl.createVertexArray();
    gl.bindVertexArray(this.vao);
    const a = gl.getAttribLocation(this.prog, "a_pos");
    gl.enableVertexAttribArray(a);
    gl.vertexAttribPointer(a, 2, gl.FLOAT, false, 0, 0);

    this.gradTex = gl.createTexture();
    this.setGradient(GRADIENT);
    this.kernelTex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, this.kernelTex);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    this.tex = gl.createTexture();
    this.ncols = 0;
    this.nrows = 0;
  }

  _program(vs, fs) {
    const gl = this.gl;
    const mk = (type, src) => {
      const s = gl.createShader(type);
      gl.shaderSource(s, src);
      gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, mk(gl.VERTEX_SHADER, vs));
    gl.attachShader(p, mk(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    return p;
  }

  setGradient(stops) {
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.gradTex);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA8, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, gradientPixels(stops));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }

  // Full upload: data = Float32Array(ncols * nrows), oldest column first.
  setGrid(ncols, nrows, data) {
    const gl = this.gl;
    this.ncols = ncols;
    this.nrows = nrows;
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, nrows, ncols, 0, gl.RED, gl.FLOAT, data);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }

  // Overwrite one ring slot with a new column.
  setColumn(slot, col) {
    const gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, slot, this.nrows, 1, gl.RED, gl.FLOAT, col);
  }

  // p: {x, y, w, h} heat area in device px (y from the top of the canvas; x/y may be offset
  //    while dragging), clip {x, y, w, h} visible area, nowX, colPx, rowPx, shift, head,
  //    valid, scaleLo, scaleHi, smoothing, future
  draw(p) {
    const gl = this.gl;
    const H = this.canvas.height;
    gl.viewport(0, 0, this.canvas.width, H);
    gl.clearColor(BG[0] / 255, BG[1] / 255, BG[2] / 255, 1);
    gl.disable(gl.SCISSOR_TEST);
    gl.clear(gl.COLOR_BUFFER_BIT);
    if (!this.ncols || !p.w || !p.h) return;
    const oy = H - (p.y + p.h); // GL origin is bottom-left
    const c = p.clip || p;
    gl.enable(gl.SCISSOR_TEST);
    gl.scissor(c.x, H - (c.y + c.h), c.w, c.h);
    gl.viewport(p.x, oy, p.w, p.h);
    gl.useProgram(this.prog);
    gl.bindVertexArray(this.vao);
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.tex);
    gl.uniform1i(this.loc.u_tex, 0);
    gl.activeTexture(gl.TEXTURE1);
    gl.bindTexture(gl.TEXTURE_2D, this.gradTex);
    gl.uniform1i(this.loc.u_grad, 1);
    gl.activeTexture(gl.TEXTURE2);
    gl.bindTexture(gl.TEXTURE_2D, this.kernelTex);
    const weights = p.smoothing.weights;
    if (this.kernelWeights !== weights) {
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.R32F, weights.length, 1, 0, gl.RED, gl.FLOAT, weights);
      this.kernelWeights = weights;
    }
    gl.uniform1i(this.loc.u_kernel, 2);
    gl.uniform1i(this.loc.u_radius, weights.length - 1);
    gl.uniform1f(this.loc.u_scaleLo, p.scaleLo);
    gl.uniform1f(this.loc.u_scaleHi, p.scaleHi);
    gl.uniform2f(this.loc.u_origin, p.x, oy);
    gl.uniform2f(this.loc.u_size, p.w, p.h);
    gl.uniform1f(this.loc.u_nowX, p.nowX);
    gl.uniform1f(this.loc.u_colPx, p.colPx);
    gl.uniform1f(this.loc.u_rowPx, p.rowPx);
    gl.uniform1f(this.loc.u_shift, p.shift);
    gl.uniform1i(this.loc.u_head, p.head);
    gl.uniform1i(this.loc.u_ncols, this.ncols);
    gl.uniform1i(this.loc.u_nrows, this.nrows);
    gl.uniform1i(this.loc.u_valid, p.valid);
    gl.uniform3f(this.loc.u_bg, BG[0] / 255, BG[1] / 255, BG[2] / 255);
    gl.uniform1i(this.loc.u_future, p.future === false ? 0 : 1);
    gl.uniform1f(this.loc.u_fade, p.fade || 0);
    gl.uniform1f(this.loc.u_brightness, p.brightness ?? 1);
    gl.uniform1f(this.loc.u_contrast, p.contrast || 0);
    gl.uniform1f(this.loc.u_gridX0, p.gridX0 || 0);
    gl.uniform1f(this.loc.u_gridStep, p.gridStep || 0);
    gl.uniform1f(this.loc.u_dpr, p.dpr || 1);
    gl.uniform1i(this.loc.u_lines, p.lines ? 1 : 0);
    gl.uniform1i(this.loc.u_usdt, p.usdt ? 1 : 0);
    gl.uniform1f(this.loc.u_p0, p.p0 || 0);
    gl.uniform1f(this.loc.u_pstep, p.pstep || 0);
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  }
}
