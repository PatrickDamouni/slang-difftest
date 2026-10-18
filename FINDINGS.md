# Findings

Reproduce everything with `python3 -m difftest.repros` (expected vs. actual per path) and the logs in [`results/evidence/`](results/evidence/). Environment: Apple M4 Pro, macOS 26.6.2, Vulkan SDK 1.4.363.0 (slangc 2026.18.3, KosmicKrisp `vulkan-sdk-1.4.363`, MoltenVK 1.4.2).

| ID | Component | Effect | Reproduces with |
|---|---|---|---|
| **A** | Slang: `bitfieldInsert` text expansion lacks outer parentheses | silent wrong values on Metal; compile errors on Metal and C++ | slangc 2026.18.3, 2026.19 (latest release), `master` 3a93b38 built from source |
| **B** | Slang: bitfield mask `(1 << bits) - 1` at `bits == 32` | silent wrong values on Metal | same three |
| **C** | Mesa KosmicKrisp: SPIR-V integer ops emitted as signed `int` arithmetic in MSL | silent wrong values | KosmicKrisp in SDK 1.4.363.0; the same emission code is on Mesa `main` (read 2026-10-16) |
| known | Slang #13553: mixed-signedness vector shifts on the CPU/CUDA targets | C++ compile error | rediscovered by the `signed` family; fixed upstream by PR #13684 (merged 2026-10-16, after the SDK build tested here) |

**Prior reports.** Searches of the Slang tracker found nothing matching A or B (queries: "bitfieldInsert metal", "bitfieldExtract 32", "bitfield full width", "metal precedence", …). The closest are [#10299](https://github.com/shader-slang/slang/pull/10299) (same emitter, different problem) and [#5858](https://github.com/shader-slang/slang/issues/5858) (open: add more bitfield tests). Searches of the Mesa tracker ("kosmickrisp overflow / signed / ineg / miscompil / undefined behavior") found nothing matching C.

---

## A. Slang: a swizzle on `bitfieldInsert(...)` binds to half of the expansion

```slang
uint4 r = uint4(bitfieldInsert(a, b, off, bits).y);   // a = (0x11111111, 0x22222222, 0x33333333, 0x44444444)
                                                      // b = (0xA, 0xB, 0xC, 0xD), off = 4, bits = 8 (runtime)
```

| path | `r` |
|---|---|
| expected (Python reference) | `0x222220b2` in all four lanes |
| KosmicKrisp, MoltenVK (Slang → SPIR-V) | `0x222220b2` ×4 ✓ |
| **native Metal (Slang → MSL)** | **`0x111110b1, 0x222220b2, 0x333330b3, 0x444440b4`** ✗ |

**Other forms fail to compile:**
- a scalar store such as `uint y = bitfieldInsert(...).y;`, or a `.xy` swizzle, makes Apple's Metal compiler reject the MSL;
- `-target cpp` produces C++ that does not compile (`invalid operands … 'Vector<uint32_t, 4>' and 'unsigned int'`).

**Cause.** `CLikeSourceEmitter::emitBitfieldInsertImpl` (`source/slang/slang-emit-c-like.cpp`) prints `((base)&~mask)|(((insert)&m)<<off)` with no outer parentheses. When the instruction is folded into a use that applies a swizzle, `.y` is appended to the text and binds only to the right operand of `|`. That gives `(base & ~mask) | ((insert & m) << off).y`, and `uint4 | uint` silently broadcasts. `emitBitfieldExtractImpl` does parenthesize its whole expansion, and `bitfieldExtract(...).y` is correct.

**Why existing tests miss it.** `tests/language-feature/bitfield/bitfield-extract.slang` stores the result in a variable before reading components, so the expansion is never folded under a swizzle.

## B. Slang: full-width bitfields (`bits == 32`) return wrong values on Metal

With offset 0 and bits 32 read from a buffer at runtime:

| expression | expected | Metal (Slang → MSL) | SPIR-V paths |
|---|---|---|---|
| `bitfieldExtract(x, 0, 32)` | `x` | **`0`** | ✓ |
| `bitfieldInsert(base, ins, 0, 32)` | `ins` | **`base`** | ✓ |
| `bitfieldExtract(int(x), 0, 32)` | `x` | **`0`** | ✓ |

Widths 0, 1, 8, 16 and 31 are correct.

**Cause.** Both C-like expansions build the mask as `(1 << bits) - 1`. At `bits == 32`:
- in MSL and C++, the shift amount equals the width, which is undefined;
- HLSL masks the shift amount, so the mask is deterministically 0.

**Why this is a bug and not undefined input.** Slang's core module documents the extracted range as `[offset, offset + bits - 1]` without forbidding `bits == 32`. GLSL and SPIR-V (`OpBitField*Extract/Insert`) leave the result undefined only when `offset + bits` *exceeds* the width, so `bits == 32` with `offset == 0` is defined. Slang's own SPIR-V output handles it.

## C. KosmicKrisp: integer arithmetic emitted as signed `int`, so wrap-around becomes undefined

```slang
uint4 r = uint4(b == -b);     // b = (0x80000000, 0, 1, 0x7FFFFFFF), all unsigned
```

| path | `r` |
|---|---|
| expected | `(1, 1, 0, 0)`; `-0x80000000` wraps to itself |
| native Metal, MoltenVK | `(1, 1, 0, 0)` ✓ |
| **KosmicKrisp** | **`(0, 1, 0, 0)`** ✗ |

The Slang source is entirely unsigned, so nothing here relies on signed overflow at the source level. When the zero is only known at run time (`b != (a - b)` with `a = 0`), KosmicKrisp is correct, which points to a compile-time rewrite.

**Cause.** In KosmicKrisp's generated MSL (`MESA_KK_DEBUG=msl`) the value is loaded as `int`, then `t47 = -t28; t51 = t28 == t47;`.
- SPIR-V and NIR integer negation wraps.
- In MSL (C++ rules), negating `INT_MIN` as an `int` is undefined, so Apple's compiler may fold `x == -x` to `x == 0`. That fold is inferred from the observed result, not seen directly.
- Mesa's `src/kosmickrisp/compiler/nir_to_msl.c` emits `nir_op_ineg` as `-` and `iadd`/`isub`/`imul` as `+ - *`, and `msl_type_inference.c` can type them `TYPE_INT`.

---

## How they were found

Logs: `results/evidence/*.jsonl`. Every record carries the `slangc` version. Commands: `python3 -m difftest.run fuzz --family <f> --batches <n> --k 32 --seed <s>`.

| run | functions | Metal value mismatches (per function) | Metal rejects (per batch) | C++ errors (first error per batch) | KosmicKrisp mismatches |
|---|---:|---:|---:|---:|---:|
| `bitfield`, seed 3, SDK slangc | 320 | 129 | 10 | 10 (bug A) | 0 |
| `bitfield`, seed 3, **patched** slangc | 320 | **0** | **0** | **0** | 0 |
| `all`, seed 9, SDK slangc | 1,280 | 117 | 12 | 40 (38 #13553-type int/uint, 2 bool) | 3 (C) |
| `all`, seed 9, **patched** slangc | 1,280 | **0** | **0** | 40 (all #13553-type) | 3 (C) |

**Notes on the counts:**
- Every SDK-slangc Metal mismatch involves a bitfield operation.
- **Clean families.** `arith`, `shift`, `divmod`, `select`, `swizzle`, `bits` and `dot` gave 0 findings over 4,096 functions: 3,456 in single-family runs plus 640 in an early all-family run that predates the `signed`/`bitfield` families. Those runs predate per-record version logging.
- **The C++ check is saturated.** In families that generate mixed-signedness shifts, #13553 makes nearly every batch's C++ fail first, and only the first error per batch is recorded. So the C++ check can't detect *new* C++ bugs in those families.

## Verified fix for A and B (not submitted)

`docs/upstream/0001-…patch` changes `source/slang/slang-emit-c-like.cpp`:
- **For A:** wraps the whole `bitfieldInsert` expansion in parentheses.
- **For B:** builds every mask with `((1 << (bits & (W-1))) - 1) | ((bits >> log2 W) * ~0)`. The second term is all ones exactly when `bits == W`, so no shift reaches the width and no subtraction wraps.

The patch also adds a regression test. Built from `master` 3a93b38 on this Mac:

| check | unpatched | patched |
|---|---|---|
| repros A, B1, B2 | FAIL | PASS |
| evidence runs above | see table | see table |
| new test `bitfield-full-width-and-swizzle.slang`, run on Metal | does not compile | 8/8 values correct |
| new test, `-target wgsl` / `cpp` (+clang) / `dxil` | — | compiles |

## Not claimed

- **Nothing has been filed upstream.** Drafts are in `docs/issue-drafts/` and `docs/upstream/`.
- **CUDA, D3D12 and WebGPU execution were not run.** B's effect on HLSL is inferred from shift masking.
- **The WGSL output was not checked by a WGSL validator** (Tint or Naga); it was only generated.
- **C's fix is a proposal**; Mesa was not built.
- **Known remaining edge cases, still undefined in the expansion and not fixed:**
  - `offset == 32` with `bits == 0` (the value shift);
  - the signed-extract sign extension at `bits == 0`.

  Both gave correct results on Metal in testing, so they are listed rather than fixed.
- **The self-test plants faults only in the generated MSL and the SPIR-V-side source** (`^` → `|`). It does not plant faults in every family or in the reference itself.
- **All three GPU paths end in Apple's Metal compiler**, so a bug there would look like an "all GPU paths" disagreement, not a per-layer one.
