"""Random well-defined Slang expressions over uint / uint4 (with int4 casts in the
`signed` family).

One AST, two printers: `to_slang` emits Slang source and `evaluate` computes
the exact expected value in Python. Because both come from the same tree, the
reference can't drift from the program it checks.

Every operation is well-defined by construction, so a mismatch can never be
blamed on undefined behaviour:
  * arithmetic is unsigned 32-bit (wraps mod 2^32); the `signed` family only uses
    signed operations that cannot overflow (compare, min/max, >>, firstbithigh)
  * shift amounts are masked with `& 31u`
  * divisors are forced odd, hence non-zero, with `| 1u`
  * clamp bounds are ordered with min/max so lo <= hi
  * no floats (Metal's runtime compiler defaults to fast math)
Semantics of the bit intrinsics follow Slang's core module
(source/slang/hlsl.meta.slang): firstbithigh/firstbitlow return 0xFFFFFFFF
for an input of 0.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

M32 = 0xFFFFFFFF

# --------------------------------------------------------------------- nodes


@dataclass
class Node:
    kind: str                      # see SLANG / EVAL tables below
    ty: str                        # "u" (uint) or "v" (uint4)
    kids: list = field(default_factory=list)
    arg: object = None             # leaf name, swizzle pattern, op token, ...


def leaf(name: str) -> Node:
    return Node("var", "v" if name in ("a", "b") else "u", arg=name)


# ------------------------------------------------------------------ helpers


def _popcount(x: int) -> int:
    return bin(x & M32).count("1")


def _reverse(x: int) -> int:
    return int(f"{x & M32:032b}"[::-1], 2)


def _fbh(x: int) -> int:
    x &= M32
    return M32 if x == 0 else x.bit_length() - 1


def _fbl(x: int) -> int:
    x &= M32
    return M32 if x == 0 else (x & -x).bit_length() - 1


BIN_OPS = {
    "+": lambda x, y: (x + y) & M32,
    "-": lambda x, y: (x - y) & M32,
    "*": lambda x, y: (x * y) & M32,
    "&": lambda x, y: x & y,
    "|": lambda x, y: x | y,
    "^": lambda x, y: x ^ y,
}
CMP_OPS = {
    "<": lambda x, y: x < y, "<=": lambda x, y: x <= y,
    ">": lambda x, y: x > y, ">=": lambda x, y: x >= y,
    "==": lambda x, y: x == y, "!=": lambda x, y: x != y,
}
UNARY_FNS = {
    "countbits": _popcount,
    "reversebits": _reverse,
    "firstbithigh": _fbh,
    "firstbitlow": _fbl,
}
SWZ = "xyzw"


# <hand:signed-bitfield>
def _signed(x: int) -> int:
    x &= M32
    return x - (1 << 32) if x & 0x80000000 else x


def _sfbh(x: int) -> int:
    """firstbithigh on a signed int (Slang core module): for negative values
    the highest 0-bit; 0xFFFFFFFF for 0 and for -1."""
    v = _signed(x)
    if v < 0:
        v = ~v
    return M32 if v == 0 else v.bit_length() - 1


def _mask(bits: int) -> int:
    return M32 if bits >= 32 else (1 << bits) - 1


def _bfe(value: int, off: int, bits: int) -> int:
    return (value >> off) & _mask(bits)


def _bfi(base: int, ins: int, off: int, bits: int) -> int:
    m = (_mask(bits) << off) & M32
    return (base & ~m & M32) | ((ins << off) & m)
# </hand:signed-bitfield>

# Feature families: the generator can be restricted to one at a time, so a
# planted or real bug is attributed to a small set of constructs.
FAMILIES = {
    "arith": ["bin", "bin_vs", "neg", "not"],
    "divmod": ["div", "mod"],
    "shift": ["shl", "shr", "shl_vs", "shr_vs"],
    "select": ["select", "cmpcast", "minmax", "clamp"],
    "swizzle": ["swizzle", "construct", "broadcast"],
    "bits": ["unary"],
    "dot": ["dot"],
    # <hand:signed-bitfield>
    # Signed and mixed-signedness integer ops, written as uint4 -> int4 -> uint4
    # so the AST stays uint4-typed. Excluded because they are undefined for
    # signed ints in C++/MSL: left shift of negatives, overflow, INT_MIN / -1.
    "signed": ["sshr", "ushl_s", "scmp", "sminmax", "sfbh"],
    # bitfieldExtract / bitfieldInsert with runtime offset/bits,
    # offset + bits <= 32 (bits 0 and 32 included on purpose).
    "bitfield": ["bfe", "bfi"],
    # </hand:signed-bitfield>
}
ALL_KINDS = sorted({k for ks in FAMILIES.values() for k in ks})

# Plugin families (difftest/plugins.py loads them). A plugin adds node kinds and
# provides, for each one, a generator, a Slang printer and a Python reference,
# so plugin programs keep the one-AST-two-printers guarantee.
PLUGINS: dict = {}


def register_plugin(mod) -> None:
    for kind in mod.KINDS:
        if kind in ALL_KINDS or kind in PLUGINS:
            raise ValueError(f"plugin kind {kind!r} clashes with an existing kind")
        PLUGINS[kind] = mod
    FAMILIES[mod.NAME] = list(mod.KINDS)

# ------------------------------------------------------------------ generator


class Gen:
    def __init__(self, seed: int, kinds: list[str] | None = None, max_depth: int = 4):
        self.r = random.Random(seed)
        self.kinds = kinds or ALL_KINDS
        self.max_depth = max_depth

    @staticmethod
    def node(kind: str, ty: str, kids: list, arg=None) -> "Node":
        """Constructor exposed to plugins, which may not import this module."""
        return Node(kind, ty, kids, arg)

    def vec(self, depth: int = 0) -> Node:
        if depth >= self.max_depth or self.r.random() < 0.15 + 0.15 * depth:
            return leaf(self.r.choice("ab"))
        kind = self.r.choice(self.kinds)
        d = depth + 1
        r = self.r
        if kind in PLUGINS:
            return PLUGINS[kind].generate(self, kind, d)
        if kind == "bin":
            return Node("bin", "v", [self.vec(d), self.vec(d)], r.choice(list(BIN_OPS)))
        if kind == "bin_vs":   # vector op scalar (implicit broadcast)
            return Node("bin", "v", [self.vec(d), self.scalar(d)], r.choice(list(BIN_OPS)))
        if kind == "neg":
            return Node("neg", "v", [self.vec(d)])
        if kind == "not":
            return Node("not", "v", [self.vec(d)])
        if kind in ("div", "mod"):
            return Node(kind, "v", [self.vec(d), self.vec(d)])
        if kind in ("shl", "shr"):
            return Node(kind, "v", [self.vec(d), self.vec(d)])
        if kind in ("shl_vs", "shr_vs"):   # vector shifted by scalar (mixed)
            return Node(kind[:3], "v", [self.vec(d), self.scalar(d)])
        if kind == "select":
            return Node("select", "v", [self.vec(d), self.vec(d), self.vec(d), self.vec(d)],
                        r.choice(list(CMP_OPS)))
        if kind == "cmpcast":
            return Node("cmpcast", "v", [self.vec(d), self.vec(d)], r.choice(list(CMP_OPS)))
        if kind == "minmax":
            return Node(r.choice(["min", "max"]), "v", [self.vec(d), self.vec(d)])
        if kind == "clamp":
            return Node("clamp", "v", [self.vec(d), self.vec(d), self.vec(d)])
        if kind == "swizzle":
            return Node("swizzle", "v", [self.vec(d)], "".join(r.choice(SWZ) for _ in range(4)))
        if kind == "construct":   # uint4(v.xy, w.zw)
            return Node("construct", "v", [self.vec(d), self.vec(d)],
                        ("".join(r.choice(SWZ) for _ in range(2)),
                         "".join(r.choice(SWZ) for _ in range(2))))
        if kind == "broadcast":
            return Node("broadcast", "v", [self.scalar(d)])
        if kind == "unary":
            return Node("unary", "v", [self.vec(d)], r.choice(list(UNARY_FNS)))
        # <hand:signed-bitfield>
        if kind == "sshr":    # int4 >> uint4 (mixed signedness, arithmetic shift)
            return Node("sshr", "v", [self.vec(d), self.vec(d)])
        if kind == "ushl_s":  # uint4 << int4 (mixed signedness, amount 0..31)
            return Node("ushl_s", "v", [self.vec(d), self.vec(d)])
        if kind == "scmp":
            return Node("scmp", "v", [self.vec(d), self.vec(d), self.vec(d), self.vec(d)],
                        r.choice(list(CMP_OPS)))
        if kind == "sminmax":
            return Node("sminmax", "v", [self.vec(d), self.vec(d)], r.choice(["min", "max"]))
        if kind == "sfbh":
            return Node("sfbh", "v", [self.vec(d)])
        if kind in ("bfe", "bfi"):
            # value(s), then two scalars X, Y: offset = X & 31, bits = Y % (33 - offset)
            vals = [self.vec(d)] if kind == "bfe" else [self.vec(d), self.vec(d)]
            return Node(kind, "v", vals + [self.scalar(d), self.scalar(d)])
        # </hand:signed-bitfield>
        if kind == "dot":   # broadcast an integer dot product back to a vector
            return Node("broadcast", "v", [Node("dot", "u", [self.vec(d), self.vec(d)])])
        raise AssertionError(kind)

    def scalar(self, depth: int) -> Node:
        r = self.r
        if depth >= self.max_depth or r.random() < 0.5:
            if r.random() < 0.5:
                return leaf("s")
            return Node("comp", "u", [leaf(r.choice("ab"))], r.choice(SWZ))
        choice = r.random()
        d = depth + 1
        if choice < 0.4:
            return Node("bin", "u", [self.scalar(d), self.scalar(d)], r.choice(list(BIN_OPS)))
        if choice < 0.6 and "unary" in self.kinds:
            return Node("unary", "u", [self.scalar(d)], r.choice(list(UNARY_FNS)))
        if choice < 0.8 and "dot" in self.kinds:
            return Node("dot", "u", [self.vec(d), self.vec(d)])
        return Node("comp", "u", [self.vec(d)], r.choice(SWZ))


# ------------------------------------------------------------------ printers


def to_slang(n: Node) -> str:
    k, c = n.kind, n.kids
    p = to_slang
    if k == "var":
        return n.arg
    if k in PLUGINS:
        return PLUGINS[k].to_slang(n, p)
    if k == "bin":
        return f"({p(c[0])} {n.arg} {p(c[1])})"
    if k == "neg":
        return f"(-{p(c[0])})"
    if k == "not":
        return f"(~{p(c[0])})"
    if k == "div":
        return f"({p(c[0])} / ({p(c[1])} | 1u))"
    if k == "mod":
        return f"({p(c[0])} % ({p(c[1])} | 1u))"
    if k == "shl":
        return f"({p(c[0])} << ({p(c[1])} & 31u))"
    if k == "shr":
        return f"({p(c[0])} >> ({p(c[1])} & 31u))"
    if k == "select":
        return f"select({p(c[0])} {n.arg} {p(c[1])}, {p(c[2])}, {p(c[3])})"
    if k == "cmpcast":
        return f"uint4({p(c[0])} {n.arg} {p(c[1])})"
    if k in ("min", "max"):
        return f"{k}({p(c[0])}, {p(c[1])})"
    if k == "clamp":
        lo, hi = p(c[1]), p(c[2])
        return f"clamp({p(c[0])}, min({lo}, {hi}), max({lo}, {hi}))"
    if k == "swizzle":
        return f"({p(c[0])}).{n.arg}"
    if k == "construct":
        return f"uint4(({p(c[0])}).{n.arg[0]}, ({p(c[1])}).{n.arg[1]})"
    if k == "broadcast":
        return f"uint4({p(c[0])})"
    if k == "unary":
        return f"{n.arg}({p(c[0])})"
    if k == "dot":
        return f"dot({p(c[0])}, {p(c[1])})"
    if k == "comp":
        return f"({p(c[0])}).{n.arg}"
    # <hand:signed-bitfield>
    if k == "sshr":
        return f"uint4(int4({p(c[0])}) >> ({p(c[1])} & 31u))"
    if k == "ushl_s":
        return f"({p(c[0])} << int4({p(c[1])} & 31u))"
    if k == "scmp":
        return f"select(int4({p(c[0])}) {n.arg} int4({p(c[1])}), {p(c[2])}, {p(c[3])})"
    if k == "sminmax":
        return f"uint4({n.arg}(int4({p(c[0])}), int4({p(c[1])})))"
    if k == "sfbh":
        return f"firstbithigh(int4({p(c[0])}))"
    if k in ("bfe", "bfi"):
        x, y = p(c[-2]), p(c[-1])
        off = f"({x} & 31u)"
        bits = f"({y} % (33u - ({x} & 31u)))"
        if k == "bfe":
            return f"bitfieldExtract({p(c[0])}, {off}, {bits})"
        return f"bitfieldInsert({p(c[0])}, {p(c[1])}, {off}, {bits})"
    # </hand:signed-bitfield>
    raise AssertionError(k)


def evaluate(n: Node, env: dict):
    """Returns an int (uint) or a 4-tuple of ints (uint4)."""
    k, c = n.kind, n.kids
    e = lambda x: evaluate(x, env)  # noqa: E731

    def vmap(f, *xs):   # lift a scalar function over vector/scalar operands
        if any(isinstance(x, tuple) for x in xs):
            xs = [x if isinstance(x, tuple) else (x,) * 4 for x in xs]
            return tuple(f(*t) for t in zip(*xs))
        return f(*xs)

    if k == "var":
        return env[n.arg]
    if k in PLUGINS:
        return PLUGINS[k].evaluate(n, e, vmap)
    if k == "bin":
        return vmap(BIN_OPS[n.arg], e(c[0]), e(c[1]))
    if k == "neg":
        return vmap(lambda x: (-x) & M32, e(c[0]))
    if k == "not":
        return vmap(lambda x: (~x) & M32, e(c[0]))
    if k == "div":
        return vmap(lambda x, y: x // (y | 1), e(c[0]), e(c[1]))
    if k == "mod":
        return vmap(lambda x, y: x % (y | 1), e(c[0]), e(c[1]))
    if k == "shl":
        return vmap(lambda x, y: (x << (y & 31)) & M32, e(c[0]), e(c[1]))
    if k == "shr":
        return vmap(lambda x, y: x >> (y & 31), e(c[0]), e(c[1]))
    if k == "select":
        f = CMP_OPS[n.arg]
        return vmap(lambda x, y, t, u: t if f(x, y) else u, e(c[0]), e(c[1]), e(c[2]), e(c[3]))
    if k == "cmpcast":
        f = CMP_OPS[n.arg]
        return vmap(lambda x, y: 1 if f(x, y) else 0, e(c[0]), e(c[1]))
    if k == "min":
        return vmap(min, e(c[0]), e(c[1]))
    if k == "max":
        return vmap(max, e(c[0]), e(c[1]))
    if k == "clamp":
        return vmap(lambda x, p, q: min(max(x, min(p, q)), max(p, q)), e(c[0]), e(c[1]), e(c[2]))
    if k == "swizzle":
        v = e(c[0])
        return tuple(v[SWZ.index(ch)] for ch in n.arg)
    if k == "construct":
        v, w = e(c[0]), e(c[1])
        return tuple(v[SWZ.index(ch)] for ch in n.arg[0]) + tuple(w[SWZ.index(ch)] for ch in n.arg[1])
    if k == "broadcast":
        return (e(c[0]),) * 4
    if k == "unary":
        return vmap(UNARY_FNS[n.arg], e(c[0]))
    if k == "dot":
        v, w = e(c[0]), e(c[1])
        return sum(x * y for x, y in zip(v, w)) & M32
    if k == "comp":
        return e(c[0])[SWZ.index(n.arg)]
    # <hand:signed-bitfield>
    if k == "sshr":
        return vmap(lambda x, y: (_signed(x) >> (y & 31)) & M32, e(c[0]), e(c[1]))
    if k == "ushl_s":
        return vmap(lambda x, y: (x << (y & 31)) & M32, e(c[0]), e(c[1]))
    if k == "scmp":
        f = CMP_OPS[n.arg]
        return vmap(lambda x, y, t, u: t if f(_signed(x), _signed(y)) else u,
                    e(c[0]), e(c[1]), e(c[2]), e(c[3]))
    if k == "sminmax":
        f = min if n.arg == "min" else max
        return vmap(lambda x, y: f(_signed(x), _signed(y)) & M32, e(c[0]), e(c[1]))
    if k == "sfbh":
        return vmap(_sfbh, e(c[0]))
    if k in ("bfe", "bfi"):
        x, y = e(c[-2]), e(c[-1])
        off = x & 31
        bits = y % (33 - off)
        if k == "bfe":
            return vmap(lambda v: _bfe(v, off, bits), e(c[0]))
        return vmap(lambda b, i: _bfi(b, i, off, bits), e(c[0]), e(c[1]))
    # </hand:signed-bitfield>
    raise AssertionError(k)


def size(n: Node) -> int:
    return 1 + sum(size(k) for k in n.kids)


def kinds_in(n: Node) -> set[str]:
    out = {n.kind if n.kind != "unary" else f"unary:{n.arg}"}
    for k in n.kids:
        out |= kinds_in(k)
    return out
