"""
nexus_captions.py -- live captions computed on a borrowed GPU.

A speaker's laptop (no GPU needed) streams its microphone to the GPU laptop.
The GPU turns speech into text with Whisper and every listener sees the
captions live in their browser (scan the QR code, no app needed). Only a
little audio goes in and a little text comes out; all the heavy AI work is
on the GPU.

On the GPU laptop:
    python nexus_captions.py --serve                 (prints the speaker code)
    python nexus_captions.py --serve --mic           (also use this laptop's mic)
On the speaker's laptop:
    python nexus_captions.py --speak --code 123456   (finds the GPU laptop on the LAN)
Listeners open http://<gpu-laptop-ip>:8770 or scan the QR code.
"""

import argparse
import asyncio
import http
import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
from collections import deque

import numpy as np

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
MODEL_DIR = os.path.join(os.path.dirname(HERE), "models", "whisper")
PORT = 8770
DISCOVERY_PORT = 47630
ANNOUNCE_MAGIC = "COMPUTE_NEXUS_CAPTIONS"
RATE = 16000
PARTIAL_EVERY = 0.6          # seconds between draft updates while someone is speaking
END_SILENCE = 0.7            # this much trailing silence ends a caption line
MAX_UTTERANCE = 14.0         # force a line break on very long sentences
SPEECH_RMS = 0.012           # loudness threshold for "someone is speaking"


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def rms(x):
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


# =============================================================================
# Server (GPU laptop)
# =============================================================================
class CaptionServer:
    def __init__(self, args):
        import torch
        import whisper
        self.args = args
        self.code = args.code or "".join(secrets.choice("0123456789") for _ in range(6))
        self.device = "cuda" if torch.cuda.is_available() and not args.cpu else "cpu"
        self.gpu_name = torch.cuda.get_device_name(0) if self.device == "cuda" else "CPU (no GPU)"
        print(f"[captions] loading Whisper '{args.model}' on {self.gpu_name} ...", flush=True)
        self.model = whisper.load_model(args.model, device=self.device, download_root=MODEL_DIR)
        self.language = None if args.language == "auto" else args.language
        self.audio = queue.Queue()
        self.listeners = set()
        self.speakers = 0
        self.history = deque(maxlen=60)
        self.partial = ""
        self.stats = deque(maxlen=20)
        self.loop = None
        self.url = f"http://{get_lan_ip()}:{args.port}"

    # ----------------------------------------------------------- recognition
    def transcribe(self, audio):
        t0 = time.perf_counter()
        r = self.model.transcribe(audio, fp16=self.device == "cuda", language=self.language,
                                  condition_on_previous_text=False, temperature=0.0, without_timestamps=True)
        ms = (time.perf_counter() - t0) * 1000
        segs = r.get("segments", [])
        if segs and all(s.get("no_speech_prob", 0) > 0.6 and s.get("avg_logprob", 0) < -0.8 for s in segs):
            return "", ms, r.get("language")
        return r["text"].strip(), ms, r.get("language")

    def asr_loop(self):
        """Collects audio into the current line. Re-transcribes it as a draft
        every PARTIAL_EVERY seconds and finalises it after a pause."""
        buf = np.zeros(0, np.float32)
        last_partial = 0.0
        last_voice = time.time()
        heard_speech = False
        while True:
            try:
                chunk = self.audio.get(timeout=0.1)
                buf = np.concatenate([buf, chunk])
                if rms(chunk) > SPEECH_RMS:
                    last_voice = time.time()
                    heard_speech = True
            except queue.Empty:
                pass
            now = time.time()
            if not heard_speech:
                buf = buf[-int(0.5 * RATE):]          # keep a little lead-in only
                continue
            dur = len(buf) / RATE
            silent_for = now - last_voice
            if silent_for >= END_SILENCE or dur >= MAX_UTTERANCE:
                text, ms, lang = self.transcribe(buf)
                latency = (time.time() - last_voice) * 1000 if silent_for >= END_SILENCE else ms
                if text:
                    item = {"type": "final", "text": text, "t": time.strftime("%H:%M:%S"), "lang": lang,
                            "latency_ms": round(latency), "gpu_ms": round(ms), "audio_s": round(dur, 1)}
                    self.history.append(item)
                    self.stats.append((dur, ms, latency))
                    self.broadcast(item)
                    print(f"[captions] {item['t']} ({dur:.1f}s audio, GPU {ms:.0f} ms, shown {latency:.0f} ms after speech): {text}", flush=True)
                self.broadcast({"type": "partial", "text": ""})
                buf = np.zeros(0, np.float32)
                heard_speech = False
                last_partial = now
            elif now - last_partial >= PARTIAL_EVERY and dur > 0.8 and silent_for < 0.3:
                text, ms, _ = self.transcribe(buf)
                last_partial = time.time()
                self.broadcast({"type": "partial", "text": text})

    def status(self):
        rtf = [ms / 1000 / d for d, ms, _ in self.stats if d > 0]
        lat = [l for *_, l in self.stats]
        return {"type": "status", "gpu": self.gpu_name, "model": self.args.model, "listeners": len(self.listeners),
                "speakers": self.speakers, "url": self.url,
                "speed_x": round(1 / (sum(rtf) / len(rtf)), 1) if rtf else None,
                "latency_ms": round(sum(lat) / len(lat)) if lat else None}

    def broadcast(self, msg):
        if self.loop:
            asyncio.run_coroutine_threadsafe(self._broadcast(json.dumps(msg)), self.loop)

    async def _broadcast(self, text):
        for ws in list(self.listeners):
            try:
                await ws.send(text)
            except Exception:
                pass

    # ----------------------------------------------------------- connections
    async def handle(self, ws):
        try:
            hello = json.loads(await asyncio.wait_for(ws.recv(), 10))
        except Exception:
            return
        role = hello.get("role")
        if role == "listener":
            self.listeners.add(ws)
            await ws.send(json.dumps({"type": "history", "items": list(self.history), **self.status()}))
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass
            finally:
                self.listeners.discard(ws)
        elif role == "speaker":
            if hello.get("code") != self.code:
                await ws.send(json.dumps({"status": "rejected"}))
                print("[captions] rejected a speaker with a wrong code", flush=True)
                return
            await ws.send(json.dumps({"status": "approved", "gpu": self.gpu_name}))
            self.speakers += 1
            print(f"[captions] speaker connected: {hello.get('name', '?')}", flush=True)
            try:
                async for msg in ws:
                    if isinstance(msg, (bytes, bytearray)):
                        self.audio.put(np.frombuffer(msg, np.int16).astype(np.float32) / 32768.0)
            except Exception:
                pass
            finally:
                self.speakers -= 1
                print(f"[captions] speaker left: {hello.get('name', '?')}", flush=True)

    def process_request(self, connection, request):
        from websockets.datastructures import Headers
        from websockets.http11 import Response
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        if path in ("/", "/captions"):
            with open(os.path.join(WEB_DIR, "captions.html"), "rb") as f:
                body, ctype = f.read(), "text/html; charset=utf-8"
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

    async def status_loop(self):
        while True:
            await asyncio.sleep(2)
            await self._broadcast(json.dumps(self.status()))

    def announce_loop(self):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        while True:
            msg = json.dumps({"type": ANNOUNCE_MAGIC, "host": socket.gethostname(), "ip": get_lan_ip(),
                              "port": self.args.port, "gpu": self.gpu_name}).encode()
            for target in ("255.255.255.255", "127.0.0.1"):
                try:
                    sock.sendto(msg, (target, DISCOVERY_PORT))
                except Exception:
                    pass
            time.sleep(1.0)

    def local_mic(self):
        import sounddevice as sd
        self.speakers += 1

        def cb(indata, frames, t, status):
            self.audio.put(indata[:, 0].copy())
        with sd.InputStream(samplerate=RATE, channels=1, dtype="float32", blocksize=RATE // 4, callback=cb):
            print("[captions] listening on this laptop's microphone", flush=True)
            while True:
                time.sleep(1)

    async def main(self):
        from websockets.asyncio.server import serve
        self.loop = asyncio.get_running_loop()
        threading.Thread(target=self.asr_loop, daemon=True).start()
        threading.Thread(target=self.announce_loop, daemon=True).start()
        if self.args.mic:
            threading.Thread(target=self.local_mic, daemon=True).start()
        async with serve(self.handle, "0.0.0.0", self.args.port, process_request=self.process_request,
                         max_size=2 ** 20, compression=None):
            print("=" * 60)
            print(f"  LIVE CAPTIONS on {self.gpu_name}  (Whisper {self.args.model})")
            print(f"  Listeners open:  {self.url}")
            print(f"  SPEAKER CODE:    {self.code}")
            print("=" * 60, flush=True)
            await self.status_loop()


# =============================================================================
# Speaker (any laptop, no GPU needed)
# =============================================================================
def discover(timeout=3.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.5)
    found = {}
    try:
        sock.bind(("", DISCOVERY_PORT))
        end = time.time() + timeout
        while time.time() < end:
            try:
                data, addr = sock.recvfrom(4096)
                info = json.loads(data.decode())
                if info.get("type") == ANNOUNCE_MAGIC:
                    found[info.get("ip", addr[0])] = info
            except (socket.timeout, ValueError):
                continue
    finally:
        sock.close()
    return list(found.values())


def run_speaker(args):
    import sounddevice as sd
    from websockets.sync.client import connect
    host, port = args.host, args.port
    if not host:
        print("Looking for the captions GPU laptop on the LAN (3 s)...", flush=True)
        hosts = discover()
        if not hosts:
            print("No captions host found. Use --host <ip>.")
            sys.exit(1)
        host, port = hosts[0]["ip"], hosts[0]["port"]
        print(f"Found {hosts[0]['host']} ({hosts[0]['gpu']}) at {host}", flush=True)
    ws = connect(f"ws://{host}:{port}/ws", open_timeout=5, max_size=None)
    ws.send(json.dumps({"role": "speaker", "code": args.code, "name": socket.gethostname()}))
    ack = json.loads(ws.recv(timeout=5))
    if ack.get("status") != "approved":
        print("Wrong speaker code.")
        sys.exit(1)
    print(f"Connected. Speak now -- captions are computed on {ack.get('gpu')}. Ctrl+C to stop.", flush=True)
    q = queue.Queue()

    def cb(indata, frames, t, status):
        q.put(indata[:, 0].copy())
    with sd.InputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=RATE // 4, callback=cb):
        last = 0
        while True:
            chunk = q.get()
            ws.send(chunk.tobytes())
            if time.time() - last > 0.5:
                level = int(min(30, rms(chunk.astype(np.float32) / 32768) * 400))
                print("mic " + "#" * level + " " * (30 - level), end="\r", flush=True)
                last = time.time()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serve", action="store_true")
    ap.add_argument("--speak", action="store_true")
    ap.add_argument("--mic", action="store_true", help="server: also caption this laptop's microphone")
    ap.add_argument("--model", default="medium", help="tiny | base | small | medium")
    ap.add_argument("--language", default="auto", help="auto, or a language code such as en, ta, hi")
    ap.add_argument("--code", default=None)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--cpu", action="store_true", help="server: force CPU (for comparison)")
    args = ap.parse_args()
    if args.serve:
        try:
            asyncio.run(CaptionServer(args).main())
        except KeyboardInterrupt:
            pass
    elif args.speak:
        if not args.code:
            args.code = input("Speaker code shown on the GPU laptop: ").strip()
        try:
            run_speaker(args)
        except KeyboardInterrupt:
            pass
    else:
        ap.error("use --serve (GPU laptop) or --speak (speaker's laptop)")


if __name__ == "__main__":
    main()
