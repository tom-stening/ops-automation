#!/usr/bin/env python3
"""Back up Git repositories to storage outside GitHub, and prove they restore.

For each repository it:

1. keeps a ``git clone --mirror`` under ``<dest>/mirrors/`` and fetches into it,
   so later runs only download what changed;
2. writes a dated ``git bundle`` of every ref (branches, tags, pull-request refs);
3. restores the bundle into a scratch repository, runs ``git fsck`` on it, and
   confirms the default branch comes back at the same commit;
4. optionally saves issues, pull requests, comments and releases as JSON, since
   those live in GitHub rather than in git;
5. records everything in ``MANIFEST.json`` and ``SHA256SUMS`` for the set.

Old dated sets beyond ``--keep`` are deleted; the mirrors are kept.

Usage::

    # every repo owned by the signed-in account (needs a token, see below)
    python scripts/backup_repos.py --dest /Volumes/Backup/git --owner my-user

    # named repos only, without issue metadata
    python scripts/backup_repos.py --dest ~/backups/git --repo my-user/app --no-metadata

    # re-check a set later, for example the copy on a second drive
    python scripts/backup_repos.py --verify /Volumes/Backup2/git/2026-12-01

Authentication: a token is read from ``GH_TOKEN`` or ``GITHUB_TOKEN``, or from
``gh auth token`` when the GitHub CLI is signed in. It needs read access to the
repositories' contents (and issues/metadata for ``--metadata``). Git receives it
through environment-based config, so it never appears on the command line or in
the mirror's config. Without a token, only public repositories can be read.

The destination is not encrypted by this script: put it on an encrypted disk or
an encrypted cloud folder, and keep a second copy somewhere else.

Standard library only; Python 3.11+. Exits 1 if any repository failed.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

SET_NAME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
LINK_NEXT_RE = re.compile(r'<([^>]+)>;\s*rel="next"')
DEFAULT_KEEP = 12


@dataclass
class RepoResult:
    repo: str
    ok: bool = False
    empty: bool = False
    bundle: str = ""
    bundle_sha256: str = ""
    bundle_bytes: int = 0
    refs: int = 0
    default_branch: str = ""
    head: str = ""
    restored_head: str = ""
    metadata: list[str] = field(default_factory=list)
    error: str = ""


# ── GitHub API ──────────────────────────────────────────────────────────────


def find_token() -> str:
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
    if shutil.which("gh"):
        try:
            out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        if out.returncode == 0:
            return out.stdout.strip()
    return ""


def api_get_all(url: str, token: str) -> list[dict]:
    """GET a paginated GitHub API list, following ``Link: rel="next"``."""
    items: list[dict] = []
    next_url: str | None = url
    while next_url:
        if urllib.parse.urlsplit(next_url).scheme != "https":
            raise ValueError(f"refusing non-HTTPS API URL: {next_url}")
        req = urllib.request.Request(next_url)  # noqa: S310 - scheme checked above
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "ops-automation-backup")
        if token:
            req.add_header("Authorization", f"Bearer {token}")
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            page = json.load(resp)
            link = resp.headers.get("Link", "") or ""
        if not isinstance(page, list):
            raise ValueError(f"expected a list from {next_url}")
        items.extend(page)
        m = LINK_NEXT_RE.search(link)
        next_url = m.group(1) if m else None
    return items


def list_owner_repos(owner: str, api_url: str, token: str, include_forks: bool) -> list[str]:
    """Repos owned by ``owner``: private ones too when the token can see them."""
    if token:
        url = f"{api_url}/user/repos?per_page=100&affiliation=owner,organization_member"
        repos = [r for r in api_get_all(url, token) if r["owner"]["login"].lower() == owner.lower()]
    else:
        repos = api_get_all(f"{api_url}/users/{owner}/repos?per_page=100&type=owner", token)
    return sorted(r["full_name"] for r in repos if include_forks or not r.get("fork"))


def save_metadata(repo: str, out_dir: Path, api_url: str, token: str) -> list[str]:
    stem = repo.replace("/", "__")
    saved = []
    for kind, path in (
        ("issues", f"/repos/{repo}/issues?state=all&per_page=100"),  # includes pull requests
        ("issue-comments", f"/repos/{repo}/issues/comments?per_page=100"),
        ("review-comments", f"/repos/{repo}/pulls/comments?per_page=100"),
        ("releases", f"/repos/{repo}/releases?per_page=100"),
    ):
        data = api_get_all(api_url + path, token)
        name = f"{stem}.{kind}.json"
        (out_dir / name).write_text(json.dumps(data, indent=1, sort_keys=True), encoding="utf-8")
        saved.append(name)
    return saved


# ── git ─────────────────────────────────────────────────────────────────────


def git_env(base_url: str, token: str) -> dict[str, str]:
    """Environment that hands git the token as an HTTP header for ``base_url`` only."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token and base_url.startswith("https://"):
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)  # keep any config already passed this way
        env[f"GIT_CONFIG_KEY_{n}"] = f"http.{base_url.rstrip('/')}/.extraheader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"AUTHORIZATION: basic {basic}"
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


def git(*args: str, env: dict[str, str] | None = None, cwd: Path | None = None) -> str:
    out = subprocess.run(["git", *args], capture_output=True, text=True, env=env, cwd=cwd)
    if out.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def update_mirror(repo: str, mirrors: Path, base_url: str, env: dict[str, str]) -> Path:
    mirror = mirrors / f"{repo}.git"
    url = f"{base_url.rstrip('/')}/{repo}.git"
    if mirror.exists():
        git("-C", str(mirror), "remote", "set-url", "origin", url, env=env)
        git("-C", str(mirror), "fetch", "--prune", "--quiet", "origin", env=env)
    else:
        mirror.parent.mkdir(parents=True, exist_ok=True)
        git("clone", "--mirror", "--quiet", url, str(mirror), env=env)
    return mirror


def default_branch(mirror: Path) -> str:
    try:
        ref = git("-C", str(mirror), "symbolic-ref", "HEAD")
    except RuntimeError:
        return ""
    return ref.removeprefix("refs/heads/")


def restore_bundle(bundle: Path) -> dict[str, str]:
    """Restore a bundle into a scratch repo, check every object, return its refs.

    ``git bundle verify`` alone only reads the header, so a truncated pack passes
    it; cloning and running ``git fsck`` proves the data is really there.
    """
    with tempfile.TemporaryDirectory(prefix="restore-") as tmp:
        target = Path(tmp) / "repo.git"
        git("clone", "--quiet", "--bare", str(bundle.resolve()), str(target))
        git("-C", str(target), "fsck", "--full", "--no-progress", "--no-dangling")
        lines = git("-C", str(target), "for-each-ref", "--format=%(objectname) %(refname)").splitlines()
    return {ref: sha for sha, ref in (line.split(" ", 1) for line in lines)}


def back_up_repo(
    repo: str,
    set_dir: Path,
    mirrors: Path,
    base_url: str,
    env: dict[str, str],
    metadata: bool,
    api_url: str,
    token: str,
) -> RepoResult:
    result = RepoResult(repo=repo)
    try:
        mirror = update_mirror(repo, mirrors, base_url, env)
        refs = git("-C", str(mirror), "for-each-ref", "--format=%(refname)").splitlines()
        result.refs = len(refs)
        if not refs:
            result.empty = result.ok = True
            return result
        result.default_branch = default_branch(mirror)
        bundle = set_dir / f"{repo.replace('/', '__')}.bundle"
        git("-C", str(mirror), "bundle", "create", "--quiet", str(bundle), "--all")
        restored = restore_bundle(bundle)
        result.bundle = bundle.name
        result.bundle_sha256 = sha256(bundle)
        result.bundle_bytes = bundle.stat().st_size
        if result.default_branch:
            result.head = git("-C", str(mirror), "rev-parse", f"refs/heads/{result.default_branch}")
            result.restored_head = restored.get(f"refs/heads/{result.default_branch}", "")
            if result.restored_head != result.head:
                raise RuntimeError(f"restored {result.restored_head}, expected {result.head}")
        if metadata:
            result.metadata = save_metadata(repo, set_dir, api_url, token)
        result.ok = True
    except (RuntimeError, OSError, ValueError) as exc:  # urllib errors are OSError
        result.error = str(exc)
    return result


# ── sets: write, verify, prune ──────────────────────────────────────────────


def write_manifest(set_dir: Path, results: list[RepoResult], today: datetime.date) -> None:
    manifest = {
        "created": today.isoformat(),
        "tool": "ops-automation scripts/backup_repos.py",
        "repos": [asdict(r) for r in results],
    }
    (set_dir / "MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    files = sorted(p for p in set_dir.iterdir() if p.is_file() and p.name != "SHA256SUMS")
    sums = "".join(f"{sha256(p)}  {p.name}\n" for p in files)
    (set_dir / "SHA256SUMS").write_text(sums, encoding="utf-8")


def verify_set(set_dir: Path) -> list[str]:
    """Return problems found in a backup set (empty list means it is sound)."""
    problems = []
    sums = set_dir / "SHA256SUMS"
    if not sums.exists():
        return [f"{sums} is missing"]
    for line in sums.read_text(encoding="utf-8").splitlines():
        digest, _, name = line.partition("  ")
        path = set_dir / name
        if not path.exists():
            problems.append(f"{name}: missing")
        elif sha256(path) != digest:
            problems.append(f"{name}: checksum mismatch")
        elif name.endswith(".bundle"):
            try:
                restore_bundle(path)
            except RuntimeError as exc:
                problems.append(f"{name}: {exc}")
    return problems


def prune_sets(dest: Path, keep: int) -> list[str]:
    sets = sorted(p for p in dest.iterdir() if p.is_dir() and SET_NAME_RE.match(p.name))
    removed = []
    for old in sets[: max(0, len(sets) - keep)]:
        shutil.rmtree(old)
        removed.append(old.name)
    return removed


def report(results: list[RepoResult], set_dir: Path, removed: list[str]) -> str:
    lines = [f"# Repository backup: {set_dir}", "", "| Repo | Result | Refs | Size | Restore check |"]
    lines.append("| --- | --- | --- | --- | --- |")
    for r in results:
        if not r.ok:
            lines.append(f"| {r.repo} | FAILED: {r.error} | | | |")
        elif r.empty:
            lines.append(f"| {r.repo} | empty repository, nothing to bundle | 0 | | |")
        else:
            check = f"{r.default_branch} at {r.head[:10]}" if r.head else "no default branch"
            size = f"{r.bundle_bytes / 1_048_576:.1f} MB"
            lines.append(f"| {r.repo} | ok | {r.refs} | {size} | {check} |")
    failed = sum(not r.ok for r in results)
    lines += ["", f"{len(results) - failed} of {len(results)} repositories backed up."]
    if removed:
        lines.append(f"Removed old sets: {', '.join(removed)}.")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dest", type=Path, help="backup folder (mirrors and dated sets go here)")
    ap.add_argument("--owner", help="back up every repository this user or organisation owns")
    ap.add_argument("--repo", action="append", default=[], help="owner/name; repeatable")
    ap.add_argument("--include-forks", action="store_true")
    ap.add_argument(
        "--metadata",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="save issues, pull requests, comments and releases as JSON (default on)",
    )
    ap.add_argument("--keep", type=int, default=DEFAULT_KEEP, help="dated sets to keep (default 12)")
    ap.add_argument("--base-url", default="https://github.com", help="git host to clone from")
    ap.add_argument("--api-url", default="https://api.github.com")
    ap.add_argument("--date", type=datetime.date.fromisoformat, default=datetime.date.today())
    ap.add_argument("--verify", type=Path, metavar="SET_DIR", help="only verify an existing set")
    args = ap.parse_args(argv)

    if args.verify:
        problems = verify_set(args.verify)
        for p in problems:
            print(f"- {p}")
        print(f"{args.verify}: {'OK' if not problems else f'{len(problems)} problem(s)'}")
        return 1 if problems else 0

    if not args.dest or not (args.owner or args.repo):
        ap.error("--dest and at least one of --owner or --repo are required")
    if args.keep < 1:
        ap.error("--keep must be at least 1")

    token = find_token()
    repos = list(args.repo)
    if args.owner:
        repos += list_owner_repos(args.owner, args.api_url, token, args.include_forks)
    repos = sorted(set(repos))
    if not repos:
        print("No repositories found.", file=sys.stderr)
        return 1

    dest = args.dest.expanduser().resolve()
    set_dir = dest / args.date.isoformat()
    if set_dir.exists():
        shutil.rmtree(set_dir)
    set_dir.mkdir(parents=True)
    env = git_env(args.base_url, token)
    results = [
        back_up_repo(r, set_dir, dest / "mirrors", args.base_url, env, args.metadata, args.api_url, token)
        for r in repos
    ]
    write_manifest(set_dir, results, args.date)
    removed = prune_sets(dest, args.keep)
    sys.stdout.write(report(results, set_dir, removed))
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
