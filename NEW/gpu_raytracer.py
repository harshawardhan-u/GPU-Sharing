"""
GPU ray tracer -- the 'impossible on this laptop' demo.

Renders a classic reflective-spheres-on-a-checkered-floor scene by casting
rays and bouncing them off mirrors, computing everything as PyTorch tensor
ops across every pixel at once. This is deliberately from-scratch (no
pretrained model, no downloaded weights, no external API) so it's
unambiguously "this GPU did real work", not "we called a library".

Run directly to test:
    python gpu_raytracer.py --device cuda --width 1280 --height 720
    python gpu_raytracer.py --device cpu  --width 1280 --height 720

Run via cloud-run (see cloud_run.py) to execute on a peer's GPU and have
the rendered PNG sent back and opened automatically:
    cloud-run gpu_raytracer.py
"""

import argparse
import base64
import math
import struct
import sys
import time
import zlib

import torch


def normalize(v, dim=-1, eps=1e-8):
    return v / (v.norm(dim=dim, keepdim=True) + eps)


def intersect_scene(ray_orig, ray_dir, centers, radii):
    """Ray-sphere intersection for every pixel against every object at once.
    ray_orig/ray_dir: (H, W, 3). centers: (N, 3). radii: (N,).
    Returns hit_t (H, W) and hit_idx (H, W), hit_idx=-1 for misses."""
    H, W, _ = ray_orig.shape
    N = centers.shape[0]
    oc = ray_orig.unsqueeze(2) - centers.view(1, 1, N, 3)          # (H, W, N, 3)
    d = ray_dir.unsqueeze(2).expand(H, W, N, 3)                    # (H, W, N, 3)
    a = (d * d).sum(-1)
    b = (oc * d).sum(-1)
    c = (oc * oc).sum(-1) - (radii.view(1, 1, N) ** 2)
    disc = b * b - a * c
    sqrt_disc = torch.sqrt(disc.clamp(min=0))
    t0 = (-b - sqrt_disc) / a
    valid = (disc > 0) & (t0 > 1e-3)
    t0 = torch.where(valid, t0, torch.full_like(t0, float('inf')))
    hit_t, hit_idx = t0.min(dim=-1)
    hit_idx = torch.where(torch.isfinite(hit_t), hit_idx, torch.full_like(hit_idx, -1))
    return hit_t, hit_idx


def build_scene(device):
    centers = torch.tensor([
        [0.0, -1000.0, 0.0],   # giant sphere standing in for the ground
        [0.0, 1.0, 0.0],
        [-2.6, 1.0, 0.8],
        [2.6, 1.0, -0.4],
        [-1.0, 0.6, 2.4],
    ], device=device)
    radii = torch.tensor([1000.0, 1.0, 1.0, 1.0, 0.6], device=device)
    albedos = torch.tensor([
        [0.5, 0.5, 0.5],
        [0.9, 0.9, 0.95],
        [0.8, 0.15, 0.15],
        [0.9, 0.7, 0.2],
        [0.2, 0.4, 0.9],
    ], device=device)
    is_mirror = torch.tensor([False, True, False, True, False], device=device)
    is_ground = torch.tensor([True, False, False, False, False], device=device)
    return centers, radii, albedos, is_mirror, is_ground


def render(device, width, height, samples, max_bounces, progress=True):
    centers, radii, albedos, is_mirror, is_ground = build_scene(device)

    aspect = width / height
    cam_origin = torch.tensor([0.0, 2.2, 7.0], device=device)
    look_at = torch.tensor([0.0, 0.7, 0.0], device=device)
    vup = torch.tensor([0.0, 1.0, 0.0], device=device)
    vfov = 35.0
    half_h = math.tan(math.radians(vfov) / 2)
    half_w = aspect * half_h

    w_axis = normalize(cam_origin - look_at)
    u_axis = normalize(torch.linalg.cross(vup, w_axis))
    v_axis = torch.linalg.cross(w_axis, u_axis)

    lower_left = cam_origin - half_w * u_axis - half_h * v_axis - w_axis
    horizontal = 2 * half_w * u_axis
    vertical = 2 * half_h * v_axis

    ys, xs = torch.meshgrid(
        torch.arange(height, device=device),
        torch.arange(width, device=device),
        indexing='ij',
    )
    xs = xs.float()
    ys = (height - 1 - ys).float()

    light_dirs = normalize(torch.tensor([
        [-1.0, 1.2, 0.6],
        [-0.85, 1.3, 0.75],
        [-1.15, 1.15, 0.5],
        [-1.0, 1.25, 0.4],
    ], device=device))

    accum = torch.zeros(height, width, 3, device=device)

    for s in range(samples):
        u = (xs + torch.rand_like(xs)) / (width - 1)
        v = (ys + torch.rand_like(ys)) / (height - 1)
        ray_dir = normalize(
            lower_left.view(1, 1, 3)
            + u.unsqueeze(-1) * horizontal.view(1, 1, 3)
            + v.unsqueeze(-1) * vertical.view(1, 1, 3)
            - cam_origin.view(1, 1, 3)
        )
        ray_orig = cam_origin.view(1, 1, 3).expand(height, width, 3).clone()

        throughput = torch.ones(height, width, 3, device=device)
        color = torch.zeros(height, width, 3, device=device)
        active = torch.ones(height, width, dtype=torch.bool, device=device)

        for bounce in range(max_bounces):
            hit_t, hit_idx = intersect_scene(ray_orig, ray_dir, centers, radii)
            hit_mask = hit_idx >= 0

            t_sky = 0.5 * (ray_dir[..., 1] + 1.0)
            sky = (1 - t_sky).unsqueeze(-1) * torch.tensor([1.0, 1.0, 1.0], device=device) + \
                t_sky.unsqueeze(-1) * torch.tensor([0.5, 0.7, 1.0], device=device)
            escaped = active & (~hit_mask)
            color = color + torch.where(escaped.unsqueeze(-1), throughput * sky, torch.zeros_like(color))
            active = active & hit_mask
            if not bool(active.any()):
                break

            safe_idx = hit_idx.clamp(min=0)
            hit_point = ray_orig + hit_t.unsqueeze(-1) * ray_dir
            normal = normalize(hit_point - centers[safe_idx])

            albedo = albedos[safe_idx].clone()
            ground_mask = is_ground[safe_idx] & active
            checker = (torch.floor(hit_point[..., 0] * 0.7) + torch.floor(hit_point[..., 2] * 0.7)) % 2
            dark = checker < 1
            light_floor = torch.tensor([0.85, 0.85, 0.82], device=device)
            dark_floor = torch.tensor([0.1, 0.12, 0.15], device=device)
            floor_color = torch.where(dark.unsqueeze(-1), dark_floor, light_floor)
            albedo = torch.where(ground_mask.unsqueeze(-1), floor_color, albedo)

            direct = torch.zeros(height, width, 3, device=device)
            shadow_orig = hit_point + normal * 1e-3
            for ld in light_dirs:
                ndotl = (normal * ld.view(1, 1, 3)).sum(-1).clamp(min=0)
                ld_expand = ld.view(1, 1, 3).expand(height, width, 3)
                _, shadow_idx = intersect_scene(shadow_orig, ld_expand, centers, radii)
                lit = (shadow_idx < 0).float()
                direct = direct + (ndotl * lit).unsqueeze(-1) * albedo
            direct = direct / len(light_dirs)

            ambient = 0.08 * albedo
            mirror_mask = is_mirror[safe_idx]
            surface_color = torch.where(
                mirror_mask.unsqueeze(-1), torch.zeros_like(albedo), direct + ambient,
            )
            color = color + torch.where(active.unsqueeze(-1), throughput * surface_color, torch.zeros_like(color))

            reflect_dir = ray_dir - 2 * (ray_dir * normal).sum(-1, keepdim=True) * normal
            ray_dir = torch.where(mirror_mask.unsqueeze(-1), reflect_dir, ray_dir)
            ray_orig = hit_point + normal * 1e-3
            throughput = torch.where(
                mirror_mask.unsqueeze(-1), throughput * albedo.clamp(min=0.05), throughput,
            )
            active = active & mirror_mask

        accum = accum + color

        if progress and (s + 1) % max(1, samples // 10) == 0:
            pct = 100 * (s + 1) / samples
            print(f"  rendering... {pct:5.1f}%  (sample {s + 1}/{samples})", flush=True)

    img = (accum / samples).clamp(0, 1)
    img = img.pow(1 / 2.2)  # gamma correction
    img = (img * 255).round().byte().cpu().numpy()
    return img


def save_png(path, rgb_array):
    """Dependency-free PNG writer (only stdlib: struct + zlib) so this works
    inside any container regardless of whether Pillow happens to be installed."""
    h, w, _ = rgb_array.shape

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack(
            '>I', zlib.crc32(tag + data) & 0xffffffff,
        )

    sig = b'\x89PNG\r\n\x1a\n'
    ihdr = struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)  # 8-bit depth, RGB
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter type: none
        raw.extend(rgb_array[y].tobytes())
    idat = zlib.compress(bytes(raw), 6)
    png = sig + chunk(b'IHDR', ihdr) + chunk(b'IDAT', idat) + chunk(b'IEND', b'')
    with open(path, 'wb') as f:
        f.write(png)


def send_file_over_stdout(path):
    """Emits the file wrapped in sentinel markers that cloud_run.py knows to
    intercept, decode, and save on the requester's machine instead of
    printing as raw text."""
    import os
    with open(path, 'rb') as f:
        encoded = base64.b64encode(f.read()).decode('ascii')
    name = os.path.basename(path)
    print(f"##CLOUDRUN_FILE_START:{name}##", flush=True)
    # split into lines so it streams cleanly over the chunked HTTP response
    for i in range(0, len(encoded), 4000):
        print(encoded[i:i + 4000], flush=True)
    print("##CLOUDRUN_FILE_END##", flush=True)


# Quality presets. 'low' is meant to run live on a non-GPU laptop's CPU in
# about a second (small + grainy); 'high' is what gets sent to a real GPU
# (large + smooth). Same scene, same code -- only the workload size differs,
# which is exactly what makes the side-by-side comparison honest.
QUALITY_PRESETS = {
    'low':    dict(width=400,  height=225, samples=3,  bounces=2),
    'medium': dict(width=960,  height=540, samples=16, bounces=4),
    'high':   dict(width=1280, height=720, samples=32, bounces=4),
}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--device', default=None, choices=['cuda', 'cpu'])
    p.add_argument('--quality', choices=list(QUALITY_PRESETS), default='high',
                    help='Quality preset; individual --width/--height/--samples/--bounces override it')
    p.add_argument('--width', type=int, default=None)
    p.add_argument('--height', type=int, default=None)
    p.add_argument('--samples', type=int, default=None)
    p.add_argument('--bounces', type=int, default=None)
    p.add_argument('--out', default='render.png')
    p.add_argument('--no-send', action='store_true', help='skip the base64 file transfer (for local testing)')
    args = p.parse_args()

    preset = QUALITY_PRESETS[args.quality]
    width = args.width if args.width is not None else preset['width']
    height = args.height if args.height is not None else preset['height']
    samples = args.samples if args.samples is not None else preset['samples']
    bounces = args.bounces if args.bounces is not None else preset['bounces']

    device = args.device or ('cuda' if torch.cuda.is_available() else 'cpu')
    if device == 'cuda' and not torch.cuda.is_available():
        print("⚠ CUDA requested but not available, falling back to CPU.")
        device = 'cpu'

    print(f"Quality: {args.quality}  |  Rendering {width}x{height}, {samples} samples, {bounces} bounces")
    print(f"Device: {device}" + (f" ({torch.cuda.get_device_name(0)})" if device == 'cuda' else " (no GPU)"))
    sys.stdout.flush()

    t0 = time.time()
    img = render(device, width, height, samples, bounces)
    if device == 'cuda':
        torch.cuda.synchronize()
    elapsed = time.time() - t0

    save_png(args.out, img)
    print(f"Done in {elapsed:.2f}s on {device}. Saved to {args.out}")

    if not args.no_send:
        send_file_over_stdout(args.out)
    elif sys.platform == 'win32':
        # Running standalone (not through cloud-run) -- open it directly so
        # there's nothing to go hunting for during a live demo.
        import os
        try:
            os.startfile(os.path.abspath(args.out))
        except Exception:
            pass


if __name__ == '__main__':
    main()
