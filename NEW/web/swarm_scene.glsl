#version 300 es
// Room Supercomputer scene: a Mandelbulb fractal, ray-marched.
// Every device (phone, laptop) runs this same shader for its squares of
// the picture. swarm_scene.py holds a NumPy copy of the same maths, which
// the host uses to spot-check the squares a device sends back.
precision highp float;
precision highp int;

uniform vec2 uImage;      // full picture size in pixels
uniform vec2 uOrigin;     // this square's top-left pixel
uniform float uTileH;     // this square's height in pixels
uniform vec3 uCamPos;
uniform vec3 uCamTarget;
uniform float uFov;       // tan(half vertical field of view)
uniform float uPower;     // fractal power (8 = classic Mandelbulb)
uniform int uSpp;         // samples per axis (2 -> 4 rays per pixel)
uniform int uMaxSteps;    // ray-march steps
uniform int uShadowSteps; // soft-shadow steps
out vec4 fragColor;

const int ITER = 8;
const float BOUND = 1.25;
const vec3 LIGHT = vec3(-0.5025, 0.8040, -0.3015);

float de(vec3 p, out float trap) {
    vec3 z = p;
    float dr = 1.0;
    float r = 0.0;
    trap = 1e10;
    for (int i = 0; i < ITER; i++) {
        r = length(z);
        if (r > 2.0) break;
        trap = min(trap, r);
        float rs = max(r, 1e-6);
        float theta = acos(clamp(z.y / rs, -1.0, 1.0)) * uPower;
        float phi = atan(z.z, z.x) * uPower;
        dr = pow(rs, uPower - 1.0) * uPower * dr + 1.0;
        float zr = pow(rs, uPower);
        z = zr * vec3(sin(theta) * cos(phi), cos(theta), sin(theta) * sin(phi)) + p;
    }
    return 0.5 * log(max(r, 1e-6)) * r / dr;
}

float de0(vec3 p) {
    float t;
    return de(p, t);
}

vec3 background(vec3 rd) {
    float h = rd.y * 0.5 + 0.5;
    return mix(vec3(0.012, 0.016, 0.040), vec3(0.055, 0.080, 0.170), h);
}

vec3 shade(vec3 ro, vec3 rd, float pix) {
    vec3 bg = background(rd);
    float b = dot(ro, rd);
    float c = dot(ro, ro) - BOUND * BOUND;
    float disc = b * b - c;
    if (disc < 0.0) return bg;
    float sq = sqrt(disc);
    float t = max(-b - sq, 0.0);
    float tEnd = -b + sq;
    float trap = 0.0;
    float minD = 1e10;
    bool hit = false;
    for (int i = 0; i < uMaxSteps; i++) {
        vec3 p = ro + rd * t;
        float d = de(p, trap);
        minD = min(minD, d);
        if (d < pix * t) { hit = true; break; }
        t += d;
        if (t > tEnd) break;
    }
    if (!hit) return bg + vec3(0.25, 0.45, 1.0) * exp(-minD * 45.0) * 0.60;

    vec3 p = ro + rd * t;
    float h = max(pix * t, 1e-5);
    vec2 e = vec2(1.0, -1.0) * h;
    vec3 n = normalize(e.xyy * de0(p + e.xyy) + e.yyx * de0(p + e.yyx) +
                       e.yxy * de0(p + e.yxy) + e.xxx * de0(p + e.xxx));

    float ao = 0.0;
    float w = 1.0;
    for (int k = 1; k <= 5; k++) {
        float hk = 0.015 + 0.025 * float(k);
        ao += w * (hk - de0(p + n * hk));
        w *= 0.65;
    }
    ao = clamp(1.0 - 3.0 * ao, 0.0, 1.0);

    float sh = 1.0;
    float st = 0.01;
    vec3 sp = p + n * (2.0 * h);
    for (int i = 0; i < uShadowSteps; i++) {
        float d = de0(sp + LIGHT * st);
        sh = min(sh, 10.0 * d / st);
        st += clamp(d, 0.004, 0.08);
        if (sh < 0.001 || st > 2.5) break;
    }
    sh = clamp(sh, 0.0, 1.0);

    float cm = clamp((trap - 0.80) * 3.3, 0.0, 1.0);
    vec3 base = mix(vec3(1.0, 0.58, 0.20), vec3(0.10, 0.55, 0.95), cm);
    float dif = max(dot(n, LIGHT), 0.0) * sh;
    float sky = 0.5 + 0.5 * n.y;
    float rim = pow(clamp(1.0 + dot(rd, n), 0.0, 1.0), 3.0);
    vec3 refl = reflect(rd, n);
    float spe = pow(max(dot(refl, LIGHT), 0.0), 24.0) * sh;
    return base * (1.6 * dif * vec3(1.0, 0.92, 0.80) + 0.35 * sky * ao * vec3(0.35, 0.45, 0.70) + 0.06 * ao)
           + 0.45 * spe * vec3(1.0) + 0.25 * rim * ao * vec3(0.40, 0.60, 1.0);
}

void main() {
    vec2 px = uOrigin + vec2(gl_FragCoord.x, uTileH - gl_FragCoord.y);   // pixel centre, y down
    vec3 fw = normalize(uCamTarget - uCamPos);
    vec3 rt = normalize(cross(fw, vec3(0.0, 1.0, 0.0)));
    vec3 up = cross(rt, fw);
    float aspect = uImage.x / uImage.y;
    float pix = uFov / uImage.y;     // half a pixel's angular size
    vec3 acc = vec3(0.0);
    for (int sy = 0; sy < uSpp; sy++) {
        for (int sx = 0; sx < uSpp; sx++) {
            vec2 o = (vec2(float(sx), float(sy)) + 0.5) / float(uSpp) - 0.5;
            vec2 q = px + o;
            float x = (2.0 * q.x / uImage.x - 1.0) * aspect * uFov;
            float y = (1.0 - 2.0 * q.y / uImage.y) * uFov;
            vec3 rd = normalize(fw + x * rt + y * up);
            acc += shade(uCamPos, rd, pix);
        }
    }
    vec3 col = acc / float(uSpp * uSpp);
    col = 1.0 - exp(-col * 1.25);
    float lum = dot(col, vec3(0.299, 0.587, 0.114));
    col = clamp(lum + (col - lum) * 1.4, 0.0, 1.0);
    col = pow(col, vec3(0.4545));
    vec2 uv = px / uImage - 0.5;
    col *= 1.0 - 0.35 * dot(uv, uv);
    fragColor = vec4(col, 1.0);
}
