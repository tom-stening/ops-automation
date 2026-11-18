from __future__ import annotations

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests.helpers import load_script

r = load_script("release_check")

EXTRA = "      extra:\n        type: string\n        {}\n    outputs:"

BASE = """\
name: Demo (shared)

# A comment: with a colon
on:
  workflow_call:
    inputs:
      mode:
        description: "Mode: fast or slow"  # trailing comment
        type: string
        default: fast
      strict:
        type: boolean
        default: false
      script:
        description: >-
          folded text
          inputs: not a key
        type: string
        default: ""
    outputs:
      result:
        value: ${{ jobs.run.outputs.result }}
    secrets:
      token:
        required: false

jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - run: |
          echo "inputs:"
"""


def iface(source: str) -> dict:
    return {"demo.yml": r.workflow_interface(source)}


class ParseTests(unittest.TestCase):
    def test_reads_interface(self):
        i = r.workflow_interface(BASE)
        self.assertEqual(set(i.inputs), {"mode", "strict", "script"})
        self.assertEqual(i.inputs["mode"]["type"], "string")
        self.assertEqual(i.inputs["mode"]["description"], "Mode: fast or slow")
        self.assertEqual(i.outputs, {"result"})
        self.assertEqual(set(i.secrets), {"token"})

    def test_not_reusable(self):
        self.assertIsNone(r.workflow_interface("on:\n  push:\n    branches: [main]\n"))
        self.assertIsNone(r.workflow_interface("on: push\n"))

    def test_bare_workflow_call(self):
        self.assertEqual(r.workflow_interface("on: workflow_call\n").inputs, {})
        self.assertEqual(r.workflow_interface('"on":\n  workflow_call:\n').inputs, {})

    def test_reads_this_repos_workflows(self):
        root = Path(__file__).resolve().parent.parent / r.WORKFLOW_DIR
        found = {}
        for path in root.glob("*.yml"):
            i = r.workflow_interface(path.read_text(encoding="utf-8"))
            if i is not None:
                found[path.name] = i
        self.assertIn("maintenance-reminder.yml", found)
        self.assertEqual(found["maintenance-reminder.yml"].inputs["cadence"].get("required"), "true")
        self.assertIn("changed", found["triage-emails.yml"].outputs)
        self.assertNotIn("ci.yml", found)


class BreakingTests(unittest.TestCase):
    def check(self, new: str) -> list[str]:
        return r.breaking_changes(iface(BASE), iface(new))

    def test_identical(self):
        self.assertEqual(self.check(BASE), [])

    def test_optional_input_added_is_fine(self):
        new = BASE.replace("    outputs:", EXTRA.format("default: x"))
        self.assertEqual(self.check(new), [])

    def test_required_input_added(self):
        new = BASE.replace("    outputs:", EXTRA.format("required: true"))
        self.assertEqual(self.check(new), ["demo.yml: new input 'extra' is required"])

    def test_input_removed(self):
        new = BASE.replace("      strict:\n        type: boolean\n        default: false\n", "")
        self.assertEqual(self.check(new), ["demo.yml: input 'strict' removed"])

    def test_type_changed(self):
        new = BASE.replace("type: boolean", "type: string")
        self.assertEqual(self.check(new), ["demo.yml: input 'strict' changed type from boolean to string"])

    def test_now_required(self):
        new = BASE.replace("        default: fast\n", "        required: true\n")
        self.assertEqual(self.check(new), ["demo.yml: input 'mode' is now required"])

    def test_output_and_secret_removed(self):
        new = BASE.replace("      result:\n", "      renamed:\n").replace("      token:\n", "      key:\n")
        self.assertEqual(
            self.check(new), ["demo.yml: output 'result' removed", "demo.yml: secret 'token' removed"]
        )

    def test_workflow_removed(self):
        self.assertEqual(r.breaking_changes(iface(BASE), {}), ["demo.yml: reusable workflow removed"])


class GitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "user.name", "Test")
        self.commit(BASE)
        self.git("tag", "v1.0.0")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args: str) -> None:
        subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)

    def commit(self, source: str) -> None:
        path = self.repo / r.WORKFLOW_DIR / "demo.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "change")

    def run_main(self, *args: str) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return r.main(["--repo-root", str(self.repo), *args])

    def test_compatible_minor_release(self):
        self.commit(BASE.replace("default: fast", "default: slow"))
        self.assertEqual(self.run_main("breaking"), 0)
        self.assertEqual(self.run_main("check-version", "1.1.0"), 0)

    def test_breaking_change_needs_new_major(self):
        self.commit(BASE.replace("type: boolean", "type: string"))
        self.assertEqual(self.run_main("breaking"), 1)
        self.assertEqual(self.run_main("check-version", "1.1.0"), 1)
        self.assertEqual(self.run_main("check-version", "2.0.0"), 0)

    def test_version_must_increase(self):
        self.assertEqual(self.run_main("check-version", "1.0.0"), 1)
        self.git("tag", "v1.2.0")
        self.assertEqual(self.run_main("check-version", "1.1.9"), 1)

    def test_compares_with_latest_tag_by_version(self):
        self.git("tag", "v1.10.0")
        self.git("tag", "v1.9.0")
        self.assertEqual(r.latest_tag(self.repo), "v1.10.0")
        self.assertEqual(self.run_main("check-version", "1.9.5"), 1)

    def test_bad_version_strings(self):
        for bad in ("v1.2.0", "1.2", "0.1.0", "1.2.3-rc1"):
            self.assertEqual(self.run_main("check-version", bad), 2, bad)


if __name__ == "__main__":
    unittest.main()
