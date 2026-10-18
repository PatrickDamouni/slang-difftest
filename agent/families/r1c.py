"""r1c: integer (uint / int / bool) and exact-valued float MATRICES.

Why: Metal and SPIR-V have no integer or bool matrices, so Slang hand-lowers
every uint/int/bool matrix to an array of row vectors on both targets
(slang-ir-legalize-matrix-types.cpp:12-31 targetLegalizesMatrixTypes,
:65-78 shouldLowerMatrixType, :234-340 element-wise/mixed binary lowering,
:404-480 comparisons, :482-559 unary/cast lowering incl. the int->float
"operand needs lowering, result does not" path at :640-660). The C++ target
instead uses the prelude's Matrix<T,R,C> operator templates, and Metal float
matrices use hand-written preludes (slang-emit-metal-prelude.cpp:7-50:
_slang_matrixCompMult, _slang_matrixFmod) plus operand-swapped `($1 * $0)`
for mul. None of the existing families touch matrices.

Semantics used by evaluate() (all element-wise unless stated):
  * matrix<T,R,C> constructors from R*C scalars, R row vectors, a scalar
    fill, and truncating reshape from a larger matrix:
    core.meta.slang:2914-2965 (MakeMatrix / MatrixReshape);
    the peephole lowering of MatrixReshape (slang-ir-peephole.cpp:1605-1650)
    keeps the top-left R x C block.
  * uint2x2 <-> uint4: core.meta.slang:2617-2636 (row 0 = .xy, row 1 = .zw).
  * binary operators incl. matrix-scalar and scalar-matrix overloads:
    core.meta.slang:3160-3232. Unsigned ops wrap mod 2^32; shift amounts are
    masked `& 31u`, divisors forced odd `| 1u` (HLSL/SPIR-V define these).
  * unary ~ and - on matrices: core.meta.slang:3044-3068.
  * comparisons give bool matrices; bool->uint gives 1/0; uint->bool is != 0.
  * select / ?: with a bool-matrix condition picks per element:
    core.meta.slang:1125-1145.
  * transpose (integer: hand-written loop on non-HLSL targets):
    hlsl.meta.slang:16470-16488; float: :16451-16469.
  * min / max / clamp on matrices: hlsl.meta.slang:13758-13800 (row-wise
    __vectorMaxImpl), 14147-14215, 9890-9901 (clamp = min(max(x,lo),hi), bounds
    ordered here so lo <= hi).
  * mul(matrix, scalar) is plain kIROp_Mul: hlsl.meta.slang:14628-14637.
  * int matrices: uint<->int casts are bit-preserving; only well-defined signed
    ops are used (bitwise, arithmetic >> with masked amount, signed compare,
    signed min/max) - never signed + - * or division.
  * float matrices: built from uint values masked to <= 0xFFFF, every op keeps
    values non-negative integers (or exact dyadic fractions only right before
    the final truncating float->uint cast) with a tracked bound <= 2^24, so all
    results are exact even under fast math. `%` uses only power-of-two
    divisors (fmod exact; Metal _slang_matrixFmod). mul(float matrices) is the
    linear-algebra product: hlsl.meta.slang:14670-14700, 14750-14780,
    14832-14862.

NOT generated (Slang bugs found while building this family; see report):
  * integer mul(matrix, vector/matrix): hlsl.meta.slang:14696-14723 /
    14779-14806 / 14861-14888 use `case metal: __intrinsic_asm "($1 * $0)"`
    but the operands were already lowered to `array<uintN,R>` -> invalid MSL.
  * 8/16/64-bit integer matrices: shouldLowerMatrixType only lowers
    bool/int/uint (slang-ir-legalize-matrix-types.cpp:76-77) so Metal gets
    `matrix<ushort,..>` which Metal rejects.
  * C++ prelude: ~ / ! on matrices (SLANG_MATRIX_UNARY_OP writes
    `result[i].rows[i][j]`) and << / >> on integer matrices (no operator) do
    not compile. These ARE generated, at a low rate, so the cpp oracle sees them.
"""

NAME = "r1c"
KINDS = ["imat_arith", "imat_shape", "imat_cmp", "fmat_exact"]

M32 = 0xFFFFFFFF
SW = "xyzw"
FLIM = 1 << 24          # every float value stays <= this, so it is exact

# ------------------------------------------------------------------ helpers


def _s32(x):
    x &= M32
    return x - (1 << 32) if x >> 31 else x


def _tname(t, r, c):
    return {"u": "uint", "i": "int", "f": "float", "b": "bool"}[t] + f"{r}x{c}"


CMP = {
    "<": lambda x, y: x < y, "<=": lambda x, y: x <= y,
    ">": lambda x, y: x > y, ">=": lambda x, y: x >= y,
    "==": lambda x, y: x == y, "!=": lambda x, y: x != y,
}

UOPS = {
    "+": lambda x, y: (x + y) & M32,
    "-": lambda x, y: (x - y) & M32,
    "*": lambda x, y: (x * y) & M32,
    "&": lambda x, y: x & y,
    "|": lambda x, y: x | y,
    "^": lambda x, y: x ^ y,
    "/": lambda x, y: x // (y | 1),
    "%": lambda x, y: x % (y | 1),
    "<<": lambda x, y: (x << (y & 31)) & M32,
    ">>": lambda x, y: x >> (y & 31),
}


def _rhs(op, txt):
    """Make the right operand of an unsigned op well-defined."""
    if op in ("/", "%"):
        return f"({txt} | 1u)"
    if op in ("<<", ">>"):
        return f"({txt} & 31u)"
    return txt


def _ew(f, A, B):
    return [[f(x, y) for x, y in zip(ra, rb)] for ra, rb in zip(A, B)]


def _map(f, A):
    return [[f(x) for x in ra] for ra in A]


# ------------------------------------------------------------------ generator


class _Ctx:
    def __init__(self, g, depth):
        self.g = g
        self.r = g.r
        self.depth = depth
        self.kids = []
        self.vecs = []
        self.scals = []

    def vec(self):
        if self.vecs and self.r.random() < 0.45:
            return self.r.choice(self.vecs)
        # kids are printed once per use, so keep them shallow (mostly leaves or
        # one existing-family op) to stop textual blow-up
        self.kids.append(self.g.vec(max(self.depth + 1, self.g.max_depth - 1)))
        i = len(self.kids) - 1
        self.vecs.append(i)
        return i

    def scal(self):
        if self.scals and self.r.random() < 0.4:
            return self.r.choice(self.scals)
        self.kids.append(self.g.scalar(max(self.depth + 1, self.g.max_depth - 1)))
        i = len(self.kids) - 1
        self.scals.append(i)
        return i


def _pick(r, table):
    tot = sum(w for _, w in table)
    x = r.random() * tot
    for name, w in table:
        x -= w
        if x < 0:
            return name
    return table[-1][0]


W_ARITH = [("leaf", 3), ("ew", 6), ("ms", 3), ("sm", 2), ("muls", 1), ("neg", 1),
           ("bnot", 0.3), ("minmax", 2), ("clamp", 1), ("tr", 1), ("rs", 1),
           ("sel", 1), ("b2u", 1), ("nz", 0.5), ("i2u", 1), ("f2u", 0.6)]
W_SHAPE = [("leaf", 5), ("ew", 2), ("ms", 1), ("tr", 4), ("rs", 4), ("minmax", 1),
           ("sel", 1), ("i2u", 0.5), ("f2u", 0.5), ("neg", 0.5)]
W_CMP = [("leaf", 3), ("ew", 2), ("ms", 1), ("sel", 5), ("b2u", 4), ("nz", 1.5),
         ("i2u", 3), ("minmax", 1), ("clamp", 1), ("tr", 1), ("f2u", 1)]
W_FLOAT = [("leaf", 2), ("ew", 1), ("f2u", 8), ("tr", 1), ("rs", 1), ("sel", 1)]
TABLES = {"imat_arith": W_ARITH, "imat_shape": W_SHAPE, "imat_cmp": W_CMP, "fmat_exact": W_FLOAT}


def _dim(r):
    return r.choice([2, 2, 3, 4, 4])


def _gu(cx, R, C, d, table):
    """uint matrix expression of shape R x C: (op, 'u', R, C, ...)."""
    r = cx.r
    op = "leaf" if d <= 0 else _pick(r, table)
    if op == "leaf":
        k = r.random()
        if k < 0.6:
            vs = [cx.vec() for _ in range(min(R, r.choice([1, 2])))]
            rows = [(r.choice(vs), "".join(r.choice(SW) for _ in range(C))) for _ in range(R)]
            return ("build", "u", R, C, rows)
        if k < 0.72 and R * C <= 6:
            vs = [cx.vec() for _ in range(r.choice([1, 2]))]
            es = [(r.choice(vs), r.choice(SW)) for _ in range(R * C)]
            return ("elems", "u", R, C, es)
        if k < 0.84 and R == 2 and C == 2:
            return ("from4", "u", 2, 2, cx.vec())
        if k < 0.92:
            return ("fill", "u", R, C, cx.scal())
        return ("build", "u", R, C, [(cx.vec(), "".join(r.choice(SW) for _ in range(C)))
                                     for _ in range(R)])
    d1 = d - 1
    if op == "ew":
        o = _pick(r, [("+", 3), ("-", 3), ("*", 3), ("&", 2), ("|", 2), ("^", 2),
                      ("/", 1.5), ("%", 1.5), ("<<", 0.4), (">>", 0.4)])
        return ("ew", "u", R, C, o, _gu(cx, R, C, d1, table), _gu(cx, R, C, d1, table))
    if op in ("ms", "sm"):
        o = _pick(r, [("+", 3), ("-", 3), ("*", 3), ("&", 1), ("|", 1), ("^", 2),
                      ("/", 1.5), ("%", 1.5), ("<<", 0.4), (">>", 0.4)])
        return (op, "u", R, C, o, _gu(cx, R, C, d1, table), cx.scal())
    if op == "muls":
        return ("muls", "u", R, C, r.random() < 0.5, _gu(cx, R, C, d1, table), cx.scal())
    if op in ("neg", "bnot"):
        return (op, "u", R, C, _gu(cx, R, C, d1, table))
    if op == "minmax":
        return ("minmax", "u", R, C, r.choice(["min", "max"]),
                _gu(cx, R, C, d1, table), _gu(cx, R, C, d1, table))
    if op == "clamp":
        return ("clamp", "u", R, C, _gu(cx, R, C, d1, table), _gu(cx, R, C, d1, table),
                _gu(cx, R, C, d1, table))
    if op == "tr":
        return ("tr", "u", R, C, _gu(cx, C, R, d1, table))
    if op == "rs":
        R2, C2 = r.randint(R, 4), r.randint(C, 4)
        if (R2, C2) == (R, C):
            if R < 4:
                R2 = 4
            elif C < 4:
                C2 = 4
            else:
                return _gu(cx, R, C, d1, table)
        return ("rs", "u", R, C, _gu(cx, R2, C2, d1, table))
    if op in ("sel", "b2u"):
        t = _pick(r, [("u", 4), ("i", 2), ("f", 1.5)])
        A, B = _cmp_operands(cx, t, R, C, d1, table)
        cop = r.choice(list(CMP))
        if op == "sel":
            return ("sel", "u", R, C, cop, A, B, _gu(cx, R, C, d1, table),
                    _gu(cx, R, C, d1, table), r.random() < 0.5)
        return ("b2u", "u", R, C, cop, A, B, r.random() < 0.3)
    if op == "nz":
        return ("nz", "u", R, C, _gu(cx, R, C, d1, table))
    if op == "i2u":
        return ("i2u", "u", R, C, _gi(cx, R, C, d1, table))
    if op == "f2u":
        F = _gf(cx, R, C, max(d1, 1), table)
        scale = r.choice([1, 1, 1, 2, 4, 8]) if F[-1] > 0 else 1
        return ("f2u", "u", R, C, F, scale)
    raise AssertionError(op)


def _cmp_operands(cx, t, R, C, d, table):
    if t == "u":
        return _gu(cx, R, C, d, table), _gu(cx, R, C, d, table)
    if t == "i":
        return _gi(cx, R, C, d, table), _gi(cx, R, C, d, table)
    return _gf(cx, R, C, d, table), _gf(cx, R, C, d, table)


def _gi(cx, R, C, d, table):
    """int matrix (values held as uint32 bit patterns)."""
    r = cx.r
    k = r.random() if d > 0 else 0.0
    d1 = max(d - 1, 0)
    if k < 0.35:
        return ("u2i", "i", R, C, _gu(cx, R, C, d1, table))
    if k < 0.55:
        return ("ibit", "i", R, C, r.choice("&|^"), _gi(cx, R, C, d1, table), _gi(cx, R, C, d1, table))
    if k < 0.75:
        if r.random() < 0.5:
            return ("isar", "i", R, C, _gi(cx, R, C, d1, table), _gu(cx, R, C, d1, table))
        return ("isars", "i", R, C, _gi(cx, R, C, d1, table), cx.scal())
    if k < 0.92:
        return ("iminmax", "i", R, C, r.choice(["min", "max"]),
                _gi(cx, R, C, d1, table), _gi(cx, R, C, d1, table))
    return ("inot", "i", R, C, _gi(cx, R, C, d1, table))


def _gf(cx, R, C, d, table):
    """float matrix with exact non-negative integer values; last field = bound."""
    r = cx.r
    if d <= 0 or r.random() < 0.25:
        mask = r.choice([0xFF, 0xFFF, 0xFFFF])
        return ("u2f", "f", R, C, _gu(cx, R, C, max(d - 1, 0), table), mask, mask)
    d1 = d - 1
    k = r.random()
    if k < 0.18:
        A, B = _gf(cx, R, C, d1, table), _gf(cx, R, C, d1, table)
        if A[-1] + B[-1] <= FLIM:
            return ("fadd", "f", R, C, A, B, A[-1] + B[-1])
        return ("fminmax", "f", R, C, "max", A, B, max(A[-1], B[-1]))
    if k < 0.32:
        A, B = _gf(cx, R, C, d1, table), _gf(cx, R, C, d1, table)
        if A[-1] * B[-1] <= FLIM:
            return ("fmul", "f", R, C, A, B, A[-1] * B[-1])
        return ("fadiff", "f", R, C, A, B, max(A[-1], B[-1]))
    if k < 0.44:
        A, B = _gf(cx, R, C, d1, table), _gf(cx, R, C, d1, table)
        return ("fminmax", "f", R, C, r.choice(["min", "max"]), A, B, max(A[-1], B[-1]))
    if k < 0.54:
        A, B = _gf(cx, R, C, d1, table), _gf(cx, R, C, d1, table)
        return ("fadiff", "f", R, C, A, B, max(A[-1], B[-1]))
    if k < 0.64:
        A = _gf(cx, R, C, d1, table)
        dv = r.choice([2, 4, 8, 16, 256])
        return ("fmod", "f", R, C, A, dv, min(A[-1], dv - 1))
    if k < 0.72:
        A = _gf(cx, C, R, d1, table)
        return ("ftr", "f", R, C, A, A[-1])
    if k < 0.78:
        R2, C2 = r.randint(R, 4), r.randint(C, 4)
        if (R2, C2) == (R, C):
            return _gf(cx, R, C, d1, table)
        A = _gf(cx, R2, C2, d1, table)
        return ("frs", "f", R, C, A, A[-1])
    if k < 0.92:
        N = _dim(r)
        A, B = _gf(cx, R, N, d1, table), _gf(cx, N, C, d1, table)
        if A[-1] * B[-1] * N <= FLIM:
            return ("fmm", "f", R, C, A, B, A[-1] * B[-1] * N)
        return ("fmod", "f", R, C, _gf(cx, R, C, 0, table), 16, 15)
    cop = r.choice(list(CMP))
    A, B = _gf(cx, R, C, d1, table), _gf(cx, R, C, d1, table)
    X, Y = _gf(cx, R, C, d1, table), _gf(cx, R, C, d1, table)
    return ("fsel", "f", R, C, cop, A, B, X, Y, max(X[-1], Y[-1]))


def generate(g, kind, depth):
    cx = _Ctx(g, depth)
    r = g.r
    table = TABLES[kind]
    R, C = _dim(r), _dim(r)
    md = r.choice([1, 2, 2, 3])
    if kind == "fmat_exact":
        F = _gf(cx, R, C, md, table)
        m = ("f2u", "u", R, C, F, r.choice([1, 1, 2, 4]))
    else:
        m = _gu(cx, R, C, md, table)
    # projection of the R x C uint matrix to a uint4. Dynamic row/element
    # indexing (drow/delem) forces Slang to materialise the lowered matrix as a
    # Metal array initializer `{ { ... } }`; admit.py's planted-fault regex
    # stops at the first `}` and cannot handle that, so dynamic indexing is
    # only used at the root of a function (depth 1) and rarely there; the
    # dynamic *column* form (dcol, a dynamic vector-component index) needs no
    # array and is the common dynamic case.
    k = r.random()
    if depth > 1 and k >= 0.85:
        k = r.random() * 0.85
    if k < 0.42:
        cells = [(r.randrange(R), r.randrange(C)) for _ in range(4)]
        proj = ("swz", r.random() < 0.7, cells)
    elif k < 0.75:
        proj = ("row", r.randrange(R), "".join(r.choice(SW[:C]) for _ in range(4)))
    elif k < 0.80 or (k < 0.85 and R == 2 and C == 2):
        proj = ("v4",) if R == 2 and C == 2 else ("row", r.randrange(R), "".join(r.choice(SW[:C]) for _ in range(4)))
    elif k < 0.93:
        proj = ("dcol", cx.scal(), r.randrange(R))
    elif k < 0.98:
        proj = ("drow", cx.scal(), "".join(r.choice(SW[:C]) for _ in range(4)))
    else:
        proj = ("delem", cx.scal(), r.randrange(C))
    return g.node(kind, "v", cx.kids, (m, proj))


# ------------------------------------------------------------------ printer


def _pm(m, p, kids):
    op, t, R, C = m[0], m[1], m[2], m[3]
    T = _tname(t, R, C)
    if op == "build":
        return f"{T}(" + ", ".join(f"({p(kids[k])}).{s}" for k, s in m[4]) + ")"
    if op == "elems":
        return f"{T}(" + ", ".join(f"({p(kids[k])}).{c}" for k, c in m[4]) + ")"
    if op == "from4":
        return f"{T}({p(kids[m[4]])})"
    if op == "fill":
        return f"{T}({p(kids[m[4]])})"
    if op == "ew":
        o = m[4]
        return f"({_pm(m[5], p, kids)} {o} {_rhs(o, _pm(m[6], p, kids))})"
    if op == "ms":
        o = m[4]
        return f"({_pm(m[5], p, kids)} {o} {_rhs(o, p(kids[m[6]]))})"
    if op == "sm":
        o = m[4]
        return f"({p(kids[m[6]])} {o} {_rhs(o, _pm(m[5], p, kids))})"
    if op == "muls":
        a, s = _pm(m[5], p, kids), p(kids[m[6]])
        return f"mul({a}, {s})" if m[4] else f"mul({s}, {a})"
    if op == "neg":
        return f"(-{_pm(m[4], p, kids)})"
    if op == "bnot":
        return f"(~{_pm(m[4], p, kids)})"
    if op == "minmax":
        return f"{m[4]}({_pm(m[5], p, kids)}, {_pm(m[6], p, kids)})"
    if op == "clamp":
        lo, hi = _pm(m[5], p, kids), _pm(m[6], p, kids)
        return f"clamp({_pm(m[4], p, kids)}, min({lo}, {hi}), max({lo}, {hi}))"
    if op == "tr":
        return f"transpose({_pm(m[4], p, kids)})"
    if op == "rs":
        return f"{T}({_pm(m[4], p, kids)})"
    if op == "sel":
        c = f"({_pm(m[5], p, kids)} {m[4]} {_pm(m[6], p, kids)})"
        x, y = _pm(m[7], p, kids), _pm(m[8], p, kids)
        return f"select({c}, {x}, {y})" if m[9] else f"({c} ? {x} : {y})"
    if op == "b2u":
        c = f"({_pm(m[5], p, kids)} {m[4]} {_pm(m[6], p, kids)})"
        return f"{T}(!{c})" if m[7] else f"{T}({c})"
    if op == "nz":
        return f"{T}({_tname('b', R, C)}({_pm(m[4], p, kids)}))"
    if op == "i2u":
        return f"{T}({_pm(m[4], p, kids)})"
    if op == "f2u":
        f = _pm(m[4], p, kids)
        return f"{T}({f})" if m[5] == 1 else f"{T}({f} / {m[5]}.0f)"
    if op == "u2i":
        return f"{T}({_pm(m[4], p, kids)})"
    if op == "ibit":
        return f"({_pm(m[5], p, kids)} {m[4]} {_pm(m[6], p, kids)})"
    if op == "isar":
        return f"({_pm(m[4], p, kids)} >> {T}({_pm(m[5], p, kids)} & 31u))"
    if op == "isars":
        return f"({_pm(m[4], p, kids)} >> int({p(kids[m[5]])} & 31u))"
    if op == "iminmax":
        return f"{m[4]}({_pm(m[5], p, kids)}, {_pm(m[6], p, kids)})"
    if op == "inot":
        return f"(~{_pm(m[4], p, kids)})"
    if op == "u2f":
        return f"{T}({_pm(m[4], p, kids)} & {m[5]}u)"
    if op == "fadd":
        return f"({_pm(m[4], p, kids)} + {_pm(m[5], p, kids)})"
    if op == "fmul":
        return f"({_pm(m[4], p, kids)} * {_pm(m[5], p, kids)})"
    if op == "fminmax":
        return f"{m[4]}({_pm(m[5], p, kids)}, {_pm(m[6], p, kids)})"
    if op == "fadiff":
        a, b = _pm(m[4], p, kids), _pm(m[5], p, kids)
        return f"(max({a}, {b}) - min({a}, {b}))"
    if op == "fmod":
        return f"({_pm(m[4], p, kids)} % {m[5]}.0f)"
    if op == "ftr":
        return f"transpose({_pm(m[4], p, kids)})"
    if op == "frs":
        return f"{T}({_pm(m[4], p, kids)})"
    if op == "fmm":
        return f"mul({_pm(m[4], p, kids)}, {_pm(m[5], p, kids)})"
    if op == "fsel":
        c = f"({_pm(m[5], p, kids)} {m[4]} {_pm(m[6], p, kids)})"
        return f"select({c}, {_pm(m[7], p, kids)}, {_pm(m[8], p, kids)})"
    raise AssertionError(op)


def to_slang(node, p):
    m, proj = node.arg
    kids = node.kids
    M = _pm(m, p, kids)
    R, C = m[2], m[3]
    if proj[0] == "swz":
        if proj[1]:
            return f"({M})." + "".join(f"_m{i}{j}" for i, j in proj[2])
        return f"({M})." + "".join(f"_{i + 1}{j + 1}" for i, j in proj[2])
    if proj[0] == "row":
        return f"({M})[{proj[1]}].{proj[2]}"
    if proj[0] == "drow":
        return f"({M})[{_idx(p(kids[proj[1]]), R)}].{proj[2]}"
    if proj[0] == "v4":
        return f"uint4({M})"
    if proj[0] == "delem":
        return f"uint4(({M})[{_idx(p(kids[proj[1]]), R)}][{proj[2]}])"
    if proj[0] == "dcol":
        return f"uint4(({M})[{proj[2]}][{_idx(p(kids[proj[1]]), C)}])"
    raise AssertionError(proj)


def _idx(s, R):
    return f"(({s}) & {R - 1}u)" if R in (2, 4) else f"(({s}) % 3u)"


# ------------------------------------------------------------------ reference


def _em(m, kv):
    op, t, R, C = m[0], m[1], m[2], m[3]
    if op == "build":
        return [[kv[k][SW.index(ch)] for ch in s] for k, s in m[4]]
    if op == "elems":
        vals = [kv[k][SW.index(c)] for k, c in m[4]]
        return [vals[i * C:(i + 1) * C] for i in range(R)]
    if op == "from4":
        v = kv[m[4]]
        return [[v[0], v[1]], [v[2], v[3]]]
    if op == "fill":
        return [[kv[m[4]]] * C for _ in range(R)]
    if op == "ew":
        return _ew(UOPS[m[4]], _em(m[5], kv), _em(m[6], kv))
    if op == "ms":
        s, f = kv[m[6]], UOPS[m[4]]
        return _map(lambda x: f(x, s), _em(m[5], kv))
    if op == "sm":
        s, f = kv[m[6]], UOPS[m[4]]
        return _map(lambda x: f(s, x), _em(m[5], kv))
    if op == "muls":
        s = kv[m[6]]
        return _map(lambda x: (x * s) & M32, _em(m[5], kv))
    if op == "neg":
        return _map(lambda x: (-x) & M32, _em(m[4], kv))
    if op == "bnot":
        return _map(lambda x: (~x) & M32, _em(m[4], kv))
    if op == "minmax":
        return _ew(min if m[4] == "min" else max, _em(m[5], kv), _em(m[6], kv))
    if op == "clamp":
        X, P, Q = _em(m[4], kv), _em(m[5], kv), _em(m[6], kv)
        return [[min(max(x, min(p, q)), max(p, q)) for x, p, q in zip(rx, rp, rq)]
                for rx, rp, rq in zip(X, P, Q)]
    if op in ("tr", "ftr"):
        A = _em(m[4], kv)
        return [[A[j][i] for j in range(C)] for i in range(R)]
    if op in ("rs", "frs"):
        A = _em(m[4], kv)
        return [row[:C] for row in A[:R]]
    if op in ("sel", "fsel"):
        f = CMP[m[4]]
        A, B = _cmpval(m[5], kv), _cmpval(m[6], kv)
        X, Y = _em(m[7], kv), _em(m[8], kv)
        return [[x if f(a, b) else y for a, b, x, y in zip(ra, rb, rx, ry)]
                for ra, rb, rx, ry in zip(A, B, X, Y)]
    if op == "b2u":
        f = CMP[m[4]]
        A, B = _cmpval(m[5], kv), _cmpval(m[6], kv)
        neg = m[7]
        return [[1 if (f(a, b) != neg) else 0 for a, b in zip(ra, rb)] for ra, rb in zip(A, B)]
    if op == "nz":
        return _map(lambda x: 1 if x else 0, _em(m[4], kv))
    if op in ("i2u", "u2i"):
        return _em(m[4], kv)
    if op == "f2u":
        sc = m[5]
        return _map(lambda x: x // sc, _em(m[4], kv))
    if op == "ibit":
        return _ew(UOPS[m[4]], _em(m[5], kv), _em(m[6], kv))
    if op == "isar":
        return _ew(lambda x, y: (_s32(x) >> (y & 31)) & M32, _em(m[4], kv), _em(m[5], kv))
    if op == "isars":
        s = kv[m[5]] & 31
        return _map(lambda x: (_s32(x) >> s) & M32, _em(m[4], kv))
    if op == "iminmax":
        f = min if m[4] == "min" else max
        return _ew(lambda x, y: f(_s32(x), _s32(y)) & M32, _em(m[5], kv), _em(m[6], kv))
    if op == "inot":
        return _map(lambda x: (~x) & M32, _em(m[4], kv))
    if op == "u2f":
        mask = m[5]
        return _map(lambda x: x & mask, _em(m[4], kv))
    if op == "fadd":
        return _ew(lambda x, y: x + y, _em(m[4], kv), _em(m[5], kv))
    if op == "fmul":
        return _ew(lambda x, y: x * y, _em(m[4], kv), _em(m[5], kv))
    if op == "fminmax":
        return _ew(min if m[4] == "min" else max, _em(m[5], kv), _em(m[6], kv))
    if op == "fadiff":
        return _ew(lambda x, y: abs(x - y), _em(m[4], kv), _em(m[5], kv))
    if op == "fmod":
        dv = m[5]
        return _map(lambda x: x % dv, _em(m[4], kv))
    if op == "fmm":
        A, B = _em(m[4], kv), _em(m[5], kv)
        N = len(B)
        return [[sum(A[i][n] * B[n][j] for n in range(N)) for j in range(C)] for i in range(R)]
    raise AssertionError(op)


def _cmpval(m, kv):
    """Values as compared: signed for int matrices, exact ints otherwise."""
    A = _em(m, kv)
    if m[1] == "i":
        return _map(_s32, A)
    return A


def evaluate(node, e, vmap):
    m, proj = node.arg
    kv = [e(k) for k in node.kids]
    M = _em(m, kv)
    R, C = m[2], m[3]
    if proj[0] == "swz":
        return tuple(M[i][j] & M32 for i, j in proj[2])
    if proj[0] == "row":
        row = M[proj[1]]
        return tuple(row[SW.index(ch)] & M32 for ch in proj[2])
    if proj[0] == "drow":
        s = kv[proj[1]]
        i = (s & (R - 1)) if R in (2, 4) else s % 3
        row = M[i]
        return tuple(row[SW.index(ch)] & M32 for ch in proj[2])
    if proj[0] == "v4":
        return (M[0][0], M[0][1], M[1][0], M[1][1])
    if proj[0] == "delem":
        s = kv[proj[1]]
        i = (s & (R - 1)) if R in (2, 4) else s % 3
        return (M[i][proj[2]] & M32,) * 4
    if proj[0] == "dcol":
        s = kv[proj[1]]
        j = (s & (C - 1)) if C in (2, 4) else s % 3
        return (M[proj[2]][j] & M32,) * 4
    raise AssertionError(proj)
