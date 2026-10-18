"""Unattended proposer loop on Claude Code headless (`claude -p`).

Each round:
  1. the proposer (a fresh `claude -p` session working in the blind workspace)
     writes one family plugin, following AGENT.md;
  2. the deterministic gate (agent.admit) decides whether it may hunt;
  3. an admitted family hunts for a fixed budget of functions;
  4. the proposer's transcript is audited for blindness violations.
Results of earlier rounds land in runs/, which later proposers may read.

This uses your Claude Code login (subscription), not an API key: make sure
ANTHROPIC_API_KEY is unset, or `claude -p` bills the API instead.

Safety: the proposer gets an unrestricted Bash tool with your permissions and
network access for hours. Run this only under a separate macOS user that can see
nothing but the blind workspace and the Vulkan SDK; the audit afterwards reports
misuse, it does not prevent it.

Status: untested. The evaluation in docs/bughunter/ ran the same prompt through
Claude Code subagents, because the CLI was not logged in on the test machine.

Usage: python3 -m agent.loop --blind ~/Downloads/_bughunter-blind --rounds 4 [--model opus]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PROMPT = """You are agent **{name}** in a blind compiler-bug-hunting experiment.
Your brief is in AGENT.md in the current directory; read it first and follow it exactly.
Only read/write inside the current directory. No web access.
Write exactly one plugin: engine/families/{name}.py with NAME = "{name}".
Earlier rounds' families are in engine/families/ and their results in runs/: target something new.
Iterate until `python3 -m agent.admit families/{name}.py` (run from engine/) prints "admitted": true,
then reply in the format AGENT.md asks for."""

TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "Bash"]   # the whole tool set; nothing else exists
# Files the proposer must not change: the gate and the engine judge its work.
ENGINE_FILES = ["difftest/__init__.py", "difftest/ast.py", "difftest/run.py", "difftest/reduce.py",
                "difftest/plugins.py", "agent/__init__.py", "agent/admit.py", "build/vk_run", "build/mtl_run"]


def engine_digest(engine: Path) -> str:
    h = hashlib.sha256()
    for rel in ENGINE_FILES:
        h.update(rel.encode() + b"\0" + (engine / rel).read_bytes())
    return h.hexdigest()


def transcript_path(cwd: Path, session_id: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(cwd.resolve()))   # Claude Code's project-dir naming
    return Path.home() / ".claude/projects" / slug / f"{session_id}.jsonl"


def last_json_line(text: str) -> dict:
    """The verdict admit.py prints is its last line; a traceback means no verdict."""
    for line in reversed(text.strip().splitlines()):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return {"admitted": False, "stage": "crash", "feedback": text[-800:]}


def run_round(blind: Path, name: str, model: str, batches: int, budget_usd: float,
              expected_digest: str) -> dict:
    out: dict[str, object] = {"name": name}
    p = subprocess.run(
        ["claude", "-p", PROMPT.format(name=name), "--output-format", "json", "--model", model,
         "--tools", *TOOLS, "--allowedTools", *TOOLS,
         # No user/global settings, plugins, hooks or MCP servers: only what the blind workspace holds.
         "--setting-sources", "project", "--strict-mcp-config",
         "--permission-mode", "acceptEdits", "--max-budget-usd", str(budget_usd)],
        cwd=blind, capture_output=True, text=True, timeout=3 * 3600)
    try:
        res = json.loads(p.stdout)
    except json.JSONDecodeError:
        out["error"] = (p.stdout + p.stderr)[-500:]
        return out
    out["proposer_reply"] = res.get("result", "")
    out["cost_usd"] = res.get("total_cost_usd")
    if res.get("is_error"):
        out["error"] = f"proposer session failed: {out['proposer_reply'][:300]}"
        return out
    plugin = blind / "engine/families" / f"{name}.py"
    if not plugin.exists():
        out["error"] = "proposer wrote no plugin"
        return out

    engine = blind / "engine"
    if engine_digest(engine) != expected_digest:
        out["error"] = "engine files changed during the round: refusing to trust admit/hunt results"
        return out
    try:
        adm = subprocess.run([sys.executable, "-m", "agent.admit", f"families/{name}.py"],
                             cwd=engine, capture_output=True, text=True, timeout=1800)
        verdict = last_json_line(adm.stdout + adm.stderr)
    except subprocess.TimeoutExpired:
        verdict = {"admitted": False, "stage": "timeout"}
    out["admit"] = verdict
    if verdict.get("admitted"):
        try:
            hunt = subprocess.run(
                [sys.executable, "-m", "difftest.run", "fuzz", "--plugin", f"families/{name}.py", "--family", name,
                 "--batches", str(batches), "--k", "32", "--seed", "9001", "--log", f"../runs/{name}-hunt.jsonl"],
                cwd=engine, capture_output=True, text=True, timeout=3 * 3600)
            out["hunt_summary"] = (hunt.stdout + hunt.stderr).strip().splitlines()[-6:]
        except subprocess.TimeoutExpired:
            out["hunt_summary"] = ["hunt timed out after 3 h"]

    tr = transcript_path(blind, res.get("session_id", ""))
    audit = subprocess.run([sys.executable, "-m", "agent.audit", "--blind", str(blind), str(tr)],
                           cwd=Path(__file__).resolve().parent.parent, capture_output=True, text=True)
    out["audit_clean"] = audit.returncode == 0
    out["audit"] = audit.stdout.strip()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blind", type=Path, required=True)
    ap.add_argument("--rounds", type=int, default=4)
    ap.add_argument("--prefix", default="loop")
    ap.add_argument("--model", default="opus")
    ap.add_argument("--batches", type=int, default=40, help="hunt budget per admitted family (x32 functions)")
    ap.add_argument("--budget-usd", type=float, default=5.0, help="per-round cap passed to claude -p")
    args = ap.parse_args()
    if os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is set: claude -p would bill the API. Unset it to use your login.")
    blind = args.blind.expanduser().resolve()
    log = blind / "runs" / f"{args.prefix}-rounds.jsonl"
    digest = engine_digest(blind / "engine")
    for i in range(1, args.rounds + 1):
        r = run_round(blind, f"{args.prefix}{i}", args.model, args.batches, args.budget_usd, digest)
        with log.open("a") as f:
            f.write(json.dumps(r) + "\n")
        print(json.dumps({k: r.get(k) for k in ("name", "admit", "hunt_summary", "audit_clean", "error")}), flush=True)
        if str(r.get("error", "")).startswith(("proposer session failed", "engine files changed")):
            sys.exit("stopping: the proposer session failed (often a usage limit); later rounds would fail the same way")


if __name__ == "__main__":
    main()
