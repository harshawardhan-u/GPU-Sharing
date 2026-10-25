"""
pool_swarm.py -- Gravity Swarm computed by a pool of GPUs at once.

Every pool worker found on the LAN (see pool_compute.py) gets a slice of the
particles each frame, sized by how fast that GPU has recently been. Particles
are coloured by the GPU that computed them, so you can watch the split move
when one GPU gets busy.

The particle state lives here, on the player's laptop. That makes three
things possible:
  * migration   - if a worker disappears, its share moves to the others (or to
                  this laptop's own GPU/CPU) and the game carries on; when it
                  comes back it rejoins automatically.
  * verification - a few random particles from every worker are recomputed
                  here each frame; a worker that returns wrong results is
                  marked UNTRUSTED and dropped.
  * pooling     - any number of GPUs, any vendor, can join mid-game.

    python pool_swarm.py                  (asks for each host's code)
    python pool_swarm.py --code 123456    (same code on every host)
    python pool_swarm.py --local off      (don't use this laptop's own GPU)

Keys: hold left mouse = attract, E / Q = more / fewer particles, Esc = quit.
"""

import argparse
import json
import os
import socket
import struct
import sys
import threading
import time
from collections import deque

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pool_kernels import WORLD, DeviceCompute, dequantize, list_gpus, quantize, reference_accel

DISCOVERY_PORT = 47615
ANNOUNCE_MAGIC = "COMPUTE_NEXUS_POOL"
FRAME_HDR = "<III"
REPLY_HDR = "<f"
PANEL_W = 380
CPU_CAP = 2500
VISUAL_MASS = 2500   # gravity is scaled so N particles weigh as much as this many
CHECKS_PER_FRAME = 4
COLORS = [(235, 104, 52), (42, 120, 214), (27, 175, 122), (232, 123, 164), (237, 161, 0), (98, 80, 214)]
LOCAL_COLOR = (160, 110, 230)
CPU_COLOR = (150, 150, 150)
FROZEN_COLOR = (60, 60, 70)


class Member:
    def __init__(self, key, name, host, kind, port=None, ip=None, compute=None):
        self.key, self.name, self.host, self.kind = key, name, host, kind
        self.port, self.ip, self.compute = port, ip, compute
        self.ws = None
        self.status = "OK" if kind != "remote" else "CONNECTING"
        self.weight = 0.05
        self.costs = deque(maxlen=8)
        self.share = 0.0
        self.last_ms = 0.0
        self.checks = deque(maxlen=10)
        self.color = CPU_COLOR
        self.slice = (0, 0)


class Pool:
    def __init__(self, args):
        self.args = args
        self.lock = threading.Lock()
        self.members = {}
        self.codes = {}
        self.events = deque(maxlen=6)
        self.banner = None
        self.used_colors = set()
        self.cpu = Member("cpu", "CPU (NumPy)", socket.gethostname(), "cpu", compute=DeviceCompute(None))
        self.local = None
        if args.local != "off":
            gpus = list_gpus()
            if args.local.isdigit():
                gpus = gpus[int(args.local):int(args.local) + 1]
            if gpus:
                dev = DeviceCompute(gpus[0])
                self.local = Member("local", dev.name + " (this laptop)", socket.gethostname(), "local", compute=dev)
                sample = np.random.default_rng(0).integers(0, 65535, (8000, 2)).astype(np.uint16)
                dev.compute(sample, 0, 8000)
                self.local.bench_ms = min(dev.compute(sample, 0, 8000)[1] for _ in range(3))
                self.local.color = LOCAL_COLOR

    def event(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.events.appendleft(f"{stamp}  {text}")
        print(f"[pool] {text}")

    def pick_color(self, vendor):
        prefer = 0 if "NVIDIA" in vendor.upper() else 1 if "INTEL" in vendor.upper() else 2
        order = [prefer] + [i for i in range(len(COLORS)) if i != prefer]
        for i in order:
            if i not in self.used_colors:
                self.used_colors.add(i)
                return COLORS[i]
        return COLORS[0]

    # ---------------------------------------------------------- discovery
    def discovery_loop(self, stop):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(0.5)
        try:
            sock.bind(("", DISCOVERY_PORT))
        except OSError as e:
            print(f"[pool] discovery port busy ({e}); only already-known workers will be used")
            return
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(8192)
                info = json.loads(data.decode())
            except (socket.timeout, ValueError):
                continue
            except Exception:
                continue
            if info.get("type") != ANNOUNCE_MAGIC:
                continue
            ip = info.get("ip") or addr[0]
            for d in info.get("devices", []):
                key = f"{ip}:{d['port']}"
                with self.lock:
                    if key not in self.members:
                        m = Member(key, d["name"], info.get("host", ip), "remote", port=d["port"], ip=ip)
                        m.vendor = d.get("vendor", "")
                        self.members[key] = m
                    self.members[key].last_seen = time.time()

    def known_hosts(self):
        with self.lock:
            return sorted({(m.ip, m.host) for m in self.members.values() if m.kind == "remote"})

    # ---------------------------------------------------------- connections
    def connect(self, m):
        from websockets.sync.client import connect
        code = self.args.code or self.codes.get(m.ip)
        if not code:
            return
        try:
            ws = connect(f"ws://{m.ip}:{m.port}", open_timeout=1.5, max_size=None, compression=None)
            ws.send(json.dumps({"code": code, "client": socket.gethostname()}))
            ack = json.loads(ws.recv(timeout=3))
        except Exception:
            return
        if ack.get("status") != "approved":
            ws.close()
            if m.status != "REJECTED":
                m.status = "REJECTED"
                self.event(f"{m.name} @ {m.host}: wrong code")
            return
        with self.lock:
            if m.color == CPU_COLOR:
                m.color = self.pick_color(getattr(m, "vendor", ""))
            rejoin = m.status == "LOST"
            m.bench_ms = float(ack.get("bench_ms") or 50.0)
            others = [o for o in self.members.values() if o.kind == "remote" and o.status == "OK" and o is not m]
            if self.local:
                others.append(self.local)
            speed_new = 1.0 / m.bench_ms
            speed_others = sum(1.0 / getattr(o, "bench_ms", 50.0) for o in others)
            m.weight = speed_new / (speed_new + speed_others) if others else 1.0
            m.ws, m.status = ws, "OK"
            m.costs.clear()
            m.checks.clear()
        self.event(f"{'rejoined' if rejoin else 'joined'}: {m.name} @ {m.host}")

    def connector_loop(self, stop):
        while not stop.is_set():
            with self.lock:
                todo = [m for m in self.members.values()
                        if m.kind == "remote" and m.status in ("CONNECTING", "LOST")
                        and time.time() - getattr(m, "last_seen", 0) < 3]
            for m in todo:
                self.connect(m)
            stop.wait(1.0)

    def drop(self, m, status, why):
        try:
            if m.ws:
                m.ws.close()
        except Exception:
            pass
        m.ws, m.status = None, status
        self.event(f"{why}: {m.name} @ {m.host}")

    # ---------------------------------------------------------- one frame
    def active(self):
        with self.lock:
            act = [m for m in self.members.values() if m.kind == "remote" and m.status == "OK"]
        if self.local:
            act.append(self.local)
        return act

    def compute(self, qpos, n):
        act = self.active()
        cpu_only = not act
        if cpu_only:
            act = [self.cpu]
        weights = [m.weight for m in act]
        total = sum(weights)
        acc = np.zeros((n, 2), np.float32)
        start = 0
        for i, (m, w) in enumerate(zip(act, weights)):
            end = n if i == len(act) - 1 else min(n, start + int(round(n * w / total)))
            m.slice, m.share = (start, end), (end - start) / max(n, 1)
            start = end
        payload = np.ascontiguousarray(qpos[:n])
        pos_f = dequantize(payload)
        for m in act:
            if m.kind == "remote":
                s, e = m.slice
                try:
                    m.ws.send(struct.pack(FRAME_HDR, n, s, e) + payload.tobytes())
                except Exception:
                    self.drop(m, "LOST", "lost")
        for m in act:
            if m.kind == "remote":
                continue
            s, e = m.slice
            out, ms = m.compute.compute(payload, s, e)
            acc[s:e] = out
            m.last_ms = ms
        for m in act:
            if m.kind != "remote" or m.status != "OK":
                continue
            s, e = m.slice
            try:
                reply = m.ws.recv(timeout=1.5)
            except Exception:
                self.drop(m, "LOST", "lost")
                continue
            (ms,) = struct.unpack_from(REPLY_HDR, reply)
            out = np.frombuffer(reply, np.float32, offset=4).reshape(-1, 2)
            if len(out) != e - s:
                continue
            acc[s:e] = out
            m.last_ms = ms
            self.verify(m, pos_f, out, s, e)
        self.rebalance([m for m in act if m.status == "OK" or m.kind != "remote"])
        return acc, cpu_only

    def rebalance(self, act):
        """Move every member's share towards the split at which all of them
        finish at the same time. Each member is judged on the median of its
        recent cost per unit of share, so one slow frame (a driver or
        power-state hiccup) cannot swing the split."""
        for m in act:
            if m.slice[1] > m.slice[0] and m.last_ms > 0 and m.share > 0:
                m.costs.append(m.last_ms / m.share)
        ready = [m for m in act if len(m.costs) >= 2]
        if len(ready) < 2:
            return
        speed = {m.key: 1.0 / float(np.median(m.costs)) for m in ready}
        total_speed = sum(speed.values())
        ready_weight = sum(m.weight for m in ready)
        for m in ready:
            target = ready_weight * speed[m.key] / total_speed
            step = 0.5 if len(m.costs) < 5 else 0.2
            m.weight = (1 - step) * m.weight + step * target
        total = sum(m.weight for m in act)
        for m in act:
            m.weight = max(m.weight / total, 0.005)

    def verify(self, m, pos_f, out, s, e):
        if e - s <= 0 or self.args.no_verify:
            return
        idx = np.random.randint(s, e, size=min(CHECKS_PER_FRAME, e - s))
        ref, scale = reference_accel(pos_f, idx)
        err = np.abs(out[idx - s] - ref).max(axis=1)
        ok = bool(np.all(err <= 2e-4 * scale + 1e-3))
        m.checks.append(ok)
        if list(m.checks).count(False) >= 3:
            self.banner = (f"UNTRUSTED: {m.name[:24]} sent wrong results", time.time())
            self.drop(m, "UNTRUSTED", "untrusted (wrong results)")


# -------------------------------------------------------------- simulation
def spawn(n, rng, total=None):
    """Particles in a rotating disk with roughly orbital speeds, so the swarm
    swirls into spiral arms instead of collapsing straight into a clump."""
    total = total or n
    radius = WORLD * 0.42
    r = radius * np.sqrt(rng.uniform(0.02, 1.0, n))
    theta = rng.uniform(0, 2 * np.pi, n)
    centre = WORLD / 2
    pos = np.stack([centre + r * np.cos(theta), centre + r * np.sin(theta)], 1)
    speed = 0.95 * np.sqrt(800.0 * VISUAL_MASS * r) / radius
    vel = np.stack([-np.sin(theta) * speed, np.cos(theta) * speed], 1)
    return pos.astype(np.float32), vel.astype(np.float32)


def integrate(pos, vel, acc, mouse, dt, active_n, n_total):
    p, v = pos[:active_n], vel[:active_n]
    a = acc[:active_n] * np.float32(VISUAL_MASS / max(n_total, 1))
    if mouse is not None:
        to = np.array(mouse, np.float32) - p
        d2 = (to * to).sum(1, keepdims=True) + 400.0
        a = a + 6000.0 * to / d2
    v += a * dt
    v *= 0.999
    speed = np.sqrt((v * v).sum(1, keepdims=True))
    np.minimum(1.0, 400.0 / np.maximum(speed, 1e-6), out=speed)
    v *= speed
    p += v * dt
    np.mod(p, WORLD, out=p)


def initial_codes(pool, args):
    if args.code:
        print("Waiting for pool workers to join (up to 5 s)...")
        deadline = time.time() + 5
        while time.time() < deadline and not any(m.status == "OK" for m in pool.members.values()):
            time.sleep(0.2)
        time.sleep(0.5)
        return
    print("Looking for pool workers on the LAN (3 s)...")
    time.sleep(3.0)
    hosts = pool.known_hosts()
    if not hosts:
        print("No workers found yet. They will join automatically if started with the code you give via --code.")
    for ip, host in hosts:
        c = input(f"  Code shown on {host} ({ip}) [Enter to skip]: ").strip()
        if c:
            pool.codes[ip] = c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--code", default=None, help="pairing code used for every host")
    ap.add_argument("--local", default="auto", help="auto | off | <OpenCL GPU index>: use this laptop's own GPU too")
    ap.add_argument("--n", type=int, default=20000)
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--bench", type=float, default=0, help="run headless for N seconds and print stats")
    ap.add_argument("--capture", default=None, help="save a screenshot after --bench seconds")
    ap.add_argument("--shots", default="", help="with --capture: extra screenshot times in seconds, e.g. 5,12")
    args = ap.parse_args()
    if args.bench:
        os.environ["SDL_VIDEODRIVER"] = "dummy"
    os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")
    import pygame

    pool = Pool(args)
    stop = threading.Event()
    threading.Thread(target=pool.discovery_loop, args=(stop,), daemon=True).start()
    if args.code:
        threading.Thread(target=pool.connector_loop, args=(stop,), daemon=True).start()
        initial_codes(pool, args)
    else:
        initial_codes(pool, args)
        threading.Thread(target=pool.connector_loop, args=(stop,), daemon=True).start()
    if pool.local:
        pool.event(f"local GPU ready: {pool.local.name}")

    pygame.init()
    screen = pygame.display.set_mode((int(WORLD) + PANEL_W, int(WORLD)))
    pygame.display.set_caption("ComputeNexus — Pooled Gravity Swarm")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("consolas", 16)
    big = pygame.font.SysFont("consolas", 20, bold=True)

    rng = np.random.default_rng(1)
    n = args.n
    pos, vel = spawn(n, rng)
    fps_hist = deque(maxlen=30)
    t_start = time.time()
    last_print = t_start
    was_cpu_only = False
    shot_times = [float(x) for x in args.shots.split(",") if x.strip()]
    running = True
    while running:
        raw_dt = clock.tick(240) / 1000.0
        dt = min(raw_dt, 1 / 30)
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN:
                if ev.key == pygame.K_ESCAPE:
                    running = False
                elif ev.key == pygame.K_e:
                    extra_p, extra_v = spawn(2000, rng, total=n + 2000)
                    pos, vel, n = np.concatenate([pos, extra_p]), np.concatenate([vel, extra_v]), n + 2000
                elif ev.key == pygame.K_q and n > 2000:
                    pos, vel, n = pos[:-2000].copy(), vel[:-2000].copy(), n - 2000

        mx, my = pygame.mouse.get_pos()
        mouse = (mx, my) if pygame.mouse.get_pressed()[0] and mx < WORLD else None

        qpos = quantize(pos)
        t0 = time.perf_counter()
        has_gpu = bool(pool.active())
        active_n = n if has_gpu else min(n, CPU_CAP)
        acc, cpu_only = pool.compute(qpos[:active_n], active_n)
        frame_ms = (time.perf_counter() - t0) * 1000
        if cpu_only and not was_cpu_only:
            pool.event(f"no GPU left: CPU fallback ({active_n} of {n} particles move)")
        was_cpu_only = cpu_only
        integrate(pos, vel, acc, mouse, dt, active_n, n)
        fps_hist.append(1.0 / max(raw_dt, 1e-4))

        # ---- draw
        screen.fill((8, 8, 14))
        pix = pygame.surfarray.pixels3d(screen)
        xs = np.clip(pos[:, 0].astype(np.int32), 0, int(WORLD) - 2)
        ys = np.clip(pos[:, 1].astype(np.int32), 0, int(WORLD) - 2)
        if active_n < n:
            pix[xs[active_n:], ys[active_n:]] = FROZEN_COLOR
        members = pool.active() or [pool.cpu]
        for m in members:
            s, e = m.slice
            s, e = min(s, active_n), min(e, active_n)
            for dx in (0, 1):
                for dy in (0, 1):
                    pix[xs[s:e] + dx, ys[s:e] + dy] = m.color
        del pix

        panel = pygame.Rect(int(WORLD), 0, PANEL_W, int(WORLD))
        pygame.draw.rect(screen, (22, 22, 30), panel)
        x0, y = int(WORLD) + 16, 16
        screen.blit(big.render("GPU POOL", True, (255, 255, 255)), (x0, y)); y += 34
        avg_fps = sum(fps_hist) / len(fps_hist)
        for line in (f"FPS        {avg_fps:6.1f}", f"Particles  {n:6d}", f"Compute    {frame_ms:6.1f} ms"):
            screen.blit(font.render(line, True, (220, 220, 230)), (x0, y)); y += 22
        y += 14
        with pool.lock:
            listing = list(pool.members.values())
        if pool.local:
            listing.append(pool.local)
        if cpu_only:
            listing.append(pool.cpu)
        for m in listing:
            active = m in members
            color = m.color if active else (90, 90, 100)
            pygame.draw.rect(screen, color, (x0, y + 3, 14, 14))
            screen.blit(font.render(m.name[:30], True, (235, 235, 240) if active else (130, 130, 140)), (x0 + 22, y))
            y += 20
            state = f"{m.share * 100:5.1f}%  {m.last_ms:6.1f} ms" if active else m.status
            status_col = (120, 255, 140) if active else (255, 90, 90) if m.status in ("UNTRUSTED", "LOST", "REJECTED") else (200, 200, 120)
            screen.blit(font.render(f"   {m.host[:16]:16s} {state}", True, status_col), (x0, y))
            y += 28
        y = int(WORLD) - 24 - 20 * len(pool.events)
        screen.blit(font.render("Events", True, (255, 255, 255)), (x0, y - 22))
        for e in pool.events:
            screen.blit(font.render(e[:40], True, (170, 170, 185)), (x0, y)); y += 20
        if pool.banner and time.time() - pool.banner[1] < 4:
            text = big.render(pool.banner[0], True, (255, 255, 255))
            bg = pygame.Rect(0, 0, min(text.get_width() + 30, int(WORLD) - 20), 44)
            bg.center = (int(WORLD) // 2, 60)
            pygame.draw.rect(screen, (190, 30, 40), bg)
            screen.blit(text, (bg.x + 15, bg.y + 10))
        screen.blit(font.render("Hold mouse: attract   E/Q: +/- 2000   Esc: quit", True, (140, 140, 150)), (10, int(WORLD) - 22))
        pygame.display.flip()

        now = time.time()
        if args.bench and now - last_print >= 1.0:
            last_print = now
            shares = "  ".join(f"{m.name}={m.share * 100:.0f}%/{m.last_ms:.1f}ms" for m in members)
            print(f"[bench] t={now - t_start:4.1f}s fps={avg_fps:6.1f} n={n} compute={frame_ms:5.1f}ms  {shares}", flush=True)
        if args.capture and args.shots:
            due = [t for t in shot_times if now - t_start >= t]
            for t in due:
                pygame.image.save(screen, args.capture.replace(".png", f"_{int(t):02d}s.png"))
                shot_times.remove(t)
        if args.bench and now - t_start >= args.bench:
            if args.capture:
                pygame.image.save(screen, args.capture)
            running = False

    stop.set()
    pygame.quit()


if __name__ == "__main__":
    main()
