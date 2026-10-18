"""Minimal reproducers for what the blind agents found, re-verified by a human.

  python3 -m agent.repros            # the Vulkan SDK's slangc
  SLANGC=/path/to/slangc python3 -m agent.repros

Same format as difftest.repros: each case runs on native Metal, KosmicKrisp and
MoltenVK and is compared with an independently computed expected value. Each
case names the agent that first reported it and, after hand triage, the layer
at fault. Compile-only findings (invalid SPIR-V, C++ that does not build) are
in agent/compile_repros.py.
"""

from __future__ import annotations

from difftest import repros as P

M = 0xFFFFFFFF


def ext_u(x, o, n):
    return (x >> o) & ((1 << n) - 1) if n else 0


def ext_s(x, o, n):
    v = ext_u(x, o, n)
    return (v - (1 << n)) & M if n and v >> (n - 1) else v


def ins(base, i, o, n):
    m = ((1 << n) - 1) << o
    return (base & ~m & M) | ((i << o) & m)


A = (0x12345678, 0x9ABCDEF0, 0xFFFFFFFF, 0x80000001)
B = (0x5, 0x2, 0x7, 0x0)

CASES = [
    # ---- Slang, C-like emitter (Metal + C++): bitfield expansion, not fixed by the A/B patch
    {"id": "N-P", "title": "[r1a, r1d] Slang Metal: bitfieldInsert offset operand spliced in without parentheses",
     "expr": "bitfieldInsert(a, b, s & 7u, 3u)", "a": A, "b": B, "s": 5,
     "expect": tuple(ins(x, y, 5, 3) for x, y in zip(A, B))},
    {"id": "N-S", "title": "[r1a, r1d] Slang Metal: signed bitfieldExtract sign-extension shift `32-` + unparenthesized count",
     "expr": "uint4(bitfieldExtract(int4(a), 0u, 28u + (s & 3u)))", "a": A, "b": B, "s": 1,
     "expect": tuple(ext_s(x, 0, 29) for x in A)},
    # ---- Slang, C-like emitter: narrow integer arithmetic is not truncated back to its type
    {"id": "N-W8", "title": "[r1b] Slang Metal: uint8_t + uint8_t is not truncated",
     "expr": "uint4(uint(uint8_t(a.y) + uint8_t(b.y)))", "a": (0, 0xFF, 0, 0), "b": (0, 0xFF, 0, 0), "s": 0,
     "expect": (0xFE,) * 4},
    {"id": "N-W16", "title": "[r1b] Slang Metal: uint16_t * uint16_t is not truncated",
     "expr": "uint4(uint(uint16_t(a.x) * uint16_t(b.x)))", "a": (0x1234, 0, 0, 0), "b": (0x5678, 0, 0, 0), "s": 0,
     "expect": ((0x1234 * 0x5678) & 0xFFFF,) * 4},
    {"id": "N-NEG16", "title": "[r1b] Slang Metal: -uint16_t is not truncated",
     "expr": "uint4(uint(-uint16_t(a.w)))", "a": (0, 0, 0, M), "b": (0, 0, 0, 0), "s": 0, "expect": (1,) * 4},
    # ---- Slang core module: countbits on negative narrow signed ints sign-extends first
    {"id": "N-CB16", "title": "[r1b] Slang SPIR-V: countbits(int16_t) of a negative value counts sign-extended bits",
     "expr": "uint4(countbits(bit_cast<int16_t>(uint16_t(a.x ^ 0xFFFFFF00u))))", "a": (0, 0, 0, 0), "b": B, "s": 0,
     "expect": (8,) * 4},
    {"id": "N-CB8", "title": "[r1b] Slang core module (all targets): countbits(int8_t) of a negative value",
     "expr": "uint4(countbits(bit_cast<int8_t>(uint8_t(a.x))))", "a": (0xFF, 0, 0, 0), "b": B, "s": 0,
     "expect": (8,) * 4},
    # ---- Slang core module: signed-only lowering applied to unsigned / unsupported types
    {"id": "N-ABS", "title": "[r2b] Slang SPIR-V: abs(uint) is emitted as GLSL.std.450 SAbs",
     "expr": "abs(a)", "a": (M, 0x80000000, 5, 0xFFFFFFFE), "b": B, "s": 0,
     "expect": (M, 0x80000000, 5, 0xFFFFFFFE)},
    {"id": "N-SIGN", "title": "[r2b] Slang Metal: sign(int4) lowers to MSL sign(), which takes only float/half",
     "expr": "uint4(sign(int4(a)))", "a": (M, 0, 5, 0x80000000), "b": B, "s": 0, "expect": (M, 0, 1, M)},
    # ---- Slang Metal: f16tof32/f32tof16 expressions keep MSL's half/ushort types
    {"id": "N-H2F", "title": "[r2c] Slang Metal: f16tof32(x) + f16tof32(y) is computed in half precision (known: #13653)",
     "expr": "uint4(uint(f16tof32(a.x) + f16tof32(a.y)))", "a": (0x6801, 0x3C00, 0, 0), "b": B, "s": 0,
     "expect": (2051,) * 4},
    {"id": "N-F2H", "title": "[r2c] Slang Metal: f32tof16 result is a ushort, promoted to int before >>",
     "expr": "uint4(~f32tof16(asfloat(a.x)) >> 30u)", "a": (0x3F800000, 0, 0, 0), "b": B, "s": 0,
     "expect": (3,) * 4},
    # ---- Slang IR passes (wrong on every target, so the tester itself cannot catch them)
    {"id": "N-LOGIC", "title": "[r2d] Slang peephole: uint4 `a && uint4(1u)` folded to `a` instead of 0/1 lanes",
     "expr": "a && uint4(1u)", "a": (0, 5, M, 1), "b": B, "s": 0, "expect": (0, 1, 1, 1)},
    {"id": "N-U64", "title": "[r2a, r2d] Slang SCCP: uint64 constants compared as signed (2^63 > 1 folds to false)",
     "expr": "uint4(uint(0x8000000000000000ull > 1ull)) + a", "a": (0, 0, 0, 0), "b": B, "s": 0,
     "expect": (1,) * 4},
    # ---- Slang Metal emitter: a splatted functional cast of a temp parses as a C-style cast
    {"id": "N-MCAST", "title": "[r2a] Slang Metal: `(uint4(uint(_S1)) ) & ...` is parsed by Metal as a cast",
     "expr": "a >> (uint4(uint(uint64_t(b.y) % (uint64_t(b.x) | 1ull))) & 31u)", "a": (M,) * 4,
     "b": (2, 7, 0, 0), "s": 0, "expect": (M >> 1,) * 4},
    # ---- Downstream of Slang
    {"id": "N-APPLE", "title": "[r1a, r1d] Apple Metal compiler: extract_bits(int, off, 0) with constant count drops the store",
     "expr": "uint4(bitfieldExtract(int4(b), 1u, 0u))", "a": A, "b": (0x18, 0x39, M, 0x7), "s": 0,
     "expect": (0, 0, 0, 0)},
    {"id": "N-KK", "title": "[r1a] KosmicKrisp: bitfieldExtract(x, 32u, 0u) with constant operands",
     "expr": "bitfieldExtract(a, 32u, 0u)", "a": A, "b": B, "s": 0, "expect": (0, 0, 0, 0)},
    {"id": "N-KK64", "title": "[r2a] KosmicKrisp: int64 bit_cast of a u64*u64 product loses bit 63",
     "expr": "uint4(uint(bit_cast<int64_t>(uint64_t(a.x) * uint64_t(b.x)) >> 63u))",
     "a": (M, 0, 0, 0), "b": (M, 0, 0, 0), "s": 0, "expect": (M,) * 4},
    {"id": "N-KKS16", "title": "[r2c] KosmicKrisp: bit_cast<int16_t2> lane 1 is not sign-extended",
     "expr": "uint4(int4(int2(bit_cast<int16_t2>(pack_u8(uint4(a.x&255u, (a.x>>8)&255u, (a.x>>16)&255u, a.x>>24) * 3u))), 0, 0))",
     "a": (M, 0, 0, 0), "b": B, "s": 0, "expect": (0xFFFFFDFD, 0xFFFFFDFD, 0, 0)},
    {"id": "C-SHL", "title": "[r2d] KosmicKrisp: (0x10000u - b) << 15u sets bit 31 (likely the same root cause as bug C)",
     "expr": "(0x10000u - b) << 15u", "a": A, "b": (0x20, 0x80000000, 1, 0xFFFF), "s": 0,
     "expect": tuple(((0x10000 - x) << 15) & M for x in (0x20, 0x80000000, 1, 0xFFFF))},
]


def main():
    P.CASES = CASES
    P.main()


if __name__ == "__main__":
    main()
