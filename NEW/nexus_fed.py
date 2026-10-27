"""
nexus_fed.py -- "Train an AI together" (federated learning).

Everyone in the room scans a QR code. Each phone gets its own private slice of
handwriting examples that never leaves the phone. Every round:

  * the coordinator (this laptop) sends the current shared AI to every phone;
  * each phone trains it a little on its own data, using the phone's GPU
    (TensorFlow.js), and sends back only the adjusted numbers -- not the data;
  * the coordinator checks each phone's update against a held-out test set,
    throws out any that look poisoned, averages the good ones, and measures
    how accurate the shared AI has become.

The accuracy climbs on the projector round by round, and climbs faster with
more phones -- because together they learn from more private data at once.

    python nexus_fed.py            (then open http://<this-ip>:8795/wall)
"""

import argparse
import asyncio
import collections
import http
import io
import json
import os
import random
import socket
import struct
import sys
import threading
import time

import numpy as np

import fed_model as M

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
DATA = os.path.join(HERE, "data", "mnist.npz")
PORT = 8795
SHARD_SIZE = 2000              # private examples per shard (one shard per phone)
N_SHARDS = 30                  # 30 * 2000 = 60,000 training images
ROUND_TIMEOUT = 25.0           # seconds to wait for phones each round
ROUND_GAP = 0.6                # pause between rounds so the chart is readable
VAL_N = 1500                   # test images used to screen each update
TEST_N = 10000
POISON_FLOOR = 0.30            # an update scoring below this on the test set is thrown out
COLORS = ["#4F8EF7", "#F58A3D", "#3CC488", "#E85FA0", "#A57BF5", "#F2C744", "#36C2D6", "#F0645A",
          "#9CCC50", "#C9925B", "#7F8CFF", "#FF9DD0", "#5ED3B8", "#D7A6FF", "#FFB74D", "#90A4AE"]


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Device:
    def __init__(self, ws, did, hello, color, shard):
        self.ws = ws
        self.id = did
        self.kind = hello.get("kind") if hello.get("kind") in ("phone", "tablet", "laptop", "cpu") else "phone"
        default = {"phone": "Phone", "tablet": "Tablet", "laptop": "Laptop", "cpu": "Laptop CPU"}[self.kind]
        self.name = (str(hello.get("name") or "").strip()[:24]) or f"{default} {did}"
        self.gpu = str(hello.get("gpu") or "GPU")[:60]
        self.maker = str(hello.get("maker") or "Other")[:20]
        self.color = color
        self.shard = shard
        self.status = "ready"       # ready / training / done / rejected / poisoned / left
        self.rounds_done = 0
        self.samples_total = 0
        self.last_ms = 0.0
        self.last_acc = None        # how its last update scored on the test set
        self.rejected = 0
        self.poison = False
        self.ready_round = 0

    def public(self):
        return {"id": self.id, "name": self.name, "gpu": self.gpu, "maker": self.maker, "kind": self.kind,
                "color": self.color, "status": self.status, "shard": self.shard, "rounds": self.rounds_done,
                "samples": self.samples_total, "ms": round(self.last_ms), "acc": self.last_acc,
                "rejected": self.rejected, "poison": self.poison}


class Fed:
    def __init__(self, args):
        self.args = args
        self.devices = {}
        self.walls = set()
        self.next_id = 1
        self.lan_ip = get_lan_ip()
        self.url = f"http://{self.lan_ip}:{args.port}"
        self.loop = None
        self.events = collections.deque(maxlen=40)
        self.load_data()
        self.reset_model()
        # training state
        self.running = False
        self.phase = "idle"         # idle / training / done
        self.round = 0
        self.total_rounds = args.rounds
        self.epochs = args.epochs
        self.lr = args.lr
        self.history = []           # per round: {round, acc, images, devices, rejected}
        self.updates = {}           # dev_id -> (flat, n, ms) for the current round
        self.expected = set()
        self.started_at = None
        self.train_task = None

    # ------------------------------------------------------------- data
    def load_data(self):
        d = np.load(DATA)
        xtr = d["x_train"].reshape(-1, 784).astype(np.uint8)
        ytr = d["y_train"].astype(np.uint8)
        rng = np.random.default_rng(42)
        idx = rng.permutation(len(xtr))
        xtr, ytr = xtr[idx], ytr[idx]
        self.shard_bytes = []
        for s in range(N_SHARDS):
            a, b = s * SHARD_SIZE, (s + 1) * SHARD_SIZE
            xi, yi = xtr[a:b], ytr[a:b]
            buf = struct.pack("<I", len(xi)) + xi.tobytes() + yi.tobytes()
            self.shard_bytes.append(buf)
        xte = d["x_test"].reshape(-1, 784).astype(np.float32) / 255.0
        yte = d["y_test"].astype(np.int64)
        self.Xtest, self.ytest = xte[:TEST_N], yte[:TEST_N]
        self.Xval, self.yval = xte[:VAL_N], yte[:VAL_N]      # for screening updates
        print(f"[fed] loaded MNIST: {N_SHARDS} private shards of {SHARD_SIZE}, test set {len(self.Xtest)}", flush=True)

    def reset_model(self):
        self.weights = M.init_flat(seed=0)
        self.base_acc = M.accuracy(self.weights, self.Xval, self.yval)

    # ------------------------------------------------------------- helpers
    def event(self, text, kind="info"):
        self.events.appendleft({"time": time.strftime("%H:%M:%S"), "text": text, "kind": kind})
        print(f"[fed] {text}", flush=True)

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

    def connected(self):
        return [d for d in self.devices.values() if d.status != "left"]

    def participants(self):
        return [d for d in self.devices.values() if d.status not in ("left",)]

    def weight_msg(self, rnd):
        head = json.dumps({"t": "train", "round": rnd, "epochs": self.epochs, "lr": self.lr}).encode()
        return struct.pack("<H", len(head)) + head + M.to_bytes(self.weights)

    def state(self):
        images_total = sum(len(self.weights) and d.samples_total for d in self.devices.values())
        return {"t": "state", "url": self.url, "phase": self.phase, "round": self.round,
                "total_rounds": self.total_rounds, "epochs": self.epochs,
                "accuracy": self.history[-1]["acc"] if self.history else None,
                "history": self.history[-60:], "devices": [d.public() for d in self.devices.values()],
                "active": len(self.connected()), "makers": sorted({d.maker for d in self.connected()}),
                "images_total": images_total, "events": list(self.events)[:8],
                "shard_size": SHARD_SIZE, "classes": M.CLASSES,
                "elapsed": round(time.time() - self.started_at, 1) if self.started_at else 0}

    # ------------------------------------------------------------- training loop
    def start(self, rounds, epochs, lr):
        if self.running:
            return
        self.total_rounds = max(1, min(60, int(rounds)))
        self.epochs = max(1, min(5, int(epochs)))
        self.lr = float(lr)
        self.reset_model()
        self.history = []
        self.round = 0
        self.phase = "training"
        self.running = True
        self.started_at = time.time()
        for d in self.devices.values():
            d.rounds_done = 0
            d.samples_total = 0
            d.last_acc = None
            d.rejected = 0
            if d.status != "left":
                d.status = "ready"
        self.event(f"Training started: a shared digit-recognition AI, {self.total_rounds} rounds, "
                   f"{len(self.participants())} device(s) so far", "start")
        self.train_task = asyncio.ensure_future(self.trainer())

    def stop(self, reason="stopped"):
        self.running = False
        if self.phase == "training":
            self.phase = "done"
        self.event(f"Training {reason}", "info")

    def reset(self):
        self.running = False
        self.phase = "idle"
        self.round = 0
        self.history = []
        self.reset_model()
        for d in self.devices.values():
            if d.status != "left":
                d.status = "ready"
            d.rounds_done = 0
            d.samples_total = 0
            d.last_acc = None
            d.rejected = 0
        self.started_at = None
        self.to_walls({"t": "reset"})
        self.event("Reset -- ready to train again")

    async def trainer(self):
        try:
            for r in range(1, self.total_rounds + 1):
                if not self.running:
                    break
                self.round = r
                parts = [d for d in self.participants() if d.status != "left"]
                if not parts:
                    self.event("No devices connected -- waiting...", "warn")
                    while self.running and not [d for d in self.participants() if d.status != "left"]:
                        await asyncio.sleep(0.5)
                    parts = [d for d in self.participants() if d.status != "left"]
                self.updates = {}
                self.expected = set(d.id for d in parts)
                for d in parts:
                    d.status = "training"
                    self.send_soon(d.ws, self.weight_msg(r))
                self.to_walls(self.state())
                deadline = time.time() + ROUND_TIMEOUT
                while self.running and (self.expected - set(self.updates)) and time.time() < deadline:
                    await asyncio.sleep(0.1)
                if not self.running:
                    break
                await self.aggregate(r)
                self.to_walls(self.state())
                await asyncio.sleep(ROUND_GAP)
            self.phase = "done"
            self.running = False
            if self.history:
                best = max(h["acc"] for h in self.history)
                self.event(f"Done! The room's AI reached {best:.1%} accuracy on digits it had never seen", "done")
            self.to_walls({"t": "finished", "state": self.state()})
        except Exception as e:
            print(f"[fed] trainer error: {e!r}", flush=True)
            import traceback
            traceback.print_exc()
            self.running = False
            self.phase = "done"

    async def aggregate(self, rnd):
        subs = dict(self.updates)
        if not subs:
            self.event(f"Round {rnd}: no updates arrived in time", "warn")
            self.history.append({"round": rnd, "acc": self.history[-1]["acc"] if self.history else self.base_acc,
                                 "images": 0, "devices": 0, "rejected": 0})
            return
        # score every submitted update on the held-out test set, in a worker thread
        def score():
            out = {}
            norms = []
            for did, (flat, n, ms) in subs.items():
                va = M.accuracy(flat, self.Xval, self.yval)
                norm = float(np.linalg.norm(flat - self.weights))
                out[did] = (va, norm)
                norms.append(norm)
            return out, float(np.median(norms))
        scored, med_norm = await asyncio.get_running_loop().run_in_executor(None, score)
        accepted, rejected = [], []
        for did, (flat, n, ms) in subs.items():
            va, norm = scored[did]
            d = self.devices.get(did)
            bad = va < POISON_FLOOR or (len(subs) >= 3 and norm > 6 * med_norm + 1e-6)
            if d:
                d.last_acc = round(va, 4)
                d.last_ms = ms
                d.rounds_done += 1
                d.status = "poisoned" if (bad and d.poison) else ("rejected" if bad else "done")
            if bad:
                rejected.append((did, va))
                if d:
                    d.rejected += 1
            else:
                accepted.append((flat, n))
                if d:
                    d.samples_total += n
        if accepted:
            tot = sum(n for _, n in accepted)
            self.weights = sum(flat * (n / tot) for flat, n in accepted).astype(np.float32)
        test_acc = M.accuracy(self.weights, self.Xtest, self.ytest)
        images = sum(n for _, n in accepted)
        self.history.append({"round": rnd, "acc": round(test_acc, 4), "images": images,
                             "devices": len(accepted), "rejected": len(rejected)})
        if rejected:
            names = ", ".join(self.devices[d].name for d, _ in rejected if d in self.devices)
            self.event(f"Round {rnd}: rejected a poisoned update from {names} -- accuracy protected "
                       f"({test_acc:.1%})", "caught")
        else:
            self.event(f"Round {rnd}: {len(accepted)} device(s), learned from {images:,} private images "
                       f"-> accuracy {test_acc:.1%}", "round")

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
                    self.start(m.get("rounds", self.total_rounds), m.get("epochs", self.epochs),
                               m.get("lr", self.lr))
                elif t == "stop":
                    self.stop()
                elif t == "reset":
                    self.reset()
                elif t == "poison" and m.get("id") in self.devices:
                    d = self.devices[m["id"]]
                    d.poison = bool(m.get("on"))
                    self.send_soon(d.ws, {"t": "poison", "on": d.poison})
                    self.event(f"Demo: {d.name} told to {'poison its updates' if d.poison else 'behave honestly'}",
                               "warn")
                elif t == "kick" and m.get("id") in self.devices:
                    await self.devices[m["id"]].ws.close()
                elif t == "predict":
                    px = m.get("pixels") or []
                    if len(px) == 784:
                        digit, probs = M.predict(self.weights, px)
                        await self.send(ws, {"t": "prediction", "digit": digit,
                                             "probs": [round(p, 4) for p in probs],
                                             "trained": bool(self.history)})
        except Exception as e:
            print(f"[fed] wall error: {e!r}", flush=True)
        finally:
            self.walls.discard(ws)

    async def handle_worker(self, ws, hello):
        did = self.next_id
        self.next_id += 1
        shard = (did - 1) % N_SHARDS
        dev = Device(ws, did, hello, COLORS[(did - 1) % len(COLORS)], shard)
        self.devices[did] = dev
        self.event(f"{dev.name} joined -- {dev.gpu} ({dev.maker}), holds {SHARD_SIZE:,} private images", "join")
        await self.send(ws, {"t": "welcome", "id": did, "name": dev.name, "color": dev.color, "shard": shard,
                             "layers": M.LAYERS, "shard_size": SHARD_SIZE})
        if self.running and self.phase == "training":
            dev.status = "ready"
        try:
            async for msg in ws:
                if isinstance(msg, bytes):
                    n = struct.unpack_from("<H", msg)[0]
                    hdr = json.loads(msg[2:2 + n])
                    if hdr.get("t") == "update" and hdr.get("round") == self.round and dev.id in self.expected \
                            and dev.id not in self.updates:
                        flat = M.from_bytes(msg[2 + n:])
                        if flat.size == M.flat_size():
                            self.updates[dev.id] = (flat, int(hdr.get("n", SHARD_SIZE)), float(hdr.get("ms", 0)))
                    continue
                m = json.loads(msg)
                if m.get("t") == "ready":
                    dev.status = "ready" if self.running else "ready"
        except Exception:
            pass
        finally:
            dev.status = "left"
            self.updates.pop(did, None)
            self.expected.discard(did)
            self.event(f"{dev.name} left")
            if self.phase == "idle":
                self.devices.pop(did, None)

    async def ticker(self):
        while True:
            await asyncio.sleep(1.0)
            if self.walls:
                self.to_walls(self.state())

    # ------------------------------------------------------------- HTTP
    def process_request(self, connection, request):
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        static = {"/": ("fed.html", "text/html; charset=utf-8"), "/join": ("fed.html", "text/html; charset=utf-8"),
                  "/wall": ("fed_wall.html", "text/html; charset=utf-8"),
                  "/fed_model.js": ("fed_model.js", "text/javascript; charset=utf-8"),
                  "/vendor/tf.min.js": ("vendor/tf.min.js", "text/javascript; charset=utf-8"),
                  "/keepawake.mp4": ("keepawake.mp4", "video/mp4")}
        body, ctype = None, "text/html; charset=utf-8"
        if path in static:
            name, ctype = static[path]
            fp = os.path.join(WEB_DIR, name)
            if os.path.exists(fp):
                with open(fp, "rb") as fh:
                    body = fh.read()
        elif path.startswith("/shard/"):
            try:
                s = int(path.rsplit("/", 1)[1])
                if 0 <= s < N_SHARDS:
                    body, ctype = self.shard_bytes[s], "application/octet-stream"
            except ValueError:
                pass
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
                         max_size=8 * 2 ** 20, ping_interval=15, ping_timeout=30):
            print("=" * 60)
            print("  FEDERATED LEARNING coordinator -- Train an AI together")
            print(f"  Phones join:     {self.url}")
            print(f"  Projector wall:  {self.url}/wall")
            print("=" * 60, flush=True)
            await self.ticker()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--epochs", type=int, default=1, help="local training passes per round on each phone")
    ap.add_argument("--lr", type=float, default=0.15)
    args = ap.parse_args()
    try:
        asyncio.run(Fed(args).main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
