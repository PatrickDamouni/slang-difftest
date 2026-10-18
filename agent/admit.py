"""Admission gate for an AI-written family plugin. Deterministic: no AI here.

A family may hunt only after it passes, in order:
  1. static check          (difftest/plugins.py allowlist)
  2. smoke test            64 generated functions evaluate to 4-lane uint32 values
                           and print to non-empty Slang
  3. compile check         slangc -> SPIR-V (+ spirv-val) and -> MSL accept a batch
  4. planted-fault test    a fault planted in the Metal output of one function must
                           be detected and blamed on the Metal path, proving the
                           value oracle works on this family's kernels
  5. calibration           a few batches where no finding may be "reference suspect"
                           (all GPU paths agree with each other but not with the
                           plugin's reference: the plugin's semantics are wrong)

Prints one JSON object: {"admitted": bool, "stage": ..., "feedback": ...}. The
feedback is written for the agent so it can repair the plugin.

Usage: python3 -m agent.admit path/to/plugin.py [--seed N]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import NoReturn

from difftest import ast as A
from difftest import plugins
from difftest import run as R

M32 = 0xFFFFFFFF


def plant_fault(msl: str) -> str:
    """Make f0 return a wrong value: `return X;` -> `return (X) + uint4(1u);`."""
    m = re.search(r"\bf0(?:_\d+)?\s*\([^)]*\)\s*\{", msl)
    if not m:
        raise ValueError("f0 not found in MSL")
    end = msl.index("}", m.end())
    body = msl[m.end():end]
    r = re.search(r"return\s+(.*?);", body, re.S)
    if not r:
        raise ValueError("no return in f0")
    new_body = body[:r.start()] + f"return ({r.group(1)}) + uint4(1u);" + body[r.end():]
    return msl[:m.end()] + new_body + msl[end:]


def fail(stage, feedback) -> NoReturn:
    print(json.dumps({"admitted": False, "stage": stage, "feedback": feedback}))
    sys.exit(1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("plugin")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    path = Path(args.plugin)

    # 1. static check + import
    problems = plugins.static_check(path.read_text())
    if problems:
        fail("static_check", problems)
    try:
        mod = plugins.load(path)
    except Exception as e:  # noqa: BLE001 - report any import-time failure to the agent
        fail("import", f"{type(e).__name__}: {e}")
    kinds = list(mod.KINDS)
    work = R.WORK / f"admit-{mod.NAME}"

    # 2. smoke test: generation, printing and evaluation behave
    gen = A.Gen(args.seed, kinds)
    env = {"a": (1, 2, 0xFFFFFFFF, 0x80000000), "b": (0, 31, 32, 0x7FFFFFFF), "s": 33}
    used = set()
    for i in range(64):
        try:
            node = gen.vec()
            text = A.to_slang(node)
            val = A.evaluate(node, env)
        except Exception as e:  # noqa: BLE001
            fail("smoke", f"function {i}: {type(e).__name__}: {e}")
        used |= A.kinds_in(node)
        if not text.strip():
            fail("smoke", f"function {i}: to_slang returned empty text")
        if not (isinstance(val, tuple) and len(val) == 4 and all(isinstance(x, int) and 0 <= x <= M32 for x in val)):
            fail("smoke", f"function {i}: evaluate must return a 4-tuple of uint32 ints, got {val!r} for {text}")
    if not used & set(kinds):
        fail("smoke", "64 generated functions never used this family's kinds")

    # 3+4. compile check and planted fault on one batch
    seed = next((s for s in range(args.seed * 1000, args.seed * 1000 + 500)
                 if set(kinds) & A.kinds_in(A.Gen(s, kinds).vec())), None)
    if seed is None:
        fail("smoke", "no seed in 500 tries put this family's kinds in function 0")
    r = R.run_batch(seed, 8, kinds, work / "planted", {"msl": plant_fault})
    compile_problems = [f for f in r["findings"] if f["kind"] in ("slangc_reject", "slangc_crash", "spirv_invalid", "harness_error")]
    if compile_problems:
        f0 = compile_problems[0]
        fail("compile", {"kind": f0["kind"], "detail": (f0.get("stderr") or f0.get("msg") or "")[-800:],
                         "hint": "a slangc_reject usually means the generated Slang is invalid; "
                                 "a slangc_crash or spirv_invalid may be a real Slang bug - keep the source"})
    caught = [f for f in r["findings"] if f["kind"] == "mismatch" and f["fn"] == 0
              and "metal" in f["bad_paths"]]
    if not caught:
        fail("planted_fault", "a fault planted in f0's Metal output was not detected: "
                              "the family's outputs may not depend on its inputs")

    # 5. calibration: no reference-suspect findings
    suspects, findings = [], []
    for i in range(4):
        r = R.run_batch(args.seed * 7919 + i, 32, kinds, work / f"calib-{i}")
        for f in r["findings"]:
            findings.append(f)
            if f["kind"] == "mismatch" and set(f["bad_paths"]) == set(R.PATHS):
                suspects.append({"expr": f["expr"], "example": f["example"]})
    if suspects:
        fail("calibration", {"reference_suspect": suspects[:3],
                             "meaning": "every GPU path agrees with the others but not with your evaluate(); "
                                        "your reference semantics are probably wrong"})
    print(json.dumps({"admitted": True, "stage": "done", "family": mod.NAME, "kinds": kinds,
                      "calibration_findings": [
                          {k: f.get(k) for k in ("kind", "layer", "expr", "bad_paths")} for f in findings][:10]}))


if __name__ == "__main__":
    main()
