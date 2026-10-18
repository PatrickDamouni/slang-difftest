# r2a: 64-bit integers (int64_t / uint64_t): comparisons, shifts by 0..63, division and
# remainder, min/max/clamp/abs, widening conversions and 32x32->64 high products, each mixed
# with *64-bit constants* (INT64_MIN, INT64_MAX, 2^63, 2^64-1, -2^32, ...).
#
# Definitions used for evaluate():
#   * 64-bit arithmetic is two's complement; unsigned wraps mod 2^64. Signed values are made with
#     bit_cast (slang-src/source/slang/core.meta.slang:3444) or by sign-extending int32 values,
#     never by signed overflow (the only signed products are int32*int32, which fit in int64).
#   * Shifts: amounts are masked `& 63`, so always < 64. `>>` on int64 is arithmetic, on uint64
#     logical (SPIR-V OpShiftRightArithmetic / OpShiftRightLogical, MSL/C++ semantics).
#   * `/` and `%`: truncating division, remainder has the sign of the dividend (HLSL/C, SPIR-V
#     OpSDiv / OpSRem, OpUDiv / OpUMod). Divisors are never 0, and signed divisors are never -1
#     (runtime divisors are `(y & -3) | 1`: odd and bit 1 clear), so INT64_MIN / -1 cannot occur.
#   * min / max / clamp / abs on 64-bit: hlsl.meta.slang:8112-8160 (abs -> metal "abs",
#     SPIR-V GLSL.std.450 SAbs), 9792 (clamp, lo <= hi always), 13628 (max). abs is only applied
#     to odd values (`x | 1`), so abs(INT64_MIN) never occurs. Clamp bounds are ordered in Python.
#   * Narrowing int64/uint64 -> uint keeps the low 32 bits; int -> int64 sign-extends.
#
# Why (white-box):
#   * slang-src/source/slang/slang-emit-c-like.cpp:1373-1385 prints an Int64 literal as
#     emitInt64(v) + "LL" with no parentheses or cast (unlike Int, which is wrapped as `int(...)`).
#     For INT64_MIN this produces `-9223372036854775808LL`, i.e. unary minus applied to a literal
#     that does not fit in `long long`, which C++/MSL types as *unsigned* long long. Comparisons,
#     division, remainder, min/max against that constant then silently become unsigned in the
#     Metal (and C++) output, while SPIR-V gets an exact 64-bit OpConstant. Verified in
#     work/r2a-probe/p2.metal: `if(x_0 < -9223372036854775808LL)`.
#   * Negative int64 literals are emitted bare (`x_0 - -5LL`, `x_0 * -3LL`), so the precedence of
#     the leading `-` is left to the surrounding expression.
#   * slang-src/source/slang/slang-ir-sccp.cpp:565-603 (evalComparisonImpl) folds *all* integer
#     comparisons with signed IRIntegerValue compares, so uint64 constants >= 2^63 compare wrong
#     after folding (found while reading; affects every target alike, so this family keeps one
#     comparison operand at runtime and the bug is reported separately, see p1.slang).
#   * 64-bit shifts/divisions/mul-high on Metal go to Apple's `long`/`ulong` code, on SPIR-V to
#     64-bit OpS*/OpU* ops that MoltenVK and KosmicKrisp lower themselves (Int64 emulation paths).

NAME = "r2a"
KINDS = ["w_cmp", "w_shift", "w_div", "w_mm", "w_conv"]

M32 = 0xFFFFFFFF
M64 = 0xFFFFFFFFFFFFFFFF
I64MIN = 1 << 63

S_CONSTS = [I64MIN, I64MIN, I64MIN, M64 >> 1, M64, (-(1 << 32)) & M64, 1 << 32, (-(1 << 31)) & M64, (1 << 31) - 1, 0, 1,
            (-5) & M64, I64MIN + 1, M32]
U_CONSTS = [I64MIN, I64MIN, M64, M64 >> 1, 0, 1, M32, 1 << 32, I64MIN + 1, 0x8000000080000000,
            0x00000001FFFFFFFF]
# divisors: never 0, never -1 when signed
S_DIVS = [I64MIN, I64MIN, M64 >> 1, (-2) & M64, 3, (-7) & M64, 1 << 32, (-(1 << 32)) & M64, 1,
          I64MIN + 1]
U_DIVS = [I64MIN, I64MIN, M64, 3, 1 << 32, M32, I64MIN + 1, 1]
CMPS = ["<", "<=", ">", ">=", "==", "!="]
LANES = "xyzw"


# ------------------------------------------------------------------ helpers

def _sv(x):
    """64-bit pattern -> Python signed int."""
    return x - (1 << 64) if x >> 63 else x


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


def _tdiv(x, y):
    q = abs(x) // abs(y)
    return q if (x < 0) == (y < 0) else -q


def _trem(x, y):
    return x - _tdiv(x, y) * y


def _const_text(c, sg, style):
    """Slang text of a 64-bit constant (pattern c); several spellings, all exact."""
    if not sg:
        return "uint64_t(0x%xull)" % c if style else "0x%xull" % c
    v = _sv(c)
    if style == 0:
        return "int64_t(0x%xull)" % c
    if c == I64MIN:
        return "(-9223372036854775807ll - 1ll)" if style == 1 else "int64_t(0x8000000000000000ull)"
    return "%dll" % v if v >= 0 else "(%dll)" % v


def _vec_t(sg, lane):
    if lane:
        return "int64_t" if sg else "uint64_t"
    return "vector<int64_t,4>" if sg else "vector<uint64_t,4>"


def _w64(p, hi, lo, sg, lane):
    """64-bit value from two uint4 children (hi:lo), optionally one lane, optionally signed."""
    if lane:
        u = "((uint64_t((%s).%s) << 32) | uint64_t((%s).%s))" % (p(hi), lane, p(lo), lane)
    else:
        u = "((vector<uint64_t,4>(%s) << 32) | vector<uint64_t,4>(%s))" % (p(hi), p(lo))
    if sg:
        return "bit_cast<%s>(%s)" % (_vec_t(True, lane), u)
    return u


def _ew64(e, hi, lo, lane):
    """Evaluate _w64 -> list of 4 unsigned 64-bit patterns (lane: broadcast of that lane)."""
    h, l = e(hi), e(lo)
    vals = [(h[i] << 32) | l[i] for i in range(4)]
    if lane:
        return [vals[LANES.index(lane)]] * 4
    return vals


def _out(r, lane, half):
    """64-bit result -> uint4 text."""
    if lane:
        return "uint4(uint(%s))" % r if half == 0 else "uint4(uint((%s) >> 32))" % r
    return "vector<uint,4>(%s)" % r if half == 0 else "vector<uint,4>((%s) >> 32)" % r


def _eout(vals, sg, half):
    res = []
    for v in vals:
        if half:
            v = (_sv(v) >> 32) & M64 if sg else v >> 32
        res.append(v & M32)
    return tuple(res)


def _lane(g):
    return g.r.choice(LANES) if g.r.random() < 0.4 else None


# ------------------------------------------------------------------ generate

def generate(g, kind, depth):
    r = g.r
    sg = r.random() < 0.6
    lane = _lane(g)
    style = r.randrange(3)
    if kind == "w_cmp":
        rhs_const = r.random() < 0.65
        a = {"sg": sg, "op": r.choice(CMPS), "lane": lane, "style": style,
             "const": rhs_const, "swap": r.random() < 0.3}
        kids = [g.vec(depth), g.vec(depth)]
        if rhs_const:
            a["c"] = r.choice(S_CONSTS if sg else U_CONSTS)
        else:
            kids += [g.vec(depth), g.vec(depth)]
        kids += [g.vec(depth), g.vec(depth)]
        return g.node(kind, "v", kids, a)
    if kind == "w_shift":
        a = {"sg": sg, "dir": r.choice(["<<", ">>", ">>"]), "lane": lane, "half": r.randrange(2),
             "amt64": r.random() < 0.3, "samt": r.random() < 0.5}
        amt = g.scalar(depth) if a["samt"] else g.vec(depth)
        return g.node(kind, "v", [g.vec(depth), g.vec(depth), amt], a)
    if kind == "w_div":
        rhs_const = r.random() < 0.6
        a = {"sg": sg, "op": r.choice(["/", "%"]), "lane": lane, "half": r.randrange(2),
             "style": style, "const": rhs_const}
        kids = [g.vec(depth), g.vec(depth)]
        if rhs_const:
            a["c"] = r.choice(S_DIVS if sg else U_DIVS)
        else:
            kids += [g.vec(depth), g.vec(depth)]
        return g.node(kind, "v", kids, a)
    if kind == "w_mm":
        fn = r.choice(["min", "max", "clamp", "abs"] if sg else ["min", "max", "clamp"])
        a = {"sg": sg, "fn": fn, "lane": lane, "half": r.randrange(2), "style": style}
        kids = [g.vec(depth), g.vec(depth)]
        pool = S_CONSTS if sg else U_CONSTS
        key = _sv if sg else (lambda x: x)
        if fn in ("min", "max"):
            a["const"] = r.random() < 0.6
            if a["const"]:
                a["c"] = r.choice(pool)
            else:
                kids += [g.vec(depth), g.vec(depth)]
        elif fn == "clamp":
            c1, c2 = sorted([r.choice(pool), r.choice(pool)], key=key)
            a["lo"], a["hi"] = c1, c2
        return g.node(kind, "v", kids, a)
    # w_conv
    a = {"v": r.choice(["sext_hi", "umulhi", "smulhi", "carry", "smix", "trunc_s"])}
    return g.node(kind, "v", [g.vec(depth), g.vec(depth)], a)


# ------------------------------------------------------------------ to_slang

def to_slang(node, p):
    a, c = node.arg, node.kids
    k = node.kind
    if k == "w_cmp":
        sg, lane = a["sg"], a["lane"]
        x = _w64(p, c[0], c[1], sg, lane)
        if a["const"]:
            y = _const_text(a["c"], sg, a["style"])
            rest = c[2:]
        else:
            y = _w64(p, c[2], c[3], sg, lane)
            rest = c[4:]
        if a["swap"]:
            x, y = y, x
        cond = "%s %s %s" % (x, a["op"], y)
        if lane:
            return "select(vector<bool,4>(%s), %s, %s)" % (cond, p(rest[0]), p(rest[1]))
        return "select(%s, %s, %s)" % (cond, p(rest[0]), p(rest[1]))
    if k == "w_shift":
        sg, lane = a["sg"], a["lane"]
        x = _w64(p, c[0], c[1], sg, lane)
        amt = p(c[2])
        if not a["samt"] and lane:
            amt = "(%s).%s" % (amt, lane)
        if a["amt64"]:
            if a["samt"] or lane:
                amt = "(uint64_t(%s) & 63ull)" % amt
            else:
                amt = "(vector<uint64_t,4>(%s) & 63ull)" % amt
        else:
            amt = "(%s & 63u)" % amt
        return _out("(%s %s %s)" % (x, a["dir"], amt), lane, a["half"])
    if k == "w_div":
        sg, lane = a["sg"], a["lane"]
        x = _w64(p, c[0], c[1], sg, lane)
        if a["const"]:
            y = _const_text(a["c"], sg, a["style"])
        else:
            y = _w64(p, c[2], c[3], sg, lane)
            y = "((%s & int64_t(-3)) | int64_t(1))" % y if sg else "(%s | 1ull)" % y
        return _out("(%s %s %s)" % (x, a["op"], y), lane, a["half"])
    if k == "w_mm":
        sg, lane, fn = a["sg"], a["lane"], a["fn"]
        x = _w64(p, c[0], c[1], sg, lane)
        if fn == "abs":
            r = "abs(%s | int64_t(1))" % x
        elif fn == "clamp":
            lo = _const_text(a["lo"], sg, a["style"])
            hi = _const_text(a["hi"], sg, a["style"])
            if lane is None:
                lo, hi = "%s(%s)" % (_vec_t(sg, None), lo), "%s(%s)" % (_vec_t(sg, None), hi)
            r = "clamp(%s, %s, %s)" % (x, lo, hi)
        else:
            y = _const_text(a["c"], sg, a["style"]) if a["const"] else _w64(p, c[2], c[3], sg, lane)
            if lane is None and a["const"]:
                y = "%s(%s)" % (_vec_t(sg, None), y)
            r = "%s(%s, %s)" % (fn, x, y)
        return _out(r, lane, a["half"])
    # w_conv
    x, y = p(c[0]), p(c[1])
    v = a["v"]
    if v == "sext_hi":
        return "vector<uint,4>(vector<int64_t,4>(int4(%s)) >> 32)" % x
    if v == "umulhi":
        return "vector<uint,4>((vector<uint64_t,4>(%s) * vector<uint64_t,4>(%s)) >> 32)" % (x, y)
    if v == "smulhi":
        return "vector<uint,4>((vector<int64_t,4>(int4(%s)) * vector<int64_t,4>(int4(%s))) >> 32)" % (x, y)
    if v == "carry":
        return "vector<uint,4>((vector<uint64_t,4>(%s) + vector<uint64_t,4>(%s)) >> 32)" % (x, y)
    if v == "smix":
        return "vector<uint,4>((vector<int64_t,4>(int4(%s)) + vector<int64_t,4>(%s)) >> 33)" % (x, y)
    # trunc_s: int64 built from sign-extended int, multiplied by 2^32-1 constant, low word
    return "vector<uint,4>(vector<int64_t,4>(int4(%s)) * 4294967295ll)" % x


# ------------------------------------------------------------------ evaluate

def evaluate(node, e, vmap):
    a, c = node.arg, node.kids
    k = node.kind
    if k == "w_cmp":
        sg, lane = a["sg"], a["lane"]
        xs = _ew64(e, c[0], c[1], lane)
        if a["const"]:
            ys = [a["c"]] * 4
            rest = c[2:]
        else:
            ys = _ew64(e, c[2], c[3], lane)
            rest = c[4:]
        if a["swap"]:
            xs, ys = ys, xs
        t, f = e(rest[0]), e(rest[1])
        conds = [_cmp(a["op"], _sv(xs[i]) if sg else xs[i], _sv(ys[i]) if sg else ys[i])
                 for i in range(4)]
        if lane:
            return t if conds[0] else f
        return tuple(t[i] if conds[i] else f[i] for i in range(4))
    if k == "w_shift":
        sg, lane = a["sg"], a["lane"]
        xs = _ew64(e, c[0], c[1], lane)
        am = e(c[2])
        if a["samt"]:
            ams = [am & 63] * 4
        elif lane:
            ams = [am[LANES.index(lane)] & 63] * 4
        else:
            ams = [v & 63 for v in am]
        res = []
        for x, s in zip(xs, ams):
            if a["dir"] == "<<":
                res.append((x << s) & M64)
            elif sg:
                res.append((_sv(x) >> s) & M64)
            else:
                res.append(x >> s)
        return _eout(res, sg, a["half"])
    if k == "w_div":
        sg, lane = a["sg"], a["lane"]
        xs = _ew64(e, c[0], c[1], lane)
        if a["const"]:
            ys = [a["c"]] * 4
        else:
            ys = _ew64(e, c[2], c[3], lane)
            ys = [((y & ((-3) & M64)) | 1) if sg else (y | 1) for y in ys]
        res = []
        for x, y in zip(xs, ys):
            if sg:
                sx, sy = _sv(x), _sv(y)
                r = _tdiv(sx, sy) if a["op"] == "/" else _trem(sx, sy)
                res.append(r & M64)
            else:
                res.append(x // y if a["op"] == "/" else x % y)
        return _eout(res, sg, a["half"])
    if k == "w_mm":
        sg, lane, fn = a["sg"], a["lane"], a["fn"]
        xs = _ew64(e, c[0], c[1], lane)
        key = _sv if sg else (lambda v: v)
        res = []
        if fn == "abs":
            res = [abs(_sv(x | 1)) & M64 for x in xs]
        elif fn == "clamp":
            lo, hi = a["lo"], a["hi"]
            for x in xs:
                v = x
                if key(v) < key(lo):
                    v = lo
                if key(v) > key(hi):
                    v = hi
                res.append(v)
        else:
            ys = [a["c"]] * 4 if a["const"] else _ew64(e, c[2], c[3], lane)
            for x, y in zip(xs, ys):
                pick = (key(x) <= key(y)) if fn == "min" else (key(x) >= key(y))
                res.append(x if pick else y)
        return _eout(res, sg, a["half"])
    # w_conv
    X, Y = e(c[0]), e(c[1])
    v = a["v"]
    out = []
    for i in range(4):
        x, y = X[i], Y[i]
        sx = x - (1 << 32) if x >> 31 else x
        sy = y - (1 << 32) if y >> 31 else y
        if v == "sext_hi":
            r = sx >> 32
        elif v == "umulhi":
            r = (x * y) >> 32
        elif v == "smulhi":
            r = (sx * sy) >> 32
        elif v == "carry":
            r = (x + y) >> 32
        elif v == "smix":
            r = (sx + y) >> 33
        else:
            r = sx * 4294967295
        out.append(r & M32)
    return tuple(out)
