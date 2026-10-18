# r2d: IR constant folding / peephole rewrites fired by mixing compile-time constants
# with runtime values (32-bit only: uint, int via casts, bool via logical ops).
#
# White-box motivation (slang-src/source/slang):
#   * slang-ir-sccp.cpp:453-535 evalBinaryImpl / evalBinaryIntImpl, :566-620
#     evalComparisonImpl (folds integer compares on the signed 64-bit intVal for ALL
#     integer types), :640-660 evalDiv/evalRem (C++ / and % on int64: truncation),
#     :760-820 evalLsh/evalRsh (bit-width guards, arithmetic vs logical by signedness),
#     :334-370 evalCast. Results are re-normalised by IRBuilder::getIntValue
#     (slang-ir.cpp:2527-2561: int sign-extended, uint zero-extended to int64).
#     So constant-only sub-expressions (kinds `kconst`, `cfold`) are generated from a
#     grammar covering every folded integer op, with edge literals, and then combined
#     with runtime values so the folded constant feeds runtime code.
#   * slang-ir-peephole.cpp:166-305 tryOptimizeArithmeticInst: x+0, x-x -> default,
#     x*1, x*0, 0/x, x/1, logical And/Or with isOne/isZero (slang-ir-util.cpp:2255-2321),
#     incl. the scalar->vector replacement path (:173-190). Kind `ident` hits these.
#   * Division / remainder / shifts by constants are strength-reduced downstream
#     (Metal compiler, SPIRV-Cross, Mesa NIR magic-number division), so `cfold` and
#     `sdivrem` emit `x / K`, `x % K`, `int4(x) / K` with K a known non-zero constant.
#   * slang-ir-legalize-binary-operator.cpp:200-300 legalizeLogicalAndOr (Metal/SPIR-V
#     only) rewrites vector logical And/Or on integer vectors through bool vectors.
#
# Definitions used for evaluate():
#   * core.meta.slang:2386-2421 vector<T,N> integer ops map to kIROp_Add/Sub/Mul/Div,
#     kIROp_Lsh/Rsh, kIROp_And/Or; core.meta.slang:1318-1340 scalar int ops (IRem for %).
#   * slang-emit-spirv.cpp:867-874 Div -> OpSDiv/OpUDiv, IRem -> OpSRem/OpUMod;
#     :956-958 Rsh -> OpShiftRightArithmetic for signed, logical for unsigned.
#     SPIR-V: OpSDiv rounds toward zero, OpSRem takes the sign of the dividend; both are
#     undefined for divisor 0 and for INT_MIN / -1 (overflow), which are never generated:
#     constant signed divisors exclude 0 and -1; runtime signed divisors are either
#     positive ((y >> 1) | 1) or odd with an odd (hence != INT_MIN) dividend.
#   * Vector `&&` / `||` on uint4 are element-wise logical ops returning 0/1 per lane
#     (f2-style codegen: uint4(bool4(x) && bool4(y))).
#   * Shift amounts are always < 32 (masked or constant in [0, 31]); unsigned divisors
#     are non-zero; left shifts are only done on uint.
#
# KNOWN BUG EXCLUDED (reported separately, would fire on every path):
#   slang-ir-peephole.cpp:258-290 folds logical And/Or with isOne/isZero operands as if
#   they were bool: `x && uint4(1)` -> x, `uint4(1) && x` -> x, `x || uint4(0)` -> x,
#   `uint4(0) || x` -> x for integer vectors (expected 0/1 lanes). `logic` therefore
#   never uses an all-1 constant with && nor an all-0 constant with ||.

NAME = "r2d"
KINDS = ["kconst", "cfold", "sdivrem", "sshr", "scmp", "ident", "logic"]

M32 = 0xFFFFFFFF
IMIN = -0x80000000

LITS = [0, 1, 2, 3, 5, 7, 10, 31, 32, 33, 0x7FFFFFFF, 0x80000000, 0x80000001,
        0xFFFFFFFE, 0xFFFFFFFF, 0x55555555, 0xAAAAAAAA, 0xFFFF, 0x10000, 0xFFFF0000,
        0xCCCCCCCD, 0x3B9ACA07, 641, 6700417, 0xFFFFFFF9, 0x40000000, 0xC0000000]
UDIVS = [1, 2, 3, 5, 6, 7, 10, 12, 25, 125, 641, 0x10000, 0x7FFFFFFF, 0x80000000,
         0x80000001, 0xFFFFFFFE, 0xFFFFFFFF, 0xCCCCCCCD, 0x3B9ACA07, 9, 11, 1000, 0x40000000]
SDIVS = [1, 2, 3, 5, 7, 9, 10, 641, -2, -3, -5, -7, -9, -10, 8, -8, 16, -16,
         1 << 30, -(1 << 30), IMIN, 0x7FFFFFFF, -0x7FFFFFFF, 6, -6, 1000, -1000]
UOPS = ["+", "-", "*", "&", "|", "^"]
CMPS = ["<", "<=", ">", ">=", "==", "!="]


def s32(x):
    x &= M32
    return x - 0x100000000 if x & 0x80000000 else x


def tdiv(n, d):
    q = abs(n) // abs(d)
    return -q if (n < 0) != (d < 0) else q


def trem(n, d):
    return n - d * tdiv(n, d)


def cmpf(op, x, y):
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


def binf(op, x, y):
    if op == "+":
        return (x + y) & M32
    if op == "-":
        return (x - y) & M32
    if op == "*":
        return (x * y) & M32
    if op == "&":
        return x & y
    if op == "|":
        return x | y
    return x ^ y


def ulit(v):
    return f"{v & M32:#x}u"


def slit(v, r):
    # signed int literal; sometimes as a negated literal, sometimes as a cast of a uint
    if r is not None and r.random() < 0.5 and v != IMIN:
        return f"({v})" if v < 0 else f"{v}"
    return f"int({ulit(v)})"


# ---------------------------------------------------------------- constant grammar
# trees are tuples; every node has a uint value


def cgen(r, depth):
    if depth <= 0 or r.random() < 0.3:
        v = r.choice(LITS) if r.random() < 0.85 else r.getrandbits(32)
        return ("lit", v)
    d = depth - 1
    c = r.randrange(11)
    if c <= 2:
        return ("ub", r.choice(UOPS), cgen(r, d), cgen(r, d))
    if c == 3:
        return (r.choice(["shl", "shr"]), cgen(r, d), cgen(r, d))
    if c == 4:
        return (r.choice(["udiv", "umod"]), cgen(r, d), cgen(r, d))
    if c == 5:
        return (r.choice(["sdiv", "srem"]), cgen(r, d), r.choice(SDIVS), r.random() < 0.5)
    if c == 6:
        return ("sshr", cgen(r, d), cgen(r, d))
    if c == 7:
        return ("ucmp", r.choice(CMPS), cgen(r, d), cgen(r, d), cgen(r, d), cgen(r, d))
    if c == 8:
        return ("scmp", r.choice(CMPS), cgen(r, d), cgen(r, d), cgen(r, d), cgen(r, d))
    if c == 9:
        return (r.choice(["not", "neg"]), cgen(r, d))
    return ("sext", cgen(r, d), r.randrange(32))


def cval(t):
    k = t[0]
    if k == "lit":
        return t[1] & M32
    if k == "ub":
        return binf(t[1], cval(t[2]), cval(t[3]))
    if k == "shl":
        return (cval(t[1]) << (cval(t[2]) & 31)) & M32
    if k == "shr":
        return cval(t[1]) >> (cval(t[2]) & 31)
    if k == "udiv":
        return cval(t[1]) // (cval(t[2]) | 1)
    if k == "umod":
        return cval(t[1]) % (cval(t[2]) | 1)
    if k == "sdiv":
        return tdiv(s32(cval(t[1])), t[2]) & M32
    if k == "srem":
        return trem(s32(cval(t[1])), t[2]) & M32
    if k == "sshr":
        return (s32(cval(t[1])) >> (cval(t[2]) & 31)) & M32
    if k == "ucmp":
        return cval(t[4]) if cmpf(t[1], cval(t[2]), cval(t[3])) else cval(t[5])
    if k == "scmp":
        return cval(t[4]) if cmpf(t[1], s32(cval(t[2])), s32(cval(t[3]))) else cval(t[5])
    if k == "not":
        return (~cval(t[1])) & M32
    if k == "neg":
        return (-cval(t[1])) & M32
    if k == "sext":   # sign-extend from bit 31-K: int(x << K) >> K
        return (s32((cval(t[1]) << t[2]) & M32) >> t[2]) & M32
    raise AssertionError(k)


def cstr(t):
    k = t[0]
    if k == "lit":
        return ulit(t[1])
    if k == "ub":
        return f"({cstr(t[2])} {t[1]} {cstr(t[3])})"
    if k == "shl":
        return f"({cstr(t[1])} << ({cstr(t[2])} & 31u))"
    if k == "shr":
        return f"({cstr(t[1])} >> ({cstr(t[2])} & 31u))"
    if k == "udiv":
        return f"({cstr(t[1])} / ({cstr(t[2])} | 1u))"
    if k == "umod":
        return f"({cstr(t[1])} % ({cstr(t[2])} | 1u))"
    if k in ("sdiv", "srem"):
        d = f"({t[2]})" if (t[3] and t[2] != IMIN and t[2] < 0) else (
            f"{t[2]}" if (t[3] and t[2] > 0) else f"int({ulit(t[2])})")
        op = "/" if k == "sdiv" else "%"
        return f"uint(int({cstr(t[1])}) {op} {d})"
    if k == "sshr":
        return f"uint(int({cstr(t[1])}) >> ({cstr(t[2])} & 31u))"
    if k == "ucmp":
        return f"(({cstr(t[2])} {t[1]} {cstr(t[3])}) ? {cstr(t[4])} : {cstr(t[5])})"
    if k == "scmp":
        return (f"((int({cstr(t[2])}) {t[1]} int({cstr(t[3])})) ? "
                f"{cstr(t[4])} : {cstr(t[5])})")
    if k == "not":
        return f"(~{cstr(t[1])})"
    if k == "neg":
        return f"(-{cstr(t[1])})"
    if k == "sext":
        return f"uint(int({cstr(t[1])} << {t[2]}u) >> {t[2]}u)"
    raise AssertionError(k)


def kvec(r, depth):
    """A constant uint4: list of 4 trees, or one tree broadcast."""
    if r.random() < 0.35:
        t = cgen(r, depth)
        return [t, t, t, t], True
    return [cgen(r, depth) for _ in range(4)], False


def kvec_str(lanes, bcast):
    if bcast:
        return f"uint4({cstr(lanes[0])})"
    return "uint4(" + ", ".join(cstr(t) for t in lanes) + ")"


def lit_lanes(vals):
    return [("lit", v) for v in vals]


def leafname(r):
    return r.choice(["a", "b"])


# ---------------------------------------------------------------- generate

IDENT = [
    # (template, fn(x) -> value)  x is the runtime child's lane value
    ("({x} - {x})", lambda x: 0),
    ("({x} * 1u)", lambda x: x),
    ("(1u * {x})", lambda x: x),
    ("({x} * uint4(1u, 1u, 1u, 1u))", lambda x: x),
    ("({x} * 0u)", lambda x: 0),
    ("(uint4(0u) * {x})", lambda x: 0),
    ("({x} / 1u)", lambda x: x),
    ("({x} / uint4(1u))", lambda x: x),
    ("(0u / ({x} | 1u))", lambda x: 0),
    ("({x} + 0u)", lambda x: x),
    ("(uint4(0u, 0u, 0u, 0u) + {x})", lambda x: x),
    ("({x} - 0u)", lambda x: x),
    ("({x} | 0u)", lambda x: x),
    ("({x} & 0xffffffffu)", lambda x: x),
    ("({x} ^ 0u)", lambda x: x),
    ("({x} << 0u)", lambda x: x),
    ("({x} >> 0u)", lambda x: x),
    ("({x} % 1u)", lambda x: 0),
    ("({x} * 0xffffffffu)", lambda x: (-x) & M32),
    ("({x} / 0xffffffffu)", lambda x: 1 if x == M32 else 0),
    ("({x} % 0x80000000u)", lambda x: x & 0x7FFFFFFF),
    ("({x} / 0x80000000u)", lambda x: x >> 31),
    ("uint4(int4({x}) / 1)", lambda x: x),
    ("uint4(int4({x}) % 1)", lambda x: 0),
    ("uint4(int4({x}) >> 0)", lambda x: x),
    ("uint4(int4({x}) >> 31)", lambda x: M32 if x & 0x80000000 else 0),
    ("uint4(int4({x}) / int(0x80000000u))", lambda x: 1 if x == 0x80000000 else 0),
    ("uint4(int4({x}) % int(0x80000000u))",
     lambda x: 0 if x == 0x80000000 else x),
    ("(({x} - {x}) + {x})", lambda x: x),
    ("((0u - {x}) + {x})", lambda x: 0),
]


def generate(g, kind, depth):
    r = g.r
    if kind == "kconst":
        lanes, bc = kvec(r, r.randrange(4))
        return g.node(kind, "v", [], (lanes, bc))
    if kind == "cfold":
        mode = r.randrange(4)
        x = g.vec(depth)
        if mode == 0:     # runtime OP constant-vector (either side), wrapping ops
            lanes, bc = kvec(r, r.randrange(4))
            return g.node(kind, "v", [x], ("bin", r.choice(UOPS), lanes, bc, r.random() < 0.5))
        if mode == 1:     # runtime / or % by a non-zero constant (folded or literal)
            if r.random() < 0.6:
                lanes = lit_lanes([r.choice(UDIVS)] * 4) if r.random() < 0.5 else \
                    lit_lanes([r.choice(UDIVS) for _ in range(4)])
            else:
                lanes = [cgen(r, 2) for _ in range(4)]
                lanes = [t if cval(t) != 0 else ("ub", "|", t, ("lit", 1)) for t in lanes]
            bc = all(t == lanes[0] for t in lanes)
            return g.node(kind, "v", [x], ("div", r.choice(["/", "%"]), lanes, bc, False))
        if mode == 2:     # runtime shifted by a constant amount < 32
            t = cgen(r, 2)
            if cval(t) >= 32:
                t = ("ub", "&", t, ("lit", 31))
            return g.node(kind, "v", [x], ("sh", r.choice(["<<", ">>"]), [t] * 4, True, False))
        # constant shifted by runtime amount
        lanes, bc = kvec(r, r.randrange(3))
        return g.node(kind, "v", [x], ("shk", r.choice(["<<", ">>"]), lanes, bc, False))
    if kind == "sdivrem":
        op = r.choice(["/", "%"])
        mode = r.randrange(4)
        if mode == 0:    # runtime int4 / constant int4 (no 0, no -1)
            ds = [r.choice(SDIVS)] * 4 if r.random() < 0.4 else [r.choice(SDIVS) for _ in range(4)]
            return g.node(kind, "v", [g.vec(depth)], ("vk", op, ds))
        if mode == 1:    # runtime int4 / constant int scalar
            return g.node(kind, "v", [g.vec(depth)], ("sk", op, r.choice(SDIVS)))
        if mode == 2:    # constant (any, incl. INT_MIN) / runtime positive divisor
            lanes, bc = kvec(r, r.randrange(3))
            return g.node(kind, "v", [g.vec(depth)], ("kv", op, lanes, bc))
        # runtime odd / runtime odd (dividend odd -> never INT_MIN)
        return g.node(kind, "v", [g.vec(depth), g.vec(depth)], ("vv", op))
    if kind == "sshr":
        mode = r.randrange(3)
        if mode == 0:
            return g.node(kind, "v", [g.vec(depth)], ("k", r.randrange(32)))
        if mode == 1:
            return g.node(kind, "v", [g.vec(depth)], ("ext", r.randrange(32)))
        lanes, bc = kvec(r, r.randrange(3))
        return g.node(kind, "v", [g.vec(depth)], ("kv", lanes, bc))
    if kind == "scmp":
        op = r.choice(CMPS)
        signed = r.random() < 0.6
        edges = [0, 1, 0xFFFFFFFF, 0x80000000, 0x7FFFFFFF, 0x80000001, 0xFFFFFFFE]
        if r.random() < 0.6:
            lanes = lit_lanes([r.choice(edges)] * 4 if r.random() < 0.5
                              else [r.choice(edges) for _ in range(4)])
            bc = all(t == lanes[0] for t in lanes)
        else:
            lanes, bc = kvec(r, r.randrange(3))
        form = r.randrange(2)
        kids = [g.vec(depth)] + ([g.vec(depth), g.vec(depth)] if form == 0 else [])
        return g.node(kind, "v", kids, (op, signed, lanes, bc, r.random() < 0.5, form))
    if kind == "ident":
        i = r.randrange(len(IDENT))
        if r.random() < 0.2:   # scalar child, broadcast after the identity
            return g.node(kind, "v", [g.scalar(depth)], (i, True))
        return g.node(kind, "v", [g.vec(depth)], (i, False))
    if kind == "logic":
        op = r.choice(["&&", "||"])
        mode = r.randrange(3)
        kids = [g.vec(depth), g.node("var", "v", [], leafname(r))]
        if mode == 2:
            kids += [g.vec(depth), g.node("var", "v", [], leafname(r))]
            return g.node(kind, "v", kids, (op, None, False, False))
        pool = [0, 1, 2, 0xFFFFFFFF, 0x80000000]
        while True:
            if r.random() < 0.5:
                lanes = lit_lanes([r.choice(pool) for _ in range(4)])
                bc = False
            else:
                lanes, bc = kvec(r, r.randrange(3))
            vals = [cval(t) for t in lanes]
            # exclude the known peephole bug (see header)
            if op == "&&" and all(v == 1 for v in vals):
                continue
            if op == "||" and all(v == 0 for v in vals):
                continue
            break
        return g.node(kind, "v", kids, (op, lanes, bc, r.random() < 0.5))
    raise AssertionError(kind)


# ---------------------------------------------------------------- print

def to_slang(node, p):
    k, c, a = node.kind, node.kids, node.arg
    if k == "kconst":
        return kvec_str(a[0], a[1])
    if k == "cfold":
        form, op, lanes, bc, swap = a
        kv = kvec_str(lanes, bc)
        x = p(c[0])
        if form == "bin":
            return f"({kv} {op} {x})" if swap else f"({x} {op} {kv})"
        if form == "div":
            return f"({x} {op} {kv})"
        if form == "sh":
            return f"({x} {op} {cstr(lanes[0])})"
        return f"({kv} {op} ({x} & 31u))"
    if k == "sdivrem":
        m, op = a[0], a[1]
        if m == "vk":
            ds = ", ".join(slit(d, None) for d in a[2])
            return f"uint4(int4({p(c[0])}) {op} int4({ds}))"
        if m == "sk":
            return f"uint4(int4({p(c[0])}) {op} {slit(a[2], None)})"
        if m == "kv":
            return f"uint4(int4({kvec_str(a[2], a[3])}) {op} int4(({p(c[0])} >> 1u) | 1u))"
        return f"uint4(int4({p(c[0])} | 1u) {op} int4({p(c[1])} | 1u))"
    if k == "sshr":
        if a[0] == "k":
            return f"uint4(int4({p(c[0])}) >> {a[1]})"
        if a[0] == "ext":
            return f"uint4(int4({p(c[0])} << {a[1]}u) >> {a[1]}u)"
        # shift amount cast to int4: `int4 >> uint4` makes slangc's C++ output invalid
        # (Vector<int,4> >> Vector<uint,4> has no operator; reported separately)
        return f"uint4(int4({kvec_str(a[1], a[2])}) >> int4({p(c[0])} & 31u))"
    if k == "scmp":
        op, signed, lanes, bc, swap, form = a
        kv = kvec_str(lanes, bc)
        x = p(c[0])
        if signed:
            lhs, rhs = f"int4({x})", f"int4({kv})"
        else:
            lhs, rhs = x, kv
        cond = f"({rhs} {op} {lhs})" if swap else f"({lhs} {op} {rhs})"
        if form == 0:
            return f"select({cond}, {p(c[1])}, {p(c[2])})"
        return f"uint4({cond})"
    if k == "ident":
        i, scal = a
        t = IDENT[i][0]
        if scal:
            t = t.replace("uint4(", "uint(").replace("int4(", "int(")
            t = t.replace("uint(1u, 1u, 1u, 1u)", "uint(1u)").replace("uint(0u, 0u, 0u, 0u)", "uint(0u)")
            return "uint4(" + t.replace("{x}", p(c[0])) + ")"
        return t.replace("{x}", p(c[0]))
    if k == "logic":
        op, lanes, bc, swap = a
        rt = f"({p(c[0])} ^ {p(c[1])})"     # never a compile-time constant
        if lanes is None:
            rt2 = f"({p(c[2])} + {p(c[3])})"
            return f"({rt} {op} {rt2})"
        kv = kvec_str(lanes, bc)
        return f"({kv} {op} {rt})" if swap else f"({rt} {op} {kv})"
    raise AssertionError(k)


# ---------------------------------------------------------------- evaluate

def _vec(x):
    return x if isinstance(x, tuple) else (x,) * 4


def evaluate(node, e, vmap):
    k, c, a = node.kind, node.kids, node.arg
    if k == "kconst":
        return tuple(cval(t) for t in a[0])
    if k == "cfold":
        form, op, lanes, bc, swap = a
        kv = tuple(cval(t) for t in lanes)
        x = _vec(e(c[0]))
        if form == "bin":
            return tuple(binf(op, q, v) if swap else binf(op, v, q) for v, q in zip(x, kv))
        if form == "div":
            return tuple((v // q) if op == "/" else (v % q) for v, q in zip(x, kv))
        if form == "sh":
            return tuple(((v << q) & M32) if op == "<<" else (v >> q) for v, q in zip(x, kv))
        return tuple(((q << (v & 31)) & M32) if op == "<<" else (q >> (v & 31))
                     for v, q in zip(x, kv))
    if k == "sdivrem":
        m, op = a[0], a[1]
        f = tdiv if op == "/" else trem
        if m == "vk":
            x = _vec(e(c[0]))
            return tuple(f(s32(v), d) & M32 for v, d in zip(x, a[2]))
        if m == "sk":
            x = _vec(e(c[0]))
            return tuple(f(s32(v), a[2]) & M32 for v in x)
        if m == "kv":
            x = _vec(e(c[0]))
            kv = [cval(t) for t in a[2]]
            return tuple(f(s32(q), s32((v >> 1) | 1)) & M32 for q, v in zip(kv, x))
        x, y = _vec(e(c[0])), _vec(e(c[1]))
        return tuple(f(s32(v | 1), s32(w | 1)) & M32 for v, w in zip(x, y))
    if k == "sshr":
        x = _vec(e(c[0]))
        if a[0] == "k":
            return tuple((s32(v) >> a[1]) & M32 for v in x)
        if a[0] == "ext":
            return tuple((s32((v << a[1]) & M32) >> a[1]) & M32 for v in x)
        kv = [cval(t) for t in a[1]]
        return tuple((s32(q) >> (v & 31)) & M32 for q, v in zip(kv, x))
    if k == "scmp":
        op, signed, lanes, bc, swap, form = a
        kv = [cval(t) for t in lanes]
        x = _vec(e(c[0]))
        conv = s32 if signed else (lambda z: z)
        res = []
        for v, q in zip(x, kv):
            lv, rv = conv(v), conv(q)
            res.append(cmpf(op, rv, lv) if swap else cmpf(op, lv, rv))
        if form == 0:
            t, f = _vec(e(c[1])), _vec(e(c[2]))
            return tuple(t[i] if res[i] else f[i] for i in range(4))
        return tuple(1 if z else 0 for z in res)
    if k == "ident":
        i, scal = a
        fn = IDENT[i][1]
        x = e(c[0])
        if scal:
            return (fn(x),) * 4
        return tuple(fn(v) for v in x)
    if k == "logic":
        op, lanes, bc, swap = a
        x = _vec(e(c[0]))
        leaf1 = e(c[1])
        rt = tuple(v ^ w for v, w in zip(x, leaf1))
        if lanes is None:
            y = _vec(e(c[2]))
            leaf2 = e(c[3])
            other = tuple((v + w) & M32 for v, w in zip(y, leaf2))
        else:
            other = tuple(cval(t) for t in lanes)
        if op == "&&":
            return tuple(1 if (u != 0 and v != 0) else 0 for u, v in zip(rt, other))
        return tuple(1 if (u != 0 or v != 0) else 0 for u, v in zip(rt, other))
    raise AssertionError(k)

