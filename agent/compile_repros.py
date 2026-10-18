"""Compile-time reproducers for the agents' findings: invalid SPIR-V, C++ that does
not build, compiler crashes, and miscompiles visible in the generated source.

  python3 -m agent.compile_repros            # the Vulkan SDK's slangc
  SLANGC=/path/to/slangc python3 -m agent.compile_repros

Each case compiles a tiny kernel and checks for a specific symptom. It prints
REPRODUCED or NOT REPRODUCED per case, so the output is the evidence.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from difftest import run as R

HEAD = "RWStructuredBuffer<uint4> outp;\nRWStructuredBuffer<uint4> inp;\n" \
       "[shader(\"compute\")][numthreads(1, 1, 1)]\nvoid main(uint3 tid : SV_DispatchThreadID)\n{\n"

CASES = [
    {"id": "5", "agent": "r1b", "title": "firstbitlow(int16_t) -> invalid SPIR-V (FindILsb operand width)",
     "body": "outp[0] = uint4(firstbitlow(int16_t(inp[0].x)));", "target": "spirv",
     "symptom": ("spirv-val", r"FindILsb")},
    {"id": "6", "agent": "r1d", "title": "bitfieldExtract on vector<uint16_t,4> -> invalid SPIR-V",
     "body": "outp[0] = uint4(bitfieldExtract(vector<uint16_t,4>(inp[0]), inp[1].x & 7u, 3u));", "target": "spirv",
     "symptom": ("spirv-val", r"BitFieldUExtract")},
    {"id": "7a", "agent": "r1b", "title": "C++: firstbithigh(int16_t) / reversebits(uint8_t) helpers undeclared",
     "body": "outp[0] = uint4(uint(firstbithigh(int16_t(inp[0].x))) + uint(reversebits(uint8_t(inp[0].y))));",
     "target": "cpp", "symptom": ("clang", r"undeclared identifier '(I16_firstbithigh|U8_reversebits)'")},
    {"id": "7b", "agent": "r2b", "title": "C++: sign(int) emits undeclared I32_sign",
     "body": "outp[0] = uint4(sign(int(inp[0].x)));", "target": "cpp",
     "symptom": ("clang", r"undeclared identifier 'I32_sign'")},
    {"id": "11", "agent": "r2c", "title": "bit_cast<uint4>(float2x2) indexes m[0][2] -> invalid SPIR-V",
     "body": "float4 v = asfloat(inp[0]); float2x2 m = float2x2(v.x, v.y, v.z, v.w); outp[0] = bit_cast<uint4>(m);",
     "target": "spirv", "symptom": ("spirv-val", r"out of bounds")},
    {"id": "12", "agent": "r2c", "title": "bit_cast<uint64_t>(struct {u16; u8; u32}) drops the low 16 bits of the u32",
     "pre": "struct S { uint16_t a; uint8_t c; uint b; };\n",
     "body": "S s; s.a = uint16_t(inp[0].x); s.c = uint8_t(inp[0].y); s.b = inp[0].z; "
             "uint64_t r = bit_cast<uint64_t>(s); outp[0] = uint4(uint(r), uint(r >> 32), 0, 0);",
     "target": "metal", "symptom": ("source", r"\(\(s_0\.b_0\) >> 16U\) & 65535U\) << 24U")},
    {"id": "13", "agent": "r2c", "title": "bit_cast<uint4>(struct {uint; uint64_t}) crashes slangc",
     "pre": "struct T { uint a; uint64_t b; };\n",
     "body": "T t; t.a = inp[0].x; t.b = uint64_t(inp[0].y) | (uint64_t(inp[0].z) << 32); outp[0] = bit_cast<uint4>(t);",
     "target": "spirv", "symptom": ("crash", None)},
]


def check(case: dict, work: Path) -> tuple[bool, str]:
    src = work / f"c{case['id']}.slang"
    src.write_text(case.get("pre", "") + HEAD + "    " + case["body"] + "\n}\n")
    ext = {"spirv": "spv", "cpp": "cpp", "metal": "metal"}[case["target"]]
    out = work / f"c{case['id']}.{ext}"
    p = subprocess.run([str(R.SLANGC), str(src), "-target", case["target"], "-entry", "main",
                        "-stage", "compute", "-o", str(out)], capture_output=True, text=True, timeout=120)
    kind, pattern = case["symptom"]
    if kind == "crash":
        return p.returncode < 0 or p.returncode > 128, f"slangc exit status {p.returncode}"
    if p.returncode != 0:
        return False, "slangc rejected the kernel: " + p.stderr.strip()[-200:]
    if kind == "spirv-val":
        v = subprocess.run([str(R.SPIRV_VAL), str(out)], capture_output=True, text=True)
        text = v.stdout + v.stderr
    elif kind == "clang":
        v = subprocess.run(R.cpp_check_cmd(out), capture_output=True, text=True)
        text = v.stdout + v.stderr
    else:
        text = out.read_text()
    m = re.search(pattern, text)
    first = next((line for line in text.splitlines() if re.search(pattern, line)), "")
    return bool(m), first.strip()[:200]


def main():
    if os.environ.get("SLANGC"):
        R.SLANGC = Path(os.environ["SLANGC"])
    print(f"slangc: {R.slangc_version()}  ({R.SLANGC})\n")
    missing = 0
    with tempfile.TemporaryDirectory() as d:
        for case in CASES:
            ok, detail = check(case, Path(d))
            missing += not ok
            print(f"[{case['id']}] ({case['agent']}) {case['title']}\n     "
                  f"{'REPRODUCED' if ok else 'NOT REPRODUCED'}  {detail}\n")
    print(f"{len(CASES) - missing}/{len(CASES)} reproduced")


if __name__ == "__main__":
    main()
