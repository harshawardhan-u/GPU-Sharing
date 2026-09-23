"""
Gravity Swarm -- the "GPU compute offload, not screen sharing" demo.

Thousands of particles pulling on each other (classic N-body gravity, an
all-pairs O(N^2) computation -- genuinely GPU-parallel work, not a toy).
Hold the left mouse button to attract particles toward the cursor. Press
E to add more particles live.

This window ALWAYS renders locally, on whichever laptop runs it -- there
is no video streaming involved anywhere. What changes is where the physics
gets computed each frame:

    python game.py
        Physics computed right here, on the CPU (NumPy). Struggles as the
        particle count grows -- watch the FPS counter and the stutter.

    python real_time_compute.py game.py
        Same window, same code, but real_time_compute.py sets a couple of
        environment variables telling this script to send its per-frame
        input to a GPU compute server instead, and use the positions it
        gets back. Only small state crosses the network each frame, never
        pixels.
"""

import json
import os
import struct
import sys
import time

# Some Windows terminals default to a legacy codepage (e.g. cp1252) that
# can't encode emoji, which would otherwise crash any of the print
# statements below.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import numpy as np
import pygame

WIDTH, HEIGHT = 900, 900
INITIAL_N = 1200
GROWTH_PER_PRESS = 800
MAX_N = 8000

FRAME_IN_FMT = '<ffBfI'   # mouse_x, mouse_y, mouse_active, dt, particle_count
FRAME_IN_SIZE = struct.calcsize(FRAME_IN_FMT)


# ---------------------------------------------------------------------------
# Local (NumPy/CPU) physics -- used when there's no compute offload, or if
# the remote connection drops mid-session.
# ---------------------------------------------------------------------------
def spawn_particles_np(n, rng):
    pos = rng.uniform(0, WIDTH, size=(n, 2)).astype(np.float32)
    vel = np.zeros((n, 2), dtype=np.float32)
    return pos, vel


def step_numpy(pos, vel, mouse_x, mouse_y, mouse_active, dt, chunk=256,
               G=800.0, softening=4.0, damping=0.999, max_speed=400.0):
    N = pos.shape[0]
    accel = np.zeros_like(pos)
    for start in range(0, N, chunk):
        end = min(start + chunk, N)
        block = pos[start:end]
        diff = pos[None, :, :] - block[:, None, :]
        dist_sq = (diff * diff).sum(-1) + softening * softening
        inv_dist3 = dist_sq ** -1.5
        idx = np.arange(start, end)
        inv_dist3[np.arange(end - start), idx] = 0.0
        accel[start:end] = G * (diff * inv_dist3[..., None]).sum(axis=1)

    if mouse_active:
        mouse = np.array([mouse_x, mouse_y], dtype=np.float32)
        to_mouse = mouse[None, :] - pos
        d2 = (to_mouse * to_mouse).sum(-1) + 400.0
        accel += 6000.0 * to_mouse / d2[:, None]

    vel = (vel + accel * dt) * damping
    speed = np.linalg.norm(vel, axis=-1)
    too_fast = speed > max_speed
    if too_fast.any():
        vel[too_fast] *= (max_speed / speed[too_fast])[:, None]
    pos = pos + vel * dt
    pos[:, 0] %= WIDTH
    pos[:, 1] %= HEIGHT
    return pos, vel


# ---------------------------------------------------------------------------
# Remote compute connection
# ---------------------------------------------------------------------------
def try_connect_remote():
    host = os.environ.get('RTCOMPUTE_HOST')
    port = os.environ.get('RTCOMPUTE_PORT')
    code = os.environ.get('RTCOMPUTE_CODE')

    if not (host and port and code):
        # No env vars (i.e. not launched via `real_time_compute.py game.py`)
        # -- fall back to a connection saved by the dashboard's "Real-Time
        # Compute Setup" pairing screen, if there is one.
        config_path = os.path.expanduser("~/.rtcompute_config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    cfg = json.load(f)
                host, port, code = cfg.get('host'), cfg.get('port'), cfg.get('code')
            except Exception:
                host = port = code = None

    if not (host and port and code):
        return None

    try:
        from websockets.sync.client import connect
    except ImportError:
        print("⚠ 'websockets' not installed locally -- falling back to local compute.")
        return None

    try:
        ws = connect(f"ws://{host}:{port}", open_timeout=5, max_size=None)
        ws.send(json.dumps({'code': code}))
        ack = json.loads(ws.recv(timeout=5))
        if ack.get('status') != 'approved':
            print("⚠ Compute host rejected the code -- falling back to local compute.")
            ws.close()
            return None
        return ws, host
    except Exception as e:
        print(f"⚠ Could not reach compute host ({e}) -- falling back to local compute.")
        return None


def remote_step(ws, mouse_x, mouse_y, mouse_active, dt, want_n):
    msg = struct.pack(FRAME_IN_FMT, mouse_x, mouse_y, 1 if mouse_active else 0, dt, want_n)
    ws.send(msg)
    reply = ws.recv(timeout=2)
    pos = np.frombuffer(reply, dtype=np.float32).reshape(-1, 2).copy()
    return pos


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
def build_palette(n):
    hues = (np.arange(n) * 0.6180339887) % 1.0  # golden-ratio spread -> even rainbow
    colors = np.zeros((n, 3), dtype=np.uint8)
    for i, h in enumerate(hues):
        r, g, b = _hsv_to_rgb(h, 0.65, 1.0)
        colors[i] = (r, g, b)
    return colors


def _hsv_to_rgb(h, s, v):
    i = int(h * 6)
    f = h * 6 - i
    p = v * (1 - s)
    q = v * (1 - f * s)
    t = v * (1 - (1 - f) * s)
    i %= 6
    r, g, b = [(v, t, p), (q, v, p), (p, v, t), (p, q, v), (t, p, v), (v, p, q)][i]
    return int(r * 255), int(g * 255), int(b * 255)


def main():
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT))
    pygame.display.set_caption('Gravity Swarm')
    clock = pygame.time.Clock()
    font = pygame.font.SysFont('consolas', 18)

    remote = try_connect_remote()
    ws, remote_host = (remote[0], remote[1]) if remote else (None, None)

    rng = np.random.default_rng(0)
    want_n = INITIAL_N
    palette = build_palette(MAX_N)

    if ws is None:
        pos, vel = spawn_particles_np(want_n, rng)
        mode = 'LOCAL COMPUTE (this laptop\'s CPU)'
    else:
        pos = remote_step(ws, WIDTH / 2, HEIGHT / 2, False, 1 / 60, want_n)
        vel = None
        mode = f'REMOTE GPU COMPUTE  (via {remote_host})'

    fps_samples = []
    running = True
    while running:
        dt_ms = clock.tick(240)
        dt = max(dt_ms / 1000.0, 1e-4)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_e:
                    want_n = min(MAX_N, want_n + GROWTH_PER_PRESS)
                    if ws is None:
                        extra_pos, extra_vel = spawn_particles_np(want_n - pos.shape[0], rng)
                        pos = np.concatenate([pos, extra_pos], axis=0)
                        vel = np.concatenate([vel, extra_vel], axis=0)

        mouse_x, mouse_y = pygame.mouse.get_pos()
        mouse_active = pygame.mouse.get_pressed()[0]

        if ws is not None:
            try:
                pos = remote_step(ws, mouse_x, mouse_y, mouse_active, dt, want_n)
            except Exception as e:
                print(f"⚠ Lost connection to compute host ({e}). Falling back to local compute.")
                vel = np.zeros_like(pos)
                ws = None
                mode = 'LOCAL COMPUTE (remote connection lost)'
        else:
            pos, vel = step_numpy(pos, vel, mouse_x, mouse_y, mouse_active, dt)

        fps_samples.append(1.0 / dt)
        if len(fps_samples) > 30:
            fps_samples.pop(0)
        avg_fps = sum(fps_samples) / len(fps_samples)

        screen.fill((8, 8, 14))
        xs = np.clip(pos[:, 0].astype(np.int32), 0, WIDTH - 1)
        ys = np.clip(pos[:, 1].astype(np.int32), 0, HEIGHT - 1)
        n = pos.shape[0]
        pixels = pygame.surfarray.pixels3d(screen)
        pixels[xs, ys] = palette[:n]
        del pixels  # release the surface lock before further drawing/flip

        hud1 = font.render(f'FPS: {avg_fps:5.1f}   Particles: {n}', True, (255, 255, 255))
        hud2 = font.render(mode, True, (255, 210, 90) if ws is None else (120, 255, 140))
        hud3 = font.render('Hold left mouse to attract  |  E: add particles  |  Esc: quit', True, (150, 150, 160))
        screen.blit(hud1, (10, 8))
        screen.blit(hud2, (10, 30))
        screen.blit(hud3, (10, HEIGHT - 24))

        pygame.display.flip()

    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass
    pygame.quit()


if __name__ == '__main__':
    main()
