from __future__ import annotations

import contextlib
import io
import json
import tempfile
import textwrap
import unittest
from datetime import date
from pathlib import Path

from tests.helpers import load_script

p = load_script("polyglot_assess")

HEAVY = textwrap.dedent(
    """
    import math

    def crunch(xs):
        total = 0
        for x in xs:
            for y in xs:
                for z in xs:
                    total += math.sqrt(x * y * z)
        return sum([v for v in xs]) + sum(sorted(xs)) + total

    async def fetch():
        for _ in range(10):
            pass

    def light():
        return 1
    """
)


class PolyglotTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "core.py").write_text(HEAVY)
        (self.root / "app.ts").write_text("export const a = 1;\nexport const b = 2;\n")
        (self.root / "Dockerfile").write_text("FROM python:3.12\n")
        # Excluded folders must not be counted.
        for skip in (".ops-automation", "node_modules", ".venv"):
            (self.root / skip).mkdir()
            (self.root / skip / "x.py").write_text("print(1)\n" * 50)

    def test_counts_lines_and_skips_excluded_dirs(self) -> None:
        cfg = p.load_config(self.root)
        counts = p.count_lines_by_language(cfg)
        self.assertEqual(counts["Python"], len(HEAVY.splitlines()))
        self.assertEqual(counts["TypeScript"], 2)
        self.assertEqual(counts["Dockerfile"], 1)

    def test_finds_cpu_candidates_with_relative_paths(self) -> None:
        cfg = p.load_config(self.root, hot_path="pkg")
        found = p.find_cpu_candidates(cfg)
        self.assertEqual([c["name"] for c in found], ["crunch"])
        self.assertEqual(found[0]["file"], "pkg/core.py")

    def test_hot_path_outside_repo_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            p.load_config(self.root, hot_path="../..")

    def test_config_file_overrides_defaults(self) -> None:
        (self.root / ".github").mkdir()
        (self.root / ".github" / "polyglot-assessment.json").write_text(
            json.dumps(
                {
                    "bottleneck": "Network calls to upstream APIs.",
                    "action_items": ["Do the thing"],
                    "candidates": [{"lang": "Rust", "verdict": "MONITOR"}],
                    "exclude_dirs": ["pkg"],
                }
            )
        )
        cfg = p.load_config(self.root)
        self.assertEqual(cfg.source, ".github/polyglot-assessment.json")
        self.assertEqual(cfg.candidates[0]["horizon"], "N/A")
        self.assertNotIn("Python", p.count_lines_by_language(cfg))
        md = p.render_markdown(cfg, {}, [], {"branch": "b", "commit": "c"}, "Q1 2027")
        self.assertIn("Network calls to upstream APIs.", md)
        self.assertIn("- [ ] Do the thing", md)
        self.assertIn("_No source files in recognised languages._", md)

    def test_bad_config_fails_clearly(self) -> None:
        (self.root / "bad.json").write_text("{not json")
        with self.assertRaises(SystemExit):
            p.load_config(self.root, config_path=str(self.root / "bad.json"))
        (self.root / "bad2.json").write_text(json.dumps({"candidates": [{"lang": "Go"}]}))
        with self.assertRaises(SystemExit):
            p.load_config(self.root, config_path=str(self.root / "bad2.json"))
        with self.assertRaises(SystemExit):
            p.load_config(self.root, config_path=str(self.root / "missing.json"))

    def test_markdown_table_cells_are_escaped(self) -> None:
        cfg = p.load_config(self.root)
        cfg.candidates = [
            p._check_candidate({"lang": "A|B", "verdict": "DEFER", "trigger": "x|y"}, Path("c"))
        ]
        md = p.render_markdown(cfg, {"Python": 1}, [], {"branch": "b", "commit": "c|d"}, "Q1 2027")
        self.assertIn("| ⏳ **DEFER** | A/B | N/A | x/y |", md)

    def test_current_quarter(self) -> None:
        self.assertEqual(p.current_quarter(date(2026, 1, 1)), "Q1 2026")
        self.assertEqual(p.current_quarter(date(2026, 6, 30)), "Q2 2026")
        self.assertEqual(p.current_quarter(date(2026, 10, 1)), "Q4 2026")

    def test_main_writes_json(self) -> None:
        out = self.root / "out.json"
        with contextlib.redirect_stderr(io.StringIO()):
            p.main(["--json", "--out", str(out), "--repo-root", str(self.root), "--hot-path-dir", "pkg"])
        data = json.loads(out.read_text())
        self.assertEqual(data["cpu_candidates"][0]["name"], "crunch")
        self.assertIn("Python", data["language_composition"])


if __name__ == "__main__":
    unittest.main()
