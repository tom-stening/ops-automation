from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tests.helpers import load_script

m = load_script("maintenance_issue")

SAMPLE = """# Cadence

## Weekly Tasks

### W-01 — Scan

```bash
- not a bullet, it's code
```

- Run the scan.
- [x] Already ticked stays as is.

---

## Monthly Tasks

- Review things.
  - Nested detail stays nested.

## Quarterly Tasks

### Q-01 — Review

- Check docs.
"""


class MaintenanceIssueTests(unittest.TestCase):
    def test_titles(self) -> None:
        d = date(2026, 11, 17)
        self.assertEqual(m.issue_title("weekly", d), "Weekly maintenance: 2026-W47")
        self.assertEqual(m.issue_title("monthly", d), "Monthly maintenance: 2026-11")
        self.assertEqual(m.issue_title("quarterly", d), "Quarterly maintenance: Q4 2026")

    def test_weekly_section_checklist(self) -> None:
        body = m.render("weekly", SAMPLE, date(2026, 11, 17), "cadence.md")["body"]
        self.assertIn("- [ ] Run the scan.", body)
        self.assertIn("- [x] Already ticked stays as is.", body)
        self.assertIn("- not a bullet, it's code", body)
        self.assertNotIn("Review things", body)
        self.assertNotIn("\n---\n\n- [ ]", body)

    def test_monthly_keeps_nested_bullets(self) -> None:
        body = m.render("monthly", SAMPLE, date(2026, 11, 17), "cadence.md")["body"]
        self.assertIn("- [ ] Review things.", body)
        self.assertIn("  - Nested detail stays nested.", body)

    def test_missing_section_raises(self) -> None:
        with self.assertRaises(ValueError):
            m.extract_section("# nothing here", "Monthly Tasks")

    def test_shipped_cadence_file_has_every_section(self) -> None:
        text = m.DEFAULT_CADENCE_FILE.read_text(encoding="utf-8")
        for cadence in m.SECTION_HEADINGS:
            body = m.render(cadence, text, date(2026, 1, 1), "MAINTENANCE_CADENCE.md")["body"]
            self.assertIn("- [ ]", body, cadence)

    def test_main_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "c.md"
            f.write_text(SAMPLE)
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = m.main(["quarterly", "--cadence-file", str(f), "--date", "2027-02-01"])
        self.assertEqual(rc, 0)
        issue = json.loads(buf.getvalue())
        self.assertEqual(issue["title"], "Quarterly maintenance: Q1 2027")
        self.assertIn("- [ ] Check docs.", issue["body"])

    def test_main_missing_file(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m.main(["monthly", "--cadence-file", "/nonexistent.md"]), 1)


if __name__ == "__main__":
    unittest.main()
