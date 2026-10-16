"""Greedy AST reducer: shrink a failing function to a minimal reproducer.

Works on the generator's AST (not on text), so every candidate is still a
well-formed, well-defined program. A candidate replaces one subtree with a
leaf of the same type or with one of its own same-typed children; it is kept
only if the failure predicate still holds. Repeats until nothing shrinks.

Usage:
  python3 -m difftest.run fuzz --family bitfield --seed 3 --keep-work   # keep the batch dirs
  python3 -m difftest.reduce --family bitfield --seed 300013 --k 32 --oracle metal_reject
  python3 -m difftest.reduce --family signed --seed 300009 --k 32 --oracle cpp_invalid
"""

from __future__ import annotations

import argparse
import copy
import re
import subprocess
from pathlib import Path

from . import ast as A
from . import run as R

# Compile-time failures only. Value mismatches (silent miscompiles) were minimized by
# hand from the reducer's output; see FINDINGS.md.
ORACLES = ("metal_reject", "cpp_invalid")


def _paths(node, prefix=()):
    yield prefix, node
    for i, k in enumerate(node.kids):
        yield from _paths(k, prefix + (i,))


def _replace(root, path, new):
    if not path:
        return new
    root = copy.deepcopy(root)
    cur = root
    for i in path[:-1]:
        cur = cur.kids[i]
    cur.kids[path[-1]] = new
    return root


def _candidates(root):
    """Smaller trees: each subtree replaced by a leaf or one of its same-typed kids."""
    for path, node in _paths(root):
        if node.kind == "var":
            continue
        leaves = [A.leaf("a"), A.leaf("b")] if node.ty == "v" else [A.leaf("s")]
        for repl in [k for k in node.kids if k.ty == node.ty] + leaves:
            yield _replace(root, path, copy.deepcopy(repl))


class Predicate:
    """Does a single-function kernel built from `node` still fail the same way?"""

    def __init__(self, oracle: str, signature: str, workdir: Path):
        self.oracle, self.signature, self.dir = oracle, signature, workdir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.calls = 0

    def __call__(self, node) -> bool:
        self.calls += 1
        src = self.dir / "cand.slang"
        src.write_text(R.kernel_source([A.to_slang(node)]))
        if self.oracle == "metal_reject":
            msl = src.with_suffix(".metal")
            rc, _, _, _ = R.run(R.slangc_cmd(src, "metal", msl))
            if rc != 0:
                return False
            m = re.search(r"\[\[kernel\]\]\s*void\s+(\w+)", msl.read_text())
            inb = self.dir / "in.bin"
            inb.write_bytes(bytes(4 * R.IN_WORDS * R.THREADS))
            rc, _, err, _ = R.run([str(R.ROOT / "build/mtl_run"), str(msl), m.group(1), "0", "1",
                                   str(R.THREADS), str(inb), str(self.dir / "o.bin"), str(4 * R.THREADS)])
            return rc == 4 and self.signature in err
        if self.oracle == "cpp_invalid":
            cpp = src.with_suffix(".cpp")
            rc, _, _, _ = R.run(R.slangc_cmd(src, "cpp", cpp))
            if rc != 0:
                return False
            rc, _, err, _ = R.run(R.cpp_check_cmd(cpp))
            return rc != 0 and self.signature in err
        raise ValueError(self.oracle)


def reduce(node, pred: Predicate):
    assert pred(node), "the starting program does not fail"
    changed = True
    while changed:
        changed = False
        for cand in sorted(_candidates(node), key=A.size):
            if A.size(cand) < A.size(node) and pred(cand):
                node, changed = cand, True
                break
    return node


def failing_function(src_text: str, err: str) -> int:
    """Map an error's `kernel.slang:LINE` back to the function f<i> on that line."""
    lines = src_text.splitlines()
    for m in re.finditer(r"kernel\.slang:(\d+):", err):
        line = lines[int(m.group(1)) - 1]
        fm = re.match(r"uint4 f(\d+)\(", line)
        if fm:
            return int(fm.group(1))
    raise SystemExit("could not map the error to a function")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--family", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--k", type=int, default=32)
    ap.add_argument("--oracle", choices=ORACLES, required=True)
    ap.add_argument("--signature", default="", help="error text the failure must keep showing")
    args = ap.parse_args()

    kinds = A.FAMILIES[args.family] if args.family != "all" else None
    gen = A.Gen(args.seed, kinds)
    fns = [gen.vec() for _ in range(args.k)]
    batch = R.WORK / f"{args.family}-{args.seed}"
    src_text = (batch / "kernel.slang").read_text()

    # Find which function fails and the error signature to preserve.
    probe = Predicate(args.oracle, "", R.WORK / "reduce-probe")
    errtext = ""
    if args.oracle == "metal_reject":
        msl = batch / "kernel.metal"
        m = re.search(r"\[\[kernel\]\]\s*void\s+(\w+)", msl.read_text())
        _, _, errtext, _ = R.run([str(R.ROOT / "build/mtl_run"), str(msl), m.group(1), "0", "1",
                                  str(R.THREADS), str(batch / "in.bin"), str(probe.dir / "o.bin"),
                                  str(4 * R.THREADS * args.k)])
    else:
        _, _, errtext, _ = R.run(R.cpp_check_cmd(batch / "kernel.cpp"))
    idx = failing_function(src_text, errtext)
    sig = args.signature or re.search(r"error: ([^(]{0,60})", errtext).group(1).strip()
    print(f"failing function f{idx} (size {A.size(fns[idx])}); keeping error: {sig!r}")

    pred = Predicate(args.oracle, sig, R.WORK / f"reduce-{args.family}-{args.seed}")
    small = reduce(fns[idx], pred)
    print(f"reduced to size {A.size(small)} after {pred.calls} compiles:\n  {A.to_slang(small)}")


if __name__ == "__main__":
    main()
