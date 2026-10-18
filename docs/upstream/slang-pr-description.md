**Title:** Fix parenthesization and full-width masks in the C-like bitfield expansions

## Motivation

Consider this shader, where `off = 4` and `bits = 8` come from a buffer at runtime:

```slang
uint4 a = uint4(0x11111111, 0x22222222, 0x33333333, 0x44444444);
uint4 b = uint4(0xA, 0xB, 0xC, 0xD);
uint4 r = uint4(bitfieldInsert(a, b, off, bits).y);  // expected 0x222220B2 in every lane
```

- On Metal, `r` is `0x111110B1, 0x222220B2, 0x333330B3, 0x444440B4`.
- A scalar use such as `uint y = bitfieldInsert(a, b, off, bits).y;` fails to compile on Metal, and so does `.xy`.
- With `-target cpp` the generated C++ does not compile: `'Vector<uint32_t, 4>' | 'unsigned int'`.
- The SPIR-V path is correct.

Separately, with `off = 0` and `bits = 32`:
- `bitfieldExtract(x, off, bits)` returns `0` on Metal instead of `x`;
- `bitfieldInsert(base, ins, off, bits)` returns `base` instead of `ins`.

GLSL and SPIR-V only leave the result undefined when `offset + bits` exceeds the width, so these full-width operations are defined, and the SPIR-V path returns the expected values.

Third, the offset and bit-count operands are spliced into the expansion without parentheses. With `v = 0xDEADBEEF`, `off = 4`, `bits = 8` at runtime:
- `bitfieldExtract(v, off, bits | 4u)` returns `0x102` on Metal instead of `0xBEE` (`1<<bits | 4` parses as `(1<<bits) | 4`);
- `bitfieldInsert(v, ins, off | 1u, 4u)` returns `0xDEADBFAE` instead of `0xDEADBFAF`;
- `uint(bitfieldExtract(int(v), 0, bits + 20u))` returns `0xFFFDBEEF` instead of `0xFEADBEEF` (`32-bits + 20` instead of `32-(bits+20)`).

Both problems reproduce with 2026.18.3, 2026.19 and `master` (3a93b38). They were found by a differential tester that compares Slang's Metal output, its SPIR-V output (run on MoltenVK and KosmicKrisp) and a reference evaluator over randomly generated well-defined functions.

## Proposed solution

Both problems are in the text expansions `CLikeSourceEmitter` uses for `kIROp_BitfieldInsert` and `kIROp_BitfieldExtract`. The IR instructions are fine; the malformed output comes entirely from the emitter's expansion, so the emitter is the layer that owns the fix.

1. **Parenthesization.** The insert expansion's top-level operator is `|`. When the instruction is folded into a use with a postfix operator, the swizzle is appended to the emitted text and binds only to the right operand. The extract expansion is already fully parenthesized, which is why `bitfieldExtract(...).y` is correct. We wrap the insert expansion the same way.
2. **Full-width mask.** Both expansions build the mask as `(1 << bits) - 1`. When `bits` equals the bit width, the shift amount equals the width: that is undefined in C, C++ and MSL, and HLSL masks the shift amount so the mask becomes 0. We add `_emitBitfieldMask`, which emits `((1 << (bits & (W-1))) - 1) | ((bits >> log2 W) * ~0)`.
   - The second term is all ones exactly when `bits == W`.
   - No shift reaches the width, and nothing can wrap, so a literal `bits` also stays a valid WGSL constant expression.
   - One branch-free form serves every C-like target that shares this code.
3. **Operand parentheses.** Every offset and bit-count operand is now emitted inside parentheses. The expansion is fixed text with operators of its own (`<<`, `-`, `&`), so an operand whose top-level operator binds more loosely must be grouped explicitly; `emitOperand(..., EmitOp::General)` alone does not do that.

**Alternative considered:** lowering to native intrinsics where they exist (`extract_bits`/`insert_bits` in MSL, `extractBits`/`insertBits` in WGSL). That would need per-target changes and per-target edge-case auditing, so this PR keeps the shared expansion correct instead. I'm happy to switch if maintainers prefer the intrinsic lowering.

## Change summary

| File | Change |
|---|---|
| `source/slang/slang-emit-c-like.cpp` | Adds `_emitBitfieldMask`. `emitBitfieldExtractImpl` and `emitBitfieldInsertImpl` use it for every mask and parenthesize every spliced operand. `emitBitfieldInsertImpl` also parenthesizes its whole expansion. |
| `tests/language-feature/bitfield/bitfield-full-width-and-swizzle.slang` | New regression test with the same targets as the existing bitfield tests. Offset and width come from a buffer so they are not constant-folded. Covers full-width extract/insert (unsigned and signed), `bitfieldInsert(...).y` and `.xy`, operands with low-precedence operators (`bits | 4u`, `off | 1u`, `bits + 20u`), and ordinary widths. CHECK lines are anchored with `{{^}}…{{$}}` so a value can't match as a substring. |

## Concepts and vocabulary

- **Folding into use sites:** a pure, single-use instruction is emitted inline in its user's expression rather than as a temporary. That is why the expansion's own precedence matters.

## Process report

- **Parentheses.** With the example above, unpatched MSL contains `((base)&~mask)|(((ins)&m)<<off).y`; patched MSL contains `(((base)&~mask)|(((ins)&m)<<off)).y`. On Metal (Apple M4 Pro, macOS 26.6.2) the new test's `.y`/`.xy` checks fail to compile unpatched and produce `222220B2`, `111110A1`, `222220B2` patched.
- **Mask.** Unpatched, the test's three full-width checks give `0`, `DEADBEEF`, `0` on Metal; patched, they give `DEADBEEF`, `BADF00D`, `DEADBEEF`. Ordinary widths (`EE`, `DEADB0DF`) are unchanged.
- **Other targets.**
  - **Run locally:** Metal execution (8/8 checks), `-target cpp` (compiles with clang), `-target dxil` (compiles), `-target wgsl` (generates).
  - **Not run locally:** CUDA, D3D12 and WGPU execution, and validation of the WGSL output. These are left to CI.
  - **Pre-existing, unchanged by this patch:** `bitfield-insert-extract-non32bit.slang` fails `-target dxil` with "unexpected IR opcode" with or without it, and that test declares only `-vk`.
- **Operand parentheses.** The three new checks give `0x102`, `0xDEADBFAE`, `0xFFFDBEEF` on Metal unpatched and `0xBEE`, `0xDEADBFAF`, `0xFEADBEEF` patched.
- **Fuzzing.** On 1,280 generated functions (40 kernels) the unpatched compiler gives 117 Metal value mismatches and 12 Metal compile rejections; the patched compiler gives 0 of each. On 320 bitfield-only functions: 129 mismatches and 10 rejections unpatched, 0 patched. A second generator that varies the operand expressions more widely produced 946 findings in 1,280 functions: the mask and outer-parentheses fixes alone remove 480 of them, and adding operand parentheses removes 945. The remaining one is downstream of Slang (see below).
- **Not changed.** Two edge cases still shift by the full width in the expansion:
  - the value shift when `offset == 32` (with `bits == 0`);
  - the signed-extract sign extension when `bits == 0`.

  Both produced correct results on Metal in testing. A version of this patch that also masked these shift amounts to `W-1` changed no fuzzing outcome (the same 12 findings were fixed with or without it), so this PR leaves them alone rather than change behaviour without a failing test.
