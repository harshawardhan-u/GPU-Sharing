"""
real_time_compute.py -- real-time GPU compute offloading (NOT screen sharing).

Unlike cloud_run.py (upload a script once, read back its text output when
it's done), this keeps a live, two-way connection open for the lifetime of
an interactive program: every frame, the client sends a little input/state
data out and gets back updated results, computed on a peer's GPU. No video
ever crosses the network -- only whatever tiny state the target script
defines. The target script (e.g. game.py) still opens its own window and
renders locally; only the heavy per-frame computation is offloaded.

On the GPU laptop, start the compute server:
    python real_time_compute.py --serve

On the non-GPU laptop, run a real-time-compute-aware script through it:
    python real_time_compute.py game.py
    python real_time_compute.py game.py --host 192.168.1.23 --code 481937
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

# Some Windows terminals default to a legacy codepage (e.g. cp1252) that
# can't encode emoji, which would otherwise crash any of the print
# statements below.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

DISCOVERY_PORT = 47611          # distinct from dashboard.py's LAN feature (47600) -- no collision
ANNOUNCE_MAGIC = 'COMPUTE_NEXUS_RT_HOST'
DEFAULT_PORT = 47612

FRAME_IN_FMT = '<ffBfI'         # mouse_x, mouse_y, mouse_active, dt, particle_count
FRAME_IN_SIZE = struct.calcsize(FRAME_IN_FMT)

# 'defender' mode (Star Defender / star_defender.py) has richer input (fire +
# aim direction) and returns TWO variable-length arrays (enemies, bullets)
# plus event counters (kills, was-player-hit) each frame, so it uses its own
# formats instead of the simple fixed-array ones above.
FRAME_IN_FMT_DEFENDER = '<ffBfffI'    # player_x, player_y, fire, aim_dx, aim_dy, dt, want_n
FRAME_IN_SIZE_DEFENDER = struct.calcsize(FRAME_IN_FMT_DEFENDER)
FRAME_OUT_HEADER_FMT_DEFENDER = '<IIiB'  # enemy_count, bullet_count, kills_this_frame, player_hit
FRAME_OUT_HEADER_SIZE_DEFENDER = struct.calcsize(FRAME_OUT_HEADER_FMT_DEFENDER)


def get_lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.168.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def check_nvidia_gpu():
    try:
        res = subprocess.run(
            ['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'],
            capture_output=True, text=True, timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return True, res.stdout.strip().splitlines()[0].strip()
    except Exception:
        pass
    return False, None


# ===========================================================================
# Server (run on the GPU laptop)
# ===========================================================================
def announce_loop(stop_event, name, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    payload = json.dumps({'type': ANNOUNCE_MAGIC, 'name': name, 'ip': get_lan_ip(), 'port': port}).encode('utf-8')
    try:
        while not stop_event.is_set():
            try:
                sock.sendto(payload, ('255.255.255.255', DISCOVERY_PORT))
            except Exception:
                pass
            stop_event.wait(1.0)
    finally:
        sock.close()


def make_ws_handler(session_code):
    """
    Builds the connection handler for the compute server. Dispatches to one
    of a few physics modes based on a 'game' field in the handshake (default
    'gravity', for backward compatibility with clients that predate this --
    they never send that field, so they're unaffected by any of this).
    Each mode has its own physics_step + spawn function; the gravity mode's
    functions are byte-for-byte what they always were.
    """
    import torch

    # --- mode: 'gravity' (Gravity Swarm / game.py) -- UNCHANGED from before ---
    def gravity_physics_step(pos, vel, mouse_x, mouse_y, mouse_active, dt, chunk=1024,
                              G=800.0, softening=4.0, damping=0.999, max_speed=400.0):
        N = pos.shape[0]
        device = pos.device
        accel = torch.zeros_like(pos)
        idx_all = torch.arange(N, device=device)
        for start in range(0, N, chunk):
            end = min(start + chunk, N)
            block = pos[start:end]
            diff = pos.unsqueeze(0) - block.unsqueeze(1)               # (b, N, 2)
            dist_sq = (diff * diff).sum(-1) + softening * softening
            inv_dist3 = dist_sq.pow(-1.5)
            rows = torch.arange(end - start, device=device)
            inv_dist3[rows, idx_all[start:end]] = 0.0
            accel[start:end] = G * (diff * inv_dist3.unsqueeze(-1)).sum(dim=1)

        if mouse_active:
            mouse = torch.tensor([mouse_x, mouse_y], device=device)
            to_mouse = mouse.unsqueeze(0) - pos
            d2 = (to_mouse * to_mouse).sum(-1) + 400.0
            accel = accel + 6000.0 * to_mouse / d2.unsqueeze(-1)

        vel = (vel + accel * dt) * damping
        speed = vel.norm(dim=-1)
        too_fast = speed > max_speed
        if bool(too_fast.any()):
            vel[too_fast] = vel[too_fast] * (max_speed / speed[too_fast]).unsqueeze(-1)
        pos = pos + vel * dt
        # wrap around the window edges (toroidal) so particles never drift off to infinity
        pos = torch.remainder(pos, torch.tensor([900.0, 900.0], device=pos.device))
        return pos, vel

    def spawn_gravity(n, device, width=900.0, height=900.0):
        pos = torch.rand(n, 2, device=device) * torch.tensor([width, height], device=device)
        vel = torch.zeros(n, 2, device=device)
        return pos, vel

    # --- mode: 'dodge' (Swarm Dodge / dodge_swarm.py) -- new ---
    def dodge_physics_step(pos, vel, player_x, player_y, dt, chunk=512,
                            repel_strength=9000.0, repel_range=50.0,
                            chase_speed=150.0, damping=0.995, max_speed=230.0):
        N = pos.shape[0]
        device = pos.device
        accel = torch.zeros_like(pos)
        idx_all = torch.arange(N, device=device)
        for start in range(0, N, chunk):
            end = min(start + chunk, N)
            block = pos[start:end]
            diff = block.unsqueeze(1) - pos.unsqueeze(0)               # (b, N, 2): away from neighbor
            dist_sq = (diff * diff).sum(-1) + 1.0
            dist = dist_sq.sqrt()
            within = dist < repel_range
            rows = torch.arange(end - start, device=device)
            within[rows, idx_all[start:end]] = False
            force_mag = torch.where(within, repel_strength / dist_sq, torch.zeros_like(dist_sq))
            accel[start:end] = (diff / dist.unsqueeze(-1).clamp(min=1e-3) * force_mag.unsqueeze(-1)).sum(dim=1)

        player = torch.tensor([player_x, player_y], device=device)
        to_player = player.unsqueeze(0) - pos
        dist_to_player = to_player.norm(dim=-1, keepdim=True).clamp(min=1e-3)
        accel = accel + (to_player / dist_to_player) * chase_speed

        vel = (vel + accel * dt) * damping
        speed = vel.norm(dim=-1)
        too_fast = speed > max_speed
        if bool(too_fast.any()):
            vel[too_fast] = vel[too_fast] * (max_speed / speed[too_fast]).unsqueeze(-1)
        pos = pos + vel * dt
        # soft clamp well beyond the visible window -- enemies chase the player
        # so they never wander far, this just stops runaway edge cases
        pos = torch.clamp(pos, torch.tensor([-300.0, -300.0], device=device), torch.tensor([1200.0, 1200.0], device=device))
        return pos, vel

    def spawn_dodge(n, device, width=900.0, height=900.0, margin=60.0):
        if n == 0:
            return torch.zeros(0, 2, device=device), torch.zeros(0, 2, device=device)
        side = torch.randint(0, 4, (n,), device=device)
        t = torch.rand(n, device=device)
        x = torch.zeros(n, device=device)
        y = torch.zeros(n, device=device)
        m0 = side == 0  # top
        x[m0], y[m0] = t[m0] * width, -margin
        m1 = side == 1  # right
        x[m1], y[m1] = width + margin, t[m1] * height
        m2 = side == 2  # bottom
        x[m2], y[m2] = t[m2] * width, height + margin
        m3 = side == 3  # left
        x[m3], y[m3] = -margin, t[m3] * height
        pos = torch.stack([x, y], dim=1)
        vel = torch.zeros(n, 2, device=device)
        return pos, vel

    MODES = {
        'gravity': (gravity_physics_step, spawn_gravity),
        'dodge': (dodge_physics_step, spawn_dodge),
    }

    # --- mode: 'defender' (Star Defender / star_defender.py) -- new ---
    # Reuses dodge's swarm physics/spawner for the enemies, adds bullets
    # (simple constant-velocity projectiles) and bullet<->enemy /
    # enemy<->player collision, which is why it needs its own richer wire
    # format (see FRAME_IN_FMT_DEFENDER / FRAME_OUT_HEADER_FMT_DEFENDER)
    # and its own session loop instead of fitting the MODES table above.
    def spawn_bullet(player_x, player_y, aim_dx, aim_dy, device, bullet_speed=600.0):
        norm = (aim_dx ** 2 + aim_dy ** 2) ** 0.5
        if norm < 1e-3:
            aim_dx, aim_dy, norm = 0.0, -1.0, 1.0
        pos = torch.tensor([[player_x, player_y]], device=device)
        vel = torch.tensor([[aim_dx / norm * bullet_speed, aim_dy / norm * bullet_speed]], device=device)
        return pos, vel

    def step_bullets(bpos, bvel, dt, width=900.0, height=900.0, margin=50.0):
        bpos = bpos + bvel * dt
        keep = (bpos[:, 0] > -margin) & (bpos[:, 0] < width + margin) & \
               (bpos[:, 1] > -margin) & (bpos[:, 1] < height + margin)
        return bpos[keep], bvel[keep]

    def resolve_bullet_hits(epos, evel, bpos, bvel, hit_radius=16.0):
        if epos.shape[0] == 0 or bpos.shape[0] == 0:
            return epos, evel, bpos, bvel, 0
        diff = bpos.unsqueeze(1) - epos.unsqueeze(0)          # (M, N, 2)
        dist_sq = (diff * diff).sum(-1)
        hit_matrix = dist_sq < (hit_radius ** 2)              # (M, N)
        bullet_hit = hit_matrix.any(dim=1)
        enemy_hit = hit_matrix.any(dim=0)
        kills = int(enemy_hit.sum().item())
        return epos[~enemy_hit], evel[~enemy_hit], bpos[~bullet_hit], bvel[~bullet_hit], kills

    def check_player_hit(epos, player_x, player_y, hit_radius=14.0):
        if epos.shape[0] == 0:
            return False
        d2 = (epos[:, 0] - player_x) ** 2 + (epos[:, 1] - player_y) ** 2
        return bool((d2 < hit_radius ** 2).any().item())

    def run_defender_session(websocket, device):
        remote_addr = websocket.remote_address
        epos, evel = spawn_dodge(0, device)
        bpos = torch.zeros(0, 2, device=device)
        bvel = torch.zeros(0, 2, device=device)
        n_frames = 0
        t_start = time.time()
        try:
            for message in websocket:
                if not isinstance(message, (bytes, bytearray)) or len(message) != FRAME_IN_SIZE_DEFENDER:
                    continue
                player_x, player_y, fire, aim_dx, aim_dy, dt, want_n = struct.unpack(FRAME_IN_FMT_DEFENDER, message)
                dt = min(dt, 1 / 15)

                if want_n > epos.shape[0]:
                    extra_pos, extra_vel = spawn_dodge(want_n - epos.shape[0], device)
                    epos = torch.cat([epos, extra_pos], dim=0)
                    evel = torch.cat([evel, extra_vel], dim=0)

                if fire:
                    new_bpos, new_bvel = spawn_bullet(player_x, player_y, aim_dx, aim_dy, device)
                    bpos = torch.cat([bpos, new_bpos], dim=0)
                    bvel = torch.cat([bvel, new_bvel], dim=0)

                if epos.shape[0] > 0:
                    epos, evel = dodge_physics_step(epos, evel, player_x, player_y, dt)
                if bpos.shape[0] > 0:
                    bpos, bvel = step_bullets(bpos, bvel, dt)

                epos, evel, bpos, bvel, kills = resolve_bullet_hits(epos, evel, bpos, bvel)
                player_hit = check_player_hit(epos, player_x, player_y)

                header = struct.pack(FRAME_OUT_HEADER_FMT_DEFENDER, epos.shape[0], bpos.shape[0], kills, 1 if player_hit else 0)
                body = epos.to(torch.float32).cpu().numpy().tobytes() + bpos.to(torch.float32).cpu().numpy().tobytes()
                websocket.send(header + body)
                n_frames += 1
        except Exception:
            pass
        finally:
            elapsed = time.time() - t_start
            avg_fps = n_frames / elapsed if elapsed > 0 else 0
            print(f"[serve] client disconnected: {remote_addr} "
                  f"({n_frames} frames, avg {avg_fps:.1f} fps served, mode=defender)")

    def handler(websocket):
        try:
            first = json.loads(websocket.recv(timeout=5))
        except Exception:
            first = None
        if not isinstance(first, dict) or first.get('code') != session_code:
            try:
                websocket.send(json.dumps({'status': 'rejected'}))
            except Exception:
                pass
            print(f"[serve] rejected connection from {websocket.remote_address} (bad/missing code)")
            return

        game_mode = first.get('game', 'gravity')

        if game_mode == 'defender':
            try:
                websocket.send(json.dumps({'status': 'approved'}))
            except Exception:
                return
            print(f"[serve] client connected: {websocket.remote_address} (mode=defender)")
            run_defender_session(websocket, 'cuda')
            return

        if game_mode not in MODES:
            game_mode = 'gravity'
        physics_fn, spawn_fn = MODES[game_mode]

        try:
            websocket.send(json.dumps({'status': 'approved'}))
        except Exception:
            return

        remote_addr = websocket.remote_address
        print(f"[serve] client connected: {remote_addr} (mode={game_mode})")
        device = 'cuda'
        pos, vel = spawn_fn(0, device)
        n_frames = 0
        t_start = time.time()
        try:
            for message in websocket:
                if not isinstance(message, (bytes, bytearray)) or len(message) != FRAME_IN_SIZE:
                    continue
                in_x, in_y, in_flag, dt, want_n = struct.unpack(FRAME_IN_FMT, message)
                dt = min(dt, 1 / 15)  # clamp in case of a hiccup, avoid an unstable big step

                if want_n > pos.shape[0]:
                    extra_pos, extra_vel = spawn_fn(want_n - pos.shape[0], device)
                    pos = torch.cat([pos, extra_pos], dim=0)
                    vel = torch.cat([vel, extra_vel], dim=0)

                if pos.shape[0] > 0:
                    if game_mode == 'dodge':
                        pos, vel = physics_fn(pos, vel, in_x, in_y, dt)
                    else:
                        pos, vel = physics_fn(pos, vel, in_x, in_y, bool(in_flag), dt)

                websocket.send(pos.to(torch.float32).cpu().numpy().tobytes())
                n_frames += 1
        except Exception:
            pass
        finally:
            elapsed = time.time() - t_start
            avg_fps = n_frames / elapsed if elapsed > 0 else 0
            print(f"[serve] client disconnected: {remote_addr} "
                  f"({n_frames} frames, avg {avg_fps:.1f} fps served, mode={game_mode})")

    return handler


def run_server(port):
    has_gpu, gpu_name = check_nvidia_gpu()
    if not has_gpu:
        print("=" * 60)
        print("  X No NVIDIA GPU detected on this machine.")
        print("    real_time_compute.py --serve needs a real GPU to be useful.")
        print("=" * 60)
        sys.exit(1)

    import torch
    if not torch.cuda.is_available():
        print("X nvidia-smi found a GPU, but PyTorch can't see CUDA. Check your PyTorch install (needs a +cuXXX build).")
        sys.exit(1)

    session_code = ''.join(secrets.choice('0123456789') for _ in range(6))
    host_name = socket.gethostname()

    stop_event = threading.Event()
    threading.Thread(target=announce_loop, args=(stop_event, host_name, port), daemon=True).start()

    from websockets.sync.server import serve
    ip = get_lan_ip()
    print("=" * 60)
    print(f"  GPU: {gpu_name}")
    print("  Real-time compute server is live.")
    print(f"  CODE: {session_code}")
    print("  On the other laptop, just run:   python real_time_compute.py game.py")
    print("  (it auto-finds this host -- it will ask for the code above)")
    print(f"  Manual fallback:  python real_time_compute.py game.py --host {ip} --port {port} --code {session_code}")
    print("=" * 60)

    handler = make_ws_handler(session_code)
    try:
        with serve(handler, '0.0.0.0', port, max_size=None) as server:
            server.serve_forever()
    finally:
        stop_event.set()


# ===========================================================================
# Client launcher (run on the non-GPU laptop)
# ===========================================================================
def discover_hosts(timeout=3.0):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.5)
    found = {}
    try:
        sock.bind(('', DISCOVERY_PORT))
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(2048)
                info = json.loads(data.decode('utf-8'))
                if info.get('type') == ANNOUNCE_MAGIC:
                    found[info.get('ip', addr[0])] = info
            except socket.timeout:
                continue
            except Exception:
                continue
    except Exception:
        pass
    finally:
        sock.close()
    return list(found.values())


def resolve_host(args):
    if args.host:
        return args.host, args.port

    print("Searching for a GPU compute host on the LAN (3s)...")
    hosts = discover_hosts(timeout=3.0)
    if not hosts:
        print("No hosts found automatically.")
        ip = input("Enter the host IP manually: ").strip()
        return ip, args.port

    if len(hosts) == 1:
        h = hosts[0]
        print(f"Found host: {h.get('name', '?')} @ {h['ip']}")
        return h['ip'], h.get('port', args.port)

    print("Multiple hosts found:")
    for i, h in enumerate(hosts):
        print(f"  [{i}] {h.get('name', '?')} @ {h['ip']}")
    choice = input("Pick a number: ").strip()
    try:
        h = hosts[int(choice)]
        return h['ip'], h.get('port', args.port)
    except (ValueError, IndexError):
        print("Invalid choice.")
        sys.exit(1)


def run_client(args):
    if not os.path.exists(args.script):
        print(f"❌ Error: '{args.script}' not found.")
        sys.exit(1)

    host, port = resolve_host(args)
    code = args.code or input("Enter the 6-digit code shown on the host: ").strip()

    # Verify the connection + code up front so failures are reported clearly
    # here, rather than surfacing confusingly from inside the child script.
    try:
        from websockets.sync.client import connect
    except ImportError:
        print("❌ Error: the 'websockets' package isn't installed. Run: pip install websockets")
        sys.exit(1)

    url = f"ws://{host}:{port}"
    print(f"Connecting to {url} ...")
    try:
        with connect(url, open_timeout=5) as ws:
            ws.send(json.dumps({'code': code}))
            ack = json.loads(ws.recv(timeout=5))
            if ack.get('status') != 'approved':
                print("❌ Rejected by host - the code was wrong.")
                sys.exit(1)
    except Exception as e:
        print(f"❌ Could not reach the compute host: {e}")
        sys.exit(1)

    print("✅ Code accepted. Launching with GPU compute offload enabled...")
    env = os.environ.copy()
    env['RTCOMPUTE_HOST'] = host
    env['RTCOMPUTE_PORT'] = str(port)
    env['RTCOMPUTE_CODE'] = code

    result = subprocess.run([sys.executable, args.script], env=env)
    sys.exit(result.returncode)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('script', nargs='?', help='Script to run with GPU compute offload (e.g. game.py)')
    p.add_argument('--serve', action='store_true', help='Run as the GPU compute server')
    p.add_argument('--port', type=int, default=DEFAULT_PORT)
    p.add_argument('--host', default=None, help='Compute host LAN IP. Omit to auto-discover.')
    p.add_argument('--code', default=None, help='6-digit code shown by the host. Omit to be prompted.')
    args = p.parse_args()

    if args.serve:
        run_server(args.port)
        return

    if not args.script:
        print("Usage: python real_time_compute.py <script.py>   (or --serve on the GPU laptop)")
        sys.exit(1)

    run_client(args)


if __name__ == '__main__':
    main()
