# slang-difftest

**Differential testing for the [Slang](https://github.com/shader-slang/slang) shader compiler across three GPU paths on Apple Silicon.** Slang is the shading language NVIDIA contributed to Khronos.

```
                      ┌─ slangc → MSL ─────────────────────────────→ native Metal ─┐
random, well-defined  │                                                            │   compare with
Slang program (AST) ──┼─ slangc → SPIR-V ─┬─ MoltenVK  (SPIRV-Cross → MSL)  ─────┼── a Python reference
        │             │                   └─ KosmicKrisp (Mesa NIR → MSL)   ─────┤   computed from the
        │             └─ slangc → C++ ──→ clang++ (must compile)                   │   same AST
        └────────────────────────────────────────────────────────────────────────┘
```

Which paths disagree tells you which layer is broken:
- only Metal → Slang's Metal codegen
- both Vulkan paths → Slang's SPIR-V codegen
- only one Vulkan driver → that driver
- only C++ → Slang's C++ target

**Results so far** (details and root causes in [FINDINGS.md](FINDINGS.md)):

| | finding | effect |
|---|---|---|
| **A** | Slang `bitfieldInsert(...)` + swizzle is mis-parenthesised | **silent wrong results** on Metal; compile errors on Metal (`.xy`) and C++ |
| **B** | Slang bitfield mask `(1u << bits) - 1u` at `bits == 32` | **silent wrong results** on Metal: `bitfieldExtract(x,0,32)` → 0 |
| **C** | KosmicKrisp emits integer ops as signed `int`, so overflow is UB in MSL | **silent wrong results**: `x == -x` and `x + 1 > x` at INT_MIN/INT_MAX |
| known | Slang #13553 (mixed-signedness vector shifts, C++) | rediscovered automatically |

A and B reproduce on the latest Slang release (v2026.19) and are unchanged in Slang's `master` source. Nothing has been filed upstream yet; drafts are in [`docs/issue-drafts/`](docs/issue-drafts/).

## Quick start

Needs Apple Silicon, macOS 26+, the Xcode command-line tools, and the [LunarG Vulkan SDK](https://vulkan.lunarg.com/sdk/home#mac) ≥ 1.4.363.0. The SDK provides `slangc`, KosmicKrisp and MoltenVK.

```bash
make                                   # builds the two GPU runners
make selftest                          # proves the harness: clean run + planted faults
python3 -m difftest.repros             # the 5 minimal reproducers, expected vs actual per path
python3 -m difftest.run fuzz --family bitfield --batches 10
python3 -m difftest.reduce --family bitfield --seed 300013 --oracle metal_reject
```

To test another Slang build, set `SLANGC=/path/to/slangc` (e.g. the latest release) for `difftest.repros`.

## How it avoids false alarms

The council review called false positives the biggest risk. A wrong bug report against someone else's compiler costs more credibility than a real one earns.

- **One AST, two printers.** Each random program is a tree that is printed once as Slang (`to_slang`) and evaluated once in Python (`evaluate`). The expected answer can't drift from the program it checks.
- **Only well-defined operations.** The generator never produces undefined behaviour:
  - unsigned 32-bit arithmetic (wraps)
  - shifts masked `& 31u`
  - divisors forced odd with `| 1u`
  - clamp bounds ordered
  - bitfields with `offset + bits ≤ 32`
  - signed families avoid overflow, negative left shifts and `INT_MIN / -1`
  - no floats; Metal is compiled with `MTLMathModeSafe` anyway
- **The harness proves itself first.** `make selftest` runs three checks:
  - a clean batch must give **0** findings;
  - a fault planted only in the Metal output must be caught and blamed on Slang's Metal codegen;
  - a fault planted only in the SPIR-V input must be blamed on Slang's SPIR-V codegen.
- **Every finding is checked by hand** against the language definition (Slang's core-module source) and against Slang's own tests before being written up.

## Oracles, cheapest first

1. `slangc` crashes or rejects valid code.
2. `spirv-val` rejects Slang's SPIR-V.
3. Slang's C++ output fails to compile (`clang++ -fsyntax-only` with Slang's prelude).
4. Apple's Metal compiler rejects Slang's MSL.
5. A driver crashes or hangs (each path runs in its own process with a timeout).
6. Output values differ from the reference. If Metal rejects a batch, every function is re-run on Metal alone, so one rejected function can't hide silent miscompiles in the rest.

## Feature families

| family | what it exercises |
|---|---|
| `arith`, `shift`, `divmod` | `+ - * & \| ^ ~`, masked shifts, `/ %` by odd divisors, vector⊕scalar broadcasting |
| `select`, `swizzle` | `select`, `uint4(bool4)`, `min/max/clamp`, swizzles, `uint4(v.xy, w.zw)` |
| `bits`, `dot` | `countbits`, `reversebits`, `firstbithigh/low` (incl. 0), integer `dot` |
| `signed` | `int4 >> uint4`, `uint4 << int4`, signed compare/min/max, signed `firstbithigh` |
| `bitfield` | `bitfieldExtract/Insert` with runtime offset/width, widths 0 and 32 included |

Throughput is ~2,000–3,800 functions/min, each checked on 64 input vectors × 3 GPU paths. The mixed families are slower because they re-run Metal per function.

## Layout

```
difftest/ast.py      generator, Slang printer, Python reference (one AST)
difftest/run.py      compile → validate → run per path → compare → triage; fuzz + selftest
difftest/reduce.py   greedy AST reducer that preserves the failure signature
difftest/repros.py   the minimal reproducers for every finding
host/vk_run.cpp      one-shot Vulkan runner (driver via VK_DRIVER_FILES)
host/mtl_run.mm      one-shot Metal runner (runtime MSL compile, safe math)
FINDINGS.md          the bugs, root causes, evidence, and what is not claimed
docs/issue-drafts/   upstream reports, NOT filed
```

## What this does not show

- **One machine** (M4 Pro, macOS 26.6.2) and one driver version each.
- **No CUDA, HLSL or DXIL execution.** Bug B is inferred for those targets from shared emitter code.
- **Integers only.** Floats, matrices, 16/64-bit types, structs, generics and wave ops are not generated yet. Those are the next families.
- **A clean family is not proof of correctness.** It only means this generator found nothing there.
