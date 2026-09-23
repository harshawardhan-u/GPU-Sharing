"""
Swarm Dodge -- an actual arcade game for the "GPU compute offload" demo.

Move with the ARROW KEYS. A swarm of enemies spawns at the edges and homes
in on you; they repel each other so they spread into an organic-looking
mob instead of stacking on one point (that mutual repulsion is the same
kind of all-pairs O(N^2) computation as Gravity Swarm -- just tuned for a
chase instead of a slow orbit). Survive as long as you can. Press E to
throw more enemies at yourself on purpose, for the demo's "watch it break"
moment.

This window always renders locally -- no video ever crosses the network.
Only enemy positions (tiny data) get computed remotely when available.

    python dodge_swarm.py
        Enemy physics computed right here, on the CPU (NumPy). Once the
        swarm gets big enough, the game visibly lags -- and because this is
        a real-time game, lag means you can't dodge properly anymore.

    python real_time_compute.py dodge_swarm.py
        Same window, same code, but enemy physics is computed on a peer's
        GPU instead. Stays smooth (and fair to play) well past where the
        local version falls apart. Also works automatically after pairing
        via the dashboard's "Real-Time Compute Setup" screen (no env vars
        needed then -- it reads the same saved connection game.py uses).
"""

import json
import os
import struct
import sys
import time

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import numpy as np
import pygame

WIDTH, HEIGHT = 900, 900
INITIAL_N = 250
GROWTH_PER_PRESS = 200
MAX_N = 4000

PLAYER_ACCEL = 900.0
PLAYER_DRAG = 0.90
PLAYER_MAX_SPEED = 340.0
PLAYER_RADIUS = 10
HIT_RADIUS = 14.0
INVINCIBLE_SECONDS = 1.5

FRAME_IN_FMT = '<ffBfI'   # player_x, player_y, unused, dt, enemy_count
FRAME_IN_SIZE = struct.calcsize(FRAME_IN_FMT)


# ---------------------------------------------------------------------------
# Local (NumPy/CPU) enemy physics -- mirrors the server's 'dodge' mode.
# ---------------------------------------------------------------------------
def spawn_enemies_np(n, rng, margin=60.0):
    if n == 0:
        return np.zeros((0, 2), dtype=np.float32), np.zeros((0, 2), dtype=np.float32)
    side = rng.integers(0, 4, size=n)
    t = rng.random(n).astype(np.float32)
    x = np.zeros(n, dtype=np.float32)
    y = np.zeros(n, dtype=np.float32)
    m0 = side == 0
    x[m0], y[m0] = t[m0] * WIDTH, -margin
    m1 = side == 1
    x[m1], y[m1] = WIDTH + margin, t[m1] * HEIGHT
    m2 = side == 2
    x[m2], y[m2] = t[m2] * WIDTH, HEIGHT + margin
    m3 = side == 3
    x[m3], y[m3] = -margin, t[m3] * HEIGHT
    pos = np.stack([x, y], axis=1)
    vel = np.zeros((n, 2), dtype=np.float32)
    return pos, vel


def dodge_step_numpy(pos, vel, player_x, player_y, dt, chunk=256,
                      repel_strength=9000.0, repel_range=50.0,
                      chase_speed=150.0, damping=0.995, max_speed=230.0):
    N = pos.shape[0]
    accel = np.zeros_like(pos)
    for start in range(0, N, chunk):
        end = min(start + chunk, N)
        block = pos[start:end]
        diff = block[:, None, :] - pos[None, :, :]
        dist_sq = (diff * diff).sum(-1) + 1.0
        dist = np.sqrt(dist_sq)
        within = dist < repel_range
        idx = np.arange(start, end)
        within[np.arange(end - start), idx] = False
        force_mag = np.where(within, repel_strength / dist_sq, 0.0)
        accel[start:end] = (diff / np.clip(dist[..., None], 1e-3, None) * force_mag[..., None]).sum(axis=1)

    player = np.array([player_x, player_y], dtype=np.float32)
    to_player = player[None, :] - pos
    dist_to_player = np.clip(np.linalg.norm(to_player, axis=-1, keepdims=True), 1e-3, None)
    accel += (to_player / dist_to_player) * chase_speed

    vel = (vel + accel * dt) * damping
    speed = np.linalg.norm(vel, axis=-1)
    too_fast = speed > max_speed
    if too_fast.any():
        vel[too_fast] *= (max_speed / speed[too_fast])[:, None]
    pos = pos + vel * dt
    pos = np.clip(pos, -300, 1200)
    return pos, vel


# ---------------------------------------------------------------------------
# Remote compute connection
# ---------------------------------------------------------------------------
def try_connect_remote():
    host = os.environ.get('RTCOMPUTE_HOST')
    port = os.environ.get('RTCOMPUTE_PORT')
    code = os.environ.get('RTCOMPUTE_CODE')

    if not (host and port and code):
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
        ws.send(json.dumps({'code': code, 'game': 'dodge'}))
        ack = json.loads(ws.recv(timeout=5))
        if ack.get('status') != 'approved':
            print("⚠ Compute host rejected the code -- falling back to local compute.")
            ws.close()
            return None
        return ws, host
    except Exception as e:
        print(f"⚠ Could not reach compute host ({e}) -- falling back to local compute.")
        return None


def remote_step(ws, player_x, player_y, dt, want_n):
    msg = struct.pack(FRAME_IN_FMT, player_x, player_y, 0, dt, want_n)
    ws.send(msg)
    reply = ws.recv(timeout=2)
    pos = np.frombuffer(reply, dtype=np.float32).reshape(-1, 2).copy()
    return pos


def build_palette(n):
    hues = (np.arange(n) * 0.6180339887) % 1.0
    colors = np.zeros((n, 3), dtype=np.uint8)
    for i, h in enumerate(hues):
        r, g, b = _hsv_to_rgb(h, 0.75, 1.0)
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
    pygame.display.set_caption('Swarm Dodge')
    clock = pygame.time.Clock()
    font = pygame.font.SysFont('consolas', 18)

    remote = try_connect_remote()
    ws, remote_host = (remote[0], remote[1]) if remote else (None, None)

    rng = np.random.default_rng(1)
    want_n = INITIAL_N
    palette = build_palette(MAX_N)

    if ws is None:
        pos, vel = spawn_enemies_np(want_n, rng)
        mode = "LOCAL COMPUTE (this laptop's CPU)"
    else:
        pos = remote_step(ws, WIDTH / 2, HEIGHT / 2, 1 / 60, want_n)
        vel = None
        mode = f'REMOTE GPU COMPUTE  (via {remote_host})'

    player_x, player_y = WIDTH / 2, HEIGHT / 2
    player_vx, player_vy = 0.0, 0.0

    game_start = time.time()
    last_hit_time = -999.0
    best_streak = 0.0
    hits_taken = 0

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
                        extra_pos, extra_vel = spawn_enemies_np(want_n - pos.shape[0], rng)
                        pos = np.concatenate([pos, extra_pos], axis=0)
                        vel = np.concatenate([vel, extra_vel], axis=0)

        keys = pygame.key.get_pressed()
        ax = ay = 0.0
        if keys[pygame.K_LEFT]:
            ax -= PLAYER_ACCEL
        if keys[pygame.K_RIGHT]:
            ax += PLAYER_ACCEL
        if keys[pygame.K_UP]:
            ay -= PLAYER_ACCEL
        if keys[pygame.K_DOWN]:
            ay += PLAYER_ACCEL

        player_vx = (player_vx + ax * dt) * PLAYER_DRAG
        player_vy = (player_vy + ay * dt) * PLAYER_DRAG
        speed = (player_vx ** 2 + player_vy ** 2) ** 0.5
        if speed > PLAYER_MAX_SPEED:
            player_vx *= PLAYER_MAX_SPEED / speed
            player_vy *= PLAYER_MAX_SPEED / speed
        player_x = min(max(player_x + player_vx * dt, PLAYER_RADIUS), WIDTH - PLAYER_RADIUS)
        player_y = min(max(player_y + player_vy * dt, PLAYER_RADIUS), HEIGHT - PLAYER_RADIUS)

        if ws is not None:
            try:
                pos = remote_step(ws, player_x, player_y, dt, want_n)
            except Exception as e:
                print(f"⚠ Lost connection to compute host ({e}). Falling back to local compute.")
                vel = np.zeros_like(pos)
                ws = None
                mode = 'LOCAL COMPUTE (remote connection lost)'
        else:
            pos, vel = dodge_step_numpy(pos, vel, player_x, player_y, dt)

        now = time.time()
        invincible = (now - last_hit_time) < INVINCIBLE_SECONDS
        if not invincible and pos.shape[0] > 0:
            d2 = (pos[:, 0] - player_x) ** 2 + (pos[:, 1] - player_y) ** 2
            if np.any(d2 < HIT_RADIUS ** 2):
                streak = now - max(last_hit_time + INVINCIBLE_SECONDS, game_start) if hits_taken > 0 else now - game_start
                best_streak = max(best_streak, streak)
                hits_taken += 1
                last_hit_time = now

        current_streak = (now - last_hit_time - INVINCIBLE_SECONDS) if hits_taken > 0 else (now - game_start)
        current_streak = max(0.0, current_streak)
        best_streak = max(best_streak, current_streak)

        fps_samples.append(1.0 / dt)
        if len(fps_samples) > 30:
            fps_samples.pop(0)
        avg_fps = sum(fps_samples) / len(fps_samples)

        screen.fill((8, 8, 14))
        n = pos.shape[0]
        if n > 0:
            xs = np.clip(pos[:, 0].astype(np.int32), 0, WIDTH - 1)
            ys = np.clip(pos[:, 1].astype(np.int32), 0, HEIGHT - 1)
            pixels = pygame.surfarray.pixels3d(screen)
            pixels[xs, ys] = palette[:n]
            del pixels

        player_color = (255, 90, 90) if invincible and int(now * 8) % 2 == 0 else (120, 230, 255)
        pygame.draw.circle(screen, player_color, (int(player_x), int(player_y)), PLAYER_RADIUS)
        pygame.draw.circle(screen, (255, 255, 255), (int(player_x), int(player_y)), PLAYER_RADIUS, width=2)

        hud1 = font.render(f'FPS: {avg_fps:5.1f}   Enemies: {n}', True, (255, 255, 255))
        hud2 = font.render(mode, True, (255, 210, 90) if ws is None else (120, 255, 140))
        hud3 = font.render(f'Survived: {current_streak:4.1f}s   Best: {best_streak:4.1f}s   Hits: {hits_taken}', True, (220, 220, 230))
        hud4 = font.render('Arrow keys: move  |  E: more enemies  |  Esc: quit', True, (150, 150, 160))
        screen.blit(hud1, (10, 8))
        screen.blit(hud2, (10, 30))
        screen.blit(hud3, (10, 52))
        screen.blit(hud4, (10, HEIGHT - 24))

        pygame.display.flip()

    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass
    pygame.quit()


if __name__ == '__main__':
    main()
