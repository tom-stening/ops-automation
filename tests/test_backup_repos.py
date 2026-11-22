from __future__ import annotations

import contextlib
import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import load_script

m = load_script("backup_repos")


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def make_repo(root: Path, name: str, commits: int = 2) -> Path:
    """A bare repo at root/name.git with a main branch, a tag and a second branch."""
    work = root / "work" / name
    work.mkdir(parents=True)
    run("init", "--quiet", "-b", "main", cwd=work)
    for i in range(commits):
        (work / "f.txt").write_text(f"v{i}\n")
        run("add", "f.txt", cwd=work)
        run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", f"c{i}", cwd=work)
    run("tag", "v1", cwd=work)
    run("branch", "feature", cwd=work)
    bare = root / f"{name}.git"
    run("clone", "--quiet", "--bare", str(work), str(bare))
    return bare


class BackupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.src = self.root / "src"
        (self.src / "me").mkdir(parents=True)
        self.bare = make_repo(self.src / "me", "app")
        self.dest = self.root / "backups"
        self.env = mock.patch.dict("os.environ", {"GH_TOKEN": "", "GITHUB_TOKEN": ""})
        self.env.start()
        self.nogh = mock.patch.object(m.shutil, "which", return_value=None)
        self.nogh.start()

    def tearDown(self) -> None:
        self.nogh.stop()
        self.env.stop()
        self.tmp.cleanup()

    def backup(self, *extra: str, day: str = "2026-12-01") -> tuple[int, str]:
        argv = [
            "--dest",
            str(self.dest),
            "--repo",
            "me/app",
            "--no-metadata",
            "--base-url",
            f"file://{self.src}",
            "--date",
            day,
            *extra,
        ]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(argv)
        return rc, buf.getvalue()

    def test_backup_bundles_every_ref_and_restores(self) -> None:
        rc, out = self.backup()
        self.assertEqual(rc, 0, out)
        set_dir = self.dest / "2026-12-01"
        manifest = json.loads((set_dir / "MANIFEST.json").read_text())
        (entry,) = manifest["repos"]
        self.assertTrue(entry["ok"])
        self.assertEqual(entry["default_branch"], "main")
        self.assertEqual(entry["head"], run("rev-parse", "main", cwd=self.bare))
        self.assertEqual(entry["restored_head"], entry["head"])
        self.assertEqual(entry["refs"], 3)  # main, feature, v1
        heads = run("bundle", "list-heads", str(set_dir / "me__app.bundle"))
        self.assertIn("refs/tags/v1", heads)
        self.assertIn("refs/heads/feature", heads)
        self.assertIn("1 of 1 repositories backed up.", out)
        self.assertEqual(m.verify_set(set_dir), [])

    def test_second_run_fetches_new_commits(self) -> None:
        self.backup(day="2026-12-01")
        work = self.src / "me" / "work" / "app"
        (work / "g.txt").write_text("new\n")
        run("add", "g.txt", cwd=work)
        run("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", "c9", cwd=work)
        run("push", "--quiet", str(self.bare), "main", cwd=work)
        rc, _ = self.backup(day="2027-01-01")
        self.assertEqual(rc, 0)
        manifest = json.loads((self.dest / "2027-01-01" / "MANIFEST.json").read_text())
        self.assertEqual(manifest["repos"][0]["head"], run("rev-parse", "main", cwd=self.bare))

    def test_verify_detects_tampering(self) -> None:
        self.backup()
        set_dir = self.dest / "2026-12-01"
        bundle = set_dir / "me__app.bundle"
        bundle.write_bytes(bundle.read_bytes()[:-10])
        problems = m.verify_set(set_dir)
        self.assertEqual(problems, ["me__app.bundle: checksum mismatch"])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(m.main(["--verify", str(set_dir)]), 1)

    def test_verify_detects_corrupt_bundle_with_matching_checksum(self) -> None:
        self.backup()
        set_dir = self.dest / "2026-12-01"
        bundle = set_dir / "me__app.bundle"
        bundle.write_bytes(bundle.read_bytes()[:-10])
        (set_dir / "SHA256SUMS").write_text(f"{m.sha256(bundle)}  me__app.bundle\n")
        (problem,) = m.verify_set(set_dir)
        self.assertTrue(problem.startswith("me__app.bundle: git clone failed"), problem)

    def test_prunes_old_sets_but_keeps_mirrors(self) -> None:
        for day in ("2026-10-01", "2026-11-01", "2026-12-01"):
            self.backup("--keep", "2", day=day)
        sets = sorted(p.name for p in self.dest.iterdir() if p.is_dir())
        self.assertEqual(sets, ["2026-11-01", "2026-12-01", "mirrors"])

    def test_missing_repo_fails_run(self) -> None:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(
                [
                    "--dest",
                    str(self.dest),
                    "--repo",
                    "me/nope",
                    "--no-metadata",
                    "--base-url",
                    f"file://{self.src}",
                    "--date",
                    "2026-12-01",
                ]
            )
        self.assertEqual(rc, 1)
        self.assertIn("me/nope | FAILED", buf.getvalue())

    def test_empty_repo_is_ok(self) -> None:
        run("init", "--quiet", "--bare", str(self.src / "me" / "blank.git"))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = m.main(
                [
                    "--dest",
                    str(self.dest),
                    "--repo",
                    "me/blank",
                    "--no-metadata",
                    "--base-url",
                    f"file://{self.src}",
                    "--date",
                    "2026-12-01",
                ]
            )
        self.assertEqual(rc, 0, buf.getvalue())
        self.assertIn("empty repository", buf.getvalue())


class TokenAndApiTests(unittest.TestCase):
    def test_token_goes_in_env_not_argv(self) -> None:
        base = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "core.x", "GIT_CONFIG_VALUE_0": "y"}
        with mock.patch.dict("os.environ", base, clear=True):
            env = m.git_env("https://github.com", "secret")
            plain = m.git_env("file:///x", "secret")
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "core.x")  # existing entries kept
        self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.https://github.com/.extraheader")
        self.assertTrue(env["GIT_CONFIG_VALUE_1"].startswith("AUTHORIZATION: basic "))
        self.assertNotIn("secret", env["GIT_CONFIG_VALUE_1"])  # base64-encoded
        self.assertEqual(plain["GIT_CONFIG_COUNT"], "1")

    def test_api_follows_pagination_and_filters_owner(self) -> None:
        pages = {
            "https://api.example/user/repos?per_page=100&affiliation=owner,organization_member": (
                [
                    {"full_name": "me/a", "owner": {"login": "me"}, "fork": False},
                    {"full_name": "other/x", "owner": {"login": "other"}, "fork": False},
                ],
                '<https://api.example/next>; rel="next"',
            ),
            "https://api.example/next": ([{"full_name": "me/b", "owner": {"login": "Me"}, "fork": True}], ""),
        }

        class Resp(io.BytesIO):
            def __init__(self, url: str) -> None:
                body, link = pages[url]
                super().__init__(json.dumps(body).encode())
                self.headers = {"Link": link}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake(req, timeout):
            return Resp(req.full_url)

        with mock.patch.object(m.urllib.request, "urlopen", side_effect=fake):
            self.assertEqual(m.list_owner_repos("me", "https://api.example", "t", False), ["me/a"])
            self.assertEqual(m.list_owner_repos("me", "https://api.example", "t", True), ["me/a", "me/b"])

    def test_api_refuses_plain_http(self) -> None:
        with self.assertRaises(ValueError):
            m.api_get_all("http://api.example/x", "t")


if __name__ == "__main__":
    unittest.main()
