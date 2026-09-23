"""
Remote Play demo -- host / standalone renderer.

Run it on its own to render the demo scene locally:

    python app.py

This REFUSES to start if no NVIDIA GPU is detected on the machine -- that's
the point: run it on the non-GPU laptop first to show it flatly won't launch.

Run it with --serve on the GPU laptop to broadcast the rendered frames to LAN
viewers (client.py) and accept their keyboard input, taking over from the
local keyboard once a viewer connects. It also broadcasts itself on the LAN
so client.py can auto-discover it, and generates a 6-digit code viewers must
supply to connect:

    python app.py --serve

Controls (local keyboard, or from a connected client.py): W A S D to move,
Space to float up, E to spawn another wave of the city (more objects = more
GPU load, watch the FPS counter react).
"""

import argparse
import ctypes
import json
import queue
import socket
import sys
import threading
import time as time_module

import cv2
import mss
import numpy as np

from shared import (
    FRAME_W, FRAME_H, JPEG_QUALITY, PORT, TARGET_STREAM_FPS,
    check_nvidia_gpu, generate_code, get_lan_ip, start_lan_announcer,
)


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--serve', action='store_true', help='Broadcast frames to LAN viewers and accept their input')
    p.add_argument('--port', type=int, default=PORT)
    return p.parse_args()


args = parse_args()

# --- Hard GPU gate: this is the whole point of the demo -------------------
# Checks for a *physically present* NVIDIA GPU via nvidia-smi. Refuses to
# even open a window without one, so running this on a non-GPU laptop is a
# flat, immediate "it won't start" -- not just a choppy render.
_has_gpu, _gpu_name = check_nvidia_gpu()
if not _has_gpu:
    print('=' * 60)
    print('  X No NVIDIA GPU detected on this machine.')
    print('    This scene needs a dedicated GPU to run at all.')
    print('    Run "python app.py --serve" on a GPU laptop, then come back')
    print('    here and run "python client.py" to control it remotely.')
    print('=' * 60)
    sys.exit(1)

if sys.platform == 'win32':
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

from ursina import Text, Ursina, color, held_keys, window  # noqa: E402
from scene import PulseCity  # noqa: E402

SESSION_CODE = generate_code() if args.serve else None

window.vsync = False
app = Ursina(borderless=True, size=(FRAME_W, FRAME_H), position=(0, 0), development_mode=True)

city = PulseCity()

# ---------------------------------------------------------------------------
# Networking state (only used when --serve is passed). Kept as simple module
# globals guarded by locks -- Panda3D/Ursina calls must stay on the main
# thread, so worker threads only ever touch plain dict/queue state here.
# ---------------------------------------------------------------------------
remote_input = {'w': False, 'a': False, 's': False, 'd': False, 'space': False}
remote_lock = threading.Lock()
last_remote_input_time = 0.0
spawn_requested = threading.Event()

capture_queue = queue.Queue(maxsize=1)
client_queues_lock = threading.Lock()
client_queues = {}  # websocket -> queue.Queue of encoded jpeg bytes
announce_stop = threading.Event()


def handle_client_message(raw):
    global last_remote_input_time
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return
    if not isinstance(data, dict):
        return
    with remote_lock:
        for k in ('w', 'a', 's', 'd', 'space'):
            if k in data:
                remote_input[k] = bool(data[k])
        last_remote_input_time = time_module.time()
    if data.get('spawn'):
        spawn_requested.set()


def broadcast_frame(jpeg_bytes):
    with client_queues_lock:
        targets = list(client_queues.values())
    for q in targets:
        if q.full():
            try:
                q.get_nowait()
            except queue.Empty:
                pass
        q.put(jpeg_bytes)


def sender_loop(websocket, q):
    while True:
        frame = q.get()
        if frame is None:
            return
        try:
            websocket.send(frame)
        except Exception:
            return


def ws_handler(websocket):
    # Handshake: the first message must be {"code": "123456"} matching this
    # session's code. Only after that do we start streaming video/accepting
    # movement input -- this is the "code matches -> it connects" gate.
    try:
        first_raw = websocket.recv(timeout=5)
        first = json.loads(first_raw)
    except Exception:
        first = None

    if not isinstance(first, dict) or first.get('code') != SESSION_CODE:
        try:
            websocket.send(json.dumps({'status': 'rejected'}))
        except Exception:
            pass
        print(f'[serve] rejected connection from {websocket.remote_address} (bad/missing code)')
        return

    try:
        websocket.send(json.dumps({'status': 'approved'}))
    except Exception:
        return

    q = queue.Queue(maxsize=2)
    with client_queues_lock:
        client_queues[websocket] = q
    print(f'[serve] viewer connected: {websocket.remote_address}')
    sender = threading.Thread(target=sender_loop, args=(websocket, q), daemon=True)
    sender.start()
    try:
        for message in websocket:
            handle_client_message(message)
    except Exception:
        pass
    finally:
        with client_queues_lock:
            client_queues.pop(websocket, None)
        q.put(None)
        print(f'[serve] viewer disconnected: {websocket.remote_address}')


def run_ws_server(port):
    from websockets.sync.server import serve
    with serve(ws_handler, '0.0.0.0', port, max_size=None) as server:
        server.serve_forever()


def encode_loop():
    while True:
        raw = capture_queue.get()
        bgr = np.array(raw)[:, :, :3]
        ok, buf = cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if ok:
            broadcast_frame(buf.tobytes())


_capture_region = None
_last_capture_t = 0.0
_sct = None


def on_window_ready():
    global _capture_region, _sct

    # Hardware presence (nvidia-smi) already passed, but on hybrid/Optimus
    # laptops Windows may still be handing this OpenGL window to the
    # integrated GPU. Catch that here so it's visible during rehearsal.
    gsg = base.win.get_gsg()  # noqa: F821 (Panda3D global injected by Ursina)
    vendor = gsg.get_driver_vendor() or ''
    renderer = gsg.get_driver_renderer() or ''
    if 'nvidia' not in vendor.lower() and 'nvidia' not in renderer.lower():
        Text(
            text=f'⚠ Rendering on "{renderer}" (not the NVIDIA GPU) -- see console',
            position=(-0.85, -0.47), scale=0.9, color=color.orange,
        )
        print('[warning] active renderer is "%s" / vendor "%s" -- not NVIDIA.' % (renderer, vendor))
        print('  Windows is probably routing this to integrated graphics (Optimus).')
        print('  Fix: Settings > System > Display > Graphics > add python.exe > "High performance".')

    if not args.serve:
        return

    props = base.win.get_properties()
    ox, oy = (props.get_x_origin(), props.get_y_origin()) if props.has_origin() else (0, 0)
    w, h = props.get_x_size(), props.get_y_size()
    _capture_region = {'left': ox, 'top': oy, 'width': w, 'height': h}
    _sct = mss.mss()

    ip = get_lan_ip()
    host_name = socket.gethostname()
    threading.Thread(target=run_ws_server, args=(args.port,), daemon=True).start()
    threading.Thread(target=encode_loop, daemon=True).start()
    threading.Thread(target=start_lan_announcer, args=(announce_stop, host_name, args.port), daemon=True).start()

    print('=' * 60)
    print(f'  GPU: {_gpu_name}')
    print('  Serving Remote Play on the LAN.')
    print(f'  CODE: {SESSION_CODE}')
    print('  On the other laptop, just run:   python client.py')
    print('  (it auto-finds this host -- it will ask for the code above)')
    print(f'  Manual fallback:  python client.py {ip} --code {SESSION_CODE}')
    print('=' * 60)


def get_input():
    with remote_lock:
        remote_active = (time_module.time() - last_remote_input_time) < 1.0
        snapshot = dict(remote_input)
    if remote_active:
        return snapshot
    return {
        'w': held_keys['w'], 'a': held_keys['a'], 's': held_keys['s'], 'd': held_keys['d'],
        'space': held_keys['space'],
    }


def input(key):  # noqa: A001 - Ursina calls a global `input(key)` if defined
    if key == 'e':
        city.spawn_wave()


def update():
    global _last_capture_t

    if spawn_requested.is_set():
        spawn_requested.clear()
        city.spawn_wave()

    city.update(get_input())

    if args.serve:
        with remote_lock:
            remote_active = (time_module.time() - last_remote_input_time) < 1.0
        with client_queues_lock:
            n_viewers = len(client_queues)
        status = f'{n_viewers} viewer(s)' + (' - remote controlling' if remote_active else ' - local keyboard')
        city.mode_text.text = f'STREAMING  |  CODE {SESSION_CODE}  |  {status}'
    else:
        city.mode_text.text = 'LOCAL RENDERING (this machine only)'

    if args.serve and _capture_region is not None:
        now = time_module.time()
        if now - _last_capture_t >= 1 / TARGET_STREAM_FPS:
            _last_capture_t = now
            raw = _sct.grab(_capture_region)
            if not capture_queue.full():
                capture_queue.put(raw)


app.run()
