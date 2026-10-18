"""Builds the blind workspace the agent works in, and verifies it leaks nothing.

The agent must rediscover bugs without having seen them, so the workspace:
  * contains the engine with the hand-written `signed` and `bitfield` families
    cut out (between the <hand:signed-bitfield> markers in difftest/ast.py),
  * has no README, FINDINGS, docs, results, reproducers or git history,
  * ships a clean export of Slang's unmodified `master` (git archive of the
    given commit), so no local fix branch or regression test is visible.
It then greps the whole workspace for terms that would leak the answers and
fails if any appear.

Usage: python3 -m agent.make_blind --dest ~/Downloads/_bughunter-blind --slang ~/Downloads/_slang-src/slang
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SLANG_COMMIT = "3a93b38"
ENGINE_FILES = ["difftest/__init__.py", "difftest/ast.py", "difftest/run.py", "difftest/reduce.py",
                "difftest/plugins.py", "agent/__init__.py", "agent/admit.py",
                "host/vk_run.cpp", "host/mtl_run.mm", "Makefile"]
# Terms that would reveal the hidden findings or the families that found them.
LEAKS = [r"bitfield", r"\bbfe\b", r"\bbfi\b", r"\bsshr\b", r"ushl_s", r"sfbh", r"13553", r"13684",
         r"(?-i:FINDINGS)", r"KosmicKrisp \(Mesa\)\s*\|", r"insert.*parenthes", r"full.width"]


def strip_hand_families(text: str) -> str:
    return re.sub(r"[ \t]*# <hand:signed-bitfield>.*?# </hand:signed-bitfield>\n", "", text, flags=re.S)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dest", type=Path, required=True)
    ap.add_argument("--slang", type=Path, required=True, help="a Slang git checkout")
    args = ap.parse_args()
    dest = args.dest.expanduser()
    if dest.exists():
        shutil.rmtree(dest)
    (dest / "engine").mkdir(parents=True)

    for rel in ENGINE_FILES:
        src = ROOT / rel
        out = dest / "engine" / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text()
        if rel == "difftest/ast.py":
            text = strip_hand_families(text)
        if rel == "difftest/reduce.py":   # its usage examples name a hidden family
            text = re.sub(r"Usage:.*?\n\"\"\"", "Usage: see AGENT.md\n\"\"\"", text, flags=re.S)
            text = text.replace("hand from the reducer's output; see FINDINGS.md.", "hand from the reducer's output.")
        out.write_text(text)
    (dest / "engine/build").mkdir()
    for b in ("vk_run", "mtl_run"):
        shutil.copy2(ROOT / "build" / b, dest / "engine/build" / b)
    (dest / "engine/families").mkdir()
    (dest / "runs").mkdir()
    shutil.copy2(ROOT / "agent/AGENT.md", dest / "AGENT.md")

    # Clean Slang source at an upstream commit: compiler sources and user docs only.
    slang_out = dest / "slang-src"
    slang_out.mkdir()
    arch = subprocess.run(["git", "-C", str(args.slang), "archive", SLANG_COMMIT, "source/slang", "docs/user-guide"],
                          capture_output=True, check=True)
    subprocess.run(["tar", "-x", "-C", str(slang_out)], input=arch.stdout, check=True)

    # Verify: the stripped engine still has no trace of the hidden families or findings.
    hits = []
    for f in (dest / "engine").rglob("*"):
        if f.is_file() and f.suffix in (".py", ".cpp", ".mm", ".md", "") and f.parent.name != "build":
            text = f.read_text(errors="ignore")
            for pat in LEAKS:
                if re.search(pat, text, re.I):
                    hits.append(f"{f.relative_to(dest)}: /{pat}/")
    agent_md = (dest / "AGENT.md").read_text()
    hits += [f"AGENT.md: /{p}/" for p in LEAKS if re.search(p, agent_md, re.I)]
    if hits:
        print("LEAK CHECK FAILED:\n  " + "\n  ".join(hits))
        sys.exit(1)
    print(f"blind workspace ready at {dest}; leak check passed "
          f"({sum(1 for _ in slang_out.rglob('*'))} Slang source files at {SLANG_COMMIT})")


if __name__ == "__main__":
    main()
