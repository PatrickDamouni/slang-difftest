# Findings

All five reproducers run with one command (`python3 -m difftest.repros`). They print expected vs. actual on native Metal, KosmicKrisp and MoltenVK. Environment: Apple M4 Pro, macOS 26.6.2, Vulkan SDK 1.4.363.0 (KosmicKrisp `vulkan-sdk-1.4.363`, MoltenVK 1.4.2).

| ID | Component | Kind | Still present in |
|---|---|---|---|
| **A** | Slang: `bitfieldInsert` emission | **silent wrong result** (Metal); compile errors (Metal `.xy`, C++) | slangc 2026.18.3 (SDK), **2026.19 (latest release)**, source at `master` `3a93b38` |
| **B** | Slang: bitfield mask at full width | **silent wrong result** (Metal) | same three |
| **C** | Mesa KosmicKrisp: integer ops emitted as signed `int` | **silent wrong result** | KosmicKrisp in SDK 1.4.363.0; same emission code on Mesa `main` (fetched 2026-10-16) |
| known | Slang #13553: mixed-signedness vector shifts, C++ target | compile error | rediscovered automatically by the `signed` family (fix PR #13684 still open) |

None of A, B, C matched an existing issue in searches of the [Slang tracker](https://github.com/shader-slang/slang/issues) (searched: "bitfieldInsert metal", "bitfieldExtract 32", "bitfield full width", "metal precedence", etc.). The closest are [#10299](https://github.com/shader-slang/slang/pull/10299) (same emitter, different issue) and [#5858](https://github.com/shader-slang/slang/issues/5858) (open: add more bitfield tests). Mesa/KosmicKrisp's tracker has not been searched exhaustively yet.

---

## A. Slang: `bitfieldInsert(...)` followed by a swizzle is mis-parenthesised

**Minimal repro.** Here `s = 356` gives offset 4, bits 8:

```slang
uint4(bitfieldInsert(a, b, (s & 31u), (s % (33u - (s & 31u)))).y)
```

| path | result |
|---|---|
| expected | `0x222220b2` ×4 |
| KosmicKrisp, MoltenVK (Slang → SPIR-V) | `0x222220b2` ×4 ✓ |
| **native Metal (Slang → MSL)** | **`0x111110b1, 0x222220b2, 0x333330b3, 0x444440b4`** ✗ |

`.xy` instead of `.y` makes Apple's Metal compiler reject the MSL ("implicit conversions between vector types"). On the C++ target the generated code fails to compile (`invalid operands to binary expression ('Vector<uint32_t, 4>' and 'unsigned int')`).

**Root cause.** `CLikeSourceEmitter::emitBitfieldInsertImpl` (`source/slang/slang-emit-c-like.cpp`) prints `((base)&~mask)|(((insert)&m)<<off)`. Its top-level operator is `|`, and the function never wraps the whole expansion in parentheses. When the instruction is folded into a use site that applies a swizzle, the emitter appends `.y` to the text, and `.y` binds only to the right operand of `|`. That yields `(base & ~mask) | ((insert & m) << off).y`, and `uint4 | uint` silently broadcasts. `emitBitfieldExtractImpl` wraps its expansion in parentheses, so `bitfieldExtract(...).y` is correct. That is consistent with this diagnosis.

**Why tests miss it.** `tests/language-feature/bitfield/bitfield-extract.slang` reads components only after storing the result in a variable (`int4 ext4 = ...; ext4.x`), so the expression is never folded under a swizzle.

## B. Slang: full-width bitfields (`bits == 32`) produce wrong results

```slang
bitfieldExtract(x, 0, 32)          // expected x       -> Metal returns 0
bitfieldInsert(base, ins, 0, 32)   // expected ins     -> Metal returns base
```

Offset/bits are runtime values (`s = 32` gives offset 0, bits 32). The SPIR-V paths (native `OpBitField*Extract/Insert`) are correct; native Metal is wrong in every lane. All other widths tested (0, 1, 8, 16, 31) are correct.

**Root cause.** Both C-like expansions build the mask as `(1u << bits) - 1u`. For `bits == 32` that shifts by the full bit width, which is undefined in MSL/C++. On this GPU it evaluates as if the mask were 0. GLSL and SPIR-V only leave the result undefined when `offset + bits` *exceeds* the width. `bits == 32` with `offset == 0` is a defined, full-width operation, and Slang's own SPIR-V path handles it.

**Likely scope (not tested here).** The same emitter is used for C++, CUDA and HLSL output, so those targets probably share the bug. Only Metal was executed.

## C. KosmicKrisp: integer arithmetic is emitted as signed `int`, so overflow is undefined behaviour

```slang
uint4(b == -b)                                   // b.x = 0x80000000: expected 1 (wraps), KosmicKrisp 0
uint4(int4(b) + int4(1,1,1,1) > int4(b))         // b.x = INT_MAX:    expected 0 (wraps), KosmicKrisp 1
```

Native Metal and MoltenVK are correct. The same expression gets the right answer on KosmicKrisp when the `0` is only known at run time (`b != (a - b)` with `a = 0`). So this is a compile-time simplification, not a hardware problem.

**Root cause.** In the MSL that KosmicKrisp generates (`MESA_KK_DEBUG=msl`), the values are loaded as `int`, then `t47 = -t28; t51 = t28 == t47;`. Signed overflow is undefined in MSL (C++ rules), so Apple's compiler may rewrite `x == -x` to `x == 0` and `x + 1 > x` to `true`. In Vulkan/SPIR-V and NIR, integer arithmetic wraps. Mesa's `src/kosmickrisp/compiler/nir_to_msl.c` emits `nir_op_ineg` as `-` and `iadd`/`isub`/`imul` as `+ - *`, and `msl_type_inference.c` can type those operations as `TYPE_INT`. A likely fix is to perform integer arithmetic on unsigned types (`as_type<uint>`) and cast back.

**Why it matters.** Hashes, RNGs and overflow checks in real shaders depend on wrapping arithmetic.

---

## How these were found

| family | functions | findings |
|---|---:|---|
| arith, shift, divmod, select, swizzle, bits, dot (uint/uint4 only) | 3,840+ | **0**, the expected null result for pass-through arithmetic |
| bitfield | 320 | A, B, plus C++ compile errors (A) |
| signed | 320 | #13553 rediscovered (C++ compile errors) |
| all (mixed) | 1,280 | 117 Metal mismatches (**all** involve bitfield ops → A/B); **3 KosmicKrisp mismatches → C** |

The harness proves itself before it reports anything (`make selftest`):
- a clean batch yields 0 findings;
- a fault planted in the Metal path only is caught and blamed on "Slang Metal codegen";
- a fault planted in the SPIR-V path only is blamed on "Slang SPIR-V codegen".

## Verified fix for A and B

I patched `source/slang/slang-emit-c-like.cpp` on Slang `master` (3a93b38), built `slangc` from source and re-ran everything. The patch and a PR description are in `docs/upstream/`, **not submitted**.

| check | unpatched master | patched |
|---|---|---|
| `python3 -m difftest.repros` (A, B1, B2) | FAIL ×3 | **PASS ×3** (C is a KosmicKrisp bug and still fails, as expected) |
| bitfield family, 320 functions (same seeds) | 129 Metal mismatches, 10 Metal rejects, 10 C++ errors | **0 findings** |
| all families, 1,280 functions (same seeds) | 117 Metal mismatches, 12 rejects | **0 Metal findings**; the 40 C++ errors left are all #13553 (signed/unsigned vector mismatch) |
| new regression test `bitfield-full-width-and-swizzle.slang`, run on Metal | does not compile | **8/8 checks pass** |
| Slang's own `bitfield-extract.slang` | — | compiles for Metal, C++, DXIL, WGSL |

## Not claimed

- **No upstream reports filed yet.** Drafts are in `docs/issue-drafts/` for review.
- **B was not executed on CUDA, HLSL or C++.** Only the Metal path was run; the other targets are inferred from shared emitter code.
- **C's fix is a proposal.** I haven't built Mesa.
- **The CPU target is checked for compiling only, not for correct results.**
