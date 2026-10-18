# r1a: bitfieldExtract / bitfieldInsert (unsigned and signed, vector and scalar).
#
# Definitions used:
#   slang-src/source/slang/core.meta.slang:3476-3491
#     bitfieldInsert(T base, T insert, uint offset, uint bits)
#     bitfieldExtract(T value, uint offset, uint bits): extracts bits
#     [offset, offset + bits - 1] into the low bits of the result.
#   Lowered on SPIR-V to OpBitFieldUExtract / OpBitFieldSExtract / OpBitFieldInsert
#   (slang-src/source/slang/slang-emit-spirv.cpp:9823-9878). The SPIR-V spec makes
#   the result undefined only if Offset, Count, or Offset+Count is greater than the
#   bit width; Count == 0 gives 0 (extract) / Base (insert). SExtract sign-extends
#   from bit Count-1. GLSL bitfieldExtract/bitfieldInsert define the same.
#   So every generated case keeps offset + bits <= 32 (checked by construction),
#   including the edges bits == 32 (offset 0) and bits == 0 (offset up to 32).
#
# Why (white-box): Metal and C++ do not get a native instruction; they get the
# hand-written string expansion in
#   slang-src/source/slang/slang-emit-c-like.cpp:4301-4410 (emitBitfieldExtractImpl)
#   slang-src/source/slang/slang-emit-c-like.cpp:4420-4541 (emitBitfieldInsertImpl)
#   * the mask is ((1u<<bits)-1): for bits == 32 that is 1u<<32 (undefined in MSL);
#   * operands are spliced with emitOperand(..., EmitOp::General), i.e. *no*
#     parentheses, into contexts like "1u<<" + bits, "(32-" + bits + ")",
#     "val>>" and "<<" + off. A folded operand whose top-level operator binds
#     looser than the surrounding one (+ - << >> & | ^) re-associates.
# So offsets/bit counts are generated as single-use expressions whose top-level
# operator is one of those looser-binding operators.

NAME = "r1a"
KINDS = ["bfe_u", "bfe_s", "bfi_u", "bfi_s"]

M32 = 0xFFFFFFFF
MODES = ["span", "span", "tmpl", "tmpl", "tmpl", "hi", "rand", "rev", "const"]
# constant (offset, bits); (32, 0) omitted, and for the signed extract every
# constant bits == 0 is omitted: both SPIR-V drivers mis-handle a constant
# OpBitFieldSExtract with Count 0 (reported separately), which would turn
# Metal-side findings into "all paths wrong".
CONSTS = [(0, 32), (0, 0), (0, 31), (1, 31), (31, 1), (16, 16), (0, 1), (8, 24), (24, 8), (5, 0)]

# Templates for a uint in [0, 16]; each has a looser-binding top-level operator.
# (text with {x}/{y}, python fn)
TMPL = [
    ("(({x}) & 7u) | (({y}) & 8u)", lambda x, y: (x & 7) | (y & 8)),
    ("(({x}) & 15u) ^ (({y}) & 1u)", lambda x, y: (x & 15) ^ (y & 1)),
    ("(({x}) & 12u) + (({y}) & 3u)", lambda x, y: (x & 12) + (y & 3)),
    ("(({x}) & 31u) - (({x}) & 15u)", lambda x, y: (x & 31) - (x & 15)),
    ("(({x}) & 3u) << 2u", lambda x, y: (x & 3) << 2),
    ("(({x}) & 255u) >> 4u", lambda x, y: (x & 255) >> 4),
    ("(({x}) & 15u) & ({y})", lambda x, y: (x & 15) & y),
    ("16u - (({x}) & 15u)", lambda x, y: 16 - (x & 15)),
    ("({x}) % 17u", lambda x, y: x % 17),
    ("(({x}) >> 28u) + (({y}) & 1u)", lambda x, y: (x >> 28) + (y & 1)),
]


def _leaf(g):
    r = g.r
    if r.random() < 0.3:
        return g.node("var", "u", [], "s")
    return g.node("comp", "u", [g.node("var", "v", [], r.choice("ab"))], r.choice("xyzw"))


def _scalar(g, depth, allow_deep=True):
    # Mostly simple scalars. A swizzle of an arbitrary vector sub-expression is
    # kept rare: swizzling an unsigned bitfieldInsert result makes slangc emit MSL
    # that Metal rejects (see report), which would hide value findings.
    # Offsets/bit counts never use g.scalar and never combine a leaf with itself,
    # so slangc cannot fold them to a constant (a *constant* bit count of 0 in
    # a signed extract breaks both SPIR-V drivers; reported separately).
    r = g.r
    u = r.random()
    if allow_deep and u < 0.12:
        return g.scalar(depth)
    if u < 0.45:
        x = _leaf(g)
        y = _leaf(g)
        while _same(x, y):
            y = _leaf(g)
        return g.node("bin", "u", [x, y], r.choice(["+", "-", "*", "&", "|", "^"]))
    return _leaf(g)


def _same(x, y):
    if x.kind != y.kind or x.arg != y.arg:
        return False
    if x.kind == "comp":
        return x.kids[0].arg == y.kids[0].arg
    return True


def generate(g, kind, depth):
    r = g.r
    mode = r.choice(MODES)
    scalar = r.random() < 0.25
    const = r.choice(CONSTS)
    if kind == "bfe_s":
        const = r.choice([c for c in CONSTS if c[1] != 0])
    t1 = r.randrange(len(TMPL))
    t2 = r.randrange(len(TMPL))
    if kind.startswith("bfe"):
        kids = [_scalar(g, depth)] if scalar else [g.vec(depth)]
    else:
        if scalar:
            kids = [_scalar(g, depth), _scalar(g, depth)]
        else:
            kids = [g.vec(depth), g.vec(depth)]
    kids = kids + [_scalar(g, depth, False), _scalar(g, depth, False), _scalar(g, depth, False)]
    return g.node(kind, "v", kids, (mode, scalar, const, t1, t2))


def _offbits_text(node, p):
    mode, scalar, const, t1, t2 = node.arg
    x = p(node.kids[-3])
    y = p(node.kids[-2])
    z = p(node.kids[-1])
    a = TMPL[t1][0].format(x=x, y=y)
    b = TMPL[t2][0].format(x=y, y=z)
    if mode == "span":      # off in [0,16], bits = 32 - off in [16,32]
        return f"({a})", f"(32u - ({a}))"
    if mode == "tmpl":      # off, bits in [0,16]
        return f"({a})", f"({b})"
    if mode == "hi":        # off = 32 - A in [16,32], bits = A & B <= A
        return f"(32u - ({a}))", f"(({a}) & ({b}))"
    if mode == "rand":
        return f"({x} % 33u)", f"({y} % (33u - ({x} % 33u)))"
    if mode == "rev":
        return f"({x} % (33u - ({y} % 33u)))", f"({y} % 33u)"
    return f"{const[0]}u", f"{const[1]}u"


def _offbits_val(node, e):
    mode, scalar, const, t1, t2 = node.arg
    x = e(node.kids[-3])
    y = e(node.kids[-2])
    z = e(node.kids[-1])
    a = TMPL[t1][1](x, y)
    b = TMPL[t2][1](y, z)
    if mode == "span":
        return a, 32 - a
    if mode == "tmpl":
        return a, b
    if mode == "hi":
        return 32 - a, a & b
    if mode == "rand":
        return x % 33, y % (33 - x % 33)
    if mode == "rev":
        return x % (33 - y % 33), y % 33
    return const


def to_slang(node, p):
    scalar = node.arg[1]
    off, bits = _offbits_text(node, p)
    k = node.kind
    v0 = p(node.kids[0])
    if scalar:
        if k == "bfe_u":
            return f"uint4(bitfieldExtract({v0}, {off}, {bits}))"
        if k == "bfe_s":
            return f"uint4(uint(bitfieldExtract(int({v0}), {off}, {bits})))"
        v1 = p(node.kids[1])
        if k == "bfi_u":
            return f"uint4(bitfieldInsert({v0}, {v1}, {off}, {bits}))"
        return f"uint4(uint(bitfieldInsert(int({v0}), int({v1}), {off}, {bits})))"
    if k == "bfe_u":
        return f"bitfieldExtract({v0}, {off}, {bits})"
    if k == "bfe_s":
        return f"uint4(bitfieldExtract(int4({v0}), {off}, {bits}))"
    v1 = p(node.kids[1])
    if k == "bfi_u":
        return f"bitfieldInsert({v0}, {v1}, {off}, {bits})"
    return f"uint4(bitfieldInsert(int4({v0}), int4({v1}), {off}, {bits}))"


def _ubfe(x, o, c):
    return (x >> o) & ((1 << c) - 1)


def _sbfe(x, o, c):
    if c == 0:
        return 0
    f = (x >> o) & ((1 << c) - 1)
    if (f >> (c - 1)) & 1:
        f -= 1 << c
    return f & M32


def _bfi(base, ins, o, c):
    mask = (((1 << c) - 1) << o) & M32
    return (base & (~mask & M32)) | ((ins << o) & mask)


def evaluate(node, e, vmap):
    scalar = node.arg[1]
    o, c = _offbits_val(node, e)
    if not (0 <= o and 0 <= c and o + c <= 32):
        raise ValueError(f"offset/bits out of the defined range: {o}, {c}")
    k = node.kind
    if k.startswith("bfe"):
        fn = _ubfe if k == "bfe_u" else _sbfe
        v = vmap(lambda x: fn(x, o, c), e(node.kids[0]))
    else:
        v = vmap(lambda x, y: _bfi(x, y, o, c), e(node.kids[0]), e(node.kids[1]))
    if scalar:
        return (v,) * 4
    return v
