# DRAFT — not filed. Target: https://github.com/shader-slang/slang/issues

**Title:** Metal (and likely C++/CUDA/HLSL): `bitfieldExtract`/`bitfieldInsert` with `bits == 32` return wrong results

**Versions:** slangc 2026.18.3 and 2026.19; expansion unchanged on `master` (3a93b38). **Platform:** Apple M4 Pro, macOS 26.6.2.

**Repro** (offset and bits come from a buffer at runtime; offset = 0, bits = 32)
```slang
outp[0] = bitfieldExtract(v, off, bits);        // expected v;   Metal returns 0
outp[1] = bitfieldInsert(base, ins, off, bits); // expected ins; Metal returns base
```
The SPIR-V path (MoltenVK and KosmicKrisp) returns the expected values. Widths 0, 1, 8, 16 and 31 are correct on Metal.

**Cause**
`emitBitfieldExtractImpl`/`emitBitfieldInsertImpl` build the mask as `(1 << bits) - 1`. For `bits` equal to the type width, that shift is undefined in MSL/C++ (on this GPU it behaves like a mask of 0). HLSL masks the shift amount, so there the mask is deterministically 0. GLSL and SPIR-V only make the result undefined when `offset + bits` *exceeds* the width, so `bits == 32` with `offset == 0` is a defined full-width operation.

**Possible fix:** a mask form that is defined at full width, e.g. for width W:
`((1 << (bits & (W-1))) - 1) | ((bits >> log2(W)) * ~0)`
This has no shift ≥ W and no wrapping subtraction. I verified it on Metal (execution), C++ (clang compile), DXIL (compile) and WGSL (generation only). A patch with a regression test is ready if useful.

Related: #5858 (add more bitfield tests). Existing tests use constant offsets/bits and never `bits == 32`.
