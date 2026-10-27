"""
swarm_scene.py -- the Room Supercomputer's scene, in NumPy.

This is a line-by-line copy of web/swarm_scene.glsl (the shader every phone
and laptop runs). The host uses it to re-compute a few random pixels of a
square a device sent back; if they do not match, that device is cheating or
broken. All maths is float32, like the GPU.
"""

import math

import numpy as np

F = np.float32
ITER = 8
BOUND = F(1.25)
LIGHT = np.array([-0.5025, 0.8040, -0.3015], F)
TILE = 128


# ----------------------------------------------------------------- jobs
def poster_job(width=3840, height=2160, spp=2):
    return {"kind": "poster", "title": f"{width} × {height} poster", "width": width, "height": height,
            "frames": [frame_params(0.0, width, height, spp=spp)]}


def movie_job(frames=96, width=1280, height=720, spp=2):
    return {"kind": "movie", "title": f"{frames}-frame movie ({width} × {height})", "width": width,
            "height": height, "frames": [frame_params(i / max(frames - 1, 1), width, height, movie=True, spp=spp)
                                         for i in range(frames)]}


def frame_params(u, width, height, movie=False, spp=2):
    """u in [0, 1]: position in the movie. The poster uses a fixed, flattering view."""
    if movie:
        ang = math.radians(-35 + 140 * u)
        dist = 3.4 - 1.0 * math.sin(math.pi * u)
        cam = [dist * math.sin(ang), 1.0 + 0.4 * math.sin(2 * math.pi * u), -dist * math.cos(ang)]
        power = 8.0 + 2.0 * math.sin(math.pi * u)
    else:
        cam = [2.0, 1.15, -2.6]
        power = 8.0
    return {"w": width, "h": height, "cam": cam, "target": [0.0, -0.08, 0.0], "fov": 0.42,
            "power": power, "spp": spp, "steps": 160, "shadow": 40}


def tiles_for(width, height, tile=TILE):
    """Squares ordered from the centre outwards, so the picture grows from the middle."""
    out = []
    for ty in range(0, height, tile):
        for tx in range(0, width, tile):
            out.append((tx, ty, min(tile, width - tx), min(tile, height - ty)))
    cx, cy = width / 2, height / 2
    out.sort(key=lambda t: (t[0] + t[2] / 2 - cx) ** 2 + (t[1] + t[3] / 2 - cy) ** 2)
    return out


# ----------------------------------------------------------------- the maths
def _length(v):
    return np.sqrt((v * v).sum(-1))


def _normalize(v):
    return v / _length(v)[..., None]


def de(p, power):
    P = F(power)
    z = p.copy()
    n = len(p)
    dr = np.ones(n, F)
    r = np.zeros(n, F)
    trap = np.full(n, F(1e10))
    act = np.ones(n, bool)
    for _ in range(ITER):
        rr = _length(z)
        r = np.where(act, rr, r)
        act = act & ~(rr > F(2.0))
        idx = np.nonzero(act)[0]
        if len(idx) == 0:
            break
        ri = r[idx]
        trap[idx] = np.minimum(trap[idx], ri)
        rs = np.maximum(ri, F(1e-6))
        zi = z[idx]
        theta = np.arccos(np.clip(zi[:, 1] / rs, F(-1), F(1))) * P
        phi = np.arctan2(zi[:, 2], zi[:, 0]) * P
        dr[idx] = np.power(rs, P - F(1)) * P * dr[idx] + F(1)
        zr = np.power(rs, P)
        st = np.sin(theta)
        z[idx] = zr[:, None] * np.stack([st * np.cos(phi), np.cos(theta), st * np.sin(phi)], 1) + p[idx]
    d = F(0.5) * np.log(np.maximum(r, F(1e-6))) * r / dr
    return d.astype(F), trap


def de0(p, power):
    return de(p, power)[0]


def background(rd):
    h = rd[:, 1:2] * F(0.5) + F(0.5)
    a = np.array([0.012, 0.016, 0.040], F)
    b = np.array([0.055, 0.080, 0.170], F)
    return a + (b - a) * h


def shade(ro, rd, pix, power, steps, shadow_steps):
    m = len(rd)
    out = background(rd)
    b = (rd * ro).sum(1)
    c = F(ro @ ro) - BOUND * BOUND
    disc = b * b - c
    inside = disc >= 0
    sq = np.sqrt(np.maximum(disc, F(0)))
    t = np.maximum(-b - sq, F(0))
    t_end = -b + sq
    trap = np.zeros(m, F)
    min_d = np.full(m, F(1e10))
    hit = np.zeros(m, bool)
    act = inside.copy()
    for _ in range(steps):
        idx = np.nonzero(act)[0]
        if len(idx) == 0:
            break
        p = ro + rd[idx] * t[idx, None]
        d, tr = de(p, power)
        trap[idx] = tr
        min_d[idx] = np.minimum(min_d[idx], d)
        h = d < pix * t[idx]
        hit[idx[h]] = True
        act[idx[h]] = False
        go = idx[~h]
        t[go] += d[~h]
        act[go[t[go] > t_end[go]]] = False
    miss = inside & ~hit
    glow = np.array([0.25, 0.45, 1.0], F)
    out[miss] += glow * (np.exp(-min_d[miss] * F(45.0)) * F(0.60))[:, None]

    hi = np.nonzero(hit)[0]
    if len(hi) == 0:
        return out
    rdh = rd[hi]
    p = ro + rdh * t[hi, None]
    h = np.maximum(pix * t[hi], F(1e-5))[:, None]
    k = [np.array(v, F) for v in ((1, -1, -1), (-1, -1, 1), (-1, 1, -1), (1, 1, 1))]
    n = sum(kk * h * de0(p + kk * h, power)[:, None] for kk in k)
    n = _normalize(n)

    ao = np.zeros(len(hi), F)
    w = F(1.0)
    for kk in range(1, 6):
        hk = F(0.015 + 0.025 * kk)
        ao += w * (hk - de0(p + n * hk, power))
        w = F(w * F(0.65))
    ao = np.clip(F(1) - F(3) * ao, F(0), F(1))

    sh = np.ones(len(hi), F)
    st = np.full(len(hi), F(0.01))
    sp = p + n * (F(2) * h)
    sact = np.ones(len(hi), bool)
    for _ in range(shadow_steps):
        idx = np.nonzero(sact)[0]
        if len(idx) == 0:
            break
        d = de0(sp[idx] + LIGHT * st[idx, None], power)
        sh[idx] = np.minimum(sh[idx], F(10) * d / st[idx])
        st[idx] += np.clip(d, F(0.004), F(0.08))
        done = (sh[idx] < F(0.001)) | (st[idx] > F(2.5))
        sact[idx[done]] = False
    sh = np.clip(sh, F(0), F(1))

    cm = np.clip((trap[hi] - F(0.80)) * F(3.3), F(0), F(1))[:, None]
    base = np.array([1.0, 0.58, 0.20], F) * (F(1) - cm) + np.array([0.10, 0.55, 0.95], F) * cm
    dif = (np.maximum((n * LIGHT).sum(1), F(0)) * sh)[:, None]
    sky = (F(0.5) + F(0.5) * n[:, 1])[:, None]
    rim = np.power(np.clip(F(1) + (rdh * n).sum(1), F(0), F(1)), F(3))[:, None]
    refl = rdh - F(2) * (n * rdh).sum(1)[:, None] * n
    spe = (np.power(np.maximum((refl * LIGHT).sum(1), F(0)), F(24)) * sh)[:, None]
    aoc = ao[:, None]
    col = base * (F(1.6) * dif * np.array([1.0, 0.92, 0.80], F)
                  + F(0.35) * sky * aoc * np.array([0.35, 0.45, 0.70], F) + F(0.06) * aoc) \
        + F(0.45) * spe + F(0.25) * rim * aoc * np.array([0.40, 0.60, 1.0], F)
    out[hi] = col
    return out


def render_pixels(fp, xs, ys):
    """RGB uint8 for pixels (xs, ys) of a frame -- exactly what the shader writes."""
    xs = np.asarray(xs, F) + F(0.5)
    ys = np.asarray(ys, F) + F(0.5)
    W, H = F(fp["w"]), F(fp["h"])
    pos = np.array(fp["cam"], F)
    tgt = np.array(fp["target"], F)
    fov = F(fp["fov"])
    fw = _normalize(tgt - pos)
    rt = _normalize(np.cross(fw, np.array([0, 1, 0], F)).astype(F))
    up = np.cross(rt, fw).astype(F)
    aspect = W / H
    pix = fov / H
    n = fp["spp"]
    acc = np.zeros((len(xs), 3), F)
    for sy in range(n):
        for sx in range(n):
            ox = F((sx + 0.5) / n - 0.5)
            oy = F((sy + 0.5) / n - 0.5)
            qx, qy = xs + ox, ys + oy
            x = (F(2) * qx / W - F(1)) * aspect * fov
            y = (F(1) - F(2) * qy / H) * fov
            rd = _normalize(fw + x[:, None] * rt + y[:, None] * up).astype(F)
            acc += shade(pos, rd, pix, fp["power"], fp["steps"], fp["shadow"])
    col = acc / F(n * n)
    col = F(1) - np.exp(-col * F(1.25))
    lum = (col * np.array([0.299, 0.587, 0.114], F)).sum(1)[:, None]
    col = np.clip(lum + (col - lum) * F(1.4), F(0), F(1))
    col = np.power(np.maximum(col, F(0)), F(0.4545))
    u = xs / W - F(0.5)
    v = ys / H - F(0.5)
    col *= (F(1) - F(0.35) * (u * u + v * v))[:, None]
    return np.clip(np.round(col * F(255)), 0, 255).astype(np.uint8)


def tile_weights(fp, tiles, n=3):
    """Rough cost of each square: squares full of fractal take far longer than plain sky.
    Shoots n*n primary rays per square and counts how many hit the fractal."""
    xs, ys = [], []
    for tx, ty, tw, th in tiles:
        for a in range(n):
            for b in range(n):
                xs.append(tx + (a + 0.5) * tw / n)
                ys.append(ty + (b + 0.5) * th / n)
    xs, ys = np.array(xs, F), np.array(ys, F)
    W, H = F(fp["w"]), F(fp["h"])
    pos, tgt, fov = np.array(fp["cam"], F), np.array(fp["target"], F), F(fp["fov"])
    fw = _normalize(tgt - pos)
    rt = _normalize(np.cross(fw, np.array([0, 1, 0], F)).astype(F))
    up = np.cross(rt, fw).astype(F)
    x = (F(2) * xs / W - F(1)) * (W / H) * fov
    y = (F(1) - F(2) * ys / H) * fov
    rd = _normalize(fw + x[:, None] * rt + y[:, None] * up).astype(F)
    b = (rd * pos).sum(1)
    disc = b * b - (F(pos @ pos) - BOUND * BOUND)
    inside = disc >= 0
    t = np.maximum(-b - np.sqrt(np.maximum(disc, F(0))), F(0))
    t_end = -b + np.sqrt(np.maximum(disc, F(0)))
    hit = np.zeros(len(rd), bool)
    act = inside.copy()
    pix = fov / H * F(8)                      # coarse threshold: this is only an estimate
    for _ in range(80):
        idx = np.nonzero(act)[0]
        if len(idx) == 0:
            break
        d = de0(pos + rd[idx] * t[idx, None], fp["power"])
        h = d < pix * t[idx]
        hit[idx[h]] = True
        act[idx[h]] = False
        go = idx[~h]
        t[go] += d[~h]
        act[go[t[go] > t_end[go]]] = False
    cover = hit.reshape(len(tiles), n * n).mean(1)
    inside_frac = inside.reshape(len(tiles), n * n).mean(1)
    return 0.15 + 0.35 * inside_frac + cover          # sky ~0.15, inside the glow ~0.5, fractal ~1.5


def render_tile(fp, tx, ty, tw, th):
    yy, xx = np.mgrid[ty:ty + th, tx:tx + tw]
    return render_pixels(fp, xx.ravel(), yy.ravel()).reshape(th, tw, 3)
