"""
pool_kernels.py -- the gravity physics used by the multi-GPU pool.

One OpenCL kernel runs on any GPU vendor (NVIDIA, Intel, AMD), which is what
lets an RTX card and an integrated Intel GPU share the same simulation.
Positions travel as 16-bit grid coordinates (4 bytes per particle) and are
turned back into floats identically on every device and on the client, so
the client can recompute any particle exactly as a worker should have.
"""

import time

import numpy as np

WORLD = 900.0          # simulation area is WORLD x WORLD pixels
QSCALE = 65535.0 / WORLD
G = 800.0
SOFTENING = 4.0
GROUP_KERNEL_BELOW = 4000   # slices smaller than this use one work-group per particle

KERNEL_SRC = """
#define LWS 64

// One thread per particle: best for large slices.
__kernel void accel_thread(__global const ushort2 *qpos, const int n, const int start,
                           const int stride, const float inv_qscale, const float G,
                           const float soft2, __global float2 *acc_out) {
    int k = get_global_id(0);
    float2 xi = convert_float2(qpos[start + k]) * inv_qscale;
    float2 a = (float2)(0.0f, 0.0f);
    for (int j = 0; j < n; j += stride) {
        float2 d = convert_float2(qpos[j]) * inv_qscale - xi;
        float r2 = d.x * d.x + d.y * d.y + soft2;
        float inv = rsqrt(r2);
        a += d * (inv * inv * inv);
    }
    acc_out[k] = G * (float)stride * a;
}

// One work-group of LWS threads per particle: small slices finish quickly
// because the loop over all n particles is split across the group.
__kernel void accel_group(__global const ushort2 *qpos, const int n, const int start,
                          const int stride, const float inv_qscale, const float G,
                          const float soft2, __global float2 *acc_out) {
    __local float2 part[LWS];
    int k = get_group_id(0);
    int t = get_local_id(0);
    float2 xi = convert_float2(qpos[start + k]) * inv_qscale;
    float2 a = (float2)(0.0f, 0.0f);
    for (int j = t * stride; j < n; j += LWS * stride) {
        float2 d = convert_float2(qpos[j]) * inv_qscale - xi;
        float r2 = d.x * d.x + d.y * d.y + soft2;
        float inv = rsqrt(r2);
        a += d * (inv * inv * inv);
    }
    part[t] = a;
    barrier(CLK_LOCAL_MEM_FENCE);
    for (int off = LWS / 2; off > 0; off >>= 1) {
        if (t < off) part[t] += part[t + off];
        barrier(CLK_LOCAL_MEM_FENCE);
    }
    if (t == 0) acc_out[k] = G * (float)stride * part[0];
}
"""


def quantize(pos):
    q = np.clip(np.rint(pos * QSCALE), 0, 65535)
    return q.astype(np.uint16)


def dequantize(q):
    return q.astype(np.float32) * np.float32(1.0 / QSCALE)


def reference_accel(pos, idx):
    """Acceleration of the particles in idx computed on the CPU from the same
    dequantized positions a worker sees, plus the sum of the magnitudes of the
    individual terms, which sets the scale of the verification tolerance.
    NumPy's pairwise summation keeps float32 accurate enough here."""
    d = pos[None, :, :] - pos[idx][:, None, :]
    r2 = d[..., 0] * d[..., 0] + d[..., 1] * d[..., 1] + np.float32(SOFTENING * SOFTENING)
    w = 1.0 / (r2 * np.sqrt(r2))
    acc = G * (d * w[..., None]).sum(1)
    scale = G * (np.sqrt(r2) * w).sum(1)
    return acc, scale


def numpy_accel(qpos, start, end, chunk=256):
    """CPU fallback with the same maths as the kernel."""
    pos = dequantize(qpos)
    out = np.empty((end - start, 2), np.float32)
    for s in range(start, end, chunk):
        e = min(s + chunk, end)
        d = pos[None, :, :] - pos[s:e, None, :]
        r2 = (d * d).sum(-1) + SOFTENING * SOFTENING
        out[s - start:e - start] = G * (d * (r2 ** -1.5)[..., None]).sum(1)
    return out


def list_gpus():
    try:
        import pyopencl as cl
    except ImportError:
        return []
    found = []
    for plat in cl.get_platforms():
        for dev in plat.get_devices(device_type=cl.device_type.GPU):
            found.append(dev)
    return found


def short_name(dev):
    name = dev.name.strip()
    for long, short in (("NVIDIA GeForce ", ""), ("Laptop GPU", "Laptop"), ("(R)", ""), ("(TM)", ""),
                        ("Graphics Controller", "Graphics"), ("Mobile Graphics", "Graphics")):
        name = name.replace(long, short)
    return " ".join(name.split())


class DeviceCompute:
    """One GPU (or the CPU fallback) that can compute a slice of accelerations."""

    def __init__(self, dev=None):
        self.dev = dev
        self.cheat = False
        if dev is None:
            self.name, self.vendor, self.cu = "CPU (NumPy)", "CPU", 0
            return
        import pyopencl as cl
        self.cl = cl
        self.name, self.vendor, self.cu = short_name(dev), dev.vendor.strip(), dev.max_compute_units
        self.ctx = cl.Context([dev])
        self.queue = cl.CommandQueue(self.ctx)
        self.prg = cl.Program(self.ctx, KERNEL_SRC).build()
        self.k_thread = cl.Kernel(self.prg, "accel_thread")
        self.k_group = cl.Kernel(self.prg, "accel_group")
        self.cap_n = 0
        self.cap_out = 0

    def _ensure(self, n, m):
        cl = self.cl
        if n > self.cap_n:
            self.cap_n = max(n, 1024) * 2
            self.qbuf = cl.Buffer(self.ctx, cl.mem_flags.READ_ONLY, self.cap_n * 4)
        if m > self.cap_out:
            self.cap_out = max(m, 1024) * 2
            self.obuf = cl.Buffer(self.ctx, cl.mem_flags.WRITE_ONLY, self.cap_out * 8)

    def compute(self, qpos, start, end):
        """Returns (accelerations for [start, end), milliseconds of compute)."""
        n, m = len(qpos), end - start
        t0 = time.perf_counter()
        if m <= 0:
            return np.zeros((0, 2), np.float32), 0.0
        if self.dev is None:
            out = numpy_accel(qpos, start, end)
            return out, (time.perf_counter() - t0) * 1000
        cl = self.cl
        self._ensure(n, m)
        stride = 2 if self.cheat else 1
        cl.enqueue_copy(self.queue, self.qbuf, np.ascontiguousarray(qpos))
        args = (self.qbuf, np.int32(n), np.int32(start), np.int32(stride),
                np.float32(1.0 / QSCALE), np.float32(G), np.float32(SOFTENING * SOFTENING), self.obuf)
        if m < GROUP_KERNEL_BELOW:
            self.k_group(self.queue, (m * 64,), (64,), *args)
        else:
            self.k_thread(self.queue, (m,), None, *args)
        out = np.empty((m, 2), np.float32)
        cl.enqueue_copy(self.queue, out, self.obuf)
        self.queue.finish()
        return out, (time.perf_counter() - t0) * 1000
