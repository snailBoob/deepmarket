import { filterHeatColumn, filterBookColumn, heatPrecision } from './orderflow.js';

// Bookmap2 palette control points, before dimming.
export const GRADIENT = [
  [0, [26,34,38]], [0.45045045045045046, [0,140,204]],
  [0.6246246246246246, [237,237,237]], [0.7687687687687688, [255,255,0]],
  [0.8778778778778779, [255,155,0]], [0.958958958958959, [255,0,0]],
  [1.0000200000000001, [150,0,0]],
];
export const BG = [26,34,38];

// Auto switches off at 3 * sigma pixels. Explicit sigma=0 also disables filtering.
let lastKernel;
export function smoothingKernel(rowPx, nrows, sigmaPx = 5, auto = true) {
  if (lastKernel?.rowPx === rowPx && lastKernel.nrows === nrows && lastKernel.sigmaPx === sigmaPx && lastKernel.auto === auto) return lastKernel;
  // FI receives a Java float: round the input BEFORE Auto's threshold/division.
  const height = Math.fround(rowPx), width = Math.fround(sigmaPx);
  const sigma = width && (!auto || height < Math.fround(3 * width)) ? Math.fround(width / height) : 0;
  return lastKernel = { rowPx, nrows, sigmaPx, sigma, auto };
}

const VS = `#version 300 es
in vec2 a_pos;
void main() { gl_Position = vec4(a_pos, 0.0, 1.0); }`;
const FS = `#version 300 es
precision highp float;
precision highp int;
uniform highp sampler2D u_tex;
uniform sampler2D u_grad;
uniform highp sampler2D u_transfer;
uniform int u_transferSize;
uniform float u_quantum;
uniform bool u_hasTransfer;
uniform vec2 u_origin, u_size;
uniform float u_nowX, u_colPx, u_rowPx, u_shift;
uniform int u_head, u_ncols, u_nrows, u_valid;
uniform vec3 u_bg;
uniform bool u_future, u_lines;
uniform float u_fade, u_scaleLo, u_scaleHi, u_gridX0, u_gridStep, u_dpr;
out vec4 outColor;

vec3 finish(vec3 c, float x, float y) {
  if (u_lines && mod(y, u_rowPx) < u_dpr) c = mix(c, vec3(0.13,0.16,0.19),0.35);
  if (u_gridStep > 0.0 && mod(x-u_gridX0,u_gridStep) < u_dpr && mod(y,6.0*u_dpr) < 2.0*u_dpr)
    c = mix(c,vec3(0.82,0.86,0.9),0.45);
  return c;
}
int bin(float value) {
  if (value < 4096.0) return max(0,int(floor(value)));
  float exponent = floor(log2(value));
  return 4096 + int(exponent-12.0)*256 + int(floor((value-exp2(exponent))/exp2(exponent-8.0)));
}
void main() {
  float x = gl_FragCoord.x-u_origin.x;
  float y = u_size.y-(gl_FragCoord.y-u_origin.y);
  int row = u_nrows-1-int(floor(y/u_rowPx));
  float xr = u_nowX-u_shift*u_colPx;
  int j = x >= xr ? u_ncols-1 : u_ncols-1-int(floor((xr-x)/u_colPx));
  if (row < 0 || row >= u_nrows || u_valid == 0 || j < u_ncols-u_valid || (x >= xr && !u_future)) {
    outColor = vec4(finish(u_bg,x,y),1.0); return;
  }
  float value = texelFetch(u_tex,ivec2(row,(u_head+j)%u_ncols),0).r;
  float level;
  if (u_hasTransfer) {
    int index = bin(value/u_quantum);
    level = index >= u_transferSize ? 1.0 : texelFetch(u_transfer,ivec2(index,0),0).r;
  } else {
    level = clamp((value-u_scaleLo)/max(0.000001,u_scaleHi-u_scaleLo),0.0,1.0);
  }
  vec3 c = texture(u_grad,vec2(level*255.0/256.0+0.5/256.0,0.5)).rgb;
  if (u_fade > 0.0) {
    vec3 zero = texelFetch(u_grad,ivec2(0,0),0).rgb;
    c = max(zero,mix(c,zero,u_fade));
  }
  outColor = vec4(finish(c,x,y),1.0);
}`;

function gradientPixels(stops) {
  const px = new Uint8Array(1024);
  for (let i = 0; i < 256; i++) {
    const t = i / 255;
    let k = 0;
    while (k < stops.length-2 && t > stops[k+1][0]) k++;
    const [t0,c0] = stops[k], [t1,c1] = stops[k+1];
    const f = Math.min(1,Math.max(0,(t-t0)/(t1-t0)));
    for (let ch = 0; ch < 3; ch++) px[i*4+ch] = Math.trunc(c0[ch]+(c1[ch]-c0[ch])*f);
    px[i*4+3] = 255;
  }
  return px;
}

export class Heatmap {
  constructor(canvas) {
    const gl = canvas.getContext('webgl2',{antialias:false,premultipliedAlpha:false});
    if (!gl) throw new Error('WebGL2 is niet beschikbaar in deze browser');
    this.gl = gl; this.canvas = canvas;
    this.prog = this._program(VS,FS); this.loc = {};
    for (const name of ['tex','grad','transfer','transferSize','quantum','hasTransfer','origin','size','nowX',
      'colPx','rowPx','shift','head','ncols','nrows','valid','bg','future','lines','fade','scaleLo','scaleHi',
      'gridX0','gridStep','dpr']) this.loc[name] = gl.getUniformLocation(this.prog,`u_${name}`);
    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER,buffer);
    gl.bufferData(gl.ARRAY_BUFFER,new Float32Array([-1,-1,1,-1,-1,1,1,1]),gl.STATIC_DRAW);
    this.vao = gl.createVertexArray(); gl.bindVertexArray(this.vao);
    const pos = gl.getAttribLocation(this.prog,'a_pos');
    gl.enableVertexAttribArray(pos); gl.vertexAttribPointer(pos,2,gl.FLOAT,false,0,0);
    this.tex = this._texture(); this.transferTex = this._texture(); this.gradTex = this._texture();
    this.setGradient(GRADIENT);
    this.ncols = this.nrows = 0;
  }
  _texture() {
    const gl = this.gl, tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D,tex);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_S,gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_WRAP_T,gl.CLAMP_TO_EDGE);
    return tex;
  }
  _program(vs,fs) {
    const gl = this.gl;
    const shader = (type,source) => {
      const s = gl.createShader(type); gl.shaderSource(s,source); gl.compileShader(s);
      if (!gl.getShaderParameter(s,gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p,shader(gl.VERTEX_SHADER,vs)); gl.attachShader(p,shader(gl.FRAGMENT_SHADER,fs));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p,gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    return p;
  }
  setGradient(stops) {
    const gl = this.gl; gl.bindTexture(gl.TEXTURE_2D,this.gradTex);
    gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA8,256,1,0,gl.RGBA,gl.UNSIGNED_BYTE,gradientPixels(stops));
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MAG_FILTER,gl.LINEAR);
  }
  setGrid(ncols,nrows,data,boundaries=null) {
    this.ncols = ncols; this.nrows = nrows;
    this.raw = Float32Array.from(data); this.filtered = new Float32Array(data.length);
    this.boundaries = boundaries;
    this.filterKey = null;
  }
  _column(slot) {
    const {quantum,usdt,p0,pstep,sigma} = this.filterOptions;
    const start = slot*this.nrows;
    const lots = Float64Array.from(this.raw.subarray(start,start+this.nrows),v => Math.round(v/quantum));
    const boundary=this.boundaries?.[slot];
    const column = boundary ? filterBookColumn(lots,sigma,...boundary,true) : filterHeatColumn(lots,sigma);
    const precision = boundary ? heatPrecision(sigma) : 1;
    for (let r = 0; r < this.nrows; r++) this.filtered[start+r] = column[r]*quantum/precision*(usdt ? p0+r*pstep : 1);
    return this.filtered.subarray(start,start+this.nrows);
  }
  // CPU normalization and GPU colours consume the exact same filtered columns.
  // Only new ring slots are filtered during live updates; zoom/unit changes rebuild.
  prepare(smoothing,options = {}) {
    const settings = {sigma:smoothing.sigma,quantum:options.quantum || 1,
      usdt:!!options.usdt,p0:options.p0 || 0,pstep:options.pstep || 0};
    const key = JSON.stringify(settings);
    if (this.filterKey === key) return this.filtered;
    this.filterOptions = settings; this.filterKey = key;
    for (let slot = 0; slot < this.ncols; slot++) this._column(slot);
    const gl = this.gl; gl.bindTexture(gl.TEXTURE_2D,this.tex); gl.pixelStorei(gl.UNPACK_ALIGNMENT,1);
    gl.texImage2D(gl.TEXTURE_2D,0,gl.R32F,this.nrows,this.ncols,0,gl.RED,gl.FLOAT,this.filtered);
    return this.filtered;
  }
  setColumn(slot,col,boundary=null) {
    this.raw.set(col,slot*this.nrows);
    if(this.boundaries && boundary) this.boundaries[slot]=boundary;
    if (!this.filterKey) return;
    const data = this._column(slot), gl = this.gl;
    gl.bindTexture(gl.TEXTURE_2D,this.tex); gl.pixelStorei(gl.UNPACK_ALIGNMENT,1);
    gl.texSubImage2D(gl.TEXTURE_2D,0,0,slot,this.nrows,1,gl.RED,gl.FLOAT,data);
  }
  draw(p) {
    const gl = this.gl, H = this.canvas.height;
    gl.viewport(0,0,this.canvas.width,H); gl.disable(gl.SCISSOR_TEST);
    gl.clearColor(...BG.map(v=>v/255),1); gl.clear(gl.COLOR_BUFFER_BIT);
    if (!this.ncols || !p.w || !p.h) return;
    this.prepare(p.smoothing,p);
    const oy = H-p.y-p.h, clip = p.clip || p;
    gl.enable(gl.SCISSOR_TEST); gl.scissor(clip.x,H-clip.y-clip.h,clip.w,clip.h);
    gl.viewport(p.x,oy,p.w,p.h); gl.useProgram(this.prog); gl.bindVertexArray(this.vao);
    for (const [i,name,tex] of [[0,'tex',this.tex],[1,'grad',this.gradTex],[2,'transfer',this.transferTex]]) {
      gl.activeTexture(gl.TEXTURE0+i); gl.bindTexture(gl.TEXTURE_2D,tex); gl.uniform1i(this.loc[name],i);
    }
    const transfer = p.transfer;
    const lut = transfer?.lut || new Float32Array([0]);
    if (this.transferValues !== lut) {
      gl.texImage2D(gl.TEXTURE_2D,0,gl.R32F,lut.length,1,0,gl.RED,gl.FLOAT,lut);
      this.transferValues = lut;
    }
    gl.uniform1i(this.loc.hasTransfer,transfer ? 1 : 0);
    gl.uniform1i(this.loc.transferSize,lut.length);
    gl.uniform1f(this.loc.quantum,transfer?.quantum || 1);
    gl.uniform2f(this.loc.origin,p.x,oy); gl.uniform2f(this.loc.size,p.w,p.h);
    gl.uniform3f(this.loc.bg,...BG.map(v=>v/255));
    for (const [name,value] of Object.entries({nowX:p.nowX,colPx:p.colPx,rowPx:p.rowPx,shift:p.shift,
      fade:p.fade || 0,scaleLo:p.scaleLo || 0,scaleHi:p.scaleHi || 1,gridX0:p.gridX0 || 0,
      gridStep:p.gridStep || 0,dpr:p.dpr || 1})) gl.uniform1f(this.loc[name],value);
    for (const [name,value] of Object.entries({head:p.head,ncols:this.ncols,nrows:this.nrows,valid:p.valid,
      future:p.future === false ? 0 : 1,lines:p.lines ? 1 : 0})) gl.uniform1i(this.loc[name],value);
    gl.drawArrays(gl.TRIANGLE_STRIP,0,4);
  }
}
