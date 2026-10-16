"""Minimal reproducers for every finding, runnable in one command.

  python3 -m difftest.repros            # uses the Vulkan SDK's slangc
  SLANGC=/path/to/slangc python3 -m difftest.repros   # e.g. a newer release

Each case is a tiny Slang function run through the harness on native Metal,
KosmicKrisp and MoltenVK, compared with an independently computed expected
value. Prints PASS/FAIL per path, so the report is the evidence.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import run as R

M = 0xFFFFFFFF


def s32(x):
    return x - (1 << 32) if x & 0x80000000 else x


A = (0x11111111, 0x22222222, 0x33333333, 0x44444444)
B = (0x0000000A, 0x0000000B, 0x0000000C, 0x0000000D)

# s = 356 -> offset = s & 31 = 4, bits = s % (33 - 4) = 8
OFF_BITS = "(s & 31u), (s % (33u - (s & 31u)))"
CASES = [
    {
        "id": "A",
        "title": "Slang: bitfieldInsert(...).y on Metal (missing parentheses)",
        "expr": f"uint4(bitfieldInsert(a, b, {OFF_BITS}).y)",
        "a": A, "b": B, "s": 356,
        # insert 8 bits of b.y at offset 4 into a.y, then broadcast lane y
        "expect": ((A[1] & ~(0xFF << 4) & M) | ((B[1] << 4) & (0xFF << 4)),) * 4,
    },
    {
        "id": "B1",
        "title": "Slang: bitfieldExtract(x, 0, 32) on Metal (full-width mask)",
        # s = 32 -> offset 0, bits 32 % 33 = 32
        "expr": f"bitfieldExtract(a, {OFF_BITS})",
        "a": (0xDEADBEEF, 0x12345678, 0xFFFFFFFF, 0x80000001), "b": B, "s": 32,
        "expect": (0xDEADBEEF, 0x12345678, 0xFFFFFFFF, 0x80000001),
    },
    {
        "id": "B2",
        "title": "Slang: bitfieldInsert(base, ins, 0, 32) on Metal (full-width mask)",
        "expr": f"bitfieldInsert(a, b, {OFF_BITS})",
        "a": (0xDEADBEEF, 0x12345678, 0xFFFFFFFF, 0x80000001), "b": B, "s": 32,
        "expect": B,
    },
    {
        "id": "C1",
        "title": "KosmicKrisp: x == -x at INT_MIN (signed-overflow UB in emitted MSL)",
        "expr": "uint4(b == -b)",
        "a": (0, 0, 0, 0), "b": (0x80000000, 0, 1, 0x7FFFFFFF), "s": 0,
        "expect": (1, 1, 0, 0),
    },
    {
        "id": "C2",
        "title": "KosmicKrisp: int(x) + 1 > int(x) at INT_MAX",
        "expr": "uint4(int4(b) + int4(1, 1, 1, 1) > int4(b))",
        "a": (0, 0, 0, 0), "b": (0x7FFFFFFF, 5, 0x80000000, 0x40000000), "s": 0,
        "expect": tuple(int(s32((x + 1) & M) > s32(x)) for x in (0x7FFFFFFF, 5, 0x80000000, 0x40000000)),
    },
]


def main():
    if os.environ.get("SLANGC"):
        R.SLANGC = Path(os.environ["SLANGC"])
    ver = R.run([str(R.SLANGC), "-version"])
    print(f"slangc: {(ver[1] or ver[2]).strip()}  ({R.SLANGC})\n")
    failures = 0
    for case in CASES:
        d = R.WORK / f"repro-{case['id']}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "kernel.slang").write_text(R.kernel_source([case["expr"]]))
        (d / "in.bin").write_bytes(R.pack_inputs([(case["a"], case["b"], case["s"])] * R.THREADS))
        c = R.compile_batch(d / "kernel.slang")
        print(f"[{case['id']}] {case['title']}\n     {case['expr']}")
        print(f"     expected     {[hex(x) for x in case['expect']]}")
        for path in R.PATHS:
            status, words, _, err = R.run_path(path, c, d / "in.bin", 4 * R.THREADS)
            got = tuple(words[:4]) if words else None
            ok = got == tuple(case["expect"])
            failures += not ok
            shown = [hex(x) for x in got] if got else f"{status}: {err.splitlines()[0] if err else ''}"
            print(f"     {path:12s} {'PASS' if ok else 'FAIL'}  {shown}")
        print()
    print(f"{failures} failing (case, path) pairs")


if __name__ == "__main__":
    main()
