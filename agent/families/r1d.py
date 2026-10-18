# r1d: bitfieldExtract / bitfieldInsert on 32-bit uint4 and int4, with dynamic and
# edge-case offset/count operands (count == 32, offset == 32, count == 0).
#
# Definitions used (Slang core module + the SPIR-V ops it maps to):
#   slang-src/source/slang/core.meta.slang:3476-3491
#       bitfieldInsert(base, insert, offset, bits): bits [offset, offset+bits-1] come from
#       bits [0, bits-1] of insert, all other bits from base.
#       bitfieldExtract(value, offset, bits): bits [offset, offset+bits-1] of value moved to
#       the low bits (sign-extended for signed T, per OpBitFieldSExtract).
#   slang-src/source/slang/slang-emit-spirv.cpp:9823-9876 lowers them to OpBitFieldUExtract /
#       OpBitFieldSExtract / OpBitFieldInsert. SPIR-V: result undefined only if Offset, Count or
#       Offset+Count exceeds 32; Count == 0 gives 0 (extract) / Base (insert).
#   We therefore always generate offset + count <= 32 (offset in [0, 31], count in [0, 32]).
#   Offset == 32 (with count 0) is formally defined by SPIR-V, but every GPU path (Metal's
#   insert_bits/extract_bits and shift lowering) treats it as poison, so it is excluded:
#   it cannot point at a specific layer.
#
# Why: slang-src/source/slang/slang-emit-c-like.cpp:4301-4520 (Metal and C++ targets) emulate
#   these with `(1u << bits) - 1u` (C-UB for bits == 32) and splice the offset/bits operands
#   into `<<`, `-` and `&` expressions with EmitOp::General (no parentheses), e.g.
#   `(32 - bts)` and `<< off`, so compound operands may be mis-parenthesised.

NAME = "r1d"
KINDS = ["bfe_u", "bfe_s", "bfi_u", "bfi_s"]

M32 = 0xFFFFFFFF
OFF_LITS = [0, 0, 0, 1, 4, 8, 16, 31]


def _gen_operands(g, depth):
    """Returns (arg, [x_scalar, y_scalar]) describing offset and count forms."""
    r = g.r
    of = r.choice(["lit", "lit", "and31", "min31", "mod29"])
    olit = r.choice(OFF_LITS) if of == "lit" else 0
    cf = r.choice(["rest", "rest", "min", "mod", "and15", "lit"])
    clit = 0
    if cf == "lit":
        # a literal count is only safe if offset is a literal too
        if of == "lit":
            clit = r.choice([0, 1, 32 - olit, 32 - olit, max(0, 31 - olit)])
        else:
            cf = "rest"
    return (of, olit, cf, clit), [g.scalar(depth), g.scalar(depth)]


def generate(g, kind, depth):
    arg, xy = _gen_operands(g, depth)
    if kind in ("bfe_u", "bfe_s"):
        return g.node(kind, "v", [g.vec(depth)] + xy, arg)
    return g.node(kind, "v", [g.vec(depth), g.vec(depth)] + xy, arg)


def _off_text(arg, x):
    of, olit = arg[0], arg[1]
    if of == "lit":
        return f"{olit}u"
    if of == "and31":
        return f"({x} & 31u)"
    if of == "min31":
        return f"min({x}, 31u)"
    return f"({x} % 29u)"


def _cnt_text(arg, o, y):
    cf, clit = arg[2], arg[3]
    if cf == "lit":
        return f"{clit}u"
    if cf == "rest":
        return f"(32u - {o})"
    if cf == "min":
        return f"min({y}, 32u - {o})"
    if cf == "mod":
        return f"({y} % (33u - {o}))"
    return f"min({y} & 15u, 32u - {o})"


def _off_val(arg, x):
    of, olit = arg[0], arg[1]
    if of == "lit":
        return olit
    if of == "and31":
        return x & 31
    if of == "min31":
        return min(x, 31)
    return x % 29


def _cnt_val(arg, o, y):
    cf, clit = arg[2], arg[3]
    if cf == "lit":
        return clit
    if cf == "rest":
        return 32 - o
    if cf == "min":
        return min(y, 32 - o)
    if cf == "mod":
        return y % (33 - o)
    return min(y & 15, 32 - o)


def to_slang(node, p):
    k = node.kind
    n = len(node.kids)
    x, y = p(node.kids[n - 2]), p(node.kids[n - 1])
    o = _off_text(node.arg, x)
    c = _cnt_text(node.arg, o, y)
    if k == "bfe_u":
        return f"bitfieldExtract({p(node.kids[0])}, {o}, {c})"
    if k == "bfe_s":
        return f"uint4(bitfieldExtract(int4({p(node.kids[0])}), {o}, {c}))"
    if k == "bfi_u":
        return f"bitfieldInsert({p(node.kids[0])}, {p(node.kids[1])}, {o}, {c})"
    return f"uint4(bitfieldInsert(int4({p(node.kids[0])}), int4({p(node.kids[1])}), {o}, {c}))"


def _uext(v, o, c):
    return (v >> o) & ((1 << c) - 1)


def _sext(v, o, c):
    if c == 0:
        return 0
    x = _uext(v, o, c)
    if (x >> (c - 1)) & 1:
        x -= 1 << c
    return x & M32


def _ins(base, ins, o, c):
    mask = (((1 << c) - 1) << o) & M32
    return ((base & ~mask) | ((ins << o) & mask)) & M32


def evaluate(node, e, vmap):
    k = node.kind
    n = len(node.kids)
    xv, yv = e(node.kids[n - 2]), e(node.kids[n - 1])
    o = _off_val(node.arg, xv)
    c = _cnt_val(node.arg, o, yv)
    if not (0 <= o <= 31 and 0 <= c <= 32 and o + c <= 32):
        raise ValueError("offset/count out of range")
    if k == "bfe_u":
        return vmap(lambda v: _uext(v, o, c), e(node.kids[0]))
    if k == "bfe_s":
        return vmap(lambda v: _sext(v, o, c), e(node.kids[0]))
    return vmap(lambda b_, i_: _ins(b_, i_, o, c), e(node.kids[0]), e(node.kids[1]))
