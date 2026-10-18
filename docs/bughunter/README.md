# slang-bughunter: 8 Claude agents wrote test generators for NVIDIA's Slang compiler

**Result:**
- **Blind rediscovery.** 2 of 4 agents rediscovered both hidden Slang bugs (A and B) without having seen them.
- **New candidates.** The agents surfaced **13 new candidate Slang bugs** (root causes, deduplicated), 7 of them silent wrong results, and **1 confirmed bug in Apple's Metal compiler**.
- **Verification.** I reproduced every one by hand and checked it against the issue tracker. One more root cause turned out to be already reported upstream.
- **Status.** Nothing is filed yet.

**Check it yourself:**
- `python3 -m agent.repros` runs the GPU cases. All 19 fail on Slang 2026.18.3 (Vulkan SDK) and 2026.19.
- `python3 -m agent.compile_repros` runs the compile-time cases. All 7 reproduce.

**How it works.** An agent reads Slang's source and writes one *plugin*: a generator of random, well-defined Slang expressions, plus an exact Python reference for them.

`agent writes plugin` → `admit.py`: 5 deterministic checks → 3 GPU paths + C++ compile → `score.py`: re-run with a patched compiler → human reproduces → human files

The agent never judges its own work: the gate, the engine and I do.

## Silent wrong results the agents surfaced

| # | Slang | target | repro |
|---|---|---|---|
| S1 | `bitfieldInsert(a, b, s & 7u, 3u)`: offset/count operands spliced in without parentheses. A third defect in the same function as A. **Fixed in the local patch.** | Metal, C++ | `N-P`, `N-S` |
| S2 | `uint8_t(255) + uint8_t(255)` gives `0x1fe`; `~f32tof16(x) >> 30` gives `0xffffffff`. MSL promotes narrow ints to `int`. | Metal | `N-W8`, `N-W16`, `N-NEG16`, `N-F2H` |
| S4 | `countbits` of a negative `int16_t` counts sign-extended bits on SPIR-V (24, not 8). `int8_t` gives 32 on every target. | SPIR-V; all | `N-CB16`, `N-CB8` |
| S5 | `abs(0xffffffffu)` gives `1`: `abs(uint)` is emitted as GLSL.std.450 `SAbs` | SPIR-V | `N-ABS` |
| S6 | `uint4 && uint4(1u)` folded to the left operand instead of 0/1 lanes (peephole) | all | `N-LOGIC` |
| S7 | `0x8000000000000000ull > 1ull` folds to false: SCCP compares `uint64` constants as signed. Found by two agents independently. | all | `N-U64` |
| S13 | `uaddCarry` fallback computes `x * y` (`glsl.meta.slang:922`); C++ output contains `_S6 = *_S3 * *_S4` | C++/CUDA/HLSL via `-allow-glsl` | source + C++ output |
| D1 | **Apple Metal compiler:** `extract_bits(int, off, 0)` with a constant count makes the kernel skip its output store. MSL 4.1, p. 203: "If bits is zero, the result is zero". | Metal (driver) | `N-APPLE` + hand-written MSL |

S6 and S7 (and S4's `int8_t` case) are wrong on every target, so differential testing *cannot* see them by construction. The agents found them because calibration flagged their reference as suspect. They read the source, kept those cases out of their generators so the family stayed honest, and reported the bug in prose.

The full list (14 Slang root causes, downstream candidates, and what was dropped) is under [All candidates](#all-candidates).

## Blind rediscovery

The agents worked in a stripped copy of the repo built by [`make_blind.py`](../../agent/make_blind.py):
- no hand-written `bitfield`/`signed` families;
- no README, findings, docs, results or git history;
- Slang source from a clean `git archive` of upstream `master` (3a93b38), without my fix branch.

A leak check greps the workspace for 12 telltale terms.

All hunts use the same budget: 1,280 functions (40 batches × 32), seed 9001, stock slangc 2026.18.3. "A/B" means the finding disappears when the same seed is re-run with the patch for A and B (v1). "v2" adds the S1 fix.

| family | written by | findings | removed by v1 (A+B) | removed by v2 (+S1) | left after v2 |
|---|---|---:|---:|---:|---|
| `bitfield` | hand (earlier project) | 605 | 605 | 605 | 0 |
| `r1a` | agent | 946 | 480 | **945** | 1: Metal part fixed; the Vulkan paths hit D1 / KosmicKrisp |
| `r1d` | agent | 861 | 623 | **836** | 25: all contain a signed extract with a constant count of 0 (D1 / KosmicKrisp); v2 fixes the Metal path in 12 |

Raw finding counts measure how often the same few bugs fire, not discovery ability. The `bitfield` family's 605 includes one Metal reject and one C++ error per batch, so it isn't directly comparable with the agents' per-function mismatches. What matters:
- the agent families' first batch already contained findings the A/B patch removes;
- after v1, about half of r1a's findings (465 of 946) remained;
- triaging those exposed S1, a bug class the hand-written family and I had both missed.

**What the agents were told.** This is the main caveat, disclosed in full.
- **Brief.** Every agent read [AGENT.md](../../agent/AGENT.md). Its strategy section is generic. But its list of existing families names `countbits`, `reversebits` and `firstbithigh/low` and not the bitfield intrinsics, so the gap is visible.
- **Round 1 starting files.** I split the source between agents. Quoted from the prompts:
  - r1a: `slang-emit-c-like.cpp`
  - r1b: `slang-emit-metal.cpp` and the `case metal:` branches of `hlsl.meta.slang`
  - r1c: `slang-emit-cpp.cpp` and `core.meta.slang`
  - r1d: `slang-emit-spirv.cpp` and `glsl.meta.slang`, "looking for operations that SPIR-V gets natively but other targets must emulate"

  r1a's and r1d's starting points both lead to the bitfield emulation. So the result shows the agents can recognize and exploit a buggy lowering once pointed at the right file. It does not show that they found the file unaided.
- **Harness context.** Every agent ran as a Claude Code subagent of my session, and so received:
  - my memory index, including the line "slang-difftest w/ 3 compiler bugs";
  - a prompt that named `~/Downloads/slang-difftest` as off-limits.

  I checked all 12 transcripts. No bug-specific term (bitfield, the issue numbers, "full width" and so on) appears anywhere in the context round-1 agents received before their first tool call. The detailed memory file was not loaded.
- **Round 2** agents saw round 1's plugins and trial logs, and their prompts named the areas already covered.

**Controls, and their limits:**
- **Not a sandbox.** The agents had Bash with my user's permissions. Blindness rests on what the workspace contains and on the instructions.
- **Post-hoc audit.** [`audit.py`](../../agent/audit.py) checks every tool call for web tools, network commands, and paths under my home directory outside the workspace. It reports misuse; it does not prevent it. A path built at runtime, a symlink or a short `../..` would get past it. All 12 transcripts pass ([audit.txt](../../results/bughunter/audit.txt)). Two agents bent the brief inside the workspace: they wrote probe files to `engine/work/` and ran `spirv-dis`.
- **Gate integrity.** After the runs, every engine file, both GPU runners, AGENT.md and all 619 Slang source files in the workspace were byte-identical to a fresh rebuild. No agent modified the gate that judged it.
- **Plugin lint.** [`plugins.py`](../../difftest/plugins.py) allows importing only `random`/`math` and rejects private attributes, dunders and frame access. It is a lint against sloppy code, not a security boundary. It was tightened after the evaluation; all 8 plugins still pass it.
- **Calibration.** It rejects a plugin whose reference disagrees with every GPU path. A deliberately wrong plugin ([`bad_ref.py`](../../agent/examples/bad_ref.py)) is rejected.
- **Planted fault.** A wrong value injected into the Metal output must be caught and blamed on Metal.
- **Scoring.** [`score.py`](../../agent/score.py) re-runs each failing batch with the patched compiler. If the patched run doesn't compile or a path doesn't run, the batch is "unscorable", never "fixed".

## All candidates

"Verified" means I reproduced it with fixed inputs and an expected value I derived myself from the spec:
- `repro`: a case in [`agent/repros.py`](../../agent/repros.py);
- `compile`: a case in [`agent/compile_repros.py`](../../agent/compile_repros.py).

The tracker was searched by keyword on 2026-10-18.

**Slang:**

| # | candidate | target | agents | verified | status |
|---|---|---|---|---|---|
| S1 | bitfield operands not parenthesized | Metal, C++ | r1a, r1d | repro | **fixed in local patch v2** |
| S2 | MSL integer promotion of 8/16-bit results (arithmetic, `f32tof16`) | Metal (+C++/CUDA: same emitter, not run) | r1b, r2c | repro | new |
| S3 | `f16tof32` returns `half` on Metal, so the sum of two is computed in half (`2048+3` gives `2052`) | Metal | r2c | repro `N-H2F` | **known:** [#13653](https://github.com/shader-slang/slang/issues/13653) (open; reports the compile error, not the wrong value) |
| S4 | `countbits` sign-extends negative `int8_t`/`int16_t` (one helper pattern, two branches) | SPIR-V; all | r1b | repro | new. HLSL defines `countbits` on `uint` only, so the bug is the cross-target inconsistency. The closest prior is [#6433](https://github.com/shader-slang/slang/issues/6433) (non-32-bit `countbits`, closed). |
| S5 | `abs(uint)` → `SAbs` | SPIR-V | r2b | repro | new |
| S6 | peephole folds integer-vector `&&`/`\|\|` with a constant | all | r2d | repro | new |
| S7 | SCCP compares `uint64` constants as signed | all | r2a, r2d | repro | new |
| S8 | `bit_cast` lowering (`slang-ir-extract-value-from-type.cpp`):<br>• from a matrix: indexes `m[0][2]`, invalid SPIR-V;<br>• struct with a 1-byte gap: drops 16 bits of the next field (seen in MSL, not executed; padding layout is a judgment call);<br>• `struct {uint; uint64_t}`: slangc SIGSEGV | all | r2c | compile | new |
| S9 | 16-bit intrinsics emit invalid SPIR-V: `firstbitlow(int16_t)`, `bitfieldExtract` on `vector<uint16_t,4>` | SPIR-V | r1b, r1d | compile | new |
| S10 | C++ prelude lacks `I8/I16/U8/U16_firstbithigh`, `*_reversebits` and `I32_sign` | C++ | r1b, r2b | compile | new |
| S11 | `sign(int)` lowers to MSL `sign()`, which takes only float/half | Metal | r2b | repro | new (may be "unsupported" rather than a bug) |
| S12 | splatted functional cast `(uint4(uint(_S1)) ) & …` is parsed by Metal as a C-style cast | Metal | r2a | repro | new ([#12732](https://github.com/shader-slang/slang/issues/12732) is a different cast) |
| S13 | `uaddCarry` fallback computes `x * y` | C++/CUDA/HLSL via `-allow-glsl` | r1d | source + C++ output | new |
| S14 | integer/bool matrix ops (`!`, some binary ops) emit C++ that doesn't compile | C++ | r1c | hunt log only (clang errors) | unminimized |

**Downstream of Slang** (Slang's SPIR-V is valid in each case):
- **D1, Apple Metal compiler: confirmed.** `extract_bits(int, off, 0u)` with a constant count drops the store. I reproduced it in hand-written MSL on native Metal.
- **KosmicKrisp-only symptoms** (r1a, r2a, r2c, r2d): four of them.

  | input | KosmicKrisp gives |
  |---|---|
  | constant `bitfieldExtract(x, 32u, 0u)` | `0xc` |
  | int64 `bit_cast` of a u64×u64 product | bit 63 lost |
  | `bit_cast<int16_t2>` | lane 1 not sign-extended |
  | `(0x10000u - b) << 15u` | bit 31 set |

  KosmicKrisp's MSL also goes through Apple's compiler, so "only KosmicKrisp is wrong" does not prove a Mesa bug until its MSL is inspected. The 64-bit and shift cases look like bug C (signed arithmetic in its MSL). Repros: `N-KK`, `N-KK64`, `N-KKS16`, `C-SHL`.

**Dropped:**
- **Unreproduced:**
  - `pack_clamp_*` on `int16_t4` emitting undeclared `I16_min/max`: the C++ compiles;
  - `popcount(ushort4)` truncation on Apple;
  - `b + 0x80000000u` alone on KosmicKrisp: passes in isolation.
- **Already known:** mixed-width or mixed-signedness C++ vector shifts. These are [#13553](https://github.com/shader-slang/slang/issues/13553), fixed by [#13684](https://github.com/shader-slang/slang/pull/13684) (merged 2026-10-16, after the SDK build used here).

## Round-2 hunts and cost

Round-2 hunts used the same budget. I triaged them by bucket against the verified repros:

| family | area | findings | buckets |
|---|---|---:|---|
| `r1b` | 8/16-bit ints | 335 | Metal-only → S2; C++ → S10; SPIR-V → S4 |
| `r1c` | integer matrices | 39 | C++ → S14 |
| `r2a` | 64-bit ints | 81 | KosmicKrisp-only (64-bit); C++ shifts → #13553 |
| `r2b` | signed intrinsics | 2 | KosmicKrisp-only |
| `r2c` | pack/unpack, `bit_cast`, f16 | 312 | 303 Metal-only → S2/S3; KosmicKrisp-only |
| `r2d` | constant folding | 2 | KosmicKrisp-only |

The hunts understate round 2. The agents kept their strongest bugs (S5–S8, S11) out of their own generators, because those bugs would swamp every batch or fail calibration.

| | |
|---|---|
| agents | 8 admitted plugins. 4 round-2 attempts were stopped by a usage limit and re-run. r1c was stopped by me at the same limit after writing its plugin (15.7 min, ~247k tokens, no report). |
| tokens, time per agent | 128k–190k subagent tokens and 6–12 min for the 7 that finished normally |
| GPU per 1,280-function hunt | 0.5–4 min on an M4 Pro |
| human time | most of the cost: reproducing, deriving expected values by hand, deduplicating, searching the tracker |
| money | a Claude subscription; no API key |

**What limits scale** (not addressed here):
- Bucketing is by layer only, so different bugs on the same path share a bucket.
- Attribution needs a patch per bug.
- Prose findings need a human.
- The subscription limit already interrupted 4 of 12 runs.
- [`loop.py`](../../agent/loop.py) (unattended `claude -p`) is written but has not been run. It should run only under a separate macOS user, because the proposer gets a shell.

## Threats to validity

- **Same model everywhere.** The agents, the brief's co-author and the reviewer of their reports are all Claude, and the experimenter already knew A and B.
- **Small n.** Two rounds, eight agents. Two of four found A/B; the other two found different bugs.
- **White-box by design.** The agents read the source that contains the bugs. That is the method, not a leak, but it is not black-box fuzzing.
- **Version mismatch.** The agents read `master` (3a93b38) and ran the SDK's 2026.18.3 binary. Every repro also fails on 2026.19.
- **One machine.** Apple M4 Pro, macOS 26.6.2, Vulkan SDK 1.4.363.0. The downstream findings may be version-specific.
- **"Candidate" means candidate.** None is confirmed by maintainers. The tracker moves daily, so re-search before filing.

## Reproduce

```bash
make && make selftest
python3 -m agent.repros && python3 -m agent.compile_repros
python3 -m agent.make_blind --dest ~/Downloads/_bughunter-blind --slang <slang git checkout>
cd ~/Downloads/_bughunter-blind/engine
python3 -m agent.admit families/<name>.py
python3 -m difftest.run fuzz --plugin families/<name>.py --family <name> --batches 40 --seed 9001 --log ../runs/<name>-hunt.jsonl
cd - && python3 -m agent.score --engine ~/Downloads/_bughunter-blind/engine --patched <patched slangc> \
        --plugin families/<name>.py ~/Downloads/_bughunter-blind/runs/<name>-hunt.jsonl
```

The agent-written plugins are kept verbatim in [`agent/families/`](../../agent/families/). They are agent output, not my code style. Hunt logs, scores (`score-patchv1-*`, `score-patchv2-*`) and the audit are in [`results/bughunter/`](../../results/bughunter/).

## Reporting policy

The agents never touch an issue tracker. Mesa forbids autonomous AI interaction and wants reports in the filer's own words. Slang's CONTRIBUTING asks for "sufficient review, judgment, and creative direction". Every report is reproduced, minimized and written by me before filing. Upstream work is tracked in [`docs/upstream/`](../upstream/).
