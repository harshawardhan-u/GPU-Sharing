#version 300 es
// SHA-256 brute-force for the "How strong is your password?" demo.
// Each pixel tests one candidate password: it derives the candidate from the
// pixel's global index, computes SHA-256 on the GPU, and compares with the
// target hash. Passwords are short (<= 12 chars) so the message is always a
// single 512-bit block -- which keeps this shader simple.
//
// It only ever searches a keyspace the demo itself generated (a random short
// password). There is no input for an outside hash. The point it makes is
// defensive: how fast a short password falls, and how adding characters makes
// cracking hopeless.
precision highp float;
precision highp int;

uniform uint uTarget[8];       // target SHA-256, 8 big-endian words
uniform uint uCharset[95];     // allowed character codes
uniform uint uCharLen;         // number of allowed characters (base)
uniform uint uPassLen;         // password length
uniform uint uBaseLo;          // first global index in this work unit
uniform uint uWidth;           // pixels per row
uniform uint uHeight;          // pixel rows
uniform uint uReps;            // candidates each pixel tests (strided), to cut pixel read-backs
uniform uint uTotal;           // size of the whole keyspace (skip indices past it)
uniform int  uMode;            // 0 = search, 1 = dump hash of candidate uBaseLo
out highp uvec4 outColor;      // RGBA8UI

const uint K[64] = uint[64](
  0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
  0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
  0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
  0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
  0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
  0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
  0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
  0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u);

uint rotr(uint x, uint n) { return (x >> n) | (x << (32u - n)); }

// byte at message position p (0..63) for the candidate at global index idx
uint msgByte(uint p, uint idx) {
  if (p < uPassLen) {
    uint div = 1u;
    for (uint i = 0u; i < p; i++) div *= uCharLen;
    return uCharset[(idx / div) % uCharLen];
  }
  if (p == uPassLen) return 0x80u;
  if (p == 63u) return (uPassLen * 8u) & 0xffu;
  if (p == 62u) return (uPassLen * 8u >> 8u) & 0xffu;
  return 0u;
}

void sha256(uint idx, out uint h[8]) {
  uint w[64];
  for (uint t = 0u; t < 16u; t++) {
    uint b0 = msgByte(t * 4u, idx), b1 = msgByte(t * 4u + 1u, idx);
    uint b2 = msgByte(t * 4u + 2u, idx), b3 = msgByte(t * 4u + 3u, idx);
    w[t] = (b0 << 24) | (b1 << 16) | (b2 << 8) | b3;
  }
  for (uint t = 16u; t < 64u; t++) {
    uint s0 = rotr(w[t-15u],7u) ^ rotr(w[t-15u],18u) ^ (w[t-15u] >> 3u);
    uint s1 = rotr(w[t-2u],17u) ^ rotr(w[t-2u],19u) ^ (w[t-2u] >> 10u);
    w[t] = w[t-16u] + s0 + w[t-7u] + s1;
  }
  uint a=0x6a09e667u,b=0xbb67ae85u,c=0x3c6ef372u,d=0xa54ff53au;
  uint e=0x510e527fu,f=0x9b05688cu,g=0x1f83d9abu,hh=0x5be0cd19u;
  for (uint t = 0u; t < 64u; t++) {
    uint S1 = rotr(e,6u) ^ rotr(e,11u) ^ rotr(e,25u);
    uint ch = (e & f) ^ ((~e) & g);
    uint t1 = hh + S1 + ch + K[t] + w[t];
    uint S0 = rotr(a,2u) ^ rotr(a,13u) ^ rotr(a,22u);
    uint maj = (a & b) ^ (a & c) ^ (b & c);
    uint t2 = S0 + maj;
    hh=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
  }
  h[0]=a+0x6a09e667u; h[1]=b+0xbb67ae85u; h[2]=c+0x3c6ef372u; h[3]=d+0xa54ff53au;
  h[4]=e+0x510e527fu; h[5]=f+0x9b05688cu; h[6]=g+0x1f83d9abu; h[7]=hh+0x5be0cd19u;
}

void main() {
  uint x = uint(gl_FragCoord.x);
  uint y = uint(gl_FragCoord.y);
  uint h[8];
  if (uMode == 1) {                         // dump: output word x of the hash of candidate uBaseLo
    sha256(uBaseLo, h);
    uint v = h[x & 7u];
    outColor = uvec4((v >> 24) & 0xffu, (v >> 16) & 0xffu, (v >> 8) & 0xffu, v & 0xffu);
    return;
  }
  uint pixels = uWidth * uHeight;
  uint local = y * uWidth + x;
  uint found = 0u;                          // 0 = nothing; otherwise (offset within unit)+1
  for (uint r = 0u; r < uReps; r++) {
    uint off = r * pixels + local;
    uint idx = uBaseLo + off;
    if (idx >= uTotal) break;
    sha256(idx, h);
    if (h[0]==uTarget[0] && h[1]==uTarget[1] && h[2]==uTarget[2] && h[3]==uTarget[3]
     && h[4]==uTarget[4] && h[5]==uTarget[5] && h[6]==uTarget[6] && h[7]==uTarget[7]) {
      found = off + 1u; break;
    }
  }
  outColor = uvec4((found >> 24) & 0xffu, (found >> 16) & 0xffu, (found >> 8) & 0xffu, found & 0xffu);
}
