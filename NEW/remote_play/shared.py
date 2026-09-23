"""Shared constants for the Remote Play LAN demo (host <-> client)."""

PORT = 8765
DISCOVERY_PORT = 47600     # UDP: host announces itself here, client listens here
ANNOUNCE_MAGIC = 'COMPUTE_NEXUS_LAN_HOST'

# Resolution of the host's render window == resolution streamed to the client.
FRAME_W = 960
FRAME_H = 540

JPEG_QUALITY = 70          # 0-100, lower = less LAN bandwidth, blockier image
TARGET_STREAM_FPS = 30     # host will not capture/send faster than this


def get_lan_ip() -> str:
    """Best-effort local LAN IP so the host can print 'connect to this address'."""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Doesn't actually send any packets, just makes the OS pick a route/interface.
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def generate_code() -> str:
    import random
    return ''.join(random.choice('0123456789') for _ in range(6))


def check_nvidia_gpu(timeout=5.0):
    """
    Returns (has_gpu, name). Checks for a *physically present* NVIDIA GPU via
    nvidia-smi, independent of which GPU the display driver is actively using
    for rendering (a laptop with hybrid/Optimus graphics can have a real RTX
    GPU installed while OpenGL windows still default to the integrated one --
    that's a separate problem, handled by the on-screen renderer warning in
    app.py, not by this hardware-presence check).
    """
    import subprocess
    try:
        res = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=timeout,
        )
        if res.returncode == 0 and res.stdout.strip():
            return True, res.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return False, None


def start_lan_announcer(stop_event, name, ws_port):
    """Background-thread target: broadcast this host's presence on the LAN
    every second so clients can auto-discover it without typing an IP."""
    import json
    import socket as socket_module
    import time as time_module

    sock = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
    sock.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_BROADCAST, 1)
    payload = json.dumps({
        'type': ANNOUNCE_MAGIC,
        'name': name,
        'ip': get_lan_ip(),
        'ws_port': ws_port,
    }).encode('utf-8')
    try:
        while not stop_event.is_set():
            try:
                sock.sendto(payload, ('255.255.255.255', DISCOVERY_PORT))
            except Exception:
                pass
            stop_event.wait(1.0)
    finally:
        sock.close()


def discover_hosts(timeout=3.0):
    """Listen for LAN host announcements. Returns a list of dicts, deduped by IP."""
    import json
    import socket as socket_module
    import time as time_module

    sock = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
    sock.setsockopt(socket_module.SOL_SOCKET, socket_module.SO_REUSEADDR, 1)
    sock.settimeout(0.5)
    try:
        sock.bind(('', DISCOVERY_PORT))
    except Exception:
        sock.close()
        return []

    found = {}
    deadline = time_module.time() + timeout
    try:
        while time_module.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
                info = json.loads(data.decode('utf-8'))
                if info.get('type') == ANNOUNCE_MAGIC:
                    found[info.get('ip', addr[0])] = info
            except socket_module.timeout:
                continue
            except Exception:
                continue
    finally:
        sock.close()
    return list(found.values())
