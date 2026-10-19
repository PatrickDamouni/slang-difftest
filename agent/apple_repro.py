"""D1: Apple's Metal compiler mishandles `extract_bits` on a signed value with a
constant bit count of 0, in hand-written MSL on native Metal (no Slang codegen
involved in the failing line).

  python3 -m agent.apple_repro

MSL 4.1 (p. 203, "Built-in integer functions") defines `extract_bits`: "If bits
is zero, the result is zero." Slang's own Metal output never calls
`extract_bits` (it expands bitfield ops by hand), so the native-Metal path in
the main harness passes. The bug shows up there only on the Vulkan paths,
because SPIRV-Cross lowers `OpBitFieldSExtract` to `extract_bits`. Here the
same call is written into the MSL directly, so native Metal shows it.

Each variant replaces the body of `f0` in a compiled kernel and prints what the
GPU returns. Expected output is 0 for every lane. The output buffer is filled
with 0xAB bytes beforehand, so 0xabababab means the store never happened.
"""

from __future__ import annotations

import re

from difftest import run as R

VARIANTS = [
    ("signed,   constant count 0", "return uint4(extract_bits(int4(b_0), 1u, 0u));"),
    ("signed,   constant count 0, offset 0", "return uint4(extract_bits(int4(b_0), 0u, 0u));"),
    ("signed scalar, constant count 0", "return uint4(uint(extract_bits(int(b_0.x), 1u, 0u)));"),
    ("unsigned, constant count 0 (control)", "return extract_bits(b_0, 1u, 0u);"),
    ("signed,   runtime count 0 (control)", "return uint4(extract_bits(int4(b_0), 1u, s_0));"),
]


def main():
    d = R.WORK / "apple-extract-bits"
    d.mkdir(parents=True, exist_ok=True)
    (d / "kernel.slang").write_text(R.kernel_source(["uint4(bitfieldExtract(int4(b), 1u, s))"]))
    (d / "in.bin").write_bytes(R.pack_inputs([((0, 0, 0, 0), (0x18, 0x39, 0xFFFFFFFF, 0x7), 0)] * R.THREADS))
    c = R.compile_batch(d / "kernel.slang")
    if not c.ok or c.msl is None:
        raise SystemExit(f"compile failed: {c.finding}")
    msl_path = c.msl
    msl = msl_path.read_text()
    head = re.compile(r"(\bf0(?:_\d+)?\s*\([^)]*\)\s*\{)(.*?)(\})", re.S)
    print("inputs b = (0x18, 0x39, 0xffffffff, 0x7); expected (0, 0, 0, 0)\n")
    for label, body in VARIANTS:
        m = head.search(msl)
        if m is None:
            raise SystemExit("f0 not found in the generated MSL")
        msl_path.write_text(msl[:m.end(1)] + "\n    " + body + "\n" + msl[m.start(3):])
        _, words, _, err = R.run_path("metal", c, d / "in.bin", 4 * R.THREADS)
        got = [hex(x) for x in words[:4]] if words else err.strip()[:120]
        ok = words is not None and words[:4] == [0, 0, 0, 0]
        print(f"  {label:40s} {'PASS' if ok else 'FAIL'}  {got}\n      {body}")


if __name__ == "__main__":
    main()
