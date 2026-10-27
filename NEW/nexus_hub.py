"""
nexus_hub.py -- Nexus Arena: phones and laptops play on this laptop's GPU.

Open http://<this-laptop-ip>:8765 on any phone (or scan the QR code shown by
the dashboard) to join. Each player brings a swarm of enemies that chase
them; all of the physics runs on this laptop's GPU and the players' devices
only draw what they receive.

Mission Control (http://<ip>:8765/control) shows every connected device and
lets the GPU owner slice the GPU between players live:
  * GPU share     - how many physics updates per second a player's swarm gets
  * swarm budget  - how many entities (and how much GPU memory) it may use

    python nexus_hub.py                 (port 8765)
    python nexus_hub.py --port 9000
"""

import argparse
import asyncio
import http
import json
import os
import socket
import struct
import sys
import time
from collections import deque

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

WORLD = 1000.0
TICK_HZ = 60
SEND_HZ = 30
TELEMETRY_HZ = 4
DEFAULT_ENEMIES = 600
MAX_ENEMIES = 3000
MAX_PLAYERS = 12
PLAYER_SPEED = 420.0
HIT_RADIUS = 16.0
INVINCIBLE_S = 1.2
BYTES_PER_ENTITY = 16          # position + acceleration buffers on the GPU
REPEL_K, REPEL_R, CHASE = 9000.0, 50.0, 150.0
DAMPING, MAX_SPEED = 0.995, 230.0
COLORS = ["#eb6834", "#2a78d6", "#1baf7a", "#e87ba4", "#eda100", "#9085e9",
          "#e34948", "#00b8d9", "#8bc34a", "#ff9e80", "#b388ff", "#ffd54f"]
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

KERNEL_SRC = """
__kernel void dodge_accel(__global const float2 *pos, const int n, const int start,
                          const float tx, const float ty, const float k, const float r2max,
                          const float chase, __global float2 *out) {
    int r = get_global_id(0);
    int i = start + r;
    float2 xi = pos[i];
    float2 a = (float2)(0.0f, 0.0f);
    for (int j = 0; j < n; j++) {
        float2 d = xi - pos[j];
        float d2 = d.x * d.x + d.y * d.y + 1.0f;
        if (d2 < r2max && j != i) {
            float inv = rsqrt(d2);
            a += d * (k * inv * inv * inv);
        }
    }
    float2 t = (float2)(tx, ty) - xi;
    a += t * (chase / (length(t) + 1e-3f));
    out[r] = a;
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


def device_label(ua):
    ua = ua or ""
    for key, label in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                       ("Macintosh", "Mac"), ("Linux", "Linux")):
        if key in ua:
            return label
    return "Browser"


class GPU:
    """Runs the swarm physics for one group of rows on an OpenCL GPU (or NumPy)."""

    def __init__(self):
        self.name = "CPU (NumPy)"
        self.cl = None
        try:
            import pyopencl as cl
            devs = [d for p in cl.get_platforms() for d in p.get_devices(device_type=cl.device_type.GPU)]
            devs.sort(key=lambda d: 0 if "NVIDIA" in d.vendor.upper() else 1)
            if devs:
                self.cl = cl
                self.dev = devs[0]
                self.name = " ".join(self.dev.name.replace("NVIDIA GeForce ", "").replace("(R)", "").split())
                self.ctx = cl.Context([self.dev])
                self.queue = cl.CommandQueue(self.ctx)
                self.kernel = cl.Kernel(cl.Program(self.ctx, KERNEL_SRC).build(), "dodge_accel")
                self.cap = 0
        except Exception as e:
            print(f"[hub] OpenCL unavailable ({e}); using the CPU")
            self.cl = None

    def upload(self, pos):
        if self.cl is None:
            self.host_pos = pos
            return
        cl = self.cl
        n = len(pos)
        if n > self.cap:
            self.cap = max(1024, n * 2)
            self.pbuf = cl.Buffer(self.ctx, cl.mem_flags.READ_ONLY, self.cap * 8)
            self.obuf = cl.Buffer(self.ctx, cl.mem_flags.WRITE_ONLY, self.cap * 8)
        cl.enqueue_copy(self.queue, self.pbuf, np.ascontiguousarray(pos))

    def accel(self, n, start, end, target):
        m = end - start
        if m <= 0:
            return np.zeros((0, 2), np.float32)
        if self.cl is None:
            pos = self.host_pos
            d = pos[start:end, None, :] - pos[None, :, :]
            d2 = (d * d).sum(-1) + 1.0
            mask = d2 < REPEL_R ** 2
            mask[np.arange(m), np.arange(start, end)] = False
            a = (d * np.where(mask, REPEL_K * d2 ** -1.5, 0.0)[..., None]).sum(1)
            t = np.asarray(target, np.float32) - pos[start:end]
            a += t * (CHASE / (np.linalg.norm(t, axis=1, keepdims=True) + 1e-3))
            return a.astype(np.float32)
        self.kernel(self.queue, (m,), None, self.pbuf, np.int32(n), np.int32(start), np.float32(target[0]),
                    np.float32(target[1]), np.float32(REPEL_K), np.float32(REPEL_R ** 2), np.float32(CHASE), self.obuf)
        out = np.empty((m, 2), np.float32)
        self.cl.enqueue_copy(self.queue, out, self.obuf)
        self.queue.finish()
        return out


class Player:
    def __init__(self, pid, name, ua, ws):
        self.id, self.name, self.ua, self.ws = pid, name[:16] or f"Player {pid}", ua, ws
        self.device = device_label(ua)
        self.color = COLORS[pid % len(COLORS)]
        self.x, self.y = WORLD / 2, WORLD / 2
        self.tx, self.ty = self.x, self.y
        self.share = 1.0
        self.budget = DEFAULT_ENEMIES
        self.credit = 1.0
        self.last_update = time.perf_counter()
        self.joined = time.time()
        self.streak_start = time.time()
        self.best = 0.0
        self.hits = 0
        self.last_hit = -10.0
        self.rtt = 0.0
        self.updates = deque()
        self.gpu_ms = deque()


class Hub:
    def __init__(self, port):
        self.port = port
        self.gpu = GPU()
        self.players = {}
        self.order = []
        self.pos = np.zeros((0, 2), np.float32)
        self.vel = np.zeros((0, 2), np.float32)
        self.ranges = {}
        self.controls = set()
        self.events = deque(maxlen=14)
        self.next_id = 1
        self.rng = np.random.default_rng()
        self.started = time.time()
        self.url = f"http://{get_lan_ip()}:{port}"

    def event(self, text):
        self.events.appendleft({"t": time.strftime("%H:%M:%S"), "text": text})
        print(f"[hub] {text}", flush=True)

    # ----------------------------------------------------------- swarm layout
    def spawn(self, n):
        side = self.rng.integers(0, 4, n)
        t = self.rng.random(n) * WORLD
        m = 40.0
        x = np.where(side == 0, t, np.where(side == 1, WORLD + m, np.where(side == 2, t, -m)))
        y = np.where(side == 0, -m, np.where(side == 1, t, np.where(side == 2, WORLD + m, t)))
        return np.stack([x, y], 1).astype(np.float32), np.zeros((n, 2), np.float32)

    def rebuild(self, sizes):
        """Re-lay the entity arrays so every player's swarm is one contiguous
        range, keeping existing entities where possible."""
        new_pos, new_vel, ranges, start = [], [], {}, 0
        for pid in self.order:
            want = sizes.get(pid, self.players[pid].budget)
            if pid in self.ranges:
                s, e = self.ranges[pid]
                p, v = self.pos[s:e][:want], self.vel[s:e][:want]
            else:
                p, v = np.zeros((0, 2), np.float32), np.zeros((0, 2), np.float32)
            if len(p) < want:
                ep, ev = self.spawn(want - len(p))
                p, v = np.concatenate([p, ep]), np.concatenate([v, ev])
            new_pos.append(p)
            new_vel.append(v)
            ranges[pid] = (start, start + len(p))
            start += len(p)
        self.pos = np.concatenate(new_pos) if new_pos else np.zeros((0, 2), np.float32)
        self.vel = np.concatenate(new_vel) if new_vel else np.zeros((0, 2), np.float32)
        self.ranges = ranges

    # ----------------------------------------------------------- simulation
    def tick(self, dt):
        now = time.time()
        for p in self.players.values():
            dx, dy = p.tx - p.x, p.ty - p.y
            dist = (dx * dx + dy * dy) ** 0.5
            step = min(dist, PLAYER_SPEED * dt)
            if dist > 1e-3:
                p.x += dx / dist * step
                p.y += dy / dist * step

        due = []
        for pid in self.order:
            p = self.players[pid]
            p.credit += p.share
            if p.credit >= 1.0:
                p.credit -= 1.0
                due.append(p)
        if due and len(self.pos):
            n = len(self.pos)
            self.gpu.upload(self.pos)
            perf = time.perf_counter()
            for p in due:
                s, e = self.ranges[p.id]
                t0 = time.perf_counter()
                acc = self.gpu.accel(n, s, e, (p.x, p.y))
                gpu_ms = (time.perf_counter() - t0) * 1000
                elapsed = min(perf - p.last_update, 0.5)
                p.last_update = perf
                steps = max(1, int(np.ceil(elapsed / (1 / 30))))
                sub = elapsed / steps
                v, x = self.vel[s:e], self.pos[s:e]
                for _ in range(steps):
                    v += acc * sub
                    v *= DAMPING
                    sp = np.sqrt((v * v).sum(1, keepdims=True))
                    v *= np.minimum(1.0, MAX_SPEED / np.maximum(sp, 1e-6))
                    x += v * sub
                np.clip(x, -300, WORLD + 300, out=x)
                p.updates.append(now)
                p.gpu_ms.append((now, gpu_ms))

        if len(self.pos) and self.players:
            pp = np.array([[p.x, p.y] for p in self.players.values()], np.float32)
            d2 = ((self.pos[None, :, :] - pp[:, None, :]) ** 2).sum(-1)
            hit = (d2 < HIT_RADIUS ** 2).any(1)
            for p, h in zip(self.players.values(), hit):
                if h and now - p.last_hit > INVINCIBLE_S:
                    p.best = max(p.best, now - p.streak_start)
                    p.last_hit, p.streak_start = now, now
                    p.hits += 1

        for p in self.players.values():
            while p.updates and now - p.updates[0] > 1.0:
                p.updates.popleft()
            while p.gpu_ms and now - p.gpu_ms[0][0] > 1.0:
                p.gpu_ms.popleft()

    # ----------------------------------------------------------- messages
    def frame_bytes(self):
        players = list(self.players.values())
        head = struct.pack("<HI", len(players), len(self.pos))
        pdata = np.array([[p.x, p.y] for p in players], np.float32).reshape(-1, 2)
        q = np.clip(np.rint(np.concatenate([pdata, self.pos]) * (65535 / WORLD)), 0, 65535).astype(np.uint16)
        return head + q.tobytes()

    def roster(self):
        now = time.time()
        out = []
        for pid in self.order:
            p = self.players[pid]
            s, e = self.ranges.get(pid, (0, 0))
            out.append({
                "id": p.id, "name": p.name, "color": p.color, "device": p.device, "start": s, "end": e,
                "share": round(p.share, 3), "budget": p.budget, "mem_kb": round((e - s) * BYTES_PER_ENTITY / 1024, 1),
                "ups": len(p.updates), "gpu_ms": round(sum(m for _, m in p.gpu_ms), 2),
                "streak": round(now - p.streak_start, 1), "best": round(max(p.best, now - p.streak_start), 1),
                "hits": p.hits, "rtt": round(p.rtt, 1), "invincible": now - p.last_hit < INVINCIBLE_S,
            })
        return out

    def telemetry(self):
        roster = self.roster()
        total_ms = sum(r["gpu_ms"] for r in roster)
        return {"type": "telemetry", "gpu": self.gpu.name, "url": self.url, "players": roster,
                "entities": int(len(self.pos)), "gpu_busy_pct": round(total_ms / 10, 1),
                "uptime": int(time.time() - self.started), "events": list(self.events),
                "max_enemies": MAX_ENEMIES}

    # ----------------------------------------------------------- connections
    async def handle(self, ws):
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        except Exception:
            return
        if hello.get("role") == "control":
            await self.handle_control(ws)
        elif hello.get("role") == "player":
            await self.handle_player(ws, hello)

    async def handle_player(self, ws, hello):
        if len(self.players) >= MAX_PLAYERS:
            await ws.send(json.dumps({"type": "full"}))
            return
        pid = self.next_id
        self.next_id += 1
        p = Player(pid, str(hello.get("name", "")).strip(), hello.get("ua", ""), ws)
        self.players[pid] = p
        self.order.append(pid)
        self.rebuild({})
        self.event(f"{p.name} joined from {p.device}")
        await ws.send(json.dumps({"type": "welcome", "id": pid, "color": p.color, "world": WORLD, "gpu": self.gpu.name}))
        try:
            async for msg in ws:
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                if m.get("t") == "move":
                    p.tx = float(np.clip(m.get("x", 0.5), 0, 1)) * WORLD
                    p.ty = float(np.clip(m.get("y", 0.5), 0, 1)) * WORLD
                    if "rtt" in m:
                        p.rtt = float(m["rtt"])
                elif m.get("t") == "ping":
                    await ws.send(json.dumps({"type": "pong", "ts": m.get("ts")}))
        except Exception:
            pass
        finally:
            self.players.pop(pid, None)
            self.order.remove(pid)
            self.ranges.pop(pid, None)
            self.rebuild({})
            self.event(f"{p.name} left")

    async def handle_control(self, ws):
        self.controls.add(ws)
        self.event("Mission Control connected")
        try:
            async for msg in ws:
                try:
                    m = json.loads(msg)
                except ValueError:
                    continue
                p = self.players.get(m.get("id"))
                if m.get("t") == "set" and p:
                    if "share" in m:
                        p.share = float(np.clip(m["share"], 0.02, 1.0))
                        self.event(f"{p.name}: GPU share set to {p.share * 100:.0f}%")
                    if "budget" in m:
                        p.budget = int(np.clip(m["budget"], 50, MAX_ENEMIES))
                        self.rebuild({})
                        self.event(f"{p.name}: swarm budget set to {p.budget} ({p.budget * BYTES_PER_ENTITY / 1024:.0f} KB)")
                elif m.get("t") == "kick" and p:
                    self.event(f"{p.name} removed by the GPU owner")
                    await p.ws.close()
        except Exception:
            pass
        finally:
            self.controls.discard(ws)

    # ----------------------------------------------------------- loops
    async def sim_loop(self):
        period = 1 / TICK_HZ
        last = time.perf_counter()
        while True:
            now = time.perf_counter()
            self.tick(min(now - last, 0.1))
            last = now
            await asyncio.sleep(max(0.0, period - (time.perf_counter() - now)))

    async def send_loop(self):
        tick = 0
        while True:
            await asyncio.sleep(1 / SEND_HZ)
            tick += 1
            if not self.players:
                continue
            frame = self.frame_bytes()
            roster = json.dumps({"type": "roster", "players": self.roster()}) if tick % 6 == 0 else None
            for p in list(self.players.values()):
                try:
                    await p.ws.send(frame)
                    if roster:
                        await p.ws.send(roster)
                except Exception:
                    pass

    async def telemetry_loop(self):
        while True:
            await asyncio.sleep(1 / TELEMETRY_HZ)
            if not self.controls:
                continue
            msg = json.dumps(self.telemetry())
            for c in list(self.controls):
                try:
                    await c.send(msg)
                except Exception:
                    pass

    def process_request(self, connection, request):
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        files = {"/": ("play.html", "text/html; charset=utf-8"), "/play": ("play.html", "text/html; charset=utf-8"),
                 "/control": ("control.html", "text/html; charset=utf-8")}
        if path in files:
            name, ctype = files[path]
            with open(os.path.join(WEB_DIR, name), "rb") as f:
                body = f.read()
        elif path == "/qr.png":
            import io
            import qrcode
            buf = io.BytesIO()
            qrcode.make(self.url, box_size=10, border=2).save(buf, format="PNG")
            body, ctype = buf.getvalue(), "image/png"
        else:
            body, ctype = b"not found", "text/plain"
            return Response(404, "Not Found", Headers([("Content-Type", ctype), ("Content-Length", str(len(body)))]), body)
        headers = Headers([("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
        return Response(http.HTTPStatus.OK, "OK", headers, body)

    async def main(self):
        from websockets.asyncio.server import serve
        async with serve(self.handle, "0.0.0.0", self.port, process_request=self.process_request,
                         max_size=2 ** 20, compression=None, ping_interval=None):
            print("=" * 60)
            print(f"  NEXUS ARENA live on GPU: {self.gpu.name}")
            print(f"  Players open:     {self.url}")
            print(f"  Mission Control:  {self.url}/control")
            print("=" * 60, flush=True)
            self.event(f"arena started on {self.gpu.name}")
            await asyncio.gather(self.sim_loop(), self.send_loop(), self.telemetry_loop())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args()
    try:
        asyncio.run(Hub(args.port).main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
