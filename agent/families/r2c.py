# r2c: integer packing/unpacking and type-punning intrinsics.
#
# Definitions used (slang-src/source/slang/hlsl.meta.slang unless stated):
#   * pack_u8 / pack_s8 / pack_clamp_u8 / pack_clamp_s8 / unpack_u8u32 / unpack_s8s32 /
#     unpack_u8u16 / unpack_s8s16: lines 35603-35711 (public wrappers) and 35784-36004
#     (implementations). Lane i <-> byte i (bits 8i..8i+7). pack_* keep the low byte of each
#     lane ("dropping unused bits"); pack_clamp_u8 clamps the *signed* lane to [0,255],
#     pack_clamp_s8 to [-128,127]; unpack_u8* zero-extends, unpack_s8* sign-extends.
#     SPIR-V lowers unpack via OpBitcast uint -> u8vec4 / OpUConvert / OpSConvert (35792-35796),
#     Metal/C++ via the shift/mask default branch.
#   * packUnorm4x8 / packSnorm4x8 / packUnorm2x16 / packSnorm2x16 and the matching unpack*:
#     lines 36018-36340; GLSL.std.450 semantics: unpackUnorm = f/(2^n-1),
#     unpackSnorm = clamp(f/(2^(n-1)-1), -1, 1), packUnorm = round(clamp(c,0,1)*(2^n-1)),
#     packSnorm = round(clamp(c,-1,1)*(2^(n-1)-1)). So pack(unpack(x)) == x per lane, except
#     the most-negative snorm code (0x80 / 0x8000) which unpacks to -1.0 and re-packs as
#     0x81 / 0x8001. Half variants only for 4x8 (8-bit codes survive half precision exactly).
#     Metal uses native unpack_unorm4x8_to_float / pack_float_to_unorm4x8 etc.
#   * f16tof32(uint): lines 11459-11485: converts the low 16 bits as an IEEE binary16.
#     Metal: "as_type<half>((ushort)($0))" (11467), SPIR-V: OpUConvert/OpBitcast/OpFConvert.
#     Inputs are sanitised to (x & 0xFFFF9FFF) | 0x4000 -> exponent field 16..23, i.e. normal,
#     finite halves with |v| in [2, 512), every value a multiple of 2^-9. The sum/difference of
#     two is exact in float (<= 20 significant bits), the product too (<= 22 bits), so
#     int((h1 op h2) * 1024.0f) is exactly defined (truncation toward zero of an exact value).
#   * f32tof16(float): lines 11512-11538. Metal: "as_type<ushort>((half)($0))" (11520).
#     Only exactly representable inputs are generated: float(k) for integer k in [-2048, 2047].
#     The Slang result type is uint (zero-extended 16-bit pattern), so uint arithmetic on it
#     wraps mod 2^32 and >> is a logical shift.
#   * bit_cast<T>(U): core.meta.slang:3444, lowered by slang-ir-lower-bit-cast.cpp and
#     slang-ir-extract-value-from-type.cpp; natural little-endian layout: vector element i at
#     byte offset i*sizeof(elem), matrices row-major (row r = elements 2r, 2r+1 of a uint4).
#
# Why (white-box):
#   * The Metal intrinsic strings for f16tof32 / f32tof16 produce an MSL *half* / *ushort*
#     expression although Slang types the result float / uint. When two such calls meet in one
#     binary expression the MSL arithmetic happens in half (precision loss / overflow to inf) or
#     in int after integral promotion (signed compare, arithmetic >> of a negative difference).
#   * bit_cast between differently shaped values (uint2 <-> uint16_t4, uint <-> uint8_t4,
#     uint4 <-> uint64_t2, uint4 -> float2x2) goes through hand-written byte extraction in
#     slang-ir-extract-value-from-type.cpp on Metal/C++ but native OpBitcast on SPIR-V
#     (slang-ir-lower-bit-cast.cpp:345-356).
#     NOT generated: bit_cast *from* a matrix: findLeafValueAtOffset's matrix case
#     (slang-ir-extract-value-from-type.cpp, kIROp_MatrixType: rowIndex = offset /
#     (columnCount * rowStride)) indexes past the row vector, giving invalid SPIR-V on every
#     target (reported separately), so it cannot be calibrated.
#   * pack_clamp_* on int16_t4 is not generated: the C++ target fails to compile it
#     (undeclared I16_min / I16_max), a known class of C++-prelude bug.

NAME = "r2c"
KINDS = ["pk_u8", "pk_pack", "pk_norm", "pk_cast", "pk_h2f", "pk_f2h"]

M32 = 0xFFFFFFFF


# ----------------------------------------------------------------- helpers

def _sx(v, bits):
    v &= (1 << bits) - 1
    return v - (1 << bits) if v >> (bits - 1) else v


def _bytes(x):
    return [(x >> (8 * i)) & 0xFF for i in range(4)]


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def _h2f(x):
    # binary16 -> exact Python float (only normal, finite inputs are generated)
    x &= 0xFFFF
    s, e, m = x >> 15, (x >> 10) & 31, x & 0x3FF
    v = (1024 + m) * (2.0 ** (e - 25))
    return -v if s else v


def _hsan(x):
    return ((x & 0xFFFF9FFF) | 0x4000) & M32


def _f2h_int(k):
    # binary16 bit pattern of integer k, |k| <= 2048 (exact)
    if k == 0:
        return 0
    s = 0x8000 if k < 0 else 0
    a = -k if k < 0 else k
    e = a.bit_length() - 1
    m = (a - (1 << e)) << (10 - e) if e <= 10 else (a - (1 << e)) >> (e - 10)
    return s | ((e + 15) << 10) | m


def _fk(x):
    return int(x & 0xFFF) - 2048


# ----------------------------------------------------------------- tables

UNPACK = ["unpack_u8u32", "unpack_s8s32", "unpack_u8u16", "unpack_s8s16",
          "rt_u8", "rt_s8", "rt_cs8", "rt_cu8"]
PACK_LANES = ["pack_u8", "pack_s8", "pack_clamp_u8", "pack_clamp_s8",
              "pack_u8_16", "pack_s8_16"]
NORM = ["u8", "s8", "u16", "s16", "u8h", "s8h"]
CASTS = ["u16x4", "i16x4", "u8x4", "i8x4", "to32", "u64shr", "u64mul", "u64add", "fmat"]
H2F = ["add", "sub", "mul", "addf"]
F2H = ["subshr", "notshr", "cmp", "xor"]


# ----------------------------------------------------------------- generate

def generate(g, kind, depth):
    r = g.r
    if kind == "pk_u8":
        return g.node(kind, "v", [g.scalar(depth)], r.choice(UNPACK))
    if kind == "pk_pack":
        return g.node(kind, "v", [g.vec(depth) for _ in range(4)],
                      [r.choice(PACK_LANES) for _ in range(4)])
    if kind == "pk_norm":
        return g.node(kind, "v", [g.scalar(depth) for _ in range(4)],
                      [r.choice(NORM) for _ in range(4)])
    if kind == "pk_cast":
        return g.node(kind, "v", [g.vec(depth), g.vec(depth), g.scalar(depth)], r.choice(CASTS))
    if kind == "pk_h2f":
        return g.node(kind, "v", [g.scalar(depth), g.scalar(depth)], r.choice(H2F))
    # pk_f2h
    return g.node(kind, "v", [g.scalar(depth), g.scalar(depth), g.scalar(depth)],
                  (r.choice(F2H), r.randrange(0, 16)))


# ----------------------------------------------------------------- printing

def _unpack_text(op, x):
    if op == "unpack_u8u32":
        return f"unpack_u8u32({x})"
    if op == "unpack_s8s32":
        return f"uint4(unpack_s8s32({x}))"
    if op == "unpack_u8u16":
        return f"uint4(unpack_u8u16({x}))"
    if op == "unpack_s8s16":
        return f"uint4(int4(unpack_s8s16({x})))"
    if op == "rt_u8":
        return f"unpack_u8u32(pack_u8(unpack_u8u32({x}) * 3u))"
    if op == "rt_s8":
        return f"uint4(unpack_s8s32(pack_s8(unpack_s8s32({x}) - 100)))"
    if op == "rt_cs8":
        return f"uint4(unpack_s8s32(pack_clamp_s8(unpack_s8s32({x}) * 2)))"
    return f"unpack_u8u32(pack_clamp_u8(int4(unpack_s8s16({x})) * 3))"


def _pack_text(op, v):
    if op == "pack_u8":
        return f"pack_u8({v})"
    if op == "pack_s8":
        return f"pack_s8(int4({v}))"
    if op == "pack_clamp_u8":
        return f"pack_clamp_u8(int4({v}))"
    if op == "pack_clamp_s8":
        return f"pack_clamp_s8(int4({v}))"
    if op == "pack_u8_16":
        return f"pack_u8(uint16_t4({v}))"
    return f"pack_s8(int16_t4({v}))"


def _norm_text(op, x):
    return {
        "u8": f"packUnorm4x8(unpackUnorm4x8ToFloat({x}))",
        "s8": f"packSnorm4x8(unpackSnorm4x8ToFloat({x}))",
        "u16": f"packUnorm2x16(unpackUnorm2x16ToFloat({x}))",
        "s16": f"packSnorm2x16(unpackSnorm2x16ToFloat({x}))",
        "u8h": f"packUnorm4x8(unpackUnorm4x8ToHalf({x}))",
        "s8h": f"packSnorm4x8(unpackSnorm4x8ToHalf({x}))",
    }[op]


def _fmask(v):
    # normal, finite float bit patterns (exponent field odd, 1..127)
    return f"((({v}) & 0xBFFFFFFFu) | 0x00800000u)"


def _H(x):
    return f"f16tof32((({x}) & 0xFFFF9FFFu) | 0x4000u)"


def _F(x):
    return f"f32tof16(float(int(({x}) & 0xFFFu) - 2048))"


def to_slang(node, p):
    k = node.kind
    if k == "pk_u8":
        return _unpack_text(node.arg, p(node.kids[0]))
    if k == "pk_pack":
        lanes = [_pack_text(op, p(c)) for op, c in zip(node.arg, node.kids)]
        return f"uint4({', '.join(lanes)})"
    if k == "pk_norm":
        lanes = [_norm_text(op, p(c)) for op, c in zip(node.arg, node.kids)]
        return f"uint4({', '.join(lanes)})"
    if k == "pk_cast":
        v, w, x = (p(c) for c in node.kids)
        op = node.arg
        if op == "u16x4":
            return f"uint4(bit_cast<uint16_t4>(({v}).xy))"
        if op == "i16x4":
            return f"uint4(int4(bit_cast<int16_t4>(({v}).zw)))"
        if op == "u8x4":
            return f"uint4(bit_cast<uint8_t4>({x}))"
        if op == "i8x4":
            return f"uint4(int4(bit_cast<int8_t4>({x})))"
        if op == "to32":
            return (f"uint4(bit_cast<uint2>(uint16_t4({v})), bit_cast<uint>(uint8_t4({w})), "
                    f"bit_cast<uint>(int8_t4(int4({w}) - 128)))")
        if op == "u64shr":
            return f"bit_cast<uint4>(bit_cast<uint64_t2>({v}) >> uint64_t(({x}) & 63u))"
        if op == "u64mul":
            return f"bit_cast<uint4>(bit_cast<uint64_t2>({v}) * bit_cast<uint64_t2>({w}))"
        if op == "u64add":
            return (f"uint4(bit_cast<uint2>(bit_cast<uint64_t>(({v}).xy) + "
                    f"bit_cast<uint64_t>(({w}).zw)), bit_cast<uint2>(uint64_t({x}) << 20u))")
        # fmat
        return (f"uint4(asuint(bit_cast<float2x2>({_fmask(v)})[({x}) & 1u]), "
                f"asuint(bit_cast<float2x2>({_fmask(w)})[1]))")
    if k == "pk_h2f":
        x, y = (p(c) for c in node.kids)
        op = node.arg
        if op == "add":
            e = f"({_H(x)} + {_H(y)})"
        elif op == "sub":
            e = f"({_H(x)} - {_H(y)})"
        elif op == "mul":
            e = f"({_H(x)} * {_H(y)})"
        else:  # addf: one operand already float, the other a half-typed call
            e = f"({_H(x)} + float(int(({y}) & 1023u)))"
        return f"uint4(uint(int({e} * 1024.0f)))"
    # pk_f2h
    x, y, z = (p(c) for c in node.kids)
    op, sh = node.arg
    if op == "subshr":
        return f"uint4(({_F(x)} - {_F(y)}) >> {sh}u)"
    if op == "notshr":
        return f"uint4((~{_F(x)}) >> {sh}u, {_F(y)}, {_F(z)}, {sh}u)"
    if op == "cmp":
        return f"uint4(uint(({_F(x)} - {_F(y)}) < {_F(z)}), {_F(x)}, {_F(y)}, {_F(z)})"
    return f"uint4(({_F(x)} ^ {_F(y)}) >> {sh}u)"


# ----------------------------------------------------------------- evaluation

def _unpack_val(op, x):
    b = _bytes(x)
    if op == "unpack_u8u32" or op == "unpack_u8u16":
        return tuple(b)
    if op == "unpack_s8s32" or op == "unpack_s8s16":
        return tuple(_sx(v, 8) & M32 for v in b)
    if op == "rt_u8":
        return tuple((v * 3) & 0xFF for v in b)
    if op == "rt_s8":
        return tuple(_sx(_sx(v, 8) - 100, 8) & M32 for v in b)
    if op == "rt_cs8":
        return tuple(_clamp(_sx(v, 8) * 2, -128, 127) & M32 for v in b)
    return tuple(_clamp(_sx(v, 8) * 3, 0, 255) for v in b)


def _pack_val(op, v):
    if op in ("pack_u8", "pack_s8", "pack_u8_16", "pack_s8_16"):
        lanes = [c & 0xFF for c in v]
    elif op == "pack_clamp_u8":
        lanes = [_clamp(_sx(c, 32), 0, 255) for c in v]
    else:
        lanes = [_clamp(_sx(c, 32), -128, 127) & 0xFF for c in v]
    return lanes[0] | (lanes[1] << 8) | (lanes[2] << 16) | (lanes[3] << 24)


def _norm_val(op, x):
    if op in ("u8", "u16", "u8h"):
        return x
    if op in ("s8", "s8h"):
        b = [0x81 if c == 0x80 else c for c in _bytes(x)]
        return b[0] | (b[1] << 8) | (b[2] << 16) | (b[3] << 24)
    lo, hi = x & 0xFFFF, x >> 16
    lo = 0x8001 if lo == 0x8000 else lo
    hi = 0x8001 if hi == 0x8000 else hi
    return lo | (hi << 16)


def _u64(v):
    return [v[0] | (v[1] << 32), v[2] | (v[3] << 32)]


def _split64(q):
    return [q & M32, (q >> 32) & M32]


def evaluate(node, e, vmap):
    k = node.kind
    if k == "pk_u8":
        return _unpack_val(node.arg, e(node.kids[0]))
    if k == "pk_pack":
        return tuple(_pack_val(op, e(c)) for op, c in zip(node.arg, node.kids))
    if k == "pk_norm":
        return tuple(_norm_val(op, e(c)) for op, c in zip(node.arg, node.kids))
    if k == "pk_cast":
        v, w, x = (e(c) for c in node.kids)
        op = node.arg
        if op == "u16x4":
            return (v[0] & 0xFFFF, v[0] >> 16, v[1] & 0xFFFF, v[1] >> 16)
        if op == "i16x4":
            return tuple(_sx(h, 16) & M32 for h in (v[2] & 0xFFFF, v[2] >> 16,
                                                     v[3] & 0xFFFF, v[3] >> 16))
        if op == "u8x4":
            return tuple(_bytes(x))
        if op == "i8x4":
            return tuple(_sx(c, 8) & M32 for c in _bytes(x))
        if op == "to32":
            h = [c & 0xFFFF for c in v]
            bw = [c & 0xFF for c in w]
            bs = [(c - 128) & 0xFF for c in w]
            return (h[0] | (h[1] << 16), h[2] | (h[3] << 16),
                    bw[0] | (bw[1] << 8) | (bw[2] << 16) | (bw[3] << 24),
                    bs[0] | (bs[1] << 8) | (bs[2] << 16) | (bs[3] << 24))
        if op == "u64shr":
            q = [c >> (x & 63) for c in _u64(v)]
            return tuple(_split64(q[0]) + _split64(q[1]))
        if op == "u64mul":
            a, b = _u64(v), _u64(w)
            q = [(a[0] * b[0]) & 0xFFFFFFFFFFFFFFFF, (a[1] * b[1]) & 0xFFFFFFFFFFFFFFFF]
            return tuple(_split64(q[0]) + _split64(q[1]))
        if op == "u64add":
            q = ((v[0] | (v[1] << 32)) + (w[2] | (w[3] << 32))) & 0xFFFFFFFFFFFFFFFF
            t = (x << 20) & 0xFFFFFFFFFFFFFFFF
            return tuple(_split64(q) + _split64(t))
        # fmat
        fm = lambda c: (c & 0xBFFFFFFF) | 0x00800000
        row = x & 1
        return (fm(v[2 * row]), fm(v[2 * row + 1]), fm(w[2]), fm(w[3]))
    if k == "pk_h2f":
        x, y = (e(c) for c in node.kids)
        hx = _h2f(_hsan(x))
        op = node.arg
        if op == "add":
            val = hx + _h2f(_hsan(y))
        elif op == "sub":
            val = hx - _h2f(_hsan(y))
        elif op == "mul":
            val = hx * _h2f(_hsan(y))
        else:
            val = hx + float(y & 1023)
        r = int(val * 1024.0) & M32   # int() truncates toward zero; all values exact
        return (r, r, r, r)
    # pk_f2h
    x, y, z = (e(c) for c in node.kids)
    op, sh = node.arg
    fx, fy, fz = _f2h_int(_fk(x)), _f2h_int(_fk(y)), _f2h_int(_fk(z))
    if op == "subshr":
        r = ((fx - fy) & M32) >> sh
        return (r, r, r, r)
    if op == "notshr":
        return ((~fx & M32) >> sh, fy, fz, sh)
    if op == "cmp":
        return (1 if ((fx - fy) & M32) < fz else 0, fx, fy, fz)
    r = (fx ^ fy) >> sh
    return (r, r, r, r)
