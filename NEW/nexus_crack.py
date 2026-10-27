"""
nexus_crack.py -- "How strong is your password?" (a password-strength demo).

The room's phones pool their GPUs to brute-force a SHORT password that THIS
program generates at random, just to show two things, both defensive:

  * how fast a short/simple password falls when many GPUs guess together, and
  * how adding a few characters makes cracking take longer than a lifetime.

Safety: the target is always a random password this program makes up for the
demo. There is no way to load an outside password or hash, no real wordlist,
and the live search is capped to small keyspaces. It is a teaching tool about
password strength, not an attack tool.

    python nexus_crack.py          (then open http://<this-ip>:8798/wall)
"""

import argparse
import asyncio
import collections
import hashlib
import http
import io
import json
import os
import random
import socket
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
PORT = 8798
W = H = 1024
REPS = 32
UNIT = REPS * W * H                 # candidates per work unit (~33 million)
INFLIGHT = 10                       # keep each GPU's queue deep so it never waits on the network
COLORS = ["#4F8EF7", "#F58A3D", "#3CC488", "#E85FA0", "#A57BF5", "#F2C744", "#36C2D6", "#F0645A",
          "#9CCC50", "#C9925B", "#7F8CFF", "#FF9DD0", "#5ED3B8", "#D7A6FF", "#FFB74D", "#90A4AE"]

LOWER = "abcdefghijklmnopqrstuvwxyz"
UPPER = LOWER.upper()
DIGITS = "0123456789"
ALL94 = "".join(chr(c) for c in range(33, 127))
CHARSETS = {"lower": LOWER, "lowernum": LOWER + DIGITS, "lowerupper": LOWER + UPPER, "all": ALL94}
MAX_LIVE = 4_000_000_000        # biggest keyspace we brute-force live (GPU index is 32-bit); bigger -> "too strong"
# smallest-first character-set tiers, used to pick the fairest keyspace for a typed password
TIERS = [
    ("just digits", DIGITS),
    ("lowercase letters", LOWER),
    ("letters + digits", LOWER + DIGITS),
    ("upper & lower letters", LOWER + UPPER),
    ("letters + digits (mixed case)", LOWER + UPPER + DIGITS),
    ("letters, digits & symbols", ALL94),
]


def charset_for(pw):
    """Smallest standard character set that contains every character of pw."""
    chars = set(pw)
    for label, cs in TIERS:
        if chars <= set(cs):
            return label, cs
    return None, None


def password_index(pw, charset):
    """Inverse of password_at: the keyspace index of this exact password."""
    c = len(charset)
    return sum(charset.index(ch) * (c ** p) for p, ch in enumerate(pw))
# live options the presenter can pick (all small enough to crack in the room)
DIFFICULTIES = [
    {"key": "l4", "label": "4 lowercase letters", "charset": "lower", "len": 4},
    {"key": "l5", "label": "5 lowercase letters", "charset": "lower", "len": 5},
    {"key": "l6", "label": "6 lowercase letters", "charset": "lower", "len": 6},
    {"key": "n5", "label": "5 letters + digits", "charset": "lowernum", "len": 5},
    {"key": "n6", "label": "6 letters + digits", "charset": "lowernum", "len": 6},
]
# reference rows for the "how long would THIS take" table (estimate only, never run)
TABLE = [
    {"label": "6 lowercase letters", "size": 26, "len": 6},
    {"label": "8 lowercase letters", "size": 26, "len": 8},
    {"label": "8 letters + digits", "size": 36, "len": 8},
    {"label": "8 letters, digits & symbols", "size": 94, "len": 8},
    {"label": "10 letters, digits & symbols", "size": 94, "len": 10},
    {"label": "12 letters, digits & symbols", "size": 94, "len": 12},
]


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def password_at(idx, charset, length):
    c = len(charset)
    return "".join(charset[(idx // (c ** p)) % c] for p in range(length))


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
        self.status = "idle"
        self.blocks = collections.deque()   # (start, end) blocks assigned but not yet done (FIFO);
        self.block_base = 0                 # progress within the oldest block, for reclaim
        self.last_report = time.time()
        self.guesses = 0
        self.hist = collections.deque(maxlen=12)     # (time, tried)
        self.best_rate = 0.0

    def rate(self):
        now = time.time()
        pts = [(t, n) for t, n in self.hist if now - t < 4]
        if len(pts) < 1:
            return 0.0
        span = max(now - pts[0][0], 0.4)
        r = sum(n for _, n in pts) / span
        self.best_rate = max(self.best_rate, r)
        return r

    def public(self):
        return {"id": self.id, "name": self.name, "gpu": self.gpu, "maker": self.maker, "kind": self.kind,
                "color": self.color, "status": self.status, "guesses": self.guesses,
                "rate": round(self.rate())}


class Crack:
    def __init__(self, args):
        self.args = args
        self.devices = {}
        self.walls = set()
        self.next_id = 1
        self.lan_ip = get_lan_ip()
        self.url = f"http://{self.lan_ip}:{args.port}"
        self.loop = None
        self.events = collections.deque(maxlen=40)
        self.diff = DIFFICULTIES[2]        # default: 6 lowercase letters
        self.peak_rate = 0.0               # best pooled guess rate ever seen (persists across cracks)
        self.reset_job()

    def reset_job(self):
        self.phase = "idle"                 # idle / cracking / done
        self.charset = ""
        self.length = 0
        self.total = 0
        self.target = None
        self.secret = None
        self.job_id = getattr(self, "job_id", 0) + 1
        self.cursor = 0                     # next unassigned index in the keyspace
        self.blocksize = UNIT
        self.leftover = collections.deque() # blocks reclaimed from devices that left
        self.guesses = 0
        self.started = None
        self.finished = None
        self.found_by = None
        self.found_pw = None
        self.mode = "random"                # "random" or "typed"
        self.label = ""                     # human description of the current target
        self.too_strong = False             # a typed password whose keyspace is too big to brute-force live
        self.rate_hist = collections.deque(maxlen=20)

    # ------------------------------------------------------------- helpers
    def event(self, text, kind="info"):
        self.events.appendleft({"time": time.strftime("%H:%M:%S"), "text": text, "kind": kind})
        print(f"[crack] {text}", flush=True)

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

    def send_soon_all(self, msg):
        data = msg if isinstance(msg, (bytes, str)) else json.dumps(msg)
        for d in self.active():
            self.send_soon(d.ws, data)

    def active(self):
        return [d for d in self.devices.values() if d.status != "left"]

    def pooled_rate(self):
        return sum(d.rate() for d in self.active())

    def state(self):
        return {"t": "state", "url": self.url, "phase": self.phase, "mode": self.mode, "too_strong": self.too_strong,
                "difficulties": DIFFICULTIES, "diff": self.diff["key"], "table": TABLE, "max_live": MAX_LIVE,
                "charset_size": len(self.charset) if self.charset else len(CHARSETS[self.diff["charset"]]),
                "length": self.length or self.diff["len"],
                "diff_label": self.label or self.diff["label"],
                "total": self.total, "guesses": self.guesses,
                "progress": round(self.guesses / self.total, 4) if self.total else 0,
                "rate": round(self.pooled_rate()), "devices": [d.public() for d in self.devices.values()],
                "active": len(self.active()), "makers": sorted({d.maker for d in self.active()}),
                "elapsed": round((self.finished or time.time()) - self.started, 2) if self.started else 0,
                "found_by": self.found_by, "found_pw": self.found_pw if self.phase == "done" else None,
                "peak_rate": round(self.peak_rate),
                "events": list(self.events)[:8], "unit": UNIT}

    # ------------------------------------------------------------- job control
    def set_diff(self, key):
        for d in DIFFICULTIES:
            if d["key"] == key:
                self.diff = d

    def start(self, key=None, pw=None):
        if self.phase == "cracking":
            return
        if pw is not None:
            self.start_typed(pw)
            return
        if key:
            self.set_diff(key)
        self.reset_job()
        self.mode = "random"
        self.charset = CHARSETS[self.diff["charset"]]
        self.length = self.diff["len"]
        self.label = f"a random {self.diff['label']} password"
        self.total = len(self.charset) ** self.length
        self.secret = random.randrange(self.total)
        secret_pw = password_at(self.secret, self.charset, self.length)
        self.target = list(struct.unpack(">8I", hashlib.sha256(secret_pw.encode()).digest()))
        self.launch_crack()

    def start_typed(self, pw):
        pw = "".join(ch for ch in str(pw) if 33 <= ord(ch) <= 126)[:12]
        if not pw:
            self.event("Type a password of ordinary keyboard characters (no spaces), up to 12 long.", "warn")
            self.to_walls(self.state())
            return
        self.reset_job()
        self.mode = "typed"
        label, cs = charset_for(pw)
        self.charset = cs
        self.length = len(pw)
        self.total = len(cs) ** self.length
        self.label = f"your {self.length}-character password ({label})"   # stays masked until found
        self.found_pw = pw
        self.tier_label = label
        if self.total > MAX_LIVE:
            # too big to actually brute-force here -- show why it's safe, don't run an impossible search
            self.phase = "done"
            self.too_strong = True
            self.started = self.finished = time.time()
            self.event(f'"{pw}" is too strong to brute-force here ({self.total:,.0f} possibilities) -- '
                       f"that's exactly what you want", "done")
            self.to_walls({"t": "cracked", "state": self.state()})
            return
        self.secret = password_index(pw, cs)
        self.target = list(struct.unpack(">8I", hashlib.sha256(pw.encode()).digest()))
        self.launch_crack()

    def launch_crack(self):
        n = max(1, len(self.active()))
        # each device should get ~6 blocks, aligned to the work-unit size
        units = max(1, round(self.total / (n * 6) / UNIT))
        self.blocksize = min(units, 64) * UNIT
        self.phase = "cracking"
        self.started = time.time()
        self.event(f"Cracking {self.label} ({self.total:,} possible) with {n} device(s)", "start")
        tmsg = {"t": "target", "job": self.job_id, "target": self.target,
                "charset": [ord(c) for c in self.charset], "passLen": self.length, "total": self.total, "reps": REPS}
        for d in self.active():
            d.status = "cracking"
            d.blocks.clear()
            d.guesses = 0
            self.send_soon(d.ws, tmsg)
            self.assign_block(d)
        self.to_walls(self.state())

    def reset(self):
        self.reset_job()
        for d in self.devices.values():
            if d.status != "left":
                d.status = "idle"
            d.blocks.clear()
            d.guesses = 0
            self.send_soon(d.ws, {"t": "idle"})
        self.to_walls({"t": "reset"})
        self.event("Reset -- pick a difficulty and start again")

    def next_block(self):
        if self.leftover:
            return self.leftover.popleft()
        if self.cursor < self.total:
            start = self.cursor
            end = min(start + self.blocksize, self.total)
            self.cursor = end
            return (start, end)
        return None

    def assign_block(self, dev):
        """Keep each device holding up to 2 blocks, so it starts the next the instant
        it finishes one -- it never waits for a network round-trip."""
        if self.phase != "cracking" or dev.status == "left":
            return
        while len(dev.blocks) < 2:
            blk = self.next_block()
            if blk is None:
                if not any(d.blocks for d in self.active()):
                    self.finish(None)       # whole keyspace searched, nothing found
                return
            if not dev.blocks:
                dev.block_base = blk[0]
            dev.blocks.append(blk)
            dev.last_report = time.time()
            self.send_soon(dev.ws, {"t": "work", "job": self.job_id, "start": blk[0], "end": blk[1], "reps": REPS})

    def reclaim(self, dev):
        for i, blk in enumerate(dev.blocks):
            lo = max(dev.block_base, blk[0]) if i == 0 else blk[0]
            if lo < blk[1]:
                self.leftover.append((lo, blk[1]))
        dev.blocks.clear()

    def on_blockdone(self, dev):
        if dev.blocks:
            dev.blocks.popleft()
            dev.block_base = dev.blocks[0][0] if dev.blocks else dev.block_base
        self.assign_block(dev)

    def on_progress(self, dev, tried, base):
        dev.guesses += tried
        self.guesses += tried
        dev.block_base = base
        dev.last_report = time.time()
        dev.hist.append((time.time(), tried))

    def on_found(self, dev, found):
        pw = password_at(found, self.charset, self.length)
        if list(struct.unpack(">8I", hashlib.sha256(pw.encode()).digest())) == self.target:
            self.finish(dev, pw)
            return True
        return False

    def finish(self, dev, pw=None):
        if self.phase == "done":
            return
        self.phase = "done"
        self.finished = time.time()
        self.found_by = dev.name if dev else None
        self.found_pw = pw
        for d in self.active():
            d.status = "idle"
            d.blocks.clear()
        self.send_soon_all({"t": "idle"})
        elapsed = self.finished - self.started
        if pw:
            avg = self.guesses / max(elapsed, 0.001)
            self.event(f"CRACKED! The password was \"{pw}\" -- found by {self.found_by} in {elapsed:.1f}s "
                       f"after {self.guesses:,} guesses ({avg/1e6:.0f} million/sec pooled)", "done")
        else:
            self.event("Search finished.", "done")
        self.to_walls({"t": "cracked", "state": self.state()})

    # ------------------------------------------------------------- connections
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
        ip = (ws.remote_address or ("",))[0]
        control = ip in ("127.0.0.1", "::1", self.lan_ip) or ip.startswith("::ffff:127.")
        self.walls.add(ws)
        await self.send(ws, {"t": "hello", "control": control})
        await self.send(ws, self.state())
        try:
            async for msg in ws:
                try:
                    m = json.loads(msg)
                except (ValueError, TypeError):
                    continue
                if not control:
                    continue
                t = m.get("t")
                if t == "start":
                    self.start(m.get("diff"), (m.get("pw") or None))
                elif t == "diff":
                    self.set_diff(m.get("diff"))
                    self.to_walls(self.state())
                elif t == "reset":
                    self.reset()
                elif t == "kick" and m.get("id") in self.devices:
                    await self.devices[m["id"]].ws.close()
        except Exception as e:
            print(f"[crack] wall error: {e!r}", flush=True)
        finally:
            self.walls.discard(ws)

    async def handle_worker(self, ws, hello):
        did = self.next_id
        self.next_id += 1
        dev = Device(ws, did, hello, COLORS[(did - 1) % len(COLORS)])
        self.devices[did] = dev
        self.event(f"{dev.name} joined -- {dev.gpu} ({dev.maker})", "join")
        await self.send(ws, {"t": "welcome", "id": did, "name": dev.name, "color": dev.color})
        if self.phase == "cracking":
            dev.status = "cracking"
            self.send_soon(ws, {"t": "target", "job": self.job_id, "target": self.target,
                                "charset": [ord(c) for c in self.charset], "passLen": self.length,
                                "total": self.total, "reps": REPS})
            self.assign_block(dev)
        try:
            async for msg in ws:
                try:
                    m = json.loads(msg)
                except (ValueError, TypeError):
                    continue
                if m.get("job") != self.job_id or self.phase != "cracking":
                    continue
                t = m.get("t")
                if t == "progress":
                    self.on_progress(dev, int(m.get("tried", 0)), int(m.get("base", 0)))
                elif t == "found":
                    self.on_progress(dev, int(m.get("tried", 0)), int(m.get("base", 0)))
                    if self.on_found(dev, int(m.get("found", -1))):
                        break
                elif t == "blockdone":
                    self.on_progress(dev, int(m.get("tried", 0)), int(m.get("base", 0)))
                    self.on_blockdone(dev)
        except Exception:
            pass
        finally:
            dev.status = "left"
            self.reclaim(dev)
            self.event(f"{dev.name} left")
            if self.phase != "cracking":
                self.devices.pop(did, None)

    async def ticker(self):
        while True:
            await asyncio.sleep(0.4)
            if self.phase == "cracking":
                now = time.time()
                for d in self.active():
                    if d.blocks and now - d.last_report > 12:    # a stalled device: reclaim its remaining work
                        self.reclaim(d)
                    if len(d.blocks) < 2 and (self.leftover or self.cursor < self.total):
                        self.assign_block(d)
                pr = self.pooled_rate()
                self.rate_hist.append(pr)
                self.peak_rate = max(self.peak_rate, pr)
            if self.walls:
                self.to_walls(self.state())

    # ------------------------------------------------------------- HTTP
    def process_request(self, connection, request):
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        static = {"/": ("crack.html", "text/html; charset=utf-8"), "/join": ("crack.html", "text/html; charset=utf-8"),
                  "/wall": ("crack_wall.html", "text/html; charset=utf-8"),
                  "/crack_render.js": ("crack_render.js", "text/javascript; charset=utf-8"),
                  "/crack_sha256.glsl": ("crack_sha256.glsl", "text/plain; charset=utf-8"),
                  "/keepawake.mp4": ("keepawake.mp4", "video/mp4")}
        body, ctype = None, "text/html; charset=utf-8"
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
        if body is None:
            body = b"not found"
            return Response(404, "Not Found", Headers([("Content-Length", str(len(body)))]), body)
        return Response(http.HTTPStatus.OK, "OK", Headers([("Content-Type", ctype), ("Content-Length", str(len(body))),
                                                             ("Cache-Control", "no-store")]), body)

    async def main(self):
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop()
        async with serve(self.handle, "0.0.0.0", self.args.port, process_request=self.process_request,
                         max_size=2 ** 20, ping_interval=15, ping_timeout=30):
            print("=" * 60)
            print("  PASSWORD STRENGTH LAB -- how strong is your password?")
            print(f"  Phones join:     {self.url}")
            print(f"  Projector wall:  {self.url}/wall")
            print("=" * 60, flush=True)
            await self.ticker()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    try:
        asyncio.run(Crack(args).main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
