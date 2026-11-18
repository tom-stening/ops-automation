#!/usr/bin/env python3
"""Check a release of the shared workflows for breaking interface changes.

Calling repos pin a major tag such as ``v1``, so a ``v1.x.y`` release must not
break any caller. This script compares the ``workflow_call`` interface (inputs,
their types and required flags, outputs and secrets) of every reusable
workflow in ``.github/workflows/`` between two git refs, and lists the changes
that would break a caller:

- a reusable workflow was removed (or stopped being reusable)
- an input, output or secret was removed or renamed
- an input changed type
- a new input or secret is required, or an optional one became required

Usage::

    python scripts/release_check.py breaking                  # latest release tag vs HEAD
    python scripts/release_check.py breaking --base v1.2.0 --head my-branch
    python scripts/release_check.py check-version 1.3.0       # used by release.yml

``check-version`` fails when the version is not newer than the latest
release, or when it keeps the same major version as the latest release but
the interface has breaking changes since then.

Standard library only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

WORKFLOW_DIR = ".github/workflows"
TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")
BLOCK_SCALAR_RE = re.compile(r"^[|>][+-]?\d*$")


# --- A tiny YAML subset reader --------------------------------------------
#
# The scripts here stay dependency-free, so instead of PyYAML this reads only
# what the interface check needs: nested block mappings with scalar values.
# Sequences, flow collections and block-scalar bodies are skipped.


def _strip_comment(text: str) -> str:
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
    return text.rstrip()


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def _split_key(text: str) -> tuple[str, str] | None:
    """Split ``key: value`` into its parts, or None if the line is not a mapping entry."""
    if text.startswith("- "):
        return None
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"" and i == 0:
            quote = ch
        elif ch == ":" and (i + 1 == len(text) or text[i + 1] in " \t"):
            return _unquote(text[:i].strip()), text[i + 1 :].strip()
    return None


def parse_mapping(source: str) -> dict:
    """Parse the block-mapping skeleton of a YAML document into nested dicts."""
    root: dict = {}
    stack: list[tuple[int, dict]] = [(-1, root)]
    skip_deeper_than: int | None = None
    for raw in source.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if skip_deeper_than is not None:
            if indent > skip_deeper_than:
                continue
            skip_deeper_than = None
        text = _strip_comment(raw.strip())
        parts = _split_key(text)
        if parts is None:
            # A sequence item or continuation line: ignore it and its children.
            skip_deeper_than = indent
            continue
        key, value = parts
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if value == "":
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
        elif BLOCK_SCALAR_RE.match(value):
            parent[key] = ""
            skip_deeper_than = indent
        else:
            parent[key] = _unquote(value)
    return root


# --- Workflow interfaces ---------------------------------------------------


@dataclass
class Interface:
    inputs: dict[str, dict] = field(default_factory=dict)
    outputs: set[str] = field(default_factory=set)
    secrets: dict[str, dict] = field(default_factory=dict)


def workflow_interface(source: str) -> Interface | None:
    """The ``workflow_call`` interface of a workflow, or None if it is not reusable."""
    doc = parse_mapping(source)
    # YAML 1.1 readers turn a bare ``on`` key into ``true``; accept both spellings.
    triggers = doc.get("on", doc.get("true"))
    if isinstance(triggers, str):
        return Interface() if triggers == "workflow_call" else None
    if not isinstance(triggers, dict) or "workflow_call" not in triggers:
        return None
    call = triggers["workflow_call"]
    if not isinstance(call, dict):
        return Interface()

    def section(name: str) -> dict[str, dict]:
        items = call.get(name)
        if not isinstance(items, dict):
            return {}
        return {k: (v if isinstance(v, dict) else {}) for k, v in items.items()}

    return Interface(
        inputs=section("inputs"),
        outputs=set(section("outputs")),
        secrets=section("secrets"),
    )


def _is_required(spec: dict) -> bool:
    return str(spec.get("required", "false")).lower() == "true"


def breaking_changes(old: dict[str, Interface], new: dict[str, Interface]) -> list[str]:
    """Changes between two sets of workflow interfaces that would break a caller."""
    problems: list[str] = []
    for name in sorted(old):
        before = old[name]
        after = new.get(name)
        if after is None:
            problems.append(f"{name}: reusable workflow removed")
            continue
        for key in sorted(before.inputs):
            if key not in after.inputs:
                problems.append(f"{name}: input '{key}' removed")
                continue
            old_type = before.inputs[key].get("type")
            new_type = after.inputs[key].get("type")
            if old_type and new_type and old_type != new_type:
                problems.append(f"{name}: input '{key}' changed type from {old_type} to {new_type}")
            if _is_required(after.inputs[key]) and not _is_required(before.inputs[key]):
                problems.append(f"{name}: input '{key}' is now required")
        for key in sorted(set(after.inputs) - set(before.inputs)):
            if _is_required(after.inputs[key]):
                problems.append(f"{name}: new input '{key}' is required")
        for key in sorted(before.outputs - after.outputs):
            problems.append(f"{name}: output '{key}' removed")
        for key in sorted(before.secrets):
            if key not in after.secrets:
                problems.append(f"{name}: secret '{key}' removed")
            elif _is_required(after.secrets[key]) and not _is_required(before.secrets[key]):
                problems.append(f"{name}: secret '{key}' is now required")
        for key in sorted(set(after.secrets) - set(before.secrets)):
            if _is_required(after.secrets[key]):
                problems.append(f"{name}: new secret '{key}' is required")
    return problems


# --- Git access ------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def interfaces_at(repo: Path, ref: str) -> dict[str, Interface]:
    listing = _git(repo, "ls-tree", "--name-only", f"{ref}:{WORKFLOW_DIR}")
    result: dict[str, Interface] = {}
    for name in listing.split():
        if not name.endswith((".yml", ".yaml")):
            continue
        interface = workflow_interface(_git(repo, "show", f"{ref}:{WORKFLOW_DIR}/{name}"))
        if interface is not None:
            result[name] = interface
    return result


def release_tags(repo: Path) -> list[tuple[int, int, int]]:
    versions = []
    for tag in _git(repo, "tag", "--list", "v*").split():
        m = TAG_RE.match(tag)
        if m:
            versions.append((int(m[1]), int(m[2]), int(m[3])))
    return sorted(versions)


def latest_tag(repo: Path) -> str | None:
    tags = release_tags(repo)
    return "v{}.{}.{}".format(*tags[-1]) if tags else None


# --- Commands --------------------------------------------------------------


def cmd_breaking(repo: Path, base: str | None, head: str) -> int:
    base = base or latest_tag(repo)
    if base is None:
        print("No release tag yet, so nothing to compare against.")
        return 0
    problems = breaking_changes(interfaces_at(repo, base), interfaces_at(repo, head))
    if not problems:
        print(f"No breaking interface changes since {base}.")
        return 0
    print(f"Breaking interface changes since {base} (these need a new major version):")
    for p in problems:
        print(f"- {p}")
    return 1


def cmd_check_version(repo: Path, version: str, head: str) -> int:
    m = VERSION_RE.match(version)
    if not m:
        print(f"error: '{version}' is not MAJOR.MINOR.PATCH, e.g. 1.0.0 (no leading v)", file=sys.stderr)
        return 2
    wanted = (int(m[1]), int(m[2]), int(m[3]))
    if wanted[0] == 0:
        print("error: start at 1.0.0; callers pin a major tag such as v1", file=sys.stderr)
        return 2
    tags = release_tags(repo)
    if not tags:
        print(f"First release: v{version}.")
        return 0
    latest = tags[-1]
    latest_name = "v{}.{}.{}".format(*latest)
    if wanted <= latest:
        print(f"error: v{version} is not newer than the latest release {latest_name}", file=sys.stderr)
        return 1
    if wanted[0] > latest[0]:
        print(f"Major release v{version} after {latest_name}: breaking changes are allowed.")
        return 0
    problems = breaking_changes(interfaces_at(repo, latest_name), interfaces_at(repo, head))
    if problems:
        print(
            f"error: v{version} keeps major version {wanted[0]}, but these changes since "
            f"{latest_name} would break callers pinned to v{wanted[0]}:",
            file=sys.stderr,
        )
        for p in problems:
            print(f"- {p}", file=sys.stderr)
        print(f"Release v{wanted[0] + 1}.0.0 instead, or restore the old interface.", file=sys.stderr)
        return 1
    print(f"v{version} is backward compatible with {latest_name}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo-root", type=Path, default=Path.cwd(), help="Repo to check (default: cwd)")
    parser.add_argument("--head", default="HEAD", help="Ref holding the candidate release (default: HEAD)")
    sub = parser.add_subparsers(dest="command", required=True)
    p_break = sub.add_parser("breaking", help="List breaking interface changes since a ref")
    p_break.add_argument("--base", help="Ref to compare against (default: the latest vX.Y.Z tag)")
    p_ver = sub.add_parser("check-version", help="Check that a release version is allowed")
    p_ver.add_argument("version", help="MAJOR.MINOR.PATCH, without the leading v")
    args = parser.parse_args(argv)
    if args.command == "breaking":
        return cmd_breaking(args.repo_root, args.base, args.head)
    return cmd_check_version(args.repo_root, args.version, args.head)


if __name__ == "__main__":
    sys.exit(main())
