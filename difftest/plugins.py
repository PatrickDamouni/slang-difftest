"""Loading AI-written family plugins, behind a lint (not a sandbox).

A plugin is a small Python file that adds node kinds to the generator. It must
define:

    NAME  = "family_name"            # becomes a family usable with --family
    KINDS = ["kind_a", "kind_b"]     # new node kinds; every kind produces a uint4
    def generate(g, kind, depth):    # -> Node; use g.vec(depth), g.scalar(depth),
                                     #    g.r (random.Random), g.node(kind, ty, kids, arg)
    def to_slang(node, p):           # -> str; p(child) prints a child expression
    def evaluate(node, e, vmap):     # -> 4-tuple of ints (or int for "u" nodes);
                                     #    e(child) evaluates a child, vmap lifts a
                                     #    scalar function over lanes

Before a plugin is imported, `static_check` rejects anything outside a tiny
allowlist: only `random` and `math` may be imported, and names for file,
process, network and reflection access, dunder names, attributes starting with
`_` (e.g. `random._os`), frame/generator attributes and attribute-walking
format string literals are rejected. A format template held in a variable, or a
path built at runtime, gets through. This is a lint against accidents and lazy
shortcuts, not a sandbox: plugins run in-process with the user's permissions,
so only run plugins whose source you have read.
"""

from __future__ import annotations

import ast as pyast
import importlib.util
import re
from pathlib import Path

ALLOWED_IMPORTS = {"random", "math"}
FORBIDDEN_NAMES = {
    "open", "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
    "getattr", "setattr", "delattr", "input", "breakpoint", "help", "exit", "quit",
    "memoryview", "os", "sys", "subprocess", "socket", "shutil", "pathlib", "importlib",
}
# Generator, coroutine, frame, traceback and code-object attributes reach the
# interpreter's globals and builtins without any import.
FRAME_ATTRS = ("gi_", "cr_", "ag_", "f_", "tb_", "co_")
REQUIRED = ("NAME", "KINDS", "generate", "to_slang", "evaluate")


def _plain_format(receiver) -> bool:
    """str.format walks attributes ('{0._os}') the AST never shows. A literal
    format string with `.` or `[` inside a field is rejected; a template held in
    a variable cannot be checked statically and is let through."""
    return not (isinstance(receiver, pyast.Constant) and isinstance(receiver.value, str)
                and re.search(r"\{[^}]*[.\[]", receiver.value))


def static_check(source: str) -> list[str]:
    """Return a list of violations (empty means the plugin may be imported)."""
    try:
        tree = pyast.parse(source)
    except SyntaxError as e:
        return [f"syntax error: {e}"]
    problems = []
    for node in pyast.walk(tree):
        if isinstance(node, pyast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    problems.append(f"line {node.lineno}: import {a.name} not allowed")
        elif isinstance(node, pyast.ImportFrom):
            problems.append(f"line {node.lineno}: `from ... import` not allowed; use `import random` / `import math`")
        elif isinstance(node, pyast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith("__")):
            problems.append(f"line {node.lineno}: name {node.id!r} not allowed")
        elif isinstance(node, pyast.Attribute) and (node.attr.startswith("_") or node.attr.startswith(FRAME_ATTRS)
                                                    or node.attr == "mro"):
            problems.append(f"line {node.lineno}: attribute {node.attr!r} not allowed")
        elif isinstance(node, pyast.Attribute) and node.attr in ("format", "format_map") and not _plain_format(node.value):
            problems.append(f"line {node.lineno}: format string with attribute/index fields not allowed")
        elif isinstance(node, (pyast.Global, pyast.Nonlocal)):
            problems.append(f"line {node.lineno}: global/nonlocal not allowed")
    defined = {n.name for n in tree.body if isinstance(n, pyast.FunctionDef)}
    defined |= {t.id for n in tree.body if isinstance(n, pyast.Assign)
                for t in n.targets if isinstance(t, pyast.Name)}
    for name in REQUIRED:
        if name not in defined:
            problems.append(f"missing required definition {name!r}")
    return problems


def load(path: Path):
    """Static-check, import and register a plugin; returns the module."""
    from . import ast as A
    source = Path(path).read_text()
    problems = static_check(source)
    if problems:
        raise ValueError("plugin rejected by static check:\n  " + "\n  ".join(problems))
    spec = importlib.util.spec_from_file_location(f"plugin_{Path(path).stem}", path)
    if spec is None or spec.loader is None:
        raise ValueError(f"cannot load plugin from {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not isinstance(mod.NAME, str) or not mod.KINDS or not all(isinstance(k, str) for k in mod.KINDS):
        raise ValueError("NAME must be a str and KINDS a non-empty list of str")
    A.register_plugin(mod)
    return mod
