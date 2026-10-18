"""Tests that need no GPU and no Slang toolchain, so they run in CI on Linux.

  python3 -m unittest discover -s tests

They cover the parts of the harness whose correctness the GPU results rest on:
the reference evaluator, the plugin lint, the admission gate's planted fault,
the blind-workspace stripping and the transcript audit.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from agent import audit, make_blind
from agent.admit import plant_fault
from difftest import ast as A
from difftest import plugins

ROOT = Path(__file__).resolve().parent.parent
M32 = 0xFFFFFFFF
LINT_BASE = ("NAME = 'x'\nKINDS = ['k']\ndef generate(g, kind, depth): pass\n"
             "def to_slang(node, p): pass\ndef evaluate(node, e, vmap): pass\n")


class ReferenceTest(unittest.TestCase):
    def test_every_family_prints_and_evaluates_to_uint32x4(self):
        env = {"a": (1, 2, M32, 0x80000000), "b": (0, 31, 32, 0x7FFFFFFF), "s": 33}
        for family, kinds in A.FAMILIES.items():
            gen = A.Gen(11, kinds)
            for _ in range(50):
                node = gen.vec()
                self.assertTrue(A.to_slang(node).strip(), family)
                val = A.evaluate(node, env)
                assert isinstance(val, tuple), (family, val)
                self.assertEqual(len(val), 4, family)
                self.assertTrue(all(0 <= x <= M32 for x in val), (family, val))

    def test_generation_is_deterministic(self):
        kinds = A.FAMILIES["arith"]
        self.assertEqual(A.to_slang(A.Gen(5, kinds).vec()), A.to_slang(A.Gen(5, kinds).vec()))


class PluginLintTest(unittest.TestCase):
    BYPASSES = [
        "import os",
        "from random import _os as o",
        "import random\nrandom._os.getcwd()",
        "__builtins__['__imp' + 'ort__']('os')",
        "__loader__.get_data('/etc/hosts')",
        "(i for i in ()).gi_frame.f_builtins['__import__']('os')",
        "import random\n'{0._os.environ}'.format(random)",
        "open('/etc/passwd')",
    ]

    def test_known_escapes_are_rejected(self):
        for src in self.BYPASSES:
            self.assertTrue(plugins.static_check(LINT_BASE + src), src)

    def test_agent_written_families_and_good_example_pass(self):
        files = sorted((ROOT / "agent/families").glob("*.py")) + [ROOT / "agent/examples/minmax3.py"]
        self.assertGreaterEqual(len(files), 9)
        for f in files:
            self.assertEqual(plugins.static_check(f.read_text()), [], f.name)

    def test_missing_definitions_are_reported(self):
        self.assertTrue(any("missing" in p for p in plugins.static_check("NAME = 'x'\n")))


class PlantedFaultTest(unittest.TestCase):
    def test_fault_lands_in_f0_only(self):
        msl = ("uint4 f0_0(uint4 a_0)\n{\n    return a_0 + uint4(1U);\n}\n"
               "uint4 f1_0(uint4 a_1)\n{\n    return a_1;\n}\n")
        out = plant_fault(msl)
        self.assertIn("return (a_0 + uint4(1U)) + uint4(1u);", out)
        self.assertIn("return a_1;", out)


class BlindWorkspaceTest(unittest.TestCase):
    def test_hand_families_are_stripped_and_module_still_compiles(self):
        stripped = make_blind.strip_hand_families((ROOT / "difftest/ast.py").read_text())
        self.assertNotIn("<hand:signed-bitfield>", stripped)
        self.assertNotIn("bitfield", stripped.lower())
        compile(stripped, "ast.py", "exec")


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.blind = str(Path.home() / "Downloads/_bughunter-blind")

    def test_flags_web_tools_and_outside_paths(self):
        self.assertTrue(audit.violations("WebSearch", {"query": "slang"}, self.blind))
        self.assertTrue(audit.violations("Read", {"file_path": str(Path.home() / "secrets.txt")}, self.blind))
        self.assertTrue(audit.violations("Read", {"file_path": self.blind + "2/AGENT.md"}, self.blind))
        self.assertTrue(audit.violations("Bash", {"command": "curl https://example.com"}, self.blind))

    def test_allows_workspace_and_toolchain(self):
        self.assertEqual(audit.violations("Read", {"file_path": self.blind + "/AGENT.md"}, self.blind), [])
        tool = str(Path.home() / "VulkanSDK/1.4.363.0/macOS/bin/spirv-dis")
        self.assertEqual(audit.violations("Bash", {"command": f"{tool} k.spv"}, self.blind), [])

    def test_transcript_parsing(self):
        line = {"message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}}
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(line) + "\n")
        self.assertEqual(list(audit.tool_calls(Path(f.name))), [("Read", {"file_path": "x"})])


if __name__ == "__main__":
    unittest.main()
