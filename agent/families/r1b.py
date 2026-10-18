# r1b: non-32-bit integer arithmetic and bit intrinsics (8/16/64-bit, signed and unsigned).
#
# Motivation / definitions used (slang-src/source/slang/hlsl.meta.slang):
#   * countbits        lines 10100-10170 (scalar), 10172-10215 (vector): number of set bits of
#                      `value` in its own width. Metal: "($TR)popcount($0)" (10135); SPIR-V
#                      emulates 16-bit via __intCast<uint32_t>(value) (10146-10150), which
#                      sign-extends int16 -> suspicious for negative int16.
#                      NOTE: 8-bit is emulated for ALL targets via __intCast<uint32_t> before the
#                      target switch (10106-10110), so countbits(int8 < 0) is wrong on every path
#                      (counts 32-bit sign extension). Not generated (all paths would agree).
#   * firstbithigh     lines 11720-11800: unsigned -> index of highest 1 bit; signed negative ->
#                      index of highest 0 bit; 0 (or -1 if signed) -> 0xFFFFFFFF. Metal
#                      hand-written via clz (11741-11748); SPIR-V 64-bit emulated (11775-11793).
#   * firstbitlow      lines 11842-11888: index of lowest 1 bit, 0xFFFFFFFF for 0. Metal via ctz
#                      (11854); SPIR-V 64-bit emulated via halves (11868-11884). Only generated
#                      for 64-bit: the 8/16-bit SPIR-V branch (11862-11867) passes the narrow
#                      `value` (not value32) to FindILsb and fails spirv-val.
#   * reversebits      lines 15486-15520: bit n of result = bit (width-1-n) of value. Metal
#                      "reverse_bits"; SPIR-V 8/16-bit emulated via 32-bit reverse + shift, 64-bit
#                      via swapped halves.
#   * Narrow arithmetic: Slang types uint8_t/uint16_t + - * ~ unary- etc. as the narrow type
#     (wrapping mod 2^w; the SPIR-V uses OpIAdd %ushort etc.). The Metal emitter
#     (slang-emit-metal.cpp / C-like emitter) prints scalar narrow ops inline, e.g.
#     `popcount(ushort(x) - ushort(y))`, where C++ integral promotion makes the operation `int`.
#     slang-emit-c-like.cpp:2638-2700 prints Add/Sub/Mul/BitNot/Neg infix with no cast back to
#     the narrow type, and MetalSourceEmitter::tryEmitInstExprImpl (slang-emit-metal.cpp:744ff)
#     has no override for narrow integer ops; Slang itself documents that `uint8_t x; x << n`
#     stays uint8_t (slang-ir-operator-shift-overflow.cpp:66-69).
#   * Conversions: uint(uintW) zero-extends, uint(intW) sign-extends (two's complement),
#     narrowing unsigned conversions truncate; signed narrow values are made with bit_cast
#     (core.meta.slang:3444), never by out-of-range conversion or signed overflow.

NAME = "r1b"
KINDS = ["nb_arith", "nb_sext", "nb_svec", "nb_wide"]

M32 = 0xFFFFFFFF
M64 = 0xFFFFFFFFFFFFFFFF
UT = {8: "uint8_t", 16: "uint16_t"}
ST = {8: "int8_t", 16: "int16_t"}
CMP = ["<", "<=", ">", ">=", "==", "!="]
NBIN = ["+", "-", "*", "&", "|", "^"]


# ------------------------------------------------------------------ helpers

def _pop(x):
    return bin(x).count("1")


def _fbh_u(x):
    return M32 if x == 0 else x.bit_length() - 1


def _fbh_s(x, w):
    # x is the w-bit pattern of a signed value
    if x >> (w - 1):
        x = (~x) & ((1 << w) - 1)
    return _fbh_u(x)


def _fbl(x):
    return M32 if x == 0 else (x & -x).bit_length() - 1


def _rev(x, w):
    return int(format(x, "0%db" % w)[::-1], 2)


def _sext32(x, w):
    if x >> (w - 1):
        x = x - (1 << w)
    return x & M32


def _signed(x, w):
    return x - (1 << w) if x >> (w - 1) else x


def _cmp(op, x, y):
    if op == "<":
        return x < y
    if op == "<=":
        return x <= y
    if op == ">":
        return x > y
    if op == ">=":
        return x >= y
    if op == "==":
        return x == y
    return x != y


# ------------------------------------------- narrow unsigned scalar expression trees
# tree: ("leaf", i) | ("bin", op, l, r) | ("un", op, x); leaves index into node.kids.

def _gen_tree(g, depth, kids, budget):
    r = g.r
    if budget <= 0 or r.random() < 0.35:
        kids.append(g.scalar(depth))
        return ("leaf", len(kids) - 1)
    if r.random() < 0.75:
        return ("bin", r.choice(NBIN), _gen_tree(g, depth, kids, budget - 1),
                _gen_tree(g, depth, kids, budget - 1))
    return ("un", r.choice(["~", "-"]), _gen_tree(g, depth, kids, budget - 1))


def _p_tree(t, w, kids, p):
    if t[0] == "leaf":
        return "%s(%s)" % (UT[w], p(kids[t[1]]))
    if t[0] == "bin":
        return "(%s %s %s)" % (_p_tree(t[2], w, kids, p), t[1], _p_tree(t[3], w, kids, p))
    return "(%s%s)" % (t[1], _p_tree(t[2], w, kids, p))


def _e_tree(t, w, kids, e):
    m = (1 << w) - 1
    if t[0] == "leaf":
        return e(kids[t[1]]) & m
    if t[0] == "bin":
        x, y, op = _e_tree(t[2], w, kids, e), _e_tree(t[3], w, kids, e), t[1]
        if op == "+":
            return (x + y) & m
        if op == "-":
            return (x - y) & m
        if op == "*":
            return (x * y) & m
        if op == "&":
            return x & y
        if op == "|":
            return x | y
        return x ^ y
    x = _e_tree(t[2], w, kids, e)
    return (~x) & m if t[1] == "~" else (-x) & m


# ------------------------------------------------------------------ generate

def generate(g, kind, depth):
    r = g.r
    if kind == "nb_arith":
        w = r.choice([8, 16])
        kids = [g.vec(depth)]
        cons = r.choice(["cast", "countbits", "fbh", "rev", "cmp", "shr", "shl", "div", "mod"])
        t1 = _gen_tree(g, depth, kids, 2)
        extra = None
        if cons in ("cmp", "div", "mod"):
            extra = _gen_tree(g, depth, kids, 1)
        elif cons in ("shr", "shl"):
            extra = r.randrange(0, w)
        op = r.choice(CMP)
        return g.node(kind, "v", kids, (w, cons, t1, extra, op))
    if kind == "nb_sext":
        w = r.choice([8, 16])
        kids = [g.vec(depth)]
        conses = ["cast", "fbh", "rev", "cmp"] + (["countbits"] if w == 16 else [])
        cons = r.choice(conses)
        t1 = _gen_tree(g, depth, kids, r.randrange(3))
        extra = _gen_tree(g, depth, kids, 1) if cons == "cmp" else None
        return g.node(kind, "v", kids, (w, cons, t1, extra, r.choice(CMP)))
    if kind == "nb_svec":
        w = r.choice([8, 16])
        signed = r.random() < 0.6
        conses = ["cast", "fbh", "rev"]
        if w == 8 and not signed:
            # vector 16-bit countbits is excluded: Apple's Metal compiler miscounts
            # popcount(ushort4(uint4)) (ignores the truncation) for hand-written MSL too,
            # which is not a Slang bug and would flood the Metal bucket.
            conses.append("countbits")
        cons = r.choice(conses)
        op = r.choice([None, "+", "-", "^", "~"])
        kids = [g.vec(depth), g.vec(depth)]
        return g.node(kind, "v", kids, (w, signed, cons, op))
    if kind == "nb_wide":
        signed = r.random() < 0.5
        cons = r.choice(["countbits", "fbh", "fbl", "rev_lo", "rev_hi"])
        op = r.choice([None, "+", "-", "*", "^", "~"])
        scalar = r.random() < 0.3
        kids = [g.vec(depth), g.vec(depth), g.vec(depth), g.vec(depth)]
        lane = r.choice("xyzw")
        return g.node(kind, "v", kids, (signed, cons, op, scalar, lane))
    raise AssertionError(kind)


# ------------------------------------------------------------------ printing

def _u64v(p, hi, lo):
    return "((vector<uint64_t,4>(%s) << 32) | vector<uint64_t,4>(%s))" % (p(hi), p(lo))


def _u64s(p, hi, lo, lane):
    return "((uint64_t((%s).%s) << 32) | uint64_t((%s).%s))" % (p(hi), lane, p(lo), lane)


def to_slang(node, p):
    k, c, a = node.kind, node.kids, node.arg
    if k == "nb_arith":
        w, cons, t1, extra, op = a
        x = _p_tree(t1, w, c, p)
        if cons == "cast":
            s = "uint(%s)" % x
        elif cons == "countbits":
            s = "countbits(%s)" % x
        elif cons == "fbh":
            s = "firstbithigh(%s)" % x
        elif cons == "rev":
            s = "uint(reversebits(%s))" % x
        elif cons == "cmp":
            s = "uint(%s %s %s)" % (x, op, _p_tree(extra, w, c, p))
        elif cons == "shr":
            s = "uint(%s >> %s(%du))" % (x, UT[w], extra)
        elif cons == "shl":
            s = "uint(%s << %s(%du))" % (x, UT[w], extra)
        else:
            y = "(%s | %s(1u))" % (_p_tree(extra, w, c, p), UT[w])
            s = "uint(%s %s %s)" % (x, "/" if cons == "div" else "%", y)
        return "(%s ^ uint4(%s))" % (p(c[0]), s)
    if k == "nb_sext":
        w, cons, t1, extra, op = a
        x = "bit_cast<%s>(%s)" % (ST[w], _p_tree(t1, w, c, p))
        if cons == "cast":
            s = "uint(%s)" % x
        elif cons == "countbits":
            s = "countbits(%s)" % x
        elif cons == "fbh":
            s = "firstbithigh(%s)" % x
        elif cons == "rev":
            s = "uint(reversebits(%s))" % x
        else:
            y = "bit_cast<%s>(%s)" % (ST[w], _p_tree(extra, w, c, p))
            s = "uint(%s %s %s)" % (x, op, y)
        return "(%s ^ uint4(%s))" % (p(c[0]), s)
    if k == "nb_svec":
        w, signed, cons, op = a
        ut = "vector<%s,4>" % UT[w]
        u0 = "%s(%s)" % (ut, p(c[0]))
        u1 = "%s(%s)" % (ut, p(c[1]))
        if op is None:
            u = u0
        elif op == "~":
            u = "(~%s)" % u0
        else:
            u = "(%s %s %s)" % (u0, op, u1)
        x = "bit_cast<vector<%s,4>>(%s)" % (ST[w], u) if signed else u
        if cons == "cast":
            return "uint4(%s)" % x
        if cons == "countbits":
            return "countbits(%s)" % x
        if cons == "fbh":
            return "firstbithigh(%s)" % x
        return "uint4(reversebits(%s))" % x
    if k == "nb_wide":
        signed, cons, op, scalar, lane = a
        if scalar:
            u0 = _u64s(p, c[0], c[1], lane)
            u1 = _u64s(p, c[2], c[3], lane)
        else:
            u0 = _u64v(p, c[0], c[1])
            u1 = _u64v(p, c[2], c[3])
        if op is None:
            u = u0
        elif op == "~":
            u = "(~%s)" % u0
        else:
            u = "(%s %s %s)" % (u0, op, u1)
        if signed:
            x = "bit_cast<%s>(%s)" % ("int64_t" if scalar else "vector<int64_t,4>", u)
        else:
            x = u
        cvt = "uint64_t" if scalar else "vector<uint64_t,4>"
        if cons == "countbits":
            s = "countbits(%s)" % x
        elif cons == "fbh":
            s = "firstbithigh(%s)" % x
        elif cons == "fbl":
            s = "firstbitlow(%s)" % x
        elif cons == "rev_lo":
            s = "uint%s(%s(reversebits(%s)))" % ("" if scalar else "4", cvt, x)
        else:
            s = "uint%s(%s(reversebits(%s)) >> 32)" % ("" if scalar else "4", cvt, x)
        return "uint4(%s)" % s if scalar else s
    raise AssertionError(k)


# ------------------------------------------------------------------ reference

def evaluate(node, e, vmap):
    k, c, a = node.kind, node.kids, node.arg
    if k == "nb_arith":
        w, cons, t1, extra, op = a
        m = (1 << w) - 1
        x = _e_tree(t1, w, c, e)
        if cons == "cast":
            s = x
        elif cons == "countbits":
            s = _pop(x)
        elif cons == "fbh":
            s = _fbh_u(x)
        elif cons == "rev":
            s = _rev(x, w)
        elif cons == "cmp":
            s = 1 if _cmp(op, x, _e_tree(extra, w, c, e)) else 0
        elif cons == "shr":
            s = x >> extra
        elif cons == "shl":
            s = (x << extra) & m
        else:
            y = _e_tree(extra, w, c, e) | 1
            s = x // y if cons == "div" else x % y
        return vmap(lambda v: v ^ s, e(c[0]))
    if k == "nb_sext":
        w, cons, t1, extra, op = a
        x = _e_tree(t1, w, c, e)
        if cons == "cast":
            s = _sext32(x, w)
        elif cons == "countbits":
            s = _pop(x)
        elif cons == "fbh":
            s = _fbh_s(x, w)
        elif cons == "rev":
            s = _sext32(_rev(x, w), w)
        else:
            y = _e_tree(extra, w, c, e)
            s = 1 if _cmp(op, _signed(x, w), _signed(y, w)) else 0
        return vmap(lambda v: v ^ s, e(c[0]))
    if k == "nb_svec":
        w, signed, cons, op = a
        m = (1 << w) - 1

        def lane(v0, v1):
            u0, u1 = v0 & m, v1 & m
            if op is None:
                u = u0
            elif op == "~":
                u = (~u0) & m
            elif op == "+":
                u = (u0 + u1) & m
            elif op == "-":
                u = (u0 - u1) & m
            else:
                u = u0 ^ u1
            if cons == "cast":
                return _sext32(u, w) if signed else u
            if cons == "countbits":
                return _pop(u)
            if cons == "fbh":
                return _fbh_s(u, w) if signed else _fbh_u(u)
            rv = _rev(u, w)
            return _sext32(rv, w) if signed else rv
        return vmap(lane, e(c[0]), e(c[1]))
    if k == "nb_wide":
        signed, cons, op, scalar, ln = a
        vs = [e(x) for x in c]
        idx = "xyzw".index(ln)

        def lane(h0, l0, h1, l1):
            u0 = (h0 << 32) | l0
            u1 = (h1 << 32) | l1
            if op is None:
                u = u0
            elif op == "~":
                u = (~u0) & M64
            elif op == "+":
                u = (u0 + u1) & M64
            elif op == "-":
                u = (u0 - u1) & M64
            elif op == "*":
                u = (u0 * u1) & M64
            else:
                u = u0 ^ u1
            if cons == "countbits":
                return _pop(u)
            if cons == "fbh":
                return _fbh_s(u, 64) if signed else _fbh_u(u)
            if cons == "fbl":
                return _fbl(u)
            rv = _rev(u, 64)
            return rv & M32 if cons == "rev_lo" else rv >> 32
        if scalar:
            s = lane(vs[0][idx], vs[1][idx], vs[2][idx], vs[3][idx])
            return (s, s, s, s)
        return tuple(lane(vs[0][i], vs[1][i], vs[2][i], vs[3][i]) for i in range(4))
    raise AssertionError(k)
