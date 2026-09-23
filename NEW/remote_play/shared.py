"""Shared constants for the Remote Play LAN demo (host <-> client)."""

PORT = 8765

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
