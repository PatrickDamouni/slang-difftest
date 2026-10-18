# r2b: signed 32-bit integer intrinsics and operators (abs, sign, min/max/clamp,
# firstbithigh, truncating / and %, arithmetic >>, signed compares), applied to
# int4(<uint4 expr>) and int(<uint expr>) and converted back with uint4(...).
#
# Definitions used (slang-src/source/slang/hlsl.meta.slang unless noted):
#   abs  (int)     8109-8151: hlsl/metal/glsl "abs", spirv GLSL.std.450 SAbs. We never
#                  pass INT_MIN (operand is forced to (x | 1u) or (x & 0x7fffffffu)).
#   abs  (uint)    same overload (T : __BuiltinIntegerType includes uint): the doc says
#                  "returns the absolute value of x"; for an unsigned x that is x itself
#                  (HLSL abs(uint) is the identity). The SPIR-V branch emits SAbs, which
#                  GLSL.std.450 defines as interpreting x as *signed* -> suspicious.
#   sign (int)     15813-15866: -1 / 0 / 1. Metal: "vec<int,N>(sign($0))" / "int(sign($0))"
#                  (MSL sign() is a float function), spirv SSign.
#   min/max/clamp  13628ff / 9792-9850: signed comparison for int (spirv SMin/SMax/SClamp).
#                  clamp bounds are ordered with min/max so lo <= hi (SClamp undefined
#                  otherwise).
#   firstbithigh   11720-11833: for signed negative values, index of highest 0-bit; -1
#                  (0xffffffff) for 0 and -1. Metal: hand-written scalar expansion
#                  (~value, clz) mapped over vector lanes; spirv FindSMsb.
#   / and % (int)  truncate toward zero; remainder has the sign of the dividend
#                  (OpSDiv / OpSRem, C/MSL semantics). Divisor never 0; never INT_MIN / -1:
#                  a dividend that may be INT_MIN is only divided by divisors != -1.
#   >> (int)       arithmetic shift (OpShiftRightArithmetic); amount masked to [0, 31].
#   compares       signed (OpSLessThan, ...).
#   No signed overflow anywhere: no signed + - * and no unary minus on int.
#
# Why (white-box): Metal lowers sign through a float intrinsic, firstbithigh through a
# hand-written clz expansion (hlsl.meta.slang:11741-11747), abs(uint) differs between the
# metal ("abs") and spirv (SAbs) branches, and slang-ir-simplify-for-emit.cpp:344-385
# (unifyBinaryExprOperands) broadcasts scalar operands of mixed vector/scalar int ops using
# the *other* operand's type.

NAME = "r2b"
# u_abs and s_sign are implemented below but NOT hunted: both hit deterministic bugs on
# every use (found during calibration) and would mask everything else:
#   * abs(uint4): SPIR-V emits GLSL.std.450 SAbs on a uint vector, so abs(0xffffffffu) == 1
#     on both Vulkan paths, while Metal (and HLSL semantics) give 0xffffffff.
#   * sign(int)/sign(int4): Metal emits int(sign(x)) / vec<int,4>(sign(x)); MSL sign() is
#     float/half only -> "call to 'sign' is ambiguous" / "no matching function". The C++
#     target emits I32_sign(x), which the C++ prelude does not declare.
KINDS = ["s_abs", "s_minmax", "s_clamp", "s_fbh", "s_div", "s_rem", "s_shr", "s_cmp"]

M32 = 0xFFFFFFFF
DIV_LITS = [-2, -3, -4, -7, -8, -16, 2, 3, 4, 5, 7, 8, 16, 1000, -1000, 65536, -65536,
            0x40000000, -0x40000000, 0x7FFFFFFF, -0x7FFFFFFF]
CMP = ["<", "<=", ">", ">=", "==", "!="]
CMP_LITS = [0, -1, 1, -5, 100, -0x7FFFFFFF]


def _s(x):
    x &= M32
    return x - (1 << 32) if x >> 31 else x


def _u(x):
    return x & M32


def _tdiv(a, b):
    q = abs(a) // abs(b)
    return q if (a < 0) == (b < 0) else -q


def _trem(a, b):
    return a - b * _tdiv(a, b)


def _sfbh(x):
    x = _s(x)
    if x < 0:
        x = ~x
    return M32 if x == 0 else x.bit_length() - 1


def _lit(v):
    # int literal text; never relies on negating INT_MIN
    return f"({v})" if v < 0 else f"{v}"


# ---------------------------------------------------------------- generation

def generate(g, kind, depth):
    r = g.r
    vec = g.vec
    if kind == "u_abs":
        return g.node(kind, "v", [vec(depth)], None)
    if kind == "s_abs":
        if r.random() < 0.25:
            return g.node(kind, "v", [g.scalar(depth)], ("sc", r.choice(["or1", "and7f"])))
        return g.node(kind, "v", [vec(depth)], ("v", r.choice(["or1", "and7f"])))
    if kind == "s_sign":
        if r.random() < 0.25:
            return g.node(kind, "v", [g.scalar(depth)], "sc")
        return g.node(kind, "v", [vec(depth)], "v")
    if kind == "s_minmax":
        f = r.choice(["min", "max"])
        if r.random() < 0.3:
            return g.node(kind, "v", [vec(depth), g.scalar(depth)], (f, "vs"))
        return g.node(kind, "v", [vec(depth), vec(depth)], (f, "vv"))
    if kind == "s_clamp":
        return g.node(kind, "v", [vec(depth), vec(depth), vec(depth)], None)
    if kind == "s_fbh":
        if r.random() < 0.25:
            return g.node(kind, "v", [g.scalar(depth)], "sc")
        return g.node(kind, "v", [vec(depth)], "v")
    if kind in ("s_div", "s_rem"):
        dv = r.choice(["any", "or1"])          # "any" may be INT_MIN
        forms = ["lit", "lit", "pos", "svpos"]
        if dv == "or1":
            forms += ["odd", "odd", "sodd"]
        df = r.choice(forms)
        if df == "lit":
            return g.node(kind, "v", [vec(depth)], (dv, df, r.choice(DIV_LITS)))
        if df in ("svpos", "sodd"):
            return g.node(kind, "v", [vec(depth), g.scalar(depth)], (dv, df, 0))
        return g.node(kind, "v", [vec(depth), vec(depth)], (dv, df, 0))
    if kind == "s_shr":
        f = r.choice(["vv", "vs", "lit", "sv"])
        if f == "lit":
            return g.node(kind, "v", [vec(depth)], (f, r.choice([0, 1, 7, 16, 30, 31])))
        if f == "vs":
            return g.node(kind, "v", [vec(depth), g.scalar(depth)], (f, 0))
        if f == "sv":
            return g.node(kind, "v", [g.scalar(depth), vec(depth)], (f, 0))
        return g.node(kind, "v", [vec(depth), vec(depth)], (f, 0))
    # s_cmp
    op = r.choice(CMP)
    if r.random() < 0.35:
        return g.node(kind, "v", [vec(depth), vec(depth), vec(depth)],
                      (op, "lit", r.choice(CMP_LITS)))
    return g.node(kind, "v", [vec(depth), vec(depth), vec(depth), vec(depth)], (op, "vv", 0))


# ---------------------------------------------------------------- printing

def _absop(p, form):
    return f"({p} | 1u)" if form == "or1" else f"({p} & 0x7fffffffu)"


def to_slang(node, p):
    k, a, c = node.kind, node.arg, node.kids
    if k == "u_abs":
        return f"abs({p(c[0])})"
    if k == "s_abs":
        x = _absop(p(c[0]), a[1])
        if a[0] == "sc":
            return f"uint4(uint(abs(int({x}))))"
        return f"uint4(abs(int4({x})))"
    if k == "s_sign":
        if a == "sc":
            return f"uint4(uint(sign(int({p(c[0])}))))"
        return f"uint4(sign(int4({p(c[0])})))"
    if k == "s_minmax":
        if a[1] == "vs":
            return f"uint4({a[0]}(int4({p(c[0])}), int({p(c[1])})))"
        return f"uint4({a[0]}(int4({p(c[0])}), int4({p(c[1])})))"
    if k == "s_clamp":
        lo, hi = f"int4({p(c[1])})", f"int4({p(c[2])})"
        return f"uint4(clamp(int4({p(c[0])}), min({lo}, {hi}), max({lo}, {hi})))"
    if k == "s_fbh":
        if a == "sc":
            return f"uint4(firstbithigh(int({p(c[0])})))"
        return f"firstbithigh(int4({p(c[0])}))"
    if k in ("s_div", "s_rem"):
        dv, df, lit = a
        op = "/" if k == "s_div" else "%"
        x = f"int4({p(c[0])})" if dv == "any" else f"int4({p(c[0])} | 1u)"
        if df == "lit":
            d = _lit(lit)
        elif df == "pos":
            d = f"int4(({p(c[1])} >> 1u) | 1u)"
        elif df == "odd":
            d = f"int4({p(c[1])} | 1u)"
        elif df == "svpos":
            d = f"int(({p(c[1])} >> 1u) | 1u)"
        else:
            d = f"int({p(c[1])} | 1u)"
        return f"uint4({x} {op} {d})"
    if k == "s_shr":
        f = a[0]
        if f == "lit":
            return f"uint4(int4({p(c[0])}) >> {a[1]})"
        if f == "vs":
            return f"uint4(int4({p(c[0])}) >> ({p(c[1])} & 31u))"
        if f == "sv":
            return f"uint4(int({p(c[0])}) >> ({p(c[1])} & 31u))"
        return f"uint4(int4({p(c[0])}) >> int4({p(c[1])} & 31u))"
    op, form, lit = a
    if form == "lit":
        return f"select(int4({p(c[0])}) {op} {_lit(lit)}, {p(c[1])}, {p(c[2])})"
    return f"select(int4({p(c[0])}) {op} int4({p(c[1])}), {p(c[2])}, {p(c[3])})"


# ---------------------------------------------------------------- reference

CMPF = {"<": lambda x, y: x < y, "<=": lambda x, y: x <= y, ">": lambda x, y: x > y,
        ">=": lambda x, y: x >= y, "==": lambda x, y: x == y, "!=": lambda x, y: x != y}


def _bc(x):
    return x if isinstance(x, tuple) else (x,) * 4


def evaluate(node, e, vmap):
    k, a, c = node.kind, node.arg, node.kids
    if k == "u_abs":
        return vmap(lambda x: x, e(c[0]))
    if k == "s_abs":
        if a[1] == "or1":
            f = lambda x: _u(abs(_s(x | 1)))
        else:
            f = lambda x: _u(abs(_s(x & 0x7FFFFFFF)))
        return _bc(vmap(f, e(c[0])))
    if k == "s_sign":
        def sg(x):
            x = _s(x)
            return _u(-1) if x < 0 else (1 if x > 0 else 0)
        return _bc(vmap(sg, e(c[0])))
    if k == "s_minmax":
        f = min if a[0] == "min" else max
        return vmap(lambda x, y: _u(f(_s(x), _s(y))), e(c[0]), e(c[1]))
    if k == "s_clamp":
        def cl(x, p, q):
            x, p, q = _s(x), _s(p), _s(q)
            lo, hi = min(p, q), max(p, q)
            return _u(min(max(x, lo), hi))
        return vmap(cl, e(c[0]), e(c[1]), e(c[2]))
    if k == "s_fbh":
        return _bc(vmap(_sfbh, e(c[0])))
    if k in ("s_div", "s_rem"):
        dv, df, lit = a
        fn = _tdiv if k == "s_div" else _trem

        def xs(x):
            return _s(x) if dv == "any" else _s(x | 1)

        if df == "lit":
            def one(x):
                xx, d = xs(x), lit
                if xx == -(1 << 31) and d == -1:
                    raise ValueError("INT_MIN / -1")
                return _u(fn(xx, d))
            return vmap(one, e(c[0]))

        def two(x, y):
            xx = xs(x)
            if df in ("pos", "svpos"):
                d = _s((y >> 1) | 1)
            else:
                d = _s(y | 1)
            if d == 0 or (xx == -(1 << 31) and d == -1):
                raise ValueError("bad divisor")
            return _u(fn(xx, d))
        return vmap(two, e(c[0]), e(c[1]))
    if k == "s_shr":
        f = a[0]
        if f == "lit":
            return vmap(lambda x: _u(_s(x) >> a[1]), e(c[0]))
        return _bc(vmap(lambda x, y: _u(_s(x) >> (y & 31)), e(c[0]), e(c[1])))
    op, form, lit = a
    f = CMPF[op]
    if form == "lit":
        return vmap(lambda x, t, u: t if f(_s(x), lit) else u, e(c[0]), e(c[1]), e(c[2]))
    return vmap(lambda x, y, t, u: t if f(_s(x), _s(y)) else u,
                e(c[0]), e(c[1]), e(c[2]), e(c[3]))
