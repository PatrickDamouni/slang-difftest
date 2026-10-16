# DRAFT PR — not opened. Target: shader-slang/slang, label "pr: non-breaking"

**Before opening:**
1. Read and understand every line of `0001-…patch`. Slang's CONTRIBUTING.md requires that you "exercised sufficient review, judgment, and creative direction" over AI-assisted code.
2. File issues A and B first (or reference them here).
3. Comment `/format` on the PR to let the bot fix formatting.

---

**Title:** Fix bitfieldInsert parenthesization and full-width bitfield masks in C-like emitters

## Motivation

Consider this compute shader, where `off = 4` and `bits = 8` come from a buffer:

```slang
uint4 a = uint4(0x11111111, 0x22222222, 0x33333333, 0x44444444);
uint4 b = uint4(0xA, 0xB, 0xC, 0xD);
outputBuffer[0] = bitfieldInsert(a, b, off, bits).y;   // expected 0x222220B2
```

On Metal this writes `0x111110A1`, `0x222220B2`, … in the wrong lanes. With `.xy` instead of `.y`, the Metal compiler rejects the generated MSL, and `-target cpp` produces C++ that does not compile (`'Vector<uint32_t,4>' | 'unsigned int'`). The SPIR-V path is correct.

Separately, with `off = 0` and `bits = 32`, `bitfieldExtract(x, off, bits)` returns `0` on Metal instead of `x`, and `bitfieldInsert(base, ins, off, bits)` returns `base` instead of `ins`. GLSL and SPIR-V define these full-width operations; only `offset + bits` *greater than* the width is undefined. The SPIR-V path is again correct.

Both were found by a differential tester that compares Slang's Metal output, its SPIR-V output (on MoltenVK and KosmicKrisp) and a reference interpreter on random well-defined programs. Both reproduce with 2026.18.3, 2026.19 and `master` (3a93b38).

## Proposed solution

Both problems are in the text expansions that `CLikeSourceEmitter` uses for `kIROp_BitfieldInsert` and `kIROp_BitfieldExtract` on targets without a native instruction.

1. **Parenthesization.** The insert expansion's top-level operator is `|`. When the instruction is folded into a use with a postfix operator, `emitOperand` appends `.y` to the emitted text, so the swizzle binds to the right operand only. The extract expansion is already fully parenthesized, which is why `bitfieldExtract(...).y` is correct. We wrap the whole insert expansion in parentheses the same way.
2. **Full-width mask.** Both expansions build the mask as `(1 << bits) - 1`. When `bits` equals the bit width, the shift amount equals the width, which is undefined in C, C++, MSL and HLSL. We replace it with a new helper, `_emitBitfieldMask`, that emits `((1 << (bits & (W-1))) - 1) | (0 - (bits >> log2 W))`. The second term is all ones exactly when `bits == W`, so no shift reaches the width. The expression needs no conditional operator, so it stays valid for WGSL, which shares this code.

The IR instruction is the correct, canonical input. The malformed output is produced entirely by the emitter's own expansion, so the emitter is the layer that owns this fix.

## Change summary

| File | Change |
|---|---|
| `source/slang/slang-emit-c-like.cpp` | Adds `_emitBitfieldMask`. `emitBitfieldExtractImpl` and `emitBitfieldInsertImpl` use it for every mask. `emitBitfieldInsertImpl` wraps its expansion in parentheses. |
| `tests/language-feature/bitfield/bitfield-full-width-and-swizzle.slang` | New regression test, run on all six targets like the existing bitfield tests. Offset and width come from a buffer so they are not constant-folded. Covers full-width extract/insert (unsigned and signed), `bitfieldInsert(...).y` and `.xy`, and ordinary widths. |

## Concepts and vocabulary

- **Folding into use sites:** a pure, single-use instruction is emitted inline inside its user's expression instead of as a named temporary. That is why the expansion's own precedence matters here.
- **`emitVecNOrScalar`:** wraps a scalar expression in a vector constructor for vector types; the masks are emitted through it.

## Process report

- **Parentheses (insert).** Before: the swizzle test above produces `((base)&~mask)|(((ins)&m)<<off).y` in MSL. After: `(((base)&~mask)|(((ins)&m)<<off)).y`. We verified this on Metal (Apple M4 Pro, macOS 26.6.2) by running the shader: all lanes are now `0x222220B2`. Without this change, the `.y`/`.xy` checks in the new test fail on Metal, and `-target cpp` fails to compile.
- **Full-width mask (extract and insert).** Without `_emitBitfieldMask`, the test's first three checks fail on Metal (`0`, `DEADBEEF`, `0`). With it, they pass. Ordinary widths (`EE`, `DEADB0DF`) are unchanged.
- **Other targets.**
  - **Tested by the author:** Metal execution (all 8 checks pass), `-target cpp` compiles with clang, `-target dxil` compiles for both bitfield tests, `-target wgsl` generates for `bitfield-extract.slang`.
  - **Not tested:** CUDA, D3D12 and WGPU execution. These are left to CI.
  - **Pre-existing, unchanged by this patch:** `bitfield-insert-extract-non32bit.slang` already fails `-target dxil` with "unexpected IR opcode", and that test only declares `-vk`.
- **Fuzzing.** On 1,280 random programs the unpatched compiler gives 117 Metal value mismatches and 12 Metal compile rejections. The patched compiler gives 0 of each. Remaining findings are the known #13553 C++ shift issue and an unrelated KosmicKrisp bug.
- **Known limitation, not changed.** The signed-extract sign extension still shifts by `(W - bits)`, which is a full-width shift when `bits == 0`. It produced the correct result on Metal in testing, so this PR leaves it alone rather than change behaviour without a failing test.
