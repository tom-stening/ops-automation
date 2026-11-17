from __future__ import annotations

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import load_script

t = load_script("triage_emails")

CI_1 = "[ownerRepo] Run failed CI - main (a135114)_20260506_213520.msg"
CI_2 = "[ownerRepo] Run failed CI - main (b246225)_20260507_080000.msg"
DOCS = "[ownerRepo] Run failed Deploy Docs - main (c357336)_20260507_090000.msg"


class TriageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "repo"
        self.layout = t.Layout(self.root)
        self.layout.unresolved.mkdir(parents=True)
        env = mock.patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repo"})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)

    def add_email(self, name: str, run_id: str | None = None) -> Path:
        body = b"\x00\x01binary"
        if run_id:
            body += f" https://github.com/owner/repo/actions/runs/{run_id}\x00".encode()
        path = self.layout.unresolved / name
        path.write_bytes(body)
        return path

    def run_quiet(self, fn, *args, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def known_issues(self) -> str:
        return self.layout.known_issues.read_text(encoding="utf-8")


class ParseTests(TriageTestCase):
    def test_parse_filename_and_run_id(self) -> None:
        meta = t.parse_filename(self.add_email(CI_1, run_id="12345"))
        assert meta is not None
        self.assertEqual(meta.workflow, "CI")
        self.assertEqual(meta.branch, "main")
        self.assertEqual(meta.sha, "a135114")
        self.assertEqual(meta.run_id, "12345")
        self.assertEqual(meta.timestamp.isoformat(), "2026-05-06T21:35:20+00:00")
        self.assertEqual(meta.title, "CI workflow persistent failures")

    def test_unparseable_names_are_skipped(self) -> None:
        self.assertIsNone(t.parse_filename(Path("random.msg")))
        self.assertIsNone(t.parse_filename(Path("[x] Run failed CI - main (abc)_20261399_999999.msg")))

    def test_workflow_key_and_severity(self) -> None:
        meta = t.parse_filename(self.add_email(DOCS))
        assert meta is not None
        self.assertEqual(meta.workflow_key, "deploy-docs")
        self.assertEqual(meta.severity, "P2")

    def test_slug_from_remote(self) -> None:
        self.assertEqual(t.slug_from_remote("git@github.com:owner/repo.git"), "owner/repo")
        self.assertEqual(t.slug_from_remote("https://github.com/owner/repo"), "owner/repo")
        self.assertIsNone(t.slug_from_remote("https://gitlab.com/owner/repo.git"))

    def test_cell_strips_pipes_and_newlines(self) -> None:
        self.assertEqual(t.cell("a | b\nc"), "a / b c")


class TriageFlowTests(TriageTestCase):
    def test_creates_register_and_moves_emails(self) -> None:
        self.add_email(CI_1, run_id="111")
        self.add_email(CI_2, run_id="222")
        self.add_email(DOCS)
        created = self.run_quiet(t.triage, self.layout)

        self.assertEqual(created, 2)
        ki = self.known_issues()
        self.assertIn("| KI-001 | CI workflow persistent failures | P2 | Open |", ki)
        self.assertIn("| KI-002 | Deploy Docs workflow persistent failures | P2 | Open |", ki)
        self.assertIn("https://github.com/owner/repo/actions/runs/111", ki)
        self.assertIn("2 failure(s) observed between 2026-05-06 and 2026-05-07", ki)
        self.assertEqual(list(self.layout.unresolved.glob("*.msg")), [])
        self.assertEqual(len(list(self.layout.in_progress.glob("*.msg"))), 3)
        # Rows land inside the register table, before the severity reference.
        self.assertLess(ki.index("KI-002"), ki.index("## Severity Reference"))

    def test_second_run_updates_existing_open_issue(self) -> None:
        self.add_email(CI_1)
        self.run_quiet(t.triage, self.layout)
        self.add_email(CI_2)
        created = self.run_quiet(t.triage, self.layout)

        self.assertEqual(created, 0)
        ki = self.known_issues()
        self.assertNotIn("KI-002", ki)
        self.assertIn("+1 new failure(s) across commits b246225.", ki)
        row = next(line for line in ki.splitlines() if line.startswith("| KI-001"))
        self.assertEqual(len(t.split_row(row)), 6)

    def test_dry_run_changes_nothing(self) -> None:
        self.add_email(CI_1)
        created = self.run_quiet(t.triage, self.layout, dry_run=True)
        self.assertEqual(created, 1)
        self.assertFalse(self.layout.known_issues.exists())
        self.assertEqual(len(list(self.layout.unresolved.glob("*.msg"))), 1)

    def test_no_emails_is_a_no_op(self) -> None:
        self.assertEqual(self.run_quiet(t.triage, self.layout), 0)
        self.assertFalse(self.layout.known_issues.exists())

    def test_existing_register_without_table_gets_one(self) -> None:
        self.layout.known_issues.parent.mkdir(parents=True)
        self.layout.known_issues.write_text("# Known issues\n\nKI-007 was fixed long ago.\n")
        self.add_email(CI_1)
        self.run_quiet(t.triage, self.layout)
        ki = self.known_issues()
        self.assertIn("## Issue Register", ki)
        self.assertIn("| KI-008 | CI workflow persistent failures |", ki)

    def test_closed_issue_is_not_reused(self) -> None:
        self.add_email(CI_1)
        self.run_quiet(t.triage, self.layout)
        self.run_quiet(t.resolve, self.layout, "KI-001", "Fixed.")
        self.add_email(CI_2)
        self.run_quiet(t.triage, self.layout)
        self.assertIn("| KI-002 | CI workflow persistent failures | P2 | Open |", self.known_issues())


class ResolveTests(TriageTestCase):
    def test_resolve_closes_row_and_moves_only_matching_emails(self) -> None:
        self.add_email(CI_1)
        self.add_email(DOCS)
        self.run_quiet(t.triage, self.layout)

        moved = self.run_quiet(t.resolve, self.layout, "KI-001", "Fixed in #4")

        self.assertEqual(moved, 1)
        ki = self.known_issues()
        row = next(line for line in ki.splitlines() if line.startswith("| KI-001"))
        self.assertEqual(t.split_row(row)[3], "Closed")
        self.assertIn("Fixed in #4", row)
        self.assertEqual([p.name for p in self.layout.resolved.glob("*.msg")], [CI_1])
        self.assertEqual([p.name for p in self.layout.in_progress.glob("*.msg")], [DOCS])

    def test_resolve_requires_exact_id(self) -> None:
        self.add_email(CI_1)
        self.run_quiet(t.triage, self.layout)
        with self.assertRaises(SystemExit):
            self.run_quiet(t.resolve, self.layout, "KI-00", "x")

    def test_resolve_dry_run(self) -> None:
        self.add_email(CI_1)
        self.run_quiet(t.triage, self.layout)
        before = self.known_issues()
        moved = self.run_quiet(t.resolve, self.layout, "KI-001", "x", dry_run=True)
        self.assertEqual(moved, 1)
        self.assertEqual(self.known_issues(), before)
        self.assertEqual(len(list(self.layout.in_progress.glob("*.msg"))), 1)

    def test_cli_resolve(self) -> None:
        self.add_email(CI_1)
        self.run_quiet(t.main, ["--repo-root", str(self.root)])
        self.run_quiet(t.main, ["resolve", "KI-001", "--note", "Done", "--repo-root", str(self.root)])
        self.assertIn("| Closed |", self.known_issues())


class HelperTests(unittest.TestCase):
    def test_next_ki_id(self) -> None:
        self.assertEqual(t.next_ki_id("nothing"), "KI-001")
        self.assertEqual(t.next_ki_id("KI-009 and KI-012"), "KI-013")

    def test_update_last_updated(self) -> None:
        out = t.update_last_updated("_Last updated: 2020-01-01 (old)_\n", "new | thing")
        self.assertRegex(out, r"^_Last updated: \d{4}-\d{2}-\d{2} \(new / thing\)_\n$")
        self.assertTrue(t.update_last_updated("body", "x").startswith("_Last updated:"))

    def test_update_notes_only_touches_one_row(self) -> None:
        content = "| KI-001 | A | P2 | Open | — | one |\n| KI-0011 | B | P2 | Open | — | two |\n"
        out = t.update_ki_notes(content, "KI-001", "more")
        self.assertIn("| KI-001 | A | P2 | Open | — | one more |", out)
        self.assertIn("| KI-0011 | B | P2 | Open | — | two |", out)


if __name__ == "__main__":
    unittest.main()
