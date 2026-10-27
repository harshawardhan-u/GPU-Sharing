// swarm_render.js -- renders one square of the Room Supercomputer picture with
// this device's GPU (WebGL2), and reads the pixels back so they can be sent
// to the host.
class SwarmRenderer {
  constructor(fragSrc) {
    this.canvas = document.createElement("canvas");
    const gl = this.canvas.getContext("webgl2", {
      antialias: false, depth: false, stencil: false, alpha: false,
      preserveDrawingBuffer: false, powerPreference: "high-performance",
    });
    if (!gl) throw new Error("This browser has no WebGL2");
    this.gl = gl;
    const vs = "#version 300 es\nin vec2 a;\nvoid main() { gl_Position = vec4(a, 0.0, 1.0); }\n";
    const prog = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fragSrc]]) {
      const sh = gl.createShader(type);
      gl.shaderSource(sh, src);
      gl.compileShader(sh);
      if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(sh));
      gl.attachShader(prog, sh);
    }
    gl.bindAttribLocation(prog, 0, "a");
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(prog));
    gl.useProgram(prog);
    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    gl.enableVertexAttribArray(0);
    gl.vertexAttribPointer(0, 2, gl.FLOAT, false, 0, 0);
    this.u = {};
    for (const n of ["uImage", "uOrigin", "uTileH", "uCamPos", "uCamTarget", "uFov", "uPower", "uSpp",
                     "uMaxSteps", "uShadowSteps"]) this.u[n] = gl.getUniformLocation(prog, n);
    const dbg = gl.getExtension("WEBGL_debug_renderer_info");
    this.gpu = (dbg && gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL)) || gl.getParameter(gl.RENDERER) || "GPU";
    this.vendor = (dbg && gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL)) || gl.getParameter(gl.VENDOR) || "";
  }

  // fp: frame parameters from the host; tile: [x, y, w, h]; lazy: the "cheater" mode
  // used in the demo -- skips most of the work, so the pixels come out wrong.
  // Draws the square into this.canvas and returns the GPU time in ms.
  render(fp, tile, lazy = false) {
    const t0 = performance.now();
    this.draw(fp, tile, lazy);
    this.gl.readPixels(0, 0, 1, 1, this.gl.RGBA, this.gl.UNSIGNED_BYTE, this._one || (this._one = new Uint8Array(4)));
    return performance.now() - t0;
  }

  // Draws and reads back all pixels (rows bottom-up, RGB) -- used by tests.
  renderRGB(fp, tile, lazy = false) {
    this.draw(fp, tile, lazy);
    const gl = this.gl;
    const [, , tw, th] = tile;
    const rgba = new Uint8Array(tw * th * 4);
    gl.readPixels(0, 0, tw, th, gl.RGBA, gl.UNSIGNED_BYTE, rgba);   // waits for the GPU
    const rgb = new Uint8Array(tw * th * 3);
    for (let i = 0, j = 0; i < rgba.length; i += 4, j += 3) {
      rgb[j] = rgba[i]; rgb[j + 1] = rgba[i + 1]; rgb[j + 2] = rgba[i + 2];
    }
    return rgb;
  }

  draw(fp, tile, lazy) {
    const gl = this.gl, u = this.u;
    const [tx, ty, tw, th] = tile;
    if (this.canvas.width !== tw || this.canvas.height !== th) {
      this.canvas.width = tw;
      this.canvas.height = th;
    }
    gl.viewport(0, 0, tw, th);
    gl.uniform2f(u.uImage, fp.w, fp.h);
    gl.uniform2f(u.uOrigin, tx, ty);
    gl.uniform1f(u.uTileH, th);
    gl.uniform3f(u.uCamPos, fp.cam[0], fp.cam[1], fp.cam[2]);
    gl.uniform3f(u.uCamTarget, fp.target[0], fp.target[1], fp.target[2]);
    gl.uniform1f(u.uFov, fp.fov);
    gl.uniform1f(u.uPower, fp.power);
    gl.uniform1i(u.uSpp, lazy ? 1 : fp.spp);
    gl.uniform1i(u.uMaxSteps, lazy ? 12 : fp.steps);
    gl.uniform1i(u.uShadowSteps, lazy ? 0 : fp.shadow);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}

// "Adreno (TM) 730" -> Qualcomm, "Apple GPU" -> Apple, ... (for the "GPU makers" count)
function gpuMaker(gpu, vendor) {
  const s = (gpu + " " + vendor).toLowerCase();
  if (s.includes("swiftshader") || s.includes("llvmpipe") || s.includes("software")) return "CPU only";
  if (s.includes("adreno") || s.includes("qualcomm")) return "Qualcomm";
  if (s.includes("mali") || s.includes("immortalis")) return "ARM";
  if (s.includes("apple")) return "Apple";
  if (s.includes("powervr") || s.includes("imagination")) return "Imagination";
  if (s.includes("xclipse") || s.includes("samsung")) return "Samsung";
  if (s.includes("nvidia") || s.includes("geforce") || s.includes("rtx")) return "NVIDIA";
  if (s.includes("radeon") || s.includes("amd")) return "AMD";
  if (s.includes("intel")) return "Intel";
  return "Other";
}

// "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 Laptop GPU (0x000028E0) Direct3D11 vs_5_0 ps_5_0, D3D11)"
//   -> "NVIDIA GeForce RTX 4060 Laptop GPU"
function gpuShortName(gpu) {
  let s = gpu;
  const m = s.match(/^ANGLE \(([^,]*),\s*([^,(]*)/);
  if (m) s = m[2];
  s = s.replace(/\(0x[0-9a-fA-F]+\)/g, "").replace(/\(TM\)|\(R\)/g, "").replace(/Direct3D.*$/, "");
  return s.replace(/\s+/g, " ").trim() || gpu;
}
