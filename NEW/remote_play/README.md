# Remote Play — "this laptop has no GPU, but watch this" demo

A real-time, GPU-heavy 3D scene (hundreds of dynamic shadow-casting objects)
that you can prove runs on someone else's GPU over the LAN — no internet,
no Cloudflare, no sign-in, just a local network and a 6-digit code.

`app.py` **refuses to even open a window if it can't find an NVIDIA GPU**
(checked via `nvidia-smi`). That's the whole point: run it on the non-GPU
laptop and it flatly won't start.

## Setup (once, on both laptops)

```
cd remote_play
pip install -r requirements.txt
```

Both laptops must be on the **same Wi-Fi / LAN** (e.g. same router, or a
phone hotspot both laptops join). No internet connection is required.

## The demo script

**Step 1 — show it refusing to start on the non-GPU laptop.**
From `NEW/`:
```
python game.py
```
It prints an error and exits immediately — no window, nothing:
```
X No NVIDIA GPU detected on this machine.
  This scene needs a dedicated GPU to run at all.
```
This is your "before" shot: `python game.py` just doesn't run here.

**Step 2 — start it for real on the GPU laptop.**
From `NEW/`:
```
python game.py --serve
```
A window opens, genuinely rendering the scene on that GPU. The console
prints a 6-digit code and confirms which GPU it found, e.g.:
```
GPU: NVIDIA GeForce RTX 4060 Laptop GPU
Serving Remote Play on the LAN.
CODE: 583920
```
Leave this running.

**Step 3 — connect and control it from the non-GPU laptop.**
This is the payoff line — the exact same command you already use for
`train_model.py`, now on the game:
```
cloud-run game.py
```
It auto-discovers the host by listening for its LAN broadcast (~3s), or
asks for the IP manually if nothing is found, then asks for the 6-digit
code from Step 2. Once accepted, a window opens showing the *exact same
live video*, and your keypresses (`W A S D`, `Space`, `E`) are sent over
the LAN and control the scene on the GPU laptop in real time. This laptop
is doing nothing but decoding JPEG frames.

(`cloud-run game.py` just launches `remote_play/client.py` under the hood —
you can also run `python client.py` directly from inside `remote_play/`.)

**Step 4 — prove it on Task Manager.**
On the GPU laptop, open Task Manager → Performance → GPU while this is
running: you'll see real GPU utilization, live, matching the scene's
activity. Press `E` from the client a couple of times to spawn more waves
of objects — GPU usage and object count climb together on screen.

## How it works

- `scene.py` — the actual 3D scene (shared by both modes): a grid of
  animated, individually shadow-casting cubes under a sweeping directional
  light, with a player-controlled orb. Genuinely expensive to render
  (real-time shadow maps + per-pixel lighting across hundreds of dynamic
  objects) — hence the hard GPU requirement.
- `app.py` — checks for an NVIDIA GPU (`nvidia-smi`) before doing anything
  else and exits if none is found. Renders the scene; with `--serve`, also
  grabs the window (`mss`), JPEG-encodes each frame (`opencv`), and
  broadcasts it to connected viewers over a LAN WebSocket. Generates a
  6-digit session code and requires it as the first message from any
  viewer before it'll stream to them. Also broadcasts a small UDP
  "I'm here" packet every second so clients can auto-discover it.
- `client.py` — a thin viewer: discovers/connects to a host, exchanges the
  code, then just decodes the JPEG stream into a `pygame` window and sends
  keyboard state back every frame. It does zero 3D rendering itself.
- `shared.py` — the LAN discovery (UDP broadcast/listen on port 47600),
  code generation, and the `nvidia-smi`-based GPU check.
- Everything is LAN-only sockets, no cloud/tunnel involved, so there's no
  bandwidth cap or external dependency to fail mid-demo.

## Troubleshooting

- **`python app.py` says no GPU even on the GPU laptop:** confirm
  `nvidia-smi` works from a plain terminal on that machine (NVIDIA drivers
  installed). If it works in terminal but not here, something's odd with
  PATH — run `where nvidia-smi` and make sure it's found.
- **The rendered window looks GPU-heavy but Task Manager shows the
  integrated GPU busy instead:** this is Windows/Optimus routing the
  OpenGL window to the wrong GPU. The app detects and warns about this
  on-screen and in the console. Fix: Windows Settings → System → Display →
  Graphics → add `python.exe` → set it to **"High performance"**. Do this
  once on the GPU laptop before the demo.
- **Client's auto-discovery finds nothing:** some Wi-Fi networks (guest
  networks, some routers) block UDP broadcast between devices ("client
  isolation"). Fall back to typing the IP manually: `python client.py
  192.168.1.23`. A personal hotspot avoids this entirely.
- **Windows Firewall prompt:** allow Python through the firewall on the
  GPU laptop the first time you run `app.py --serve` (needed for both the
  WebSocket server and the UDP broadcast).
- **Captured video looks cropped/offset:** can happen with display scaling
  >100%. The script forces DPI awareness on Windows; if it's still off,
  set Windows display scaling to 100% on the GPU laptop for the demo.
- **Everything is choppy even on the GPU laptop:** lower `MAX_WAVES` in
  `scene.py`, or don't spawn extra waves — 625 objects is already enough to
  make the point without stressing draw-call overhead.
