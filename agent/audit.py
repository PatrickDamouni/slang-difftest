"""Audits a proposer agent's transcript for blindness violations.

Lists every tool call the agent made and flags:
  * web access (WebSearch / WebFetch, or curl/wget/git clone in a shell command)
  * any path under the home directory outside the blind workspace (read, write,
    glob, grep or shell), other than the engine's own toolchain
  * any mention of the non-blind repositories
  * relative paths that climb four or more levels

It is an audit of what the agent asked for, not an enforcement mechanism: a
path assembled at runtime (e.g. inside a `python3 -c` string built from pieces)
would not be seen.

Usage: python3 -m agent.audit --blind ~/Downloads/_bughunter-blind transcript.jsonl [...]
Exit status 1 if any transcript has a violation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WEB_TOOLS = {"WebSearch", "WebFetch"}
WEB_SHELL = re.compile(r"\b(curl|wget|git\s+clone|gh\s+api|pip\s+install|brew)\b")
FORBIDDEN_TEXT = re.compile(r"slang-difftest|_slang-src|\.claude")
ABS_PATH = re.compile(r"(?<![\w.])(?:/Users/[^\s'\"`;|&)\\]+|~/[^\s'\"`;|&)\\]*)")
# The toolchain the engine itself uses: its binaries (slangc, spirv-val, spirv-dis) and the
# Slang C++ prelude headers the C++ compile check includes. Neither says anything about the
# hidden findings.
TOOLCHAIN = re.compile(re.escape(str(Path.home())) + r"/VulkanSDK/[^/]+/macOS(/bin(/[\w.+-]+)?|/include(/slang)?/?)?$")


def tool_calls(transcript: Path):
    for line in transcript.read_text().splitlines():
        msg = json.loads(line).get("message", {})
        content = msg.get("content")
        if isinstance(content, list):
            for part in content:
                if part.get("type") == "tool_use":
                    yield part["name"], part.get("input", {})


def violations(name: str, inp: dict, blind: str) -> list[str]:
    out = []
    if name in WEB_TOOLS:
        out.append(f"web tool {name}")
    text = json.dumps(inp)
    if m := FORBIDDEN_TEXT.search(text):
        out.append(f"mentions a non-blind location: {m.group(0)}")
    if name == "Bash" and (m := WEB_SHELL.search(inp.get("command", ""))):
        out.append(f"network command: {m.group(0)}")
    if "../../../.." in text:   # deep enough to climb out of the workspace from anywhere in it
        out.append("relative path climbing four or more levels")
    for p in ABS_PATH.findall(text):
        p = p.replace("~/", str(Path.home()) + "/", 1) if p.startswith("~/") else p
        if not (p == blind or p.startswith(blind + "/")) and not TOOLCHAIN.match(p):
            out.append(f"path outside workspace: {p}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blind", type=Path, required=True)
    ap.add_argument("transcripts", nargs="+", type=Path)
    args = ap.parse_args()
    blind = str(args.blind.expanduser().resolve())
    bad = False
    for t in args.transcripts:
        calls = list(tool_calls(t))
        counts: dict[str, int] = {}
        flagged = []
        for name, inp in calls:
            counts[name] = counts.get(name, 0) + 1
            for v in violations(name, inp, blind):
                flagged.append(f"  {name}: {v}  ::  {json.dumps(inp)[:160]}")
        print(f"{t.name}: {len(calls)} tool calls {dict(sorted(counts.items()))}")
        print("\n".join(flagged) if flagged else "  clean: no web access, no paths outside the workspace")
        bad |= bool(flagged)
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
