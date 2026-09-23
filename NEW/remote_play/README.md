# Remote Play — "this laptop has no GPU, but watch this" demo

A real-time, GPU-heavy 3D scene (hundreds of dynamic shadow-casting objects)
that you can prove runs on someone else's GPU over the LAN — no internet,
no Cloudflare, just a local network.

## Setup (once, on both laptops)

```
cd remote_play
pip install -r requirements.txt
```

Both laptops must be on the **same Wi-Fi / LAN** (e.g. same router, or a
phone hotspot both laptops join). No internet connection is required.

## The demo script

**Step 1 — show it struggling locally.**
On the NON-GPU laptop:
```
python app.py
```
A window opens rendering the scene using that laptop's own graphics. On
integrated graphics the on-screen FPS counter should sit low (often single
digits once you press `E` a couple of times to add more objects). This is
your "before" shot. Move around with `W A S D`, `Space` to float. Close the
window (Esc) when done.

**Step 2 — show it running smoothly on the GPU laptop.**
On the GPU laptop:
```
python app.py --serve
```
It prints the LAN IP to connect to, e.g.:
```
On the other laptop run:  python client.py 192.168.1.23 --port 8765
```
Leave this running — this is now genuinely rendering the scene on that
machine's GPU, unmodified.

**Step 3 — control it from the non-GPU laptop.**
On the NON-GPU laptop:
```
python client.py 192.168.1.23
```
(use the IP printed in step 2). A window opens showing the *exact same live
video* being decoded and played, and your keypresses (`W A S D`, `Space`,
`E`) are sent over the LAN and control the scene on the GPU laptop in real
time. The non-GPU laptop is doing nothing but decoding JPEG frames — you can
point out its own FPS counter (if you left it running side by side) never
moved, proving it isn't rendering anything.

**Step 4 — the "watch this" moment.**
Press `E` a couple of times from the client to spawn more waves of objects.
The GPU host keeps its FPS high while the object count climbs into the
thousands; if you re-run step 1 at the same object count, the non-GPU laptop
will have already choked well before that.

## How it works

- `scene.py` — the actual 3D scene (shared by both modes): a grid of
  animated, individually shadow-casting cubes under a sweeping directional
  light, with a player-controlled orb. This is genuinely expensive to
  render (real-time shadow maps + per-pixel lighting across hundreds of
  dynamic objects), which is why integrated graphics struggles with it.
- `app.py` — renders the scene. Always playable locally with the keyboard.
  With `--serve`, it also grabs the rendered window (`mss`), JPEG-encodes
  each frame (`opencv`), and broadcasts it to any connected viewers over a
  plain LAN WebSocket (`websockets`). It accepts input back from a connected
  viewer and drives the scene from that instead of the local keyboard.
- `client.py` — a thin viewer: decodes the JPEG stream and displays it in a
  `pygame` window, and sends keyboard state back every frame. It does zero
  3D rendering itself.
- Everything is LAN-only sockets (`ws://<ip>:8765`), no cloud/tunnel
  involved, so there's no bandwidth cap or external dependency to fail
  mid-demo.

## Troubleshooting

- **Client can't connect:** confirm both laptops show IPs on the same
  subnet (e.g. both `192.168.1.x`) — run `ipconfig` on both. Some public/
  guest Wi-Fi networks block device-to-device traffic ("client isolation");
  a personal hotspot or home router avoids this.
- **Windows Firewall prompt:** allow Python through the firewall on the
  GPU laptop the first time you run `app.py --serve` (needed for the
  WebSocket server to accept the LAN connection).
- **Captured video looks cropped/offset:** this can happen with display
  scaling >100%. The script forces DPI awareness on Windows, but if it's
  still off, lower Windows display scaling to 100% on the GPU laptop for
  the demo.
- **Everything is choppy even on the GPU laptop:** lower `MAX_WAVES` in
  `scene.py`, or don't spawn extra waves — 625 objects is already enough to
  make the point without stressing draw-call overhead.
