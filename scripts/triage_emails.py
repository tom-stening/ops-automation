#!/usr/bin/env python3
"""
Email triage for GitHub Actions failure notifications.

Watches ``emails/git_issues/unresolved/`` in a repository for ``.msg`` files
(GitHub "Run failed" notification emails saved from Outlook), records each
failing workflow in ``docs/KNOWN_ISSUES.md``, and moves the emails through the
lifecycle ``unresolved -> in_progress -> resolved``.

Usage::

    python scripts/triage_emails.py                       # triage unresolved emails
    python scripts/triage_emails.py --dry-run             # preview, change nothing
    python scripts/triage_emails.py resolve KI-011 --note "Fixed in #42"
    python scripts/triage_emails.py resolve KI-011 --dry-run

Set ``OPS_REPO_ROOT`` (or pass ``--repo-root``) to run it against another
repository. Run by ``.github/workflows/triage-emails.yml``. Standard library
only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Layout:
    """Where the script reads and writes, relative to one repository root."""

    root: Path

    @property
    def unresolved(self) -> Path:
        return self.root / "emails" / "git_issues" / "unresolved"

    @property
    def in_progress(self) -> Path:
        return self.root / "emails" / "git_issues" / "in_progress"

    @property
    def resolved(self) -> Path:
        return self.root / "emails" / "git_issues" / "resolved"

    @property
    def known_issues(self) -> Path:
        return self.root / "docs" / "KNOWN_ISSUES.md"


def default_root() -> Path:
    return Path(os.environ.get("OPS_REPO_ROOT") or Path(__file__).resolve().parent.parent).resolve()


# Filename pattern written by the mail export, e.g.
#   [tom-steningExampleRepo] Run failed CI - main (a135114)_20260506_213520.msg
FILENAME_RE = re.compile(r"^\[(.+?)\] Run failed (.+?) - (.+?) \((.+?)\)_(\d{8}_\d{6})\.msg$")

# GitHub Actions run URL embedded in the binary .msg content.
RUN_URL_RE = re.compile(rb"https://github\.com/[^\s\"\\&]+/actions/runs/(\d+)")

# Row title written for every workflow; also used to map a KI back to its emails.
TITLE_SUFFIX = " workflow persistent failures"

# Severity per workflow key (lower-case, hyphenated). Anything else is P2.
SEVERITY_BY_WORKFLOW: dict[str, str] = {
    "ci": "P2",
    "deploy-docs": "P2",
    "business-board-health": "P3",
}

KI_TABLE_HEADER = "| ID | Title | Severity | Status | Target Milestone | Notes |"


def today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def cell(text: str) -> str:
    """Make text safe to place inside one Markdown table cell."""
    return " ".join(text.replace("|", "/").split())


def workflow_key(name: str) -> str:
    """Normalised key used for grouping (lower-case, spaces to hyphens)."""
    return "-".join(name.lower().split())


def default_known_issues(repo_name: str) -> str:
    return f"""# {repo_name} Known Issues

_Document type: Defect and limitation register_
_Status: Active_
_Last updated: {today()} (bootstrap)_

## Purpose

Track confirmed defects, limitations, and operational risks with clear status
and target milestones.

## Issue Register

{KI_TABLE_HEADER}
| --- | --- | --- | --- | --- | --- |

## Severity Reference

| Level | Meaning |
| --- | --- |
| P0 | Release-blocking or customer-impacting now |
| P1 | High — blocks revenue, delivery, or compliance milestone |
| P2 | Medium — degrades quality or delivery velocity |
| P3 | Low — polish or hygiene |
"""


def ensure_layout(layout: Layout) -> None:
    for d in (layout.unresolved, layout.in_progress, layout.resolved, layout.known_issues.parent):
        d.mkdir(parents=True, exist_ok=True)
    if not layout.known_issues.exists():
        layout.known_issues.write_text(default_known_issues(layout.root.name), encoding="utf-8")


def detect_repo_slug(root: Path) -> str | None:
    """Return ``owner/name`` for the repo's GitHub remote, if it has one."""
    if slug := os.environ.get("GITHUB_REPOSITORY"):
        return slug
    try:
        remote = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return slug_from_remote(remote)


def slug_from_remote(remote: str) -> str | None:
    remote = remote.removesuffix(".git").rstrip("/")
    if remote.startswith("git@github.com:"):
        return remote.split(":", 1)[1]
    if "github.com/" in remote:
        return remote.split("github.com/", 1)[1].lstrip("/")
    return None


def build_run_urls(run_ids: list[str], repo_slug: str | None) -> str:
    if not run_ids or not repo_slug:
        return "see emails"
    return ", ".join(f"[run](https://github.com/{repo_slug}/actions/runs/{run_id})" for run_id in run_ids[:3])


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class EmailMeta:
    path: Path
    org_repo: str  # e.g. "tom-steningExampleRepo"
    workflow: str  # e.g. "CI"
    branch: str
    sha: str
    timestamp: datetime
    run_id: str = ""  # extracted from binary content

    @property
    def workflow_key(self) -> str:
        return workflow_key(self.workflow)

    @property
    def title(self) -> str:
        return cell(self.workflow.strip()) + TITLE_SUFFIX

    @property
    def severity(self) -> str:
        return SEVERITY_BY_WORKFLOW.get(self.workflow_key, "P2")


def parse_filename(path: Path) -> EmailMeta | None:
    m = FILENAME_RE.match(path.name)
    if not m:
        return None
    org_repo, workflow, branch, sha, ts_raw = m.groups()
    try:
        ts = datetime.strptime(ts_raw, "%Y%m%d_%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    meta = EmailMeta(
        path=path,
        org_repo=org_repo,
        workflow=workflow,
        branch=branch,
        sha=sha,
        timestamp=ts,
    )
    try:
        urls = RUN_URL_RE.findall(path.read_bytes())
        if urls:
            meta.run_id = urls[0].decode()
    except OSError:
        pass
    return meta


# ---------------------------------------------------------------------------
# KNOWN_ISSUES.md helpers
# ---------------------------------------------------------------------------
def _row_re(ki_id: str) -> re.Pattern[str]:
    """Match the whole table row for ki_id (one line)."""
    return re.compile(r"^\|\s*" + re.escape(ki_id) + r"\s*\|.*$", re.MULTILINE)


def split_row(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def join_row(cells: list[str]) -> str:
    return "| " + " | ".join(cells) + " |"


def next_ki_id(content: str) -> str:
    """Return the next KI-NNN id (one higher than the current max)."""
    ids = re.findall(r"\bKI-(\d+)\b", content)
    if not ids:
        return "KI-001"
    return f"KI-{max(int(i) for i in ids) + 1:03d}"


def find_open_ki(content: str, title: str) -> str | None:
    """Return the id of an open issue whose Title cell equals ``title``."""
    for line in content.splitlines():
        if not re.match(r"^\|\s*KI-\d+\s*\|", line):
            continue
        cells = split_row(line)
        if len(cells) >= 4 and cells[1].lower() == title.lower() and cells[3].lower() == "open":
            return cells[0]
    return None


def table_row(ki_id: str, title: str, severity: str, notes: str) -> str:
    return join_row([ki_id, title, severity, "Open", "—", f"First seen {today()}. {cell(notes)}"])


def insert_ki_row(content: str, row: str) -> str:
    """Append a row to the end of the Issue Register table."""
    lines = content.splitlines(keepends=True)
    header_idx = next((i for i, ln in enumerate(lines) if ln.strip().startswith("| ID ")), None)
    if header_idx is None:
        sep = "" if content.endswith("\n") else "\n"
        table = f"{KI_TABLE_HEADER}\n|{' --- |' * 6}\n{row}\n"
        return f"{content}{sep}\n## Issue Register\n\n{table}"
    end = header_idx + 1
    while end < len(lines) and lines[end].lstrip().startswith("|"):
        end += 1
    if not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines.insert(end, row + "\n")
    return "".join(lines)


def update_ki_notes(content: str, ki_id: str, extra_note: str) -> str:
    """Append extra_note to the Notes (last) cell of an existing KI row."""

    def _append(m: re.Match[str]) -> str:
        cells = split_row(m.group(0))
        cells[-1] = f"{cells[-1]} {cell(extra_note)}".strip()
        return join_row(cells)

    return _row_re(ki_id).sub(_append, content, count=1)


def close_ki(content: str, ki_id: str, resolution_note: str) -> str:
    """Set the Status cell of ki_id to Closed and append the resolution note."""

    def _close(m: re.Match[str]) -> str:
        cells = split_row(m.group(0))
        if len(cells) >= 4:
            cells[3] = "Closed"
        cells[-1] = f"{cells[-1]} Closed {today()}: {cell(resolution_note)}".strip()
        return join_row(cells)

    return _row_re(ki_id).sub(_close, content, count=1)


def ki_title(content: str, ki_id: str) -> str | None:
    m = _row_re(ki_id).search(content)
    if not m:
        return None
    cells = split_row(m.group(0))
    return cells[1] if len(cells) > 1 else None


def update_last_updated(content: str, summary: str) -> str:
    pattern = re.compile(r"_Last updated:[^\n]*")
    stamp = f"_Last updated: {today()} ({cell(summary)})_"
    if pattern.search(content):
        return pattern.sub(lambda _: stamp, content, count=1)
    return f"{stamp}\n\n{content}"


# ---------------------------------------------------------------------------
# Core triage logic
# ---------------------------------------------------------------------------
def triage(layout: Layout, dry_run: bool = False) -> int:
    """Process every unresolved email. Returns the number of new KI rows."""
    msg_files = sorted(layout.unresolved.glob("*.msg")) if layout.unresolved.is_dir() else []
    if not msg_files:
        print("No unresolved emails found.")
        return 0

    print(f"Found {len(msg_files)} unresolved email(s).")
    repo_slug = detect_repo_slug(layout.root)

    parsed: list[EmailMeta] = []
    for path in msg_files:
        meta = parse_filename(path)
        if meta is None:
            print(f"  [SKIP] Could not parse filename: {path.name}")
            continue
        parsed.append(meta)

    if not parsed:
        print("No parseable emails.")
        return 0

    groups: dict[str, list[EmailMeta]] = {}
    for meta in parsed:
        groups.setdefault(meta.workflow_key, []).append(meta)

    if not dry_run:
        ensure_layout(layout)
    ki_content = (
        layout.known_issues.read_text(encoding="utf-8")
        if layout.known_issues.exists()
        else default_known_issues(layout.root.name)
    )
    new_ki_count = 0

    for wf_key, metas in sorted(groups.items()):
        metas.sort(key=lambda m: m.timestamp)
        first, last = metas[0], metas[-1]
        count = len(metas)
        shas = sorted({cell(m.sha) for m in metas})
        run_ids = sorted({m.run_id for m in metas if m.run_id})
        title = first.title

        existing = find_open_ki(ki_content, title)
        if existing:
            note = f"Updated {today()}: +{count} new failure(s) across commits {', '.join(shas[:5])}."
            print(f"  [{existing}] Updating open issue for {wf_key!r} (+{count} emails).")
            ki_content = update_ki_notes(ki_content, existing, note)
        else:
            ki_id = next_ki_id(ki_content)
            notes = (
                f"{count} failure(s) observed between "
                f"{first.timestamp:%Y-%m-%d} and {last.timestamp:%Y-%m-%d}. "
                f"Branches: {', '.join(sorted({cell(m.branch) for m in metas}))}. "
                f"Commits: {', '.join(shas[:5])}{'...' if len(shas) > 5 else ''}. "
                f"Runs: {build_run_urls(run_ids, repo_slug)}."
            )
            print(f"  [{ki_id}] Creating new issue for {wf_key!r} ({count} emails).")
            ki_content = insert_ki_row(ki_content, table_row(ki_id, title, first.severity, notes))
            new_ki_count += 1

        for meta in metas:
            print(f"    -> in_progress: {meta.path.name}")
            if not dry_run:
                shutil.move(str(meta.path), str(layout.in_progress / meta.path.name))

    if dry_run:
        print("\n[DRY RUN] No changes written.")
        return new_ki_count

    ki_content = update_last_updated(ki_content, f"email triage: {len(parsed)} emails processed")
    layout.known_issues.write_text(ki_content, encoding="utf-8")
    print(f"\nWrote {layout.known_issues.relative_to(layout.root)}.")
    return new_ki_count


def resolve(layout: Layout, ki_id: str, resolution: str, dry_run: bool = False) -> int:
    """Close ki_id and move its in_progress emails to resolved/.

    Only emails whose workflow matches the issue's title are moved. Returns
    the number of emails moved.
    """
    if not layout.known_issues.exists():
        raise SystemExit(f"ERROR: {layout.known_issues} does not exist")
    content = layout.known_issues.read_text(encoding="utf-8")
    title = ki_title(content, ki_id)
    if title is None:
        raise SystemExit(f"ERROR: {ki_id} not found in {layout.known_issues.name}")

    print(f"Closing {ki_id} ({title}): {resolution}")
    if not dry_run:
        content = close_ki(content, ki_id, resolution)
        content = update_last_updated(content, f"{ki_id} closed")
        layout.known_issues.write_text(content, encoding="utf-8")

    wanted = workflow_key(title.removesuffix(TITLE_SUFFIX)) if title.endswith(TITLE_SUFFIX) else None
    moved = 0
    if layout.in_progress.is_dir():
        for msg in sorted(layout.in_progress.glob("*.msg")):
            meta = parse_filename(msg)
            if wanted is None or meta is None or meta.workflow_key != wanted:
                continue
            print(f"  -> resolved: {msg.name}")
            if not dry_run:
                layout.resolved.mkdir(parents=True, exist_ok=True)
                shutil.move(str(msg), str(layout.resolved / msg.name))
            moved += 1
    print(f"Moved {moved} email(s) to resolved/.")
    return moved


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    def add_common(p: argparse.ArgumentParser, default: object) -> None:
        # The subcommand repeats these with SUPPRESS so it can't reset a value
        # given before the subcommand name.
        p.add_argument("--dry-run", action="store_true", default=default, help="Preview only, no changes.")
        p.add_argument(
            "--repo-root",
            type=Path,
            default=default if default is argparse.SUPPRESS else None,
            help="Repository to work on (default: $OPS_REPO_ROOT, else this checkout).",
        )

    parser = argparse.ArgumentParser(description="Triage GitHub Actions failure emails.")
    add_common(parser, False)
    sub = parser.add_subparsers(dest="cmd")
    resolve_p = sub.add_parser("resolve", help="Close a KI and move its emails to resolved/.")
    add_common(resolve_p, argparse.SUPPRESS)
    resolve_p.add_argument("ki_id", help="e.g. KI-011")
    resolve_p.add_argument("--note", default="Fixed.", help="Resolution note.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    layout = Layout((args.repo_root or default_root()).resolve())

    if args.cmd == "resolve":
        resolve(layout, args.ki_id, args.note, dry_run=args.dry_run)
    else:
        count = triage(layout, dry_run=args.dry_run)
        if count > 0:
            print(f"\n{count} new KI entr{'y' if count == 1 else 'ies'} added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
