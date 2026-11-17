#!/usr/bin/env python3
"""Render a maintenance-checklist issue from a cadence document.

Reads the ``## Weekly Tasks``, ``## Monthly Tasks`` or ``## Quarterly Tasks``
section of a Markdown cadence file (default: this repo's
``docs/MAINTENANCE_CADENCE.md``) and turns its bullets into a GitHub issue
checklist. Prints JSON ``{"title": ..., "body": ...}`` so the calling
workflow can open the issue with ``gh``.

Usage::

    python scripts/maintenance_issue.py monthly
    python scripts/maintenance_issue.py quarterly --cadence-file docs/MAINTENANCE_CADENCE.md
    python scripts/maintenance_issue.py quarterly --date 2026-10-01 --format markdown

Standard library only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
from pathlib import Path

DEFAULT_CADENCE_FILE = Path(__file__).resolve().parent.parent / "docs" / "MAINTENANCE_CADENCE.md"

SECTION_HEADINGS = {
    "weekly": "Weekly Tasks",
    "monthly": "Monthly Tasks",
    "quarterly": "Quarterly Tasks",
}


def period_label(cadence: str, day: datetime.date) -> str:
    if cadence == "weekly":
        year, week, _ = day.isocalendar()
        return f"{year}-W{week:02d}"
    if cadence == "monthly":
        return f"{day:%Y-%m}"
    return f"Q{(day.month - 1) // 3 + 1} {day.year}"


def issue_title(cadence: str, day: datetime.date) -> str:
    return f"{cadence.capitalize()} maintenance: {period_label(cadence, day)}"


def extract_section(markdown: str, heading: str) -> str:
    """Return the body of ``## heading`` up to the next ``## `` heading."""
    pattern = re.compile(
        r"^##\s+" + re.escape(heading) + r"\s*$(.*?)(?=^##\s|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(markdown)
    if not m:
        raise ValueError(f"No '## {heading}' section found")
    return m.group(1).strip()


def to_checklist(section: str) -> str:
    """Turn top-level bullets into task-list items; leave everything else alone.

    Bullets inside fenced code blocks are left as they are, and existing
    ``- [ ]`` items are kept. Horizontal rules are dropped.
    """
    out: list[str] = []
    in_fence = False
    for line in section.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence and stripped == "---":
            continue
        if not in_fence and re.match(r"^- (?!\[[ xX]\] )", line):
            line = "- [ ] " + line[2:]
        out.append(line)
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render(cadence: str, cadence_md: str, day: datetime.date, source: str) -> dict[str, str]:
    section = extract_section(cadence_md, SECTION_HEADINGS[cadence])
    body = (
        f"Scheduled {cadence} maintenance for **{period_label(cadence, day)}**.\n\n"
        "Tick each item as it is done and close this issue when the list is complete. "
        "Record findings in `docs/KNOWN_ISSUES.md` or a linked issue.\n\n"
        f"{to_checklist(section)}\n\n"
        "---\n\n"
        f"*Opened automatically by ops-automation from `{source}`.*\n"
    )
    return {"title": issue_title(cadence, day), "body": body}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("cadence", choices=sorted(SECTION_HEADINGS))
    parser.add_argument("--cadence-file", type=Path, default=DEFAULT_CADENCE_FILE)
    parser.add_argument(
        "--date",
        type=datetime.date.fromisoformat,
        default=None,
        help="Date the period is computed from (default: today, UTC)",
    )
    parser.add_argument("--format", choices=["json", "markdown"], default="json")
    args = parser.parse_args(argv)

    day = args.date or datetime.datetime.now(datetime.UTC).date()
    try:
        md = args.cadence_file.read_text(encoding="utf-8")
        issue = render(args.cadence, md, day, args.cadence_file.name)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.format == "markdown":
        sys.stdout.write(f"# {issue['title']}\n\n{issue['body']}")
    else:
        json.dump(issue, sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
