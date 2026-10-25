"""
pool_compute.py -- offer this laptop's GPUs to the multi-GPU pool.

Every OpenCL GPU on the machine (an NVIDIA card, an Intel or AMD integrated
GPU) becomes a separate pool worker, running in its own process on its own
port. Workers announce themselves on the LAN; pool_swarm.py finds them,
measures how fast each one is and gives each a matching share of the work.

    python pool_compute.py --serve              (random 6-digit code)
    python pool_compute.py --serve --code 123456

While running, type commands into this window:
    list          show workers
    cheat <n>     toggle "lazy" mode on worker n (skips half the work and
                  scales the result -- for demonstrating verification)
"""

import argparse
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from pool_kernels import DeviceCompute, list_gpus, short_name

DISCOVERY_PORT = 47615
ANNOUNCE_MAGIC = "COMPUTE_NEXUS_POOL"
PORT_BASE = 47620
FRAME_HDR = "<III"          # n, start, end
FRAME_HDR_SIZE = struct.calcsize(FRAME_HDR)
REPLY_HDR = "<f"            # compute milliseconds


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


class Worker:
    def __init__(self, index, dev, port, code):
        self.index, self.port, self.code = index, port, code
        self.compute = DeviceCompute(dev)
        self.lock = threading.Lock()
        self.clients = {}
        sample = np.random.default_rng(0).integers(0, 65535, (8000, 2)).astype(np.uint16)
        self.compute.compute(sample, 0, 8000)
        self.bench_ms = min(self.compute.compute(sample, 0, 8000)[1] for _ in range(3))

    def info(self):
        return {"index": self.index, "name": self.compute.name, "vendor": self.compute.vendor,
                "cu": self.compute.cu, "port": self.port, "bench_ms": self.bench_ms}

    def handle(self, ws):
        try:
            hello = json.loads(ws.recv(timeout=5))
        except Exception:
            return
        if not isinstance(hello, dict) or hello.get("code") != self.code:
            try:
                ws.send(json.dumps({"status": "rejected"}))
            except Exception:
                pass
            return
        addr = ws.remote_address
        client = hello.get("client", f"{addr[0]}:{addr[1]}")
        ws.send(json.dumps({"status": "approved", **self.info()}))
        self.clients[addr] = client
        print(f"[worker {self.index} {self.compute.name}] client joined: {client}", flush=True)
        try:
            for msg in ws:
                if not isinstance(msg, (bytes, bytearray)) or len(msg) < FRAME_HDR_SIZE:
                    continue
                n, start, end = struct.unpack_from(FRAME_HDR, msg)
                qpos = np.frombuffer(msg, dtype=np.uint16, offset=FRAME_HDR_SIZE, count=n * 2).reshape(n, 2)
                with self.lock:
                    acc, ms = self.compute.compute(qpos, start, end)
                ws.send(struct.pack(REPLY_HDR, ms) + acc.tobytes())
        except Exception:
            pass
        finally:
            self.clients.pop(addr, None)
            print(f"[worker {self.index} {self.compute.name}] client left: {client}", flush=True)

    def serve(self):
        from websockets.sync.server import serve
        with serve(self.handle, "0.0.0.0", self.port, max_size=None, compression=None) as server:
            server.serve_forever()


def device_info(index, dev, port):
    return {"index": index, "name": short_name(dev), "vendor": dev.vendor.strip(),
            "cu": dev.max_compute_units, "port": port}


def announce_loop(infos, stop):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    host = socket.gethostname()
    try:
        while not stop.is_set():
            msg = json.dumps({"type": ANNOUNCE_MAGIC, "host": host, "ip": get_lan_ip(), "devices": infos}).encode()
            for target in ("255.255.255.255", "127.0.0.1"):
                try:
                    sock.sendto(msg, (target, DISCOVERY_PORT))
                except Exception:
                    pass
            stop.wait(1.0)
    finally:
        sock.close()


def run_child(index, port, code):
    """One process per GPU, so a slow or hung device never stalls another."""
    worker = Worker(index, list_gpus()[index], port, code)
    threading.Thread(target=worker.serve, daemon=True).start()
    for line in sys.stdin:
        if line.strip() == "cheat":
            worker.compute.cheat = not worker.compute.cheat
            state = "ON" if worker.compute.cheat else "off"
            print(f"  worker {index} ({worker.compute.name}) lazy/cheat mode: {state}", flush=True)
    while True:
        time.sleep(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--code", default=None, help="6-digit pairing code (random if omitted)")
    ap.add_argument("--devices", default=None, help="comma-separated GPU indices to share (default: all)")
    ap.add_argument("--port-base", type=int, default=PORT_BASE)
    ap.add_argument("--child", type=int, default=None, help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.child is not None:
        run_child(args.child, args.port_base + args.child, args.code)
        return
    if not args.serve:
        ap.error("use --serve")

    gpus = list_gpus()
    if not gpus:
        print("No OpenCL GPU found. Install pyopencl and your GPU driver.")
        sys.exit(1)
    indices = [int(x) for x in args.devices.split(",")] if args.devices else list(range(len(gpus)))
    code = args.code or "".join(secrets.choice("0123456789") for _ in range(6))
    infos = [device_info(i, gpus[i], args.port_base + i) for i in indices]
    children = {
        i: subprocess.Popen([sys.executable, "-u", os.path.abspath(__file__), "--child", str(i),
                             "--code", code, "--port-base", str(args.port_base)],
                            stdin=subprocess.PIPE, text=True)
        for i in indices
    }
    stop = threading.Event()
    threading.Thread(target=announce_loop, args=(infos, stop), daemon=True).start()

    print("=" * 60)
    print(f"  Pool workers on {socket.gethostname()} ({get_lan_ip()})")
    for d in infos:
        print(f"   [{d['index']}] {d['name']:28s} {d['cu']:3d} CUs   port {d['port']}")
    print(f"  CODE: {code}")
    print("  Commands: list | cheat <n>      (Ctrl+C to stop)")
    print("=" * 60, flush=True)
    try:
        for line in sys.stdin:
            parts = line.split()
            if parts[:1] == ["list"]:
                for d in infos:
                    alive = children[d["index"]].poll() is None
                    print(f"  [{d['index']}] {d['name']:28s} port {d['port']}  {'running' if alive else 'STOPPED'}")
            elif len(parts) == 2 and parts[0] == "cheat" and parts[1].isdigit() and int(parts[1]) in children:
                children[int(parts[1])].stdin.write("cheat\n")
                children[int(parts[1])].stdin.flush()
            elif parts:
                print("  commands: list | cheat <n>")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        for c in children.values():
            c.terminate()


if __name__ == "__main__":
    main()
