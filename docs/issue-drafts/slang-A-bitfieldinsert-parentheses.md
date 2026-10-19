# DRAFT — not filed. Target: https://github.com/shader-slang/slang/issues

**Title:** Metal/C++: `bitfieldInsert(...)` followed by a swizzle is mis-parenthesized (silent wrong results on Metal)

**Versions:** slangc 2026.18.3 (Vulkan SDK 1.4.363.0) and 2026.19; `emitBitfieldInsertImpl` unchanged on `master` (3a93b38).
**Platform:** Apple M4 Pro, macOS 26.6.2.

**Repro**
```slang
RWStructuredBuffer<uint4> outp;
StructuredBuffer<uint4> inp;
[shader("compute")] [numthreads(1,1,1)]
void main() {
    uint4 a = inp[0]; uint4 b = inp[1]; uint off = inp[2].x; uint bits = inp[2].y;
    outp[0] = uint4(bitfieldInsert(a, b, off, bits).y);
}
```
With a = (0x11111111, 0x22222222, 0x33333333, 0x44444444), b = (0xA, 0xB, 0xC, 0xD), off = 4, bits = 8:
- expected: `0x222220b2` in every lane (Vulkan/SPIR-V gives this)
- Metal: `0x111110b1, 0x222220b2, 0x333330b3, 0x444440b4`

A scalar store (`uint y = bitfieldInsert(a, b, off, bits).y;`) or `.xy` makes the Metal compiler reject the generated MSL ("cannot initialize a variable of type 'uint' with an rvalue of type 'uint4'" / "implicit conversions between vector types"). With `-target cpp`, the generated C++ fails to compile (`invalid operands to binary expression ('Vector<uint32_t, 4>' and 'unsigned int')`).

**Cause**
`CLikeSourceEmitter::emitBitfieldInsertImpl` emits `((base)&~mask)|(((insert)&m)<<off)` without outer parentheses. When the instruction is folded into a swizzle, the output is `... | (... << off).y`, so `.y` binds only to the right operand of `|`. `emitBitfieldExtractImpl` wraps its expansion in parentheses, and `bitfieldExtract(...).y` is correct.

**Suggested fix:** wrap the whole `emitBitfieldInsertImpl` expansion in parentheses.

Found with a differential tester (Metal vs. SPIR-V on MoltenVK and KosmicKrisp vs. a reference). Reproducer: https://github.com/PatrickDamouni/slang-difftest (`python3 -m difftest.repros`). A patch with a regression test is ready if useful.
