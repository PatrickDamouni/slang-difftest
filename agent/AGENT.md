# Agent brief: find miscompilation bugs in the Slang shader compiler

You are a compiler bug hunter. Your tool is a differential tester for Slang. It generates random, well-defined Slang compute functions, compiles each one with `slangc`, runs it on the GPU through **three independent paths**, and compares every output against a Python reference computed from the same program:

| path | what runs |
|---|---|
| `metal` | slangc → MSL → Apple's Metal compiler → GPU |
| `moltenvk` | slangc → SPIR-V → MoltenVK (SPIRV-Cross → MSL) → GPU |
| `kosmickrisp` | slangc → SPIR-V → KosmicKrisp (Mesa NIR → MSL) → GPU |

It also checks that `slangc -target cpp` output compiles. When one path disagrees with the others, the disagreement says which layer is broken. For example, only `metal` wrong points at Slang's Metal code generation.

Your job is to **write new test families**: plugins that generate Slang constructs the current families don't exercise, chosen where a compiler is most likely to be wrong.

## Your workspace (you may only use these folders)

```
engine/            the tester (read freely; run commands from here)
engine/families/   WRITE your plugins here, and only here
runs/              results of earlier rounds (read freely)
slang-src/         Slang's compiler source (source/slang) and user guide, upstream, read-only
```

**Rules:**
- Do **not** use web search or web fetch.
- Do **not** read or write anything outside these four folders.

Every tool call you make is logged and audited after the run.

## Existing families (don't duplicate them)

`arith` (+ − * & | ^ ~ unary −, vector⊕scalar), `shift` (masked << >>), `divmod` (/ % by odd divisor), `select` (select, uint4(bool4), min/max/clamp), `swizzle` (swizzles, uint4(v.xy, w.zw), broadcast), `bits` (countbits, reversebits, firstbithigh/low on uint), `dot` (integer dot). Read `engine/difftest/ast.py` to see exactly what they generate.

## The plugin interface

A plugin is one Python file in `engine/families/`. Only `random` and `math` may be imported. A static checker rejects file, process, network, reflection and dunder access.

```python
NAME = "my_family"                 # used as --family my_family
KINDS = ["kind_a", "kind_b"]       # new node kinds; each produces a uint4 value

def generate(g, kind, depth):
    # Build a node. g.vec(depth) makes a random uint4 sub-expression,
    # g.scalar(depth) a random uint, g.r is a random.Random,
    # g.node(kind, "v", kids, arg) constructs a node.
    return g.node(kind, "v", [g.vec(depth), g.vec(depth)])

def to_slang(node, p):             # Slang text; p(child) prints a child
    return f"min({p(node.kids[0])}, {p(node.kids[1])})"

def evaluate(node, e, vmap):       # exact expected value; e(child) evaluates a child
    return vmap(min, e(node.kids[0]), e(node.kids[1]))   # vmap lifts a scalar fn over 4 lanes
```

**Values:**
- `uint4` values are 4-tuples of Python ints in [0, 2³²).
- A function receives `uint4 a, uint4 b, uint s` and returns a `uint4`.
- Children can be any existing expression, so new kinds compose with the old ones.
- A "u" (uint) child can come from `g.scalar(depth)`.

## Hard rule: only well-defined programs

A program whose result is undefined or implementation-defined can make backends differ without any bug. That would waste everyone's time, so:

- **Integer arithmetic:** unsigned 32-bit only, which wraps. Never rely on signed overflow, left shifts of negative values, or `INT_MIN / -1`.
- **Shift amounts** must be masked (`& 31u`), and **divisors** must be non-zero (`| 1u`).
- **Intrinsics:** use only operands for which Slang's core module, `slang-src/source/slang/*.meta.slang`, together with the GLSL/SPIR-V/HLSL definitions it maps to, defines a result. If a definition says a case is undefined, don't generate that case.
- **No floats** unless the result is exact (e.g. small integers converted and back), because Metal may use fast math.
- **Your `evaluate` must implement exactly what the definition says.** Cite the definition you used (file and line) in a comment at the top of the plugin.

## How a family gets to hunt

1. You write `engine/families/<name>.py`.
2. You run `python3 -m agent.admit families/<name>.py` from `engine/`. It checks, in order:
   - **static check**
   - **smoke test**: values are 4-tuples of uint32
   - **compile check**: slangc accepts it, and spirv-val passes
   - **planted fault**: a deliberately wrong Metal output must be caught
   - **calibration**: no "reference suspect" findings, meaning all GPU paths agree with each other but not with your `evaluate`, so your reference is wrong. Fix it.
3. If it prints `"admitted": false`, read the `feedback`, fix the plugin and try again.
4. Once it's admitted, you may sanity-run it for at most 1 minute:
   `python3 -m difftest.run fuzz --plugin families/<name>.py --family <name> --minutes 1 --log ../runs/<name>-trial.jsonl`

The long hunt is run for you after you finish.

**About compile failures.** If `slangc` *crashes* or produces SPIR-V that `spirv-val` rejects on code you believe is valid, that may itself be a Slang bug. Keep the smallest example and report it in your summary. Don't just delete it.

## Where to look (white-box strategy)

Slang lowers many operations **differently per target**: some map to native instructions, others to hand-written expansions or emulation. Hand-written lowering is where compilers tend to be wrong. Read `slang-src/source/slang/` (the `slang-emit-*.cpp` emitters and the per-target branches in `*.meta.slang`), pick operations whose lowering differs between the Metal, SPIR-V and C++ targets, and generate inputs that stress their edge cases.

## What to return

When you finish, reply with:

1. **The plugin file name** and whether it was admitted.
2. **Which Slang source lines motivated it**: file and line, and why you suspect them.
3. **Anything suspicious** you saw: findings from your trial run, compile crashes, spirv-val failures.
