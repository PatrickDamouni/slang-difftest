"""Scores a hunt against the known bugs, deterministically.

Every batch in a hunt log is reproducible from its seed. For each batch that had
findings, the scorer re-runs the same seed with a *patched* slangc (the fix for
bugs A and B) and classifies each original finding:

  fixed_by_patch   gone with the patched compiler  -> bug A or B (split by hand)
  narrowed         the patch fixes some paths, others stay wrong for another reason
  kosmickrisp_only a mismatch only KosmicKrisp shows (driver side)
  other            anything else                    -> triaged by hand
  unscorable       the patched re-run did not compile or a GPU path did not run,
                   so a missing finding proves nothing

It runs inside the blind engine so plugin kinds resolve exactly as in the hunt.

Usage: python3 -m agent.score --engine ~/Downloads/_bughunter-blind/engine \
         --patched ~/Downloads/_slang-src/slang/build/Release/bin/slangc \
         --plugin families/r1a.py runs/r1a-hunt.jsonl
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

def key(fd: dict) -> tuple:
    return (fd["kind"], fd.get("fn"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", type=Path, required=True)
    ap.add_argument("--patched", type=Path, required=True)
    ap.add_argument("--plugin", action="append", default=[])
    ap.add_argument("--examples", type=int, default=3)
    ap.add_argument("logs", nargs="+", type=Path)
    args = ap.parse_args()
    engine = args.engine.expanduser().resolve()
    sys.path.insert(0, str(engine))
    os.chdir(engine)
    from difftest import plugins
    from difftest import run as R
    for p in args.plugin:
        plugins.load(Path(p))

    R.SLANGC = args.patched.expanduser()
    R.SLANGC_VERSION = R.slangc_version()
    classes: dict[str, list] = {"fixed_by_patch": [], "narrowed": [], "kosmickrisp_only": [], "other": [],
                                "unscorable": []}
    batches = functions = 0
    for log in args.logs:
        for line in log.read_text().splitlines():
            rec = json.loads(line)
            batches += 1
            functions += rec["k"]
            found = [f for f in rec["findings"] if f["kind"] != "harness_error"]
            if not found:
                continue
            again = R.run_batch(rec["seed"], rec["k"], rec["kinds"], R.WORK / f"score-{rec['seed']}")
            ran = len(again["paths"]) == len(R.PATHS) and all(v["status"] == "ok" for v in again["paths"].values())
            remaining = {key(f): f for f in again["findings"]}
            for f in found:
                item = {"seed": rec["seed"], "kind": f["kind"], "fn": f.get("fn"), "layer": f.get("layer"),
                        "bad_paths": f.get("bad_paths"), "expr": f.get("expr"), "example": f.get("example"),
                        "detail": (f.get("errors") or [f.get("stderr") or ""])[0][-300:]}
                left = remaining.get(key(f))
                if not ran:
                    item["patched_paths"] = again["paths"]
                    classes["unscorable"].append(item)
                elif left is None:
                    classes["fixed_by_patch"].append(item)
                elif f["kind"] == "mismatch" and set(left["bad_paths"]) < set(f["bad_paths"]):
                    item["bad_paths_after_patch"] = left["bad_paths"]
                    classes["narrowed"].append(item)
                elif f["kind"] == "mismatch" and f.get("bad_paths") == ["kosmickrisp"]:
                    classes["kosmickrisp_only"].append(item)
                else:
                    classes["other"].append(item)
    summary = {"batches": batches, "functions": functions,
               "counts": {c: len(v) for c, v in classes.items()},
               "examples": {c: v[:args.examples] for c, v in classes.items()}}
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
