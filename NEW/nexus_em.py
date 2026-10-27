"""
nexus_em.py -- EM Wave Lab: a live electromagnetic wave simulator on the GPU.

The GPU laptop solves Maxwell's equations on a 2-D grid (FDTD, TM mode)
hundreds of times per second. Any phone or laptop on the Wi-Fi opens the lab
in its browser (scan the QR code) and sees the wave field live. Tap to place
an antenna, drag to draw a metal wall, or load a preset such as a double
slit; everyone in the room shares the same lab.

    python nexus_em.py                    (GPU, 768 x 768 cells)
    python nexus_em.py --grid 1024
    python nexus_em.py --cpu              (same simulation on the CPU, to compare)
"""

import argparse
import asyncio
import http
import json
import os
import socket
import sys
import threading
import time

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
PORT = 8780
COURANT = 0.5
SPONGE = 40                    # absorbing border width in cells
VIEW_RES = (256, 384, 512)

KERNELS = """
__kernel void update_h(__global const float *ez, __global float *hx, __global float *hy,
                       __global const float *damp, const int n) {
    int x = get_global_id(0), y = get_global_id(1);
    int i = y * n + x;
    float d = damp[i];
    if (y < n - 1) hx[i] = d * (hx[i] - 0.5f * (ez[i + n] - ez[i]));
    if (x < n - 1) hy[i] = d * (hy[i] + 0.5f * (ez[i + 1] - ez[i]));
}

__kernel void update_e(__global float *ez, __global const float *hx, __global const float *hy,
                       __global const float *damp, __global const float *ce, __global const uchar *wall,
                       const int n) {
    int x = get_global_id(0), y = get_global_id(1);
    if (x == 0 || y == 0) return;
    int i = y * n + x;
    if (wall[i]) { ez[i] = 0.0f; return; }
    ez[i] = damp[i] * (ez[i] + ce[i] * ((hy[i] - hy[i - 1]) - (hx[i] - hx[i - n])));
}

__kernel void add_sources(__global float *ez, __global const int *idx, const float value, const int count) {
    int k = get_global_id(0);
    if (k < count) ez[idx[k]] += value;
}

__kernel void sample(__global const float *ez, const int n, const int res, __global float *out) {
    int u = get_global_id(0), v = get_global_id(1);
    int x = (int)((u + 0.5f) * n / res), y = (int)((v + 0.5f) * n / res);
    out[v * res + u] = ez[y * n + x];
}
"""


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Field:
    """FDTD grid. Same equations on the GPU (OpenCL) or the CPU (NumPy)."""

    def __init__(self, n, use_gpu):
        self.n = n
        self.lock = threading.Lock()
        self.wall = np.zeros((n, n), np.uint8)
        self.eps = np.ones((n, n), np.float32)
        d = np.minimum.outer(np.arange(n), np.arange(n))
        d = np.minimum(d, np.minimum.outer(np.arange(n)[::-1], np.arange(n)[::-1]))
        edge = np.clip((SPONGE - d) / SPONGE, 0, 1)
        self.damp = (1.0 - 0.08 * edge ** 2).astype(np.float32)
        self.sources = []
        self.freq = 0.02                     # cycles per time step (wavelength = 25 cells)
        self.t = 0
        self.dirty = True
        self.cl = None
        self.name = "CPU (NumPy)"
        if use_gpu:
            try:
                import pyopencl as cl
                devs = [d for p in cl.get_platforms() for d in p.get_devices(device_type=cl.device_type.GPU)]
                devs.sort(key=lambda d: 0 if "NVIDIA" in d.vendor.upper() else 1)
                if devs:
                    self.cl = cl
                    self.name = " ".join(devs[0].name.replace("NVIDIA GeForce ", "").replace("(R)", "").split())
                    self.ctx = cl.Context([devs[0]])
                    self.q = cl.CommandQueue(self.ctx)
                    self.prg = cl.Program(self.ctx, KERNELS).build()
                    self.k_h = cl.Kernel(self.prg, "update_h")
                    self.k_e = cl.Kernel(self.prg, "update_e")
                    self.k_src = cl.Kernel(self.prg, "add_sources")
                    self.k_sample = cl.Kernel(self.prg, "sample")
                    mf = cl.mem_flags
                    size = n * n * 4
                    self.ez_b, self.hx_b, self.hy_b = (cl.Buffer(self.ctx, mf.READ_WRITE, size) for _ in range(3))
                    for b in (self.ez_b, self.hx_b, self.hy_b):
                        cl.enqueue_fill_buffer(self.q, b, np.float32(0), 0, size)
                    self.damp_b = cl.Buffer(self.ctx, mf.READ_ONLY | mf.COPY_HOST_PTR, hostbuf=self.damp)
                    self.ce_b = cl.Buffer(self.ctx, mf.READ_ONLY, size)
                    self.wall_b = cl.Buffer(self.ctx, mf.READ_ONLY, n * n)
                    self.src_b = cl.Buffer(self.ctx, mf.READ_ONLY, 4 * 4096)
                    self.out_b = cl.Buffer(self.ctx, mf.WRITE_ONLY, max(VIEW_RES) ** 2 * 4)
            except Exception as e:
                print(f"[em] OpenCL unavailable ({e}); using the CPU", flush=True)
                self.cl = None
        if self.cl is None:
            self.ez = np.zeros((n, n), np.float32)
            self.hx = np.zeros((n, n), np.float32)
            self.hy = np.zeros((n, n), np.float32)

    # ------------------------------------------------------------- scene edits
    def to_cell(self, x, y):
        return int(np.clip(y, 0, 0.999) * self.n), int(np.clip(x, 0, 0.999) * self.n)

    def add_source(self, x, y):
        with self.lock:
            r, c = self.to_cell(x, y)
            if len(self.sources) >= 12:
                self.sources.pop(0)
            self.sources.append((r, c))
            self.dirty = True

    def draw_wall(self, x0, y0, x1, y1, width=6, value=1):
        with self.lock:
            r0, c0 = self.to_cell(x0, y0)
            r1, c1 = self.to_cell(x1, y1)
            steps = max(abs(r1 - r0), abs(c1 - c0), 1)
            h = width // 2
            for k in range(steps + 1):
                r = r0 + (r1 - r0) * k // steps
                c = c0 + (c1 - c0) * k // steps
                self.wall[max(0, r - h):r + h + 1, max(0, c - h):c + h + 1] = value
            self.dirty = True

    def preset(self, name):
        n = self.n
        with self.lock:
            self.wall[:] = 0
            self.eps[:] = 1
            self.sources = []
            self.reset_fields()
            mid = n // 2
            if name == "double_slit":
                x = int(n * 0.4)
                self.wall[:, x - 3:x + 3] = 1
                gap, sep = max(6, n // 70), max(24, n // 12)
                for c in (mid - sep, mid + sep):
                    self.wall[c - gap:c + gap, x - 3:x + 3] = 0
                self.sources = [(mid, int(n * 0.15))]
            elif name == "single_slit":
                x = int(n * 0.4)
                self.wall[:, x - 3:x + 3] = 1
                g = max(10, n // 40)
                self.wall[mid - g:mid + g, x - 3:x + 3] = 0
                self.sources = [(mid, int(n * 0.15))]
            elif name == "reflector":
                yy, xx = np.mgrid[0:n, 0:n]
                fx = int(n * 0.7)
                para = np.abs((xx - fx) - ((yy - mid) ** 2) / (n * 0.9)) < 3
                para &= np.abs(yy - mid) < n * 0.35
                self.wall[para] = 1
                self.sources = [(mid, int(n * 0.47))]
            elif name == "waveguide":
                w = max(16, n // 16)
                self.wall[mid - w - 4:mid - w, int(n * 0.1):int(n * 0.9)] = 1
                self.wall[mid + w:mid + w + 4, int(n * 0.1):int(n * 0.9)] = 1
                self.sources = [(mid, int(n * 0.12))]
            elif name == "lens":
                yy, xx = np.mgrid[0:n, 0:n]
                lens = ((xx - n * 0.45) / (n * 0.06)) ** 2 + ((yy - mid) / (n * 0.3)) ** 2 < 1
                self.eps[lens] = 4.0
                self.sources = [(r, int(n * 0.12)) for r in range(int(n * 0.25), int(n * 0.75), 3)]
            elif name == "antennas":
                self.sources = [(mid - n // 10, int(n * 0.3)), (mid + n // 10, int(n * 0.3))]
            self.dirty = True

    def reset_fields(self):
        if self.cl is None:
            self.ez[:] = 0
            self.hx[:] = 0
            self.hy[:] = 0
        else:
            size = self.n * self.n * 4
            for b in (self.ez_b, self.hx_b, self.hy_b):
                self.cl.enqueue_fill_buffer(self.q, b, np.float32(0), 0, size)

    # ------------------------------------------------------------- stepping
    def sync_scene(self):
        if not self.dirty:
            return
        self.ce = (COURANT / self.eps).astype(np.float32)
        self.src_idx = np.array([r * self.n + c for r, c in self.sources], np.int32)
        if self.cl is not None:
            cl = self.cl
            cl.enqueue_copy(self.q, self.ce_b, self.ce)
            cl.enqueue_copy(self.q, self.wall_b, np.ascontiguousarray(self.wall))
            if len(self.src_idx):
                cl.enqueue_copy(self.q, self.src_b, self.src_idx)
        self.dirty = False

    def step(self, count):
        with self.lock:
            self.sync_scene()
            n = self.n
            for _ in range(count):
                self.t += 1
                value = np.float32(np.sin(2 * np.pi * self.freq * self.t))
                if self.cl is not None:
                    self.k_h(self.q, (n, n), None, self.ez_b, self.hx_b, self.hy_b, self.damp_b, np.int32(n))
                    self.k_e(self.q, (n, n), None, self.ez_b, self.hx_b, self.hy_b, self.damp_b, self.ce_b, self.wall_b, np.int32(n))
                    if len(self.src_idx):
                        self.k_src(self.q, (len(self.src_idx),), None, self.ez_b, self.src_b, value, np.int32(len(self.src_idx)))
                else:
                    ez, hx, hy, d = self.ez, self.hx, self.hy, self.damp
                    hx[:-1, :] = d[:-1, :] * (hx[:-1, :] - 0.5 * (ez[1:, :] - ez[:-1, :]))
                    hy[:, :-1] = d[:, :-1] * (hy[:, :-1] + 0.5 * (ez[:, 1:] - ez[:, :-1]))
                    ez[1:, 1:] = d[1:, 1:] * (ez[1:, 1:] + self.ce[1:, 1:] * ((hy[1:, 1:] - hy[1:, :-1]) - (hx[1:, 1:] - hx[:-1, 1:])))
                    ez[self.wall == 1] = 0
                    if len(self.src_idx):
                        ez.flat[self.src_idx] += value
            if self.cl is not None:
                self.q.finish()

    def render(self, res, state):
        """Downsampled field as bytes: 0 = wall, 128 = zero field. Brightness
        adapts to the current field so distant, weak waves stay visible."""
        with self.lock:
            idx = ((np.arange(res) + 0.5) * self.n / res).astype(int)
            if self.cl is not None:
                ez = np.empty((res, res), np.float32)
                self.k_sample(self.q, (res, res), None, self.ez_b, np.int32(self.n), np.int32(res), self.out_b)
                self.cl.enqueue_copy(self.q, ez, self.out_b)
                self.q.finish()
            else:
                ez = self.ez[np.ix_(idx, idx)].copy()
            walls = self.wall[np.ix_(idx, idx)] == 1
        level = float(np.percentile(np.abs(ez), 99.5)) + 1e-6
        state["level"] = level if "level" not in state else 0.9 * state["level"] + 0.1 * level
        out = (128 + 126 * np.tanh(ez * (1.6 / state["level"]))).astype(np.uint8)
        np.maximum(out, 1, out=out)
        out[walls] = 0
        return out


class Lab:
    def __init__(self, args):
        self.args = args
        self.field = Field(args.grid, not args.cpu)
        self.field.preset("double_slit")
        self.max_sps = self.benchmark()
        self.field.preset("double_slit")
        self.viewers = {}
        self.steps_hist = []
        self.url = f"http://{get_lan_ip()}:{args.port}"
        self.latest = {}
        self.running = True

    def benchmark(self, seconds=1.0):
        """Full-speed solver run for about a second. The live lab is capped
        (--max-steps) so the waves move at a watchable pace; this shows what
        the hardware can really do."""
        self.field.step(8)                       # warm-up (kernel compile, caches)
        steps, t0 = 0, time.perf_counter()
        while time.perf_counter() - t0 < seconds:
            self.field.step(8)
            steps += 8
        sps = int(steps / (time.perf_counter() - t0))
        n = self.field.n
        print(f"[em] max speed: {sps} steps/s  {sps * n * n / 1e6:.1f} M cell-updates/s", flush=True)
        return sps

    def sim_loop(self):
        """Runs the solver and, every 40 ms, renders the frames viewers need
        in the same thread, so stepping and drawing never wait on each other."""
        batch = 8
        last_render = 0.0
        render_state = {}
        while self.running:
            t0 = time.perf_counter()
            self.field.step(batch)
            self.steps_hist.append((time.time(), batch))
            cutoff = time.time() - 1.0
            while self.steps_hist and self.steps_hist[0][0] < cutoff:
                self.steps_hist.pop(0)
            if time.perf_counter() - last_render >= 0.04 and self.viewers:
                last_render = time.perf_counter()
                self.latest = {res: self.field.render(res, render_state.setdefault(res, {}))
                               for res in set(self.viewers.values())}
            target = batch / self.args.max_steps
            dt = time.perf_counter() - t0
            if dt < target:
                time.sleep(target - dt)

    def steps_per_sec(self):
        return sum(c for _, c in self.steps_hist)

    def stats(self):
        sps = self.steps_per_sec()
        f = self.field
        return {"type": "stats", "device": f.name, "grid": f.n, "steps_per_sec": sps,
                "mcells_per_sec": round(sps * f.n * f.n / 1e6, 1), "viewers": len(self.viewers),
                "max_steps_per_sec": self.max_sps,
                "freq": f.freq, "sources": [[c / f.n, r / f.n] for r, c in f.sources], "url": self.url}

    async def handle(self, ws):
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        except Exception:
            return
        res = int(hello.get("res", 256))
        res = min(VIEW_RES, key=lambda r: abs(r - res))
        self.viewers[ws] = res
        await ws.send(json.dumps(self.stats()))
        try:
            async for msg in ws:
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                t, f = m.get("t"), self.field
                if t == "source":
                    f.add_source(float(m["x"]), float(m["y"]))
                elif t == "wall":
                    f.draw_wall(float(m["x0"]), float(m["y0"]), float(m["x1"]), float(m["y1"]),
                                width=max(2, f.n // 110), value=0 if m.get("erase") else 1)
                elif t == "preset":
                    f.preset(str(m.get("name")))
                elif t == "freq":
                    f.freq = float(np.clip(m.get("value", 0.02), 0.01, 0.08))
                elif t == "clear_sources":
                    with f.lock:
                        f.sources = []
                        f.dirty = True
        except Exception:
            pass
        finally:
            self.viewers.pop(ws, None)

    @staticmethod
    async def send_frame(ws, data, stats):
        try:
            await ws.send(data)
            if stats:
                await ws.send(stats)
        except Exception:
            pass

    async def frame_loop(self):
        """Each viewer gets its own send task. A slow phone skips frames
        instead of holding up everyone else."""
        tick = 0
        pending = {}
        while True:
            await asyncio.sleep(1 / 25)
            tick += 1
            for ws in [w for w, task in pending.items() if task.done() or w not in self.viewers]:
                pending.pop(ws)
            if not self.viewers:
                continue
            frames = self.latest
            stats = json.dumps(self.stats()) if tick % 12 == 0 else None
            for ws, res in list(self.viewers.items()):
                if res not in frames or ws in pending:
                    continue
                data = res.to_bytes(2, "little") + frames[res].tobytes()
                pending[ws] = asyncio.create_task(self.send_frame(ws, data, stats))

    def process_request(self, connection, request):
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        if path in ("/", "/lab"):
            with open(os.path.join(WEB_DIR, "em.html"), "rb") as fh:
                body, ctype = fh.read(), "text/html; charset=utf-8"
        elif path == "/qr.png":
            import io
            import qrcode
            b = io.BytesIO()
            qrcode.make(self.url, box_size=10, border=2).save(b, format="PNG")
            body, ctype = b.getvalue(), "image/png"
        else:
            body = b"not found"
            return Response(404, "Not Found", Headers([("Content-Length", str(len(body)))]), body)
        return Response(http.HTTPStatus.OK, "OK", Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                                                             ("Cache-Control", "no-store")]), body)

    async def report_loop(self):
        while True:
            await asyncio.sleep(5)
            s = self.stats()
            print(f"[em] {s['steps_per_sec']} steps/s  {s['mcells_per_sec']} M cell-updates/s  {s['viewers']} viewer(s)", flush=True)

    async def main(self):
        from websockets.asyncio.server import serve
        threading.Thread(target=self.sim_loop, daemon=True).start()
        async with serve(self.handle, "0.0.0.0", self.args.port, process_request=self.process_request,
                         max_size=2 ** 20, compression="deflate"):
            print("=" * 60)
            print(f"  EM WAVE LAB on {self.field.name}  ({self.field.n} x {self.field.n} cells)")
            print(f"  Open on any phone/laptop:  {self.url}")
            print("=" * 60, flush=True)
            await asyncio.gather(self.frame_loop(), self.report_loop())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grid", type=int, default=768)
    ap.add_argument("--cpu", action="store_true", help="run the solver on the CPU (for comparison)")
    ap.add_argument("--max-steps", type=int, default=600, help="cap on solver steps per second")
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    try:
        asyncio.run(Lab(args).main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
