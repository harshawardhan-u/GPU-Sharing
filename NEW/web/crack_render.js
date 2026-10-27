// crack_render.js -- runs the SHA-256 brute-force search on this device's GPU
// (WebGL2). Given a target hash and a range of the keyspace, it hashes every
// candidate on the GPU and reports whether the password was found.
class CrackRenderer {
  constructor(fragSrc, W = 1024, H = 1024) {
    const gl = document.createElement("canvas").getContext("webgl2");
    if (!gl) throw new Error("This browser has no WebGL2");
    this.gl = gl; this.W = W; this.H = H;
    const vs = "#version 300 es\nin vec2 a;void main(){gl_Position=vec4(a,0.,1.);}";
    const prog = gl.createProgram();
    for (const [t, s] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fragSrc]]) {
      const o = gl.createShader(t); gl.shaderSource(o, s); gl.compileShader(o);
      if (!gl.getShaderParameter(o, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(o));
      gl.attachShader(prog, o);
    }
    gl.bindAttribLocation(prog, 0, "a"); gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog); this.prog = prog;
    const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    this.u = {};
    for (const n of ["uTarget[0]", "uCharset[0]", "uCharLen", "uPassLen", "uBaseLo", "uWidth", "uHeight",
                     "uReps", "uTotal", "uMode"]) this.u[n] = gl.getUniformLocation(prog, n);
    const tex = gl.createTexture(); gl.bindTexture(gl.TEXTURE_2D, tex);
    gl.texStorage2D(gl.TEXTURE_2D, 1, gl.RGBA8UI, W, H);
    const fb = gl.createFramebuffer(); gl.bindFramebuffer(gl.FRAMEBUFFER, fb);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE) throw new Error("framebuffer incomplete");
    gl.viewport(0, 0, W, H);
    gl.uniform1ui(this.u.uWidth, W); gl.uniform1ui(this.u.uHeight, H);
    this.out = new Uint8Array(W * H * 4);
    this.out32 = new Uint32Array(this.out.buffer);   // for a fast "any non-zero?" scan
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    this.gpu = (dbg && gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) || gl.getParameter(gl.RENDERER) || "GPU";
    this.vendor = (dbg && gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)) || "";
  }

  // target: 8 uint32 words; charset: array of char codes; total = size of the keyspace
  setTarget(target, charset, passLen, total) {
    const gl = this.gl;
    gl.uniform1uiv(this.u["uTarget[0]"], new Uint32Array(target));
    const cs = new Uint32Array(95);
    for (let i = 0; i < charset.length; i++) cs[i] = charset[i];
    gl.uniform1uiv(this.u["uCharset[0]"], cs);
    gl.uniform1ui(this.u.uCharLen, charset.length);
    gl.uniform1ui(this.u.uPassLen, passLen);
    gl.uniform1ui(this.u.uTotal, total >>> 0);
    this.total = total;
  }

  // Search one work unit of up to reps*W*H candidates starting at baseLo.
  // Returns { found: globalIndex | -1, tried, ms }.
  search(baseLo, reps) {
    const gl = this.gl, unit = reps * this.W * this.H;
    const tried = Math.min(unit, Math.max(0, this.total - baseLo));
    const t0 = performance.now();
    gl.uniform1i(this.u.uMode, 0);
    gl.uniform1ui(this.u.uBaseLo, baseLo >>> 0);
    gl.uniform1ui(this.u.uReps, reps);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.readPixels(0, 0, this.W, this.H, gl.RGBA_INTEGER, gl.UNSIGNED_BYTE, this.out);
    const u32 = this.out32, b = this.out;
    let found = -1;
    for (let i = 0; i < u32.length; i++) {      // zero-test is endianness-independent; decode only on a hit
      if (u32[i] !== 0) { const j = i * 4; found = baseLo + (((b[j] << 24) | (b[j + 1] << 16) | (b[j + 2] << 8) | b[j + 3]) >>> 0) - 1; break; }
    }
    return { found, tried, ms: performance.now() - t0 };
  }
}

function gpuMaker(gpu, vendor) {
  const s = (gpu + " " + vendor).toLowerCase();
  if (s.includes("swiftshader") || s.includes("llvmpipe") || s.includes("software")) return "CPU only";
  if (s.includes("adreno") || s.includes("qualcomm")) return "Qualcomm";
  if (s.includes("mali") || s.includes("immortalis")) return "ARM";
  if (s.includes("apple")) return "Apple";
  if (s.includes("powervr")) return "Imagination";
  if (s.includes("xclipse") || s.includes("samsung")) return "Samsung";
  if (s.includes("nvidia") || s.includes("geforce")) return "NVIDIA";
  if (s.includes("radeon") || s.includes("amd")) return "AMD";
  if (s.includes("intel")) return "Intel";
  return "Other";
}
function gpuShortName(gpu) {
  let s = gpu; const m = s.match(/^ANGLE \(([^,]*),\s*([^,(]*)/); if (m) s = m[2];
  return s.replace(/\(0x[0-9a-fA-F]+\)/g, "").replace(/\(TM\)|\(R\)/g, "").replace(/Direct3D.*$/, "").replace(/\s+/g, " ").trim() || gpu;
}
