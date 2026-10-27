"""
nexus_swarm.py -- "The Room Is the Supercomputer".

Everyone in the room scans a QR code. Each phone (or laptop) that opens the
page lends its GPU: it renders small squares of one huge 3D picture in its
browser and sends them back. This laptop is the coordinator. It:

  * hands out squares -- faster devices automatically take more,
  * spot-checks squares against its own maths and throws out cheaters,
  * moves squares away from devices that leave or fall asleep,
  * assembles the finished picture (or movie) on the projector.

    python nexus_swarm.py                 (then open http://<this-ip>:8790/wall)
"""

import argparse
import asyncio
import collections
import http
import io
import json
import os
import queue
import random
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np
from PIL import Image
from websockets.exceptions import ConnectionClosed

import swarm_scene as S

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
OUT_DIR = os.path.join(HERE, "swarm_output")
PORT = 8790
COLORS = ["#4F8EF7", "#F58A3D", "#3CC488", "#E85FA0", "#A57BF5", "#F2C744", "#36C2D6", "#F0645A",
          "#9CCC50", "#C9925B", "#7F8CFF", "#FF9DD0", "#5ED3B8", "#D7A6FF", "#FFB74D", "#90A4AE"]
PRESETS = {
    "poster8k": lambda: S.poster_job(7680, 4320, spp=4),      # 16 light rays per pixel
    "poster8k_fast": lambda: S.poster_job(7680, 4320, spp=2),  # 4 light rays per pixel
    "movie": lambda: S.movie_job(120),
}
PRESET_LABELS = {"poster8k": "8K poster · ultra quality", "poster8k_fast": "8K poster · quick",
                 "movie": "Movie · 120 frames, 720p"}
TILE_OVERHEAD_MS = 8.0      # JPEG + network per square, measured in rehearsals
AVG_TILE_FACTOR = 0.45      # average square vs the (heavy) test square, measured
_catalog = {}


def preset_info(key):
    """Size of each preset, for the wall and for the time estimate."""
    if key not in _catalog:
        spec = PRESETS[key]()
        spp = spec["frames"][0]["spp"]
        tiles = len(S.tiles_for(spec["width"], spec["height"])) * len(spec["frames"])
        _catalog[key] = {"key": key, "label": PRESET_LABELS[key], "width": spec["width"], "height": spec["height"],
                         "frames": len(spec["frames"]), "spp": spp, "tiles": tiles,
                         "rays": spec["width"] * spec["height"] * spp * spp * len(spec["frames"])}
    return _catalog[key]
INFLIGHT = 3            # squares queued on each device, so its GPU never waits for the network
CHECK_FIRST = 3         # always check a new device's first squares
CHECK_RATE = 1 / 6      # then one square in six (every square once a device looks suspicious)
SAMPLES = 16            # pixels re-computed per checked square
TOLERANCE = 32          # per-channel difference allowed (JPEG + GPU rounding)
PASS_FRACTION = 0.5     # share of sampled pixels that must match (honest GPUs: 0.9-1.0, cheaters: ~0)
BAN_AFTER = 2           # failed checks before a device is thrown out
THUMB_W = 480
CALIB_TILE = (1856, 1016, 128, 128)    # a heavy square of the 4K poster, rendered once on joining


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def scene_hash():
    """FNV-1a hash of the shader text; devices with an old copy reload themselves."""
    h = 0x811C9DC5
    with open(os.path.join(WEB_DIR, "swarm_scene.glsl"), encoding="utf-8", newline="") as fh:
        for ch in fh.read().replace("\r\n", "\n"):
            h = ((h ^ ord(ch)) * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def fmt_time(sec):
    sec = max(0, int(round(sec)))
    return f"{sec // 3600}:{sec // 60 % 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


class Device:
    def __init__(self, ws, did, hello, color):
        self.ws = ws
        self.id = did
        self.kind = hello.get("kind") if hello.get("kind") in ("phone", "tablet", "laptop", "cpu") else "phone"
        default = {"phone": "Phone", "tablet": "Tablet", "laptop": "Laptop", "cpu": "Laptop CPU"}[self.kind]
        self.name = (str(hello.get("name") or "").strip()[:24]) or f"{default} {did}"
        self.gpu = str(hello.get("gpu") or "GPU")[:60]
        self.maker = str(hello.get("maker") or "Other")[:20]
        self.color = color
        self.status = "idle"         # idle / working / asleep / untrusted / left
        self.inflight = {}           # key -> time sent
        self.done = 0
        self.done_keys = set()
        self.ms = collections.deque(maxlen=16)
        self.ms_sum = 0.0            # all squares this job, for the "alone it would take" estimate
        self.accepted_at = collections.deque(maxlen=64)
        self.checks = collections.deque(maxlen=6)
        self.match = []              # fraction of sampled pixels that matched, per check
        self.checked = 0
        self.cheat = False
        self.calib_ms = None
        self.last_result = time.time()
        self.first_t = None          # first and last accepted square this job
        self.last_t = None

    def rate(self):
        """Squares per second over the last few seconds."""
        now = time.time()
        recent = [t for t in self.accepted_at if now - t < 6]
        if len(recent) < 2:
            return 0.0
        return (len(recent) - 1) / max(now - recent[0], 0.5)

    def avg_ms(self):
        return float(np.mean(self.ms)) if self.ms else (self.calib_ms or 0.0)

    def job_avg_ms(self):
        return self.ms_sum / self.done if self.done else self.avg_ms()

    def alone_seconds(self, total):
        """How long this device would need for the whole job on its own, from its measured pace."""
        if self.done >= 8 and self.last_t and self.last_t > self.first_t:
            return total * (self.last_t - self.first_t) / (self.done - 1)
        return total * (self.job_avg_ms() + 6) / 1000

    def public(self, total):
        return {"id": self.id, "name": self.name, "gpu": self.gpu, "maker": self.maker, "kind": self.kind,
                "color": self.color, "status": self.status, "done": self.done,
                "share": round(100 * self.done / total, 1) if total else 0, "rate": round(self.rate(), 2),
                "ms": round(self.avg_ms(), 1), "calib": self.calib_ms, "cheat": self.cheat,
                "inflight": [list(k) for k in self.inflight]}


class Job:
    def __init__(self, preset):
        spec = PRESETS[preset]()
        self.preset = preset
        self.id = random.randint(1, 2 ** 30)
        self.kind = spec["kind"]
        self.title = spec["title"]
        self.width, self.height = spec["width"], spec["height"]
        self.frames = spec["frames"]
        self.tiles = S.tiles_for(self.width, self.height)
        self.keys = [(f, i) for f in range(len(self.frames)) for i in range(len(self.tiles))]
        self.total = len(self.keys)
        self.queue = collections.deque(self.keys)
        self.owner = {}                       # key -> device id of the accepted square
        self.jpeg = {}                        # key -> bytes (current frames only)
        self.spec_count = collections.Counter()
        self.started = time.time()
        self.finished = None
        self.rays = self.width * self.height * self.frames[0]["spp"] ** 2 * len(self.frames)
        self.frame_left = [len(self.tiles)] * len(self.frames)
        self.canvas = {}                      # frame -> full-size uint8 image (until saved)
        self.thumb = np.zeros((round(THUMB_W * self.height / self.width), THUMB_W, 3), np.uint8)
        self.thumb_frame = 0
        self.thumb_dirty = True
        self.frames_saved = 0
        self.revoked = 0
        self.caught = []
        self.left = []
        self.moved = 0
        self.pending_checks = 0
        self.out = os.path.join(OUT_DIR, time.strftime("%Y%m%d_%H%M%S") + "_" + preset)
        self.result_file = None
        self.summary = None
        self.done_times = collections.deque(maxlen=600)     # (time, weight) of accepted squares
        # how heavy each square is (fractal-filled squares cost far more than sky), for "time left"
        mid = self.frames[len(self.frames) // 2]
        self.weight = [float(w) for w in S.tile_weights(mid, self.tiles)]
        self.total_w = sum(self.weight) * len(self.frames)
        self.done_w = 0.0
        self.accept_time = 0.0          # coordinator CPU time spent per square (for the report)
        self.accept_n = 0

    def frame_canvas(self, f):
        if f not in self.canvas:
            saved = os.path.join(self.out, f"frame_{f:04d}.jpg")
            if os.path.exists(saved):          # a saved movie frame that needs a square redone
                self.canvas[f] = np.array(Image.open(saved).convert("RGB"))
            else:
                self.canvas[f] = np.zeros((self.height, self.width, 3), np.uint8)
        return self.canvas[f]

    def progress(self):
        return len(self.owner) / self.total

    def public(self):
        now = time.time()
        elapsed = (self.finished or now) - self.started
        recent = [(t, w) for t, w in self.done_times if now - t < 5]
        rate = (len(recent) - 1) / max(now - recent[0][0], 0.5) if len(recent) > 2 else 0.0
        rate_w = sum(w for _, w in recent[1:]) / max(now - recent[0][0], 0.5) if len(recent) > 2 else 0.0
        eta = (self.total_w - self.done_w) / rate_w if rate_w > 0 and not self.finished else None
        return {"id": self.id, "preset": self.preset, "kind": self.kind, "title": self.title,
                "width": self.width, "height": self.height, "frames": len(self.frames),
                "tiles_per_frame": len(self.tiles), "total": self.total, "done": len(self.owner),
                "elapsed": round(elapsed, 1), "eta": round(eta, 1) if eta is not None else None,
                "rate": round(rate, 1), "rays": self.rays, "finished": self.finished is not None,
                "frames_saved": self.frames_saved, "revoked": self.revoked, "moved": self.moved,
                "summary": self.summary}


class Swarm:
    def __init__(self, args):
        self.args = args
        self.devices = {}
        self.walls = set()
        self.next_id = 1
        self.job = None
        self.preset = "poster8k"
        self.lan_ip = get_lan_ip()
        self.url = f"http://{self.lan_ip}:{args.port}"
        self.verify_q = queue.Queue()
        self.loop = None
        self.events = collections.deque(maxlen=40)
        os.makedirs(OUT_DIR, exist_ok=True)

    # ------------------------------------------------------------ helpers
    def event(self, text, kind="info"):
        self.events.appendleft({"time": time.strftime("%H:%M:%S"), "text": text, "kind": kind})
        print(f"[swarm] {text}", flush=True)

    async def send(self, ws, msg):
        try:
            await ws.send(msg if isinstance(msg, (bytes, str)) else json.dumps(msg))
        except Exception:
            pass

    def send_soon(self, ws, msg):
        asyncio.ensure_future(self.send(ws, msg))

    def to_walls(self, msg):
        data = msg if isinstance(msg, (bytes, str)) else json.dumps(msg)
        for w in list(self.walls):
            self.send_soon(w, data)

    def active(self):
        return [d for d in self.devices.values() if d.status not in ("left", "untrusted")]

    def state(self):
        total = self.job.total if self.job else 1
        devs = [d.public(total) for d in self.devices.values()]
        act = self.active()
        info = preset_info(self.preset)
        k = (info["spp"] / 2) ** 2           # the test square uses 4 rays per pixel
        rates = {d.id: 1000.0 / (d.calib_ms * AVG_TILE_FACTOR * k + TILE_OVERHEAD_MS) for d in act if d.calib_ms}
        speed = sum(rates.values())
        est = round(info["tiles"] / speed, 1) if speed > 0 else None
        single = round(info["tiles"] / max(rates.values()), 1) if rates else None
        return {"t": "state", "url": self.url, "preset": self.preset, "estimate": est, "single": single,
                "presets": [preset_info(p) for p in PRESETS],
                "job": self.job.public() if self.job else None, "devices": devs,
                "active": len(act), "makers": sorted({d.maker for d in act}),
                "events": list(self.events)[:8]}

    # ------------------------------------------------------------ job control
    def start(self, preset):
        if preset not in PRESETS:
            return
        self.preset = preset
        self.job = Job(preset)
        os.makedirs(self.job.out, exist_ok=True)
        for d in self.devices.values():
            d.inflight.clear()
            d.done = 0
            d.ms_sum = 0.0
            d.first_t = d.last_t = None
            d.done_keys.clear()
            d.accepted_at.clear()
            if d.status not in ("left", "untrusted"):
                d.status = "working"
        self.devices = {k: d for k, d in self.devices.items() if d.status != "left"}
        job = self.job
        self.event(f"Started: {job.title} — {job.total:,} squares, {job.rays / 1e6:,.0f} million light rays",
                   "start")
        self.to_walls({"t": "job", "job": job.public(), "tiles": job.tiles})
        jmsg = {"t": "job", "job": job.id, "frames": job.frames, "title": job.title, "total": job.total}
        for d in self.active():
            self.send_soon(d.ws, jmsg)
            self.top_up(d)

    def reset(self):
        self.job = None
        for d in self.devices.values():
            d.inflight.clear()
            d.done = 0
            d.done_keys.clear()
            if d.status not in ("left", "untrusted"):
                d.status = "idle"
            self.send_soon(d.ws, {"t": "idle"})
        self.devices = {k: d for k, d in self.devices.items() if d.status != "left"}
        self.to_walls({"t": "reset"})
        self.event("Reset — ready for a new picture")

    def next_key(self, dev):
        job = self.job
        while job.queue:
            key = job.queue.popleft()
            if key not in job.owner:
                return key
        # Nothing left in the queue: help with squares still out on slower devices
        # ("backup tasks"); whichever copy comes back first wins.
        best, best_t = None, None
        for other in self.active():
            if other is dev:
                continue
            for key, t in other.inflight.items():
                if key in job.owner or key in dev.inflight or job.spec_count[key] >= 2:
                    continue
                if best_t is None or t < best_t:
                    best, best_t = key, t
        if best is not None and time.time() - best_t > 0.4:
            job.spec_count[best] += 1
            return best
        return None

    def top_up(self, dev):
        job = self.job
        if job is None or job.finished or dev.status in ("untrusted", "left") or dev.calib_ms is None:
            return
        while len(dev.inflight) < INFLIGHT:
            key = self.next_key(dev)
            if key is None:
                break
            dev.inflight[key] = time.time()
            f, i = key
            self.send_soon(dev.ws, {"t": "work", "job": job.id, "key": [f, i], "tile": job.tiles[i]})

    def requeue(self, dev, why):
        job = self.job
        n = 0
        for key in list(dev.inflight):
            if job and key not in job.owner:
                job.queue.appendleft(key)
                n += 1
        dev.inflight.clear()
        if job and n and not job.finished:
            job.moved += n
            self.event(f"{dev.name} {why} — {n} square{'s' if n > 1 else ''} moved to other devices", "move")
            for d in self.active():
                self.top_up(d)
        return n

    # ------------------------------------------------------------ results
    def accept(self, dev, hdr, jpg):
        t0 = time.perf_counter()
        try:
            self._accept(dev, hdr, jpg)
        finally:
            if self.job:
                self.job.accept_time += time.perf_counter() - t0
                self.job.accept_n += 1

    def _accept(self, dev, hdr, jpg):
        job = self.job
        if job is None or hdr.get("job") != job.id:
            return
        key = (int(hdr["key"][0]), int(hdr["key"][1]))
        dev.inflight.pop(key, None)
        dev.last_result = time.time()
        if dev.status == "asleep":
            dev.status = "working"
        if dev.status == "untrusted" or key in job.owner or job.finished:
            self.top_up(dev)
            return
        f, i = key
        tx, ty, tw, th = job.tiles[i]
        try:
            img = np.asarray(Image.open(io.BytesIO(jpg)).convert("RGB"))
        except Exception:
            self.top_up(dev)
            return
        if img.shape != (th, tw, 3):
            self.top_up(dev)
            return
        job.owner[key] = dev.id
        job.jpeg[key] = jpg
        job.done_times.append((time.time(), job.weight[i]))
        job.done_w += job.weight[i]
        dev.done += 1
        dev.done_keys.add(key)
        dev.accepted_at.append(time.time())
        dev.first_t = dev.first_t or time.time()
        dev.last_t = time.time()
        dev.ms.append(float(hdr.get("ms", 0)))
        dev.ms_sum += float(hdr.get("ms", 0))
        canvas = job.frame_canvas(f)
        canvas[ty:ty + th, tx:tx + tw] = img
        self.paint_thumb(job, f, tx, ty, tw, th, img)
        head = json.dumps({"t": "tile", "key": [f, i], "dev": dev.id, "tile": job.tiles[i]}).encode()
        self.to_walls(struct.pack("<H", len(head)) + head + jpg)
        if dev.checked < CHECK_FIRST or random.random() < CHECK_RATE or (dev.checks and not all(dev.checks)):
            dev.checked += 1
            job.pending_checks += 1
            rng = np.random.default_rng()
            xs = rng.integers(0, tw, SAMPLES)
            ys = rng.integers(0, th, SAMPLES)
            self.verify_q.put((job.id, dev.id, key, job.frames[f], tx, ty, xs, ys, img[ys, xs].copy()))
        job.frame_left[f] -= 1
        if job.frame_left[f] == 0 and job.kind == "movie":
            self.save_frame(job, f)
        self.top_up(dev)
        self.check_finished()

    def paint_thumb(self, job, f, tx, ty, tw, th, img):
        if job.kind == "movie" and f < job.thumb_frame:
            return
        if f != job.thumb_frame:
            job.thumb_frame = f
            job.thumb[:] = 0
        s = THUMB_W / job.width
        x0, y0 = int(tx * s), int(ty * s)
        x1, y1 = max(x0 + 1, int(round((tx + tw) * s))), max(y0 + 1, int(round((ty + th) * s)))
        small = Image.fromarray(img).resize((x1 - x0, y1 - y0), Image.BILINEAR)
        job.thumb[y0:y1, x0:x1] = np.asarray(small)
        job.thumb_dirty = True

    def save_frame(self, job, f):
        if f in job.canvas:
            Image.fromarray(job.canvas[f]).save(os.path.join(job.out, f"frame_{f:04d}.jpg"), quality=92)
            del job.canvas[f]
            for k in [k for k in job.jpeg if k[0] == f]:
                del job.jpeg[k]
            job.frames_saved += 1
            self.to_walls({"t": "frame", "frame": f})

    def on_verified(self, job_id, dev_id, key, ok, frac):
        job = self.job
        dev = self.devices.get(dev_id)
        if job is None or job.id != job_id:
            return
        job.pending_checks -= 1
        if dev is None or dev.status == "untrusted":
            self.check_finished()
            return
        dev.checks.append(ok)
        dev.match.append(frac)
        if not ok:
            fails = sum(1 for c in dev.checks if not c)
            self.event(f"Check failed: square from {dev.name} does not match ({frac:.0%} of pixels right)", "warn")
            if fails >= BAN_AFTER:
                self.ban(dev)
        self.check_finished()

    def ban(self, dev):
        job = self.job
        dev.status = "untrusted"
        revoked = [k for k in dev.done_keys if job and job.owner.get(k) == dev.id]
        for k in revoked:
            del job.owner[k]
            job.done_w -= job.weight[k[1]]
            job.jpeg.pop(k, None)
            job.queue.appendleft(k)
            f, i = k
            tx, ty, tw, th = job.tiles[i]
            job.frame_canvas(f)[ty:ty + th, tx:tx + tw] = 0
            if job.frame_left[f] == 0 and job.kind == "movie":
                job.frames_saved -= 1      # the frame is saved again once the square is redone
            job.frame_left[f] += 1
        if job:
            job.revoked += len(revoked)
            job.caught.append(dev.name)
        dev.done = 0
        dev.ms_sum = 0.0
        dev.done_keys.clear()
        self.requeue(dev, "was removed")
        self.event(f"Caught! {dev.name} sent wrong pixels — removed; its {len(revoked)} squares are being redone",
                   "caught")
        self.to_walls({"t": "revoke", "dev": dev.id, "keys": [list(k) for k in revoked]})
        self.send_soon(dev.ws, {"t": "banned", "reason": "Your device sent pixels that did not match the host's check."})
        for d in self.active():
            self.top_up(d)

    def check_finished(self):
        job = self.job
        if job is None or job.finished or len(job.owner) < job.total or job.pending_checks > 0:
            return
        job.finished = time.time()
        threading.Thread(target=self.save_result, args=(job,), daemon=True).start()
        elapsed = job.finished - job.started
        contributors = [d for d in self.devices.values() if d.done > 0 and d.status != "untrusted"]
        rows = []
        for d in sorted(contributors, key=lambda d: -d.done):
            alone = d.alone_seconds(job.total)
            rows.append({"id": d.id, "name": d.name, "maker": d.maker, "gpu": d.gpu, "color": d.color,
                         "done": d.done, "share": round(100 * d.done / job.total, 1), "alone": round(alone, 1)})
        best = min(rows, key=lambda r: r["alone"]) if rows else None
        job.summary = {"elapsed": round(elapsed, 1), "devices": len(contributors),
                       "makers": sorted({d.maker for d in contributors}), "rows": rows,
                       "best_alone": best, "speedup": round(best["alone"] / elapsed, 1) if best and elapsed else None,
                       "caught": job.caught, "revoked": job.revoked, "moved": job.moved,
                       "rays": job.rays, "total": job.total, "title": job.title, "kind": job.kind}
        self.event(f"Finished {job.title} in {fmt_time(elapsed)} with {len(contributors)} devices", "done")
        for d in contributors:
            if d.match:
                print(f"[swarm] checks for {d.name} ({d.gpu}): {len(d.match)} squares checked, "
                      f"average match {100 * sum(d.match) / len(d.match):.0f}%", flush=True)
        print(f"[swarm] coordinator time per square: {1000 * job.accept_time / max(job.accept_n, 1):.2f} ms "
              f"({job.accept_n} results)", flush=True)
        self.to_walls({"t": "finished", "job": job.public()})
        for d in self.devices.values():
            mine = next((r for r in rows if r["id"] == d.id), None)
            self.send_soon(d.ws, {"t": "done", "elapsed": round(elapsed, 1), "mine": mine,
                                  "devices": len(contributors), "title": job.title, "kind": job.kind})
            if d.status == "working":
                d.status = "idle"

    def save_result(self, job):
        try:
            if job.kind == "poster":
                img = Image.fromarray(job.canvas[0])
                path = os.path.join(job.out, "poster.jpg")
                img.save(path, quality=94)
                img.reduce(4).save(os.path.join(job.out, "poster_small.jpg"), quality=90)
                job.result_file = path
            else:
                for f in list(job.canvas):
                    self.save_frame(job, f)
                ff = shutil.which("ffmpeg") or os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe")
                mp4 = os.path.join(job.out, "movie.mp4")
                subprocess.run([ff, "-y", "-loglevel", "error", "-framerate", "24", "-i",
                                os.path.join(job.out, "frame_%04d.jpg"), "-c:v", "libx264", "-pix_fmt", "yuv420p",
                                "-crf", "18", mp4], capture_output=True, timeout=300)
                job.result_file = mp4 if os.path.exists(mp4) else None
            self.loop.call_soon_threadsafe(self.to_walls, {"t": "saved", "file": os.path.basename(job.result_file or "")})
            print(f"[swarm] saved {job.result_file}", flush=True)
        except Exception as e:
            print(f"[swarm] could not save the result: {e}", flush=True)

    # ------------------------------------------------------------ checking (background thread)
    def verify_loop(self):
        while True:
            items = [self.verify_q.get()]
            try:
                while len(items) < 24:
                    items.append(self.verify_q.get_nowait())
            except queue.Empty:
                pass
            groups = collections.defaultdict(list)
            for it in items:
                groups[id(it[3])].append(it)
            for group in groups.values():
                fp = group[0][3]
                xs = np.concatenate([it[4] + it[6] for it in group])
                ys = np.concatenate([it[5] + it[7] for it in group])
                ref = S.render_pixels(fp, xs, ys).astype(int)
                for k, it in enumerate(group):
                    got = it[8].astype(int)
                    r = ref[k * SAMPLES:(k + 1) * SAMPLES]
                    frac = float(np.mean(np.abs(got - r).max(1) <= TOLERANCE))
                    self.loop.call_soon_threadsafe(self.on_verified, it[0], it[1], it[2], frac >= PASS_FRACTION, frac)

    # ------------------------------------------------------------ connections
    async def handle(self, ws):
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 15))
        except Exception:
            return
        if hello.get("role") == "wall":
            await self.handle_wall(ws)
        else:
            await self.handle_worker(ws, hello)

    async def handle_wall(self, ws):
        # Only a wall opened on this laptop may press Start / Reset / Cheat; anyone else just watches.
        ip = (ws.remote_address or ("",))[0]
        control = ip in ("127.0.0.1", "::1", self.lan_ip) or ip.startswith("::ffff:127.")
        self.walls.add(ws)
        await self.send(ws, {"t": "hello", "control": control})
        try:
            await self.send(ws, self.state())
            job = self.job
            if job:
                await self.send(ws, {"t": "job", "job": job.public(), "tiles": job.tiles})
                for key, did in list(job.owner.items()):
                    if key in job.jpeg:
                        head = json.dumps({"t": "tile", "key": list(key), "dev": did, "tile": job.tiles[key[1]],
                                           "replay": True}).encode()
                        await self.send(ws, struct.pack("<H", len(head)) + head + job.jpeg[key])
                if job.finished:
                    await self.send(ws, {"t": "finished", "job": job.public()})
            async for msg in ws:
                try:
                    m = json.loads(msg)
                except (ValueError, TypeError):
                    continue
                t = m.get("t")
                if not control:
                    continue
                if t == "start":
                    self.start(m.get("preset", self.preset))
                elif t == "preset" and m.get("preset") in PRESETS:
                    self.preset = m["preset"]
                elif t == "reset":
                    self.reset()
                elif t == "cheat" and m.get("id") in self.devices:
                    d = self.devices[m["id"]]
                    d.cheat = bool(m.get("on"))
                    await self.send(d.ws, {"t": "cheat", "on": d.cheat})
                    self.event(f"Demo: {d.name} told to {'cheat (skip most of the work)' if d.cheat else 'stop cheating'}",
                               "warn")
                elif t == "kick" and m.get("id") in self.devices:
                    await self.devices[m["id"]].ws.close()
        except Exception as e:
            if not isinstance(e, ConnectionClosed):
                print(f"[swarm] wall error: {e!r}", flush=True)
                import traceback
                traceback.print_exc()
        finally:
            self.walls.discard(ws)

    async def handle_worker(self, ws, hello):
        did = self.next_id
        self.next_id += 1
        dev = Device(ws, did, hello, COLORS[(did - 1) % len(COLORS)])
        self.devices[did] = dev
        self.event(f"{dev.name} joined — {dev.gpu} ({dev.maker})", "join")
        await self.send(ws, {"t": "welcome", "id": did, "name": dev.name, "color": dev.color, "scene": scene_hash(),
                             "calib": {"fp": S.frame_params(0.0, 3840, 2160), "tile": CALIB_TILE}})
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    n = struct.unpack_from("<H", msg)[0]
                    hdr = json.loads(msg[2:2 + n])
                    self.accept(dev, hdr, msg[2 + n:])
                    continue
                m = json.loads(msg)
                if m.get("t") == "calib":
                    dev.calib_ms = float(m.get("ms", 100))
                    if self.job and not self.job.finished:
                        dev.status = "working"
                        await self.send(ws, {"t": "job", "job": self.job.id, "frames": self.job.frames,
                                             "title": self.job.title, "total": self.job.total})
                        self.top_up(dev)
        except Exception as e:
            if not isinstance(e, ConnectionClosed):
                print(f"[swarm] device error: {e!r}", flush=True)
                import traceback
                traceback.print_exc()
        finally:
            if dev.status not in ("untrusted",):
                was_working = bool(dev.inflight)
                dev.status = "left"
                self.requeue(dev, "left")
                if not was_working:
                    self.event(f"{dev.name} left")
            if self.job is None or dev.done == 0:
                self.devices.pop(did, None)

    async def ticker(self):
        n = 0
        while True:
            await asyncio.sleep(0.25)
            n += 1
            now = time.time()
            job = self.job
            if job and not job.finished:
                for d in self.active():
                    if d.inflight:
                        oldest = min(d.inflight.values())
                        limit = max(8.0, 6 * d.avg_ms() / 1000 * INFLIGHT)
                        if now - oldest > limit and d.status != "asleep":
                            d.status = "asleep"
                            self.requeue(d, "stopped answering (screen off?)")
                    elif d.status == "asleep" and now - d.last_result < 2:
                        d.status = "working"
                    if not d.inflight and d.status in ("working", "asleep"):
                        self.top_up(d)
            if self.walls:
                self.to_walls(self.state())
            if n % 4 == 0:
                for d in self.active():
                    total = job.total if job else 1
                    self.send_soon(d.ws, {"t": "me", "done": d.done, "share": round(100 * d.done / total, 1),
                                          "rate": round(d.rate(), 2), "progress": round(job.progress(), 3) if job else 0,
                                          "eta": job.public()["eta"] if job else None,
                                          "devices": len(self.active())})
            if job and job.thumb_dirty and n % 6 == 0:
                job.thumb_dirty = False
                b = io.BytesIO()
                Image.fromarray(job.thumb).save(b, "JPEG", quality=80)
                data = b"T" + b.getvalue()
                for d in self.active():
                    self.send_soon(d.ws, data)

    # ------------------------------------------------------------ HTTP
    def process_request(self, connection, request):
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        static = {"/": ("swarm.html", "text/html; charset=utf-8"), "/join": ("swarm.html", "text/html; charset=utf-8"),
                  "/wall": ("swarm_wall.html", "text/html; charset=utf-8"),
                  "/swarm_render.js": ("swarm_render.js", "text/javascript; charset=utf-8"),
                  "/swarm_scene.glsl": ("swarm_scene.glsl", "text/plain; charset=utf-8"),
                  "/keepawake.mp4": ("keepawake.mp4", "video/mp4")}
        body, ctype = None, None
        if path in static:
            name, ctype = static[path]
            fp = os.path.join(WEB_DIR, name)
            if os.path.exists(fp):
                with open(fp, "rb") as fh:
                    body = fh.read()
        elif path == "/qr.png":
            import qrcode
            b = io.BytesIO()
            qrcode.make(self.url, box_size=10, border=2).save(b, format="PNG")
            body, ctype = b.getvalue(), "image/png"
        elif path in ("/result.jpg", "/result_small.jpg", "/result.mp4") and self.job and self.job.result_file:
            fp = self.job.result_file
            if path == "/result_small.jpg":
                fp = os.path.join(self.job.out, "poster_small.jpg")
            if os.path.exists(fp):
                with open(fp, "rb") as fh:
                    body = fh.read()
                ctype = "video/mp4" if fp.endswith(".mp4") else "image/jpeg"
        elif path.startswith("/frames/") and self.job:
            try:
                f = int(path.rsplit("/", 1)[1].split(".")[0])
                fp = os.path.join(self.job.out, f"frame_{f:04d}.jpg")
                if os.path.exists(fp):
                    with open(fp, "rb") as fh:
                        body = fh.read()
                    ctype = "image/jpeg"
            except ValueError:
                pass
        if body is None:
            body = b"not found"
            return Response(404, "Not Found", Headers([("Content-Length", str(len(body)))]), body)
        return Response(http.HTTPStatus.OK, "OK", Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                                                             ("Cache-Control", "no-store")]), body)

    async def main(self):
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self.verify_loop, daemon=True).start()
        async with serve(self.handle, "0.0.0.0", self.args.port, process_request=self.process_request,
                         max_size=8 * 2 ** 20, ping_interval=10, ping_timeout=20):
            print("=" * 60)
            print("  ROOM SUPERCOMPUTER coordinator")
            print(f"  Phones join:      {self.url}")
            print(f"  Projector wall:   {self.url}/wall")
            print("=" * 60, flush=True)
            await self.ticker()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    try:
        asyncio.run(Swarm(args).main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
