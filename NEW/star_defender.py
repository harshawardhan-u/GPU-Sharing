"""
Star Defender -- an arrow-keys-and-shoot arcade game for the "GPU compute
offload" demo.

Move with the ARROW KEYS, shoot with SPACE (fires toward whichever
direction you last moved -- aim = facing). A swarm spawns at the edges and
homes in on you; shoot them before they reach you. Survive, rack up kills.
Press E to throw more enemies at yourself on purpose, for the demo's
"watch it break" moment.

This window always renders locally -- no video ever crosses the network.
Only enemy/bullet positions (tiny data) get computed remotely when
available.

    python star_defender.py
        Enemy/bullet physics computed right here, on the CPU (NumPy).
        Once the swarm gets big, it visibly lags -- and since this is a
        real action game, lag means missed shots and cheap hits.

    python real_time_compute.py star_defender.py
        Same window, same code, but the physics (including bullet-enemy
        collision, which is a second, separate O(bullets x enemies)
        computation on top of the swarm's own O(N^2) repulsion) runs on a
        peer's GPU instead. Also works automatically after pairing via the
        dashboard's "Real-Time Compute Setup" screen.
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
INITIAL_N = 200
GROWTH_PER_PRESS = 150
MAX_N = 3000

PLAYER_SPEED = 260.0
PLAYER_RADIUS = 10
HIT_RADIUS = 14.0
INVINCIBLE_SECONDS = 1.0
MAX_HP = 100
HP_PER_HIT = 25

BULLET_SPEED = 600.0
BULLET_RADIUS = 16.0
BULLET_COOLDOWN = 0.18  # seconds between shots

FRAME_IN_FMT = '<ffBfffI'          # player_x, player_y, fire, aim_dx, aim_dy, dt, want_n
FRAME_OUT_HEADER_FMT = '<IIiB'     # enemy_count, bullet_count, kills_this_frame, player_hit
FRAME_OUT_HEADER_SIZE = struct.calcsize(FRAME_OUT_HEADER_FMT)


# ---------------------------------------------------------------------------
# Local (NumPy/CPU) physics -- mirrors the server's 'defender' mode exactly.
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
    return np.stack([x, y], axis=1), np.zeros((n, 2), dtype=np.float32)


def enemy_step_numpy(pos, vel, player_x, player_y, dt, chunk=256,
                      repel_strength=9000.0, repel_range=50.0,
                      chase_speed=140.0, damping=0.995, max_speed=210.0):
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


def step_bullets_np(bpos, bvel, dt, margin=50.0):
    if bpos.shape[0] == 0:
        return bpos, bvel
    bpos = bpos + bvel * dt
    keep = (bpos[:, 0] > -margin) & (bpos[:, 0] < WIDTH + margin) & \
           (bpos[:, 1] > -margin) & (bpos[:, 1] < HEIGHT + margin)
    return bpos[keep], bvel[keep]


def resolve_bullet_hits_np(epos, evel, bpos, bvel, hit_radius=16.0):
    if epos.shape[0] == 0 or bpos.shape[0] == 0:
        return epos, evel, bpos, bvel, 0
    diff = bpos[:, None, :] - epos[None, :, :]
    dist_sq = (diff * diff).sum(-1)
    hit_matrix = dist_sq < (hit_radius ** 2)
    bullet_hit = hit_matrix.any(axis=1)
    enemy_hit = hit_matrix.any(axis=0)
    kills = int(enemy_hit.sum())
    return epos[~enemy_hit], evel[~enemy_hit], bpos[~bullet_hit], bvel[~bullet_hit], kills


def check_player_hit_np(epos, player_x, player_y, hit_radius=14.0):
    if epos.shape[0] == 0:
        return False
    d2 = (epos[:, 0] - player_x) ** 2 + (epos[:, 1] - player_y) ** 2
    return bool(np.any(d2 < hit_radius ** 2))


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
        ws.send(json.dumps({'code': code, 'game': 'defender'}))
        ack = json.loads(ws.recv(timeout=5))
        if ack.get('status') != 'approved':
            print("⚠ Compute host rejected the code -- falling back to local compute.")
            ws.close()
            return None
        return ws, host
    except Exception as e:
        print(f"⚠ Could not reach compute host ({e}) -- falling back to local compute.")
        return None


def remote_step(ws, player_x, player_y, fire, aim_dx, aim_dy, dt, want_n):
    msg = struct.pack(FRAME_IN_FMT, player_x, player_y, 1 if fire else 0, aim_dx, aim_dy, dt, want_n)
    ws.send(msg)
    reply = ws.recv(timeout=2)
    n_e, n_b, kills, hit = struct.unpack(FRAME_OUT_HEADER_FMT, reply[:FRAME_OUT_HEADER_SIZE])
    body = reply[FRAME_OUT_HEADER_SIZE:]
    epos = np.frombuffer(body[:n_e * 8], dtype=np.float32).reshape(-1, 2).copy()
    bpos = np.frombuffer(body[n_e * 8:n_e * 8 + n_b * 8], dtype=np.float32).reshape(-1, 2).copy()
    return epos, bpos, kills, bool(hit)


def build_palette(n, seed_offset=0.0):
    hues = (np.arange(n) * 0.6180339887 + seed_offset) % 1.0
    colors = np.zeros((n, 3), dtype=np.uint8)
    for i, h in enumerate(hues):
        r, g, b = _hsv_to_rgb(h, 0.8, 1.0)
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
    pygame.display.set_caption('Star Defender')
    clock = pygame.time.Clock()
    font = pygame.font.SysFont('consolas', 18)
    big_font = pygame.font.SysFont('consolas', 40, bold=True)

    remote = try_connect_remote()
    ws, remote_host = (remote[0], remote[1]) if remote else (None, None)

    rng = np.random.default_rng(7)
    want_n = INITIAL_N
    enemy_palette = build_palette(MAX_N)
    bullet_color = (255, 240, 140)

    def fresh_run_state():
        if ws is None:
            epos, evel = spawn_enemies_np(want_n, rng)
        else:
            epos, evel = np.zeros((0, 2), dtype=np.float32), None
        bpos = np.zeros((0, 2), dtype=np.float32)
        bvel = np.zeros((0, 2), dtype=np.float32)
        return epos, evel, bpos, bvel

    epos, evel, bpos, bvel = fresh_run_state()
    mode = "LOCAL COMPUTE (this laptop's CPU)" if ws is None else f'REMOTE GPU COMPUTE  (via {remote_host})'

    player_x, player_y = WIDTH / 2, HEIGHT / 2
    aim_dx, aim_dy = 0.0, -1.0
    last_shot_time = -999.0
    last_hit_time = -999.0
    kills = 0
    best_kills = 0
    hp = MAX_HP
    game_over_until = 0.0

    fps_samples = []
    running = True
    while running:
        dt_ms = clock.tick(240)
        dt = max(dt_ms / 1000.0, 1e-4)
        now = time.time()

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    running = False
                elif event.key == pygame.K_e:
                    want_n = min(MAX_N, want_n + GROWTH_PER_PRESS)
                    if ws is None:
                        extra_pos, extra_vel = spawn_enemies_np(want_n - epos.shape[0], rng)
                        epos = np.concatenate([epos, extra_pos], axis=0)
                        evel = np.concatenate([evel, extra_vel], axis=0)

        in_game_over = now < game_over_until
        keys = pygame.key.get_pressed()
        mx = my = 0.0
        if not in_game_over:
            if keys[pygame.K_LEFT]:
                mx -= 1.0
            if keys[pygame.K_RIGHT]:
                mx += 1.0
            if keys[pygame.K_UP]:
                my -= 1.0
            if keys[pygame.K_DOWN]:
                my += 1.0
        if mx != 0.0 or my != 0.0:
            norm = (mx * mx + my * my) ** 0.5
            mx, my = mx / norm, my / norm
            aim_dx, aim_dy = mx, my
            player_x += mx * PLAYER_SPEED * dt
            player_y += my * PLAYER_SPEED * dt
        player_x = min(max(player_x, PLAYER_RADIUS), WIDTH - PLAYER_RADIUS)
        player_y = min(max(player_y, PLAYER_RADIUS), HEIGHT - PLAYER_RADIUS)

        fire = (not in_game_over) and keys[pygame.K_SPACE] and (now - last_shot_time) >= BULLET_COOLDOWN
        if fire:
            last_shot_time = now

        if in_game_over:
            fire = False

        if ws is not None and not in_game_over:
            try:
                epos, bpos, frame_kills, was_hit = remote_step(ws, player_x, player_y, fire, aim_dx, aim_dy, dt, want_n)
            except Exception as e:
                print(f"⚠ Lost connection to compute host ({e}). Falling back to local compute.")
                ws = None
                mode = 'LOCAL COMPUTE (remote connection lost)'
                epos, evel = spawn_enemies_np(want_n, rng)
                bpos, bvel = np.zeros((0, 2), dtype=np.float32), np.zeros((0, 2), dtype=np.float32)
                frame_kills, was_hit = 0, False
        elif not in_game_over:
            if want_n > epos.shape[0]:
                extra_pos, extra_vel = spawn_enemies_np(want_n - epos.shape[0], rng)
                epos = np.concatenate([epos, extra_pos], axis=0)
                evel = np.concatenate([evel, extra_vel], axis=0)
            if fire:
                bpos = np.concatenate([bpos, [[player_x, player_y]]], axis=0).astype(np.float32)
                bvel = np.concatenate([bvel, [[aim_dx * BULLET_SPEED, aim_dy * BULLET_SPEED]]], axis=0).astype(np.float32)
            if epos.shape[0] > 0:
                epos, evel = enemy_step_numpy(epos, evel, player_x, player_y, dt)
            bpos, bvel = step_bullets_np(bpos, bvel, dt)
            epos, evel, bpos, bvel, frame_kills = resolve_bullet_hits_np(epos, evel, bpos, bvel)
            was_hit = check_player_hit_np(epos, player_x, player_y)
        else:
            frame_kills, was_hit = 0, False

        kills += frame_kills
        best_kills = max(best_kills, kills)

        invincible = (now - last_hit_time) < INVINCIBLE_SECONDS
        if was_hit and not invincible and not in_game_over:
            last_hit_time = now
            hp -= HP_PER_HIT
            if hp <= 0:
                hp = 0
                game_over_until = now + 2.5

        if in_game_over and now >= game_over_until:
            hp = MAX_HP
            kills = 0
            player_x, player_y = WIDTH / 2, HEIGHT / 2
            want_n = INITIAL_N
            last_hit_time = -999.0
            if ws is None:
                # Fully self-contained restart -- safe to hand back a fresh wave.
                epos, evel, bpos, bvel = fresh_run_state()
            # In remote mode, the compute server has no "reset" signal (yet),
            # so leave epos/bpos as whatever it last sent rather than faking
            # a reset the server doesn't know about -- that would desync the
            # two. want_n going back to INITIAL_N just stops asking it to
            # keep growing; it doesn't shrink what's already there.

        fps_samples.append(1.0 / dt)
        if len(fps_samples) > 30:
            fps_samples.pop(0)
        avg_fps = sum(fps_samples) / len(fps_samples)

        screen.fill((8, 8, 14))
        n = epos.shape[0]
        if n > 0:
            xs = np.clip(epos[:, 0].astype(np.int32), 0, WIDTH - 1)
            ys = np.clip(epos[:, 1].astype(np.int32), 0, HEIGHT - 1)
            pixels = pygame.surfarray.pixels3d(screen)
            pixels[xs, ys] = enemy_palette[:n]
            del pixels
        for bx, by in bpos:
            pygame.draw.circle(screen, bullet_color, (int(bx), int(by)), 3)

        blink = (now - last_hit_time < INVINCIBLE_SECONDS) and int(now * 8) % 2 == 0
        player_color = (255, 90, 90) if blink else (120, 230, 255)
        pygame.draw.circle(screen, player_color, (int(player_x), int(player_y)), PLAYER_RADIUS)
        pygame.draw.circle(screen, (255, 255, 255), (int(player_x), int(player_y)), PLAYER_RADIUS, width=2)
        tip = (int(player_x + aim_dx * 18), int(player_y + aim_dy * 18))
        pygame.draw.line(screen, (255, 255, 255), (int(player_x), int(player_y)), tip, 2)

        hud1 = font.render(f'FPS: {avg_fps:5.1f}   Enemies: {n}   Bullets: {bpos.shape[0]}', True, (255, 255, 255))
        hud2 = font.render(mode, True, (255, 210, 90) if ws is None else (120, 255, 140))
        hud3 = font.render(f'HP: {hp:3d}/{MAX_HP}   Kills: {kills}   Best: {best_kills}', True, (220, 220, 230))
        hud4 = font.render('Arrows: move+aim  |  Space: shoot  |  E: more enemies  |  Esc: quit', True, (150, 150, 160))
        screen.blit(hud1, (10, 8))
        screen.blit(hud2, (10, 30))
        screen.blit(hud3, (10, 52))
        screen.blit(hud4, (10, HEIGHT - 24))

        hp_w = int(200 * hp / MAX_HP)
        pygame.draw.rect(screen, (60, 60, 70), (10, 78, 200, 14))
        pygame.draw.rect(screen, (90, 220, 120) if hp > 30 else (220, 80, 80), (10, 78, hp_w, 14))

        if in_game_over:
            msg = big_font.render('GAME OVER', True, (255, 90, 90))
            screen.blit(msg, (WIDTH // 2 - msg.get_width() // 2, HEIGHT // 2 - 20))

        pygame.display.flip()

    if ws is not None:
        try:
            ws.close()
        except Exception:
            pass
    pygame.quit()


if __name__ == '__main__':
    main()
