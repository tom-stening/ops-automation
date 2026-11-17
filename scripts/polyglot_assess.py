#!/usr/bin/env python3
"""Quarterly polyglot language assessment for any repository.

Counts the repository's lines per language, finds the synchronous Python
functions with the most loop and maths work, and renders a Markdown (or JSON)
report with a decision matrix for every candidate language.

Usage::

    python scripts/polyglot_assess.py [--json] [--out FILE] [--repo-root DIR]

Environment:

* ``OPS_REPO_ROOT``: repository to assess (default: this checkout).
* ``OPS_HOT_PATH_DIR``: package folder, relative to the root, scanned for
  CPU-heavy functions (default: the whole repository).
* ``OPS_POLYGLOT_CONFIG``: path to a JSON config (default:
  ``<root>/.github/polyglot-assessment.json`` when it exists).

The config lets each repository describe its own context instead of editing
this shared script. Every key is optional::

    {
      "bottleneck": "One paragraph on what limits this repo's performance.",
      "recommendation": "Markdown for section 4.",
      "action_items": ["Checklist item", "..."],
      "candidates": [ {"lang": "...", "verdict": "ADOPT|MONITOR|DEFER|REJECT", ...} ],
      "exclude_dirs": ["vendor"],
      "score_threshold": 6
    }

``candidates`` replaces the default list; each entry takes the same keys as
``DEFAULT_CANDIDATES`` below. Standard library only; Python 3.11+.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

EXCLUDE_DIRS = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "htmlcov",
        ".benchmarks",
        ".hypothesis",
        ".tox",
        ".nox",
        "build",
        "dist",
        "site-packages",
        # The shared workflows check this repo out inside the caller's workspace.
        ".ops-automation",
    }
)

# Language -> file extensions (or exact file names, for entries without a dot).
LANG_EXTENSIONS: dict[str, list[str]] = {
    "Python": [".py"],
    "TypeScript": [".ts", ".tsx"],
    "JavaScript": [".js", ".mjs", ".cjs"],
    "Rust": [".rs"],
    "Go": [".go"],
    "C": [".c", ".h"],
    "C++": [".cc", ".cpp", ".cxx", ".hh", ".hpp"],
    "Zig": [".zig"],
    "Shell": [".sh", ".bash"],
    "PowerShell": [".ps1", ".psm1"],
    "Terraform/HCL": [".tf", ".tfvars"],
    "YAML": [".yml", ".yaml"],
    "Typst": [".typ"],
    "Dockerfile": ["Dockerfile"],
    "Makefile": ["Makefile", "makefile", "GNUmakefile"],
    "SQL": [".sql"],
}

# IO markers: calls that block on network, disk, or IPC.
IO_MARKERS = frozenset(
    [
        "await ",
        "async def",
        "httpx",
        "aiohttp",
        "requests.",
        "open(",
        ".read(",
        ".write(",
        "asyncio.sleep",
        "aiofiles",
        "boto3",
        "redis",
        "sqlite3",
        "psycopg",
        "sqlalchemy",
        "subprocess.",
        "socket.",
        "urllib",
    ]
)

# CPU markers: computation that runs in the Python interpreter.
CPU_MARKERS = frozenset(
    [
        "math.",
        "sum(",
        "Counter(",
        "random.",
        "statistics.",
        "numpy",
        "np.",
        "bootstrap",
        "resamp",
        "cosine",
        "entropy",
        "variance",
        "sorted(",
        "enumerate(",
        "zip(",
        "map(",
        "hashlib.",
    ]
)

DEFAULT_BOTTLENECK = (
    "Not described for this repository. Add a `bottleneck` paragraph to "
    "`.github/polyglot-assessment.json` saying what limits performance "
    "(for example network calls to upstream APIs, disk IO, or CPU-bound "
    "computation). A native extension speeds up CPU work only; it cannot "
    "reduce network or disk wait time."
)

DEFAULT_RECOMMENDATION = (
    "Stay with the current primary language unless a function in the table "
    "above crosses an adoption trigger. For CPU hotspots in Python, measure "
    "first (`time.perf_counter` around the call, p95 under realistic load), "
    "then prefer a narrow Rust extension via [PyO3](https://pyo3.rs) for that "
    "one function over rewriting a service. Do not add a language whose "
    "operational cost outweighs a measured benefit at current scale."
)

DEFAULT_ACTION_ITEMS = [
    "Review the CPU-bound candidates above; add a timing probe to any that run on a request path",
    "Check each MONITOR trigger against current measurements",
    "Update `.github/polyglot-assessment.json` if the repo's context has changed",
    "Re-run this assessment next quarter (automated)",
]

DEFAULT_CANDIDATES: list[dict[str, Any]] = [
    {
        "lang": "TypeScript",
        "fit_areas": ["SDK clients for Node/browser", "Editor extensions", "Web dashboards"],
        "not_fit": ["CPU-heavy numeric work"],
        "adoption_cost": "Low to medium: separate toolchain (npm, bundler) and CI job.",
        "trigger": "A new browser-facing UI, editor extension or Node SDK is planned",
        "verdict": "MONITOR",
        "horizon": "On trigger",
    },
    {
        "lang": "Rust (via PyO3 extension)",
        "fit_areas": [
            "Tight numeric loops (resampling, similarity, parsing large inputs)",
            "Functions near the top of the CPU-candidate table",
        ],
        "not_fit": ["Async IO hot paths", "Anything that awaits network calls"],
        "adoption_cost": (
            "Medium: maturin/PyO3 build step, Rust toolchain in CI, and a small "
            "FFI surface. No unsafe code needed."
        ),
        "trigger": "A measured Python function exceeds 200 ms at p95 on a user-facing path",
        "verdict": "MONITOR",
        "horizon": "On trigger",
    },
    {
        "lang": "Go",
        "fit_areas": ["High-throughput network services", "Small standalone CLIs or sidecars"],
        "not_fit": ["Core business logic already in Python (two codebases to maintain)"],
        "adoption_cost": "High: parallel implementation, second deploy pipeline and runtime.",
        "trigger": "Sustained throughput needs that horizontal scaling can't meet cheaply",
        "verdict": "DEFER",
        "horizon": "On trigger",
    },
    {
        "lang": "WebAssembly",
        "fit_areas": ["Computation that must run in a browser or editor sandbox"],
        "not_fit": ["Server-side code"],
        "adoption_cost": "Medium: compile Rust/C to WASM and add it to a JS build.",
        "trigger": "Client-side CPU work exceeds 50 ms for a user action",
        "verdict": "MONITOR",
        "horizon": "On trigger",
    },
    {
        "lang": "Java / Kotlin",
        "fit_areas": ["Android SDK", "Enterprise JVM integrations"],
        "not_fit": ["Python-native services"],
        "adoption_cost": "High: JVM runtime and separate build toolchain.",
        "trigger": "Customers ask for an Android or JVM SDK",
        "verdict": "DEFER",
        "horizon": "On demand",
    },
    {
        "lang": "Swift",
        "fit_areas": ["iOS SDK", "macOS native client"],
        "not_fit": ["Server-side code"],
        "adoption_cost": "Medium: only worthwhile if Apple-platform distribution is planned.",
        "trigger": "An iOS/macOS client is on the roadmap",
        "verdict": "DEFER",
        "horizon": "On demand",
    },
    {
        "lang": "C / C++",
        "fit_areas": ["Low-level OS integration", "Native inference runtimes"],
        "not_fit": ["Application code: memory-unsafe code isn't justified here"],
        "adoption_cost": "High: manual memory management, fragile builds and ABI.",
        "trigger": "Only to self-host native model inference or bind an existing C library",
        "verdict": "REJECT",
        "horizon": "N/A",
    },
    {
        "lang": "Zig / Julia / R / Assembly",
        "fit_areas": ["Specialist systems, scientific or research work"],
        "not_fit": ["Production services at this team size"],
        "adoption_cost": "High: small ecosystems, separate runtimes, rare skills.",
        "trigger": "Not applicable",
        "verdict": "REJECT",
        "horizon": "N/A",
    },
]

VERDICT_ICON = {"ADOPT": "✅", "MONITOR": "🔍", "DEFER": "⏳", "REJECT": "❌"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class Config:
    root: Path
    hot_path_dir: Path
    bottleneck: str = DEFAULT_BOTTLENECK
    recommendation: str = DEFAULT_RECOMMENDATION
    action_items: list[str] = field(default_factory=lambda: list(DEFAULT_ACTION_ITEMS))
    candidates: list[dict[str, Any]] = field(default_factory=lambda: list(DEFAULT_CANDIDATES))
    exclude_dirs: frozenset[str] = EXCLUDE_DIRS
    score_threshold: int = 6
    source: str = "defaults"


def load_config(root: Path, hot_path: str | None = None, config_path: str | None = None) -> Config:
    root = root.resolve()
    hot = (root / (hot_path or ".")).resolve()
    if not hot.is_relative_to(root):
        raise SystemExit(f"ERROR: hot-path dir {hot_path!r} is outside the repository")
    cfg = Config(root=root, hot_path_dir=hot)

    path = Path(config_path) if config_path else root / ".github" / "polyglot-assessment.json"
    if not path.is_file():
        if config_path:
            raise SystemExit(f"ERROR: config file {config_path} not found")
        return cfg
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"ERROR: {path} must hold a JSON object")

    if "bottleneck" in data:
        cfg.bottleneck = str(data["bottleneck"])
    if "recommendation" in data:
        cfg.recommendation = str(data["recommendation"])
    if "action_items" in data:
        cfg.action_items = [str(x) for x in data["action_items"]]
    if "candidates" in data:
        cfg.candidates = [_check_candidate(c, path) for c in data["candidates"]]
    if "exclude_dirs" in data:
        cfg.exclude_dirs = EXCLUDE_DIRS | {str(x) for x in data["exclude_dirs"]}
    if "score_threshold" in data:
        cfg.score_threshold = int(data["score_threshold"])
    try:
        cfg.source = str(path.relative_to(root))
    except ValueError:
        cfg.source = str(path)
    return cfg


def _check_candidate(c: Any, path: Path) -> dict[str, Any]:
    if not isinstance(c, dict) or "lang" not in c or "verdict" not in c:
        raise SystemExit(f"ERROR: every candidate in {path} needs at least 'lang' and 'verdict'")
    out: dict[str, Any] = {
        "fit_areas": [],
        "not_fit": [],
        "adoption_cost": "Not assessed.",
        "trigger": "Not set.",
        "horizon": "N/A",
    }
    out.update(c)
    return out


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def _walk(top: Path, exclude: frozenset[str]):
    for root, dirs, files in os.walk(top):
        dirs[:] = [d for d in dirs if d not in exclude]
        for fname in files:
            yield Path(root) / fname


def _language_of(fname: str) -> str | None:
    for lang, exts in LANG_EXTENSIONS.items():
        for ext in exts:
            if fname.endswith(ext) if ext.startswith(".") else fname == ext:
                return lang
    return None


def count_lines_by_language(cfg: Config) -> dict[str, int]:
    counts: dict[str, int] = {}
    for path in _walk(cfg.root, cfg.exclude_dirs):
        lang = _language_of(path.name)
        if lang is None:
            continue
        try:
            with open(path, "rb") as fh:
                n = sum(1 for _ in fh)
        except OSError:
            continue
        counts[lang] = counts.get(lang, 0) + n
    return dict(sorted(((k, v) for k, v in counts.items() if v > 0), key=lambda x: (-x[1], x[0])))


def score_function(node: ast.FunctionDef, src: str) -> dict[str, int]:
    body = ast.get_source_segment(src, node) or ""
    loops = sum(1 for n in ast.walk(node) if isinstance(n, (ast.For, ast.While)))
    comps = sum(
        1
        for n in ast.walk(node)
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp))
    )
    cpu = sum(1 for m in CPU_MARKERS if m in body)
    io = sum(1 for m in IO_MARKERS if m in body)
    return {"score": loops + comps + cpu - io * 2, "loops": loops, "cpu_markers": cpu, "io_markers": io}


def find_cpu_candidates(cfg: Config) -> list[dict[str, Any]]:
    """Find sync Python functions with high loop/maths density."""
    candidates: list[dict[str, Any]] = []
    if not cfg.hot_path_dir.is_dir():
        return candidates
    for path in _walk(cfg.hot_path_dir, cfg.exclude_dirs):
        if path.suffix != ".py":
            continue
        try:
            src = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(src)
        except (SyntaxError, ValueError, OSError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):  # async defs are IO-bound
                continue
            s = score_function(node, src)
            if s["score"] >= cfg.score_threshold:
                candidates.append(
                    {
                        "file": path.relative_to(cfg.root).as_posix(),
                        "line": node.lineno,
                        "name": node.name,
                        **s,
                    }
                )
    return sorted(candidates, key=lambda x: (-x["score"], x["file"], x["line"]))


def git_summary(root: Path) -> dict[str, str]:
    """Return current branch and last commit."""
    try:
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        commit = subprocess.check_output(
            ["git", "log", "-1", "--format=%h %s"],
            cwd=root,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        branch, commit = "unknown", "unknown"
    return {"branch": branch, "commit": commit}


def current_quarter(today: datetime.date | None = None) -> str:
    today = today or datetime.date.today()
    return f"Q{(today.month - 1) // 3 + 1} {today.year}"


def _md(text: str) -> str:
    """Keep free text from breaking a Markdown table cell."""
    return " ".join(str(text).replace("|", "/").split())


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_markdown(
    cfg: Config,
    lang_counts: dict[str, int],
    cpu_candidates: list[dict[str, Any]],
    git_info: dict[str, str],
    quarter: str,
) -> str:
    total_lines = sum(lang_counts.values())
    lines: list[str] = []
    a = lines.append

    a(f"# Polyglot Language Assessment — {quarter}")
    a("")
    a(f"> **Generated:** {datetime.date.today().isoformat()}  ")
    a(f"> **Repo:** {os.environ.get('GITHUB_REPOSITORY') or cfg.root.name}  ")
    a(f"> **Branch:** {git_info['branch']}  ")
    a(f"> **Commit:** {_md(git_info['commit'])}  ")
    a(f"> **Config:** {cfg.source}  ")
    a("> **Next scheduled run:** quarterly (1 Jan / 1 Apr / 1 Jul / 1 Oct)")
    a("")
    a("## 1. Current Language Composition")
    a("")
    if total_lines:
        a("| Language | Lines | % of total |")
        a("|---|---:|---:|")
        for lang, count in lang_counts.items():
            a(f"| {lang} | {count:,} | {count / total_lines * 100:.1f}% |")
        a(f"| **Total** | **{total_lines:,}** | **100%** |")
    else:
        a("_No source files in recognised languages._")
    a("")
    a("## 2. Bottleneck Profile")
    a("")
    a(cfg.bottleneck)
    a("")
    a("### CPU-Bound Function Candidates")
    a("")
    hot_rel = cfg.hot_path_dir.relative_to(cfg.root).as_posix()
    if cpu_candidates:
        a(
            f"Synchronous Python functions under `{hot_rel}` with the highest "
            "loop/maths density, the most plausible candidates for a native extension:"
        )
        a("")
        a("| Score | Location | Function | Loops | CPU markers | IO markers |")
        a("|---:|---|---|---:|---:|---:|")
        for c in cpu_candidates[:10]:
            a(
                f"| {c['score']} | `{c['file']}:{c['line']}` | `{c['name']}` | "
                f"{c['loops']} | {c['cpu_markers']} | {c['io_markers']} |"
            )
    else:
        a(f"_No synchronous Python function under `{hot_rel}` scored {cfg.score_threshold} or more._")
    a("")
    a(
        "> Score = loops + comprehensions + CPU markers − 2×IO markers. "
        "Async functions are excluded (they are IO-bound by definition)."
    )
    a("")
    a("## 3. Decision Matrix")
    a("")
    a("| Verdict | Language | Horizon | Trigger |")
    a("|---|---|---|---|")
    for c in cfg.candidates:
        icon = VERDICT_ICON.get(str(c["verdict"]).split()[0].upper(), "❓")
        a(
            f"| {icon} **{_md(c['verdict'])}** | {_md(c['lang'])} | {_md(c['horizon'])} | "
            f"{_md(c['trigger'])[:100]} |"
        )
    a("")
    for c in cfg.candidates:
        icon = VERDICT_ICON.get(str(c["verdict"]).split()[0].upper(), "❓")
        a(f"### {icon} {c['lang']}")
        a("")
        a(f"**Verdict:** {c['verdict']}  **Horizon:** {c['horizon']}")
        a("")
        if c["fit_areas"]:
            a("**Good fit for:**")
            a("\n".join(f"- {x}" for x in c["fit_areas"]))
            a("")
        if c["not_fit"]:
            a("**Poor fit for:**")
            a("\n".join(f"- {x}" for x in c["not_fit"]))
            a("")
        a(f"**Adoption cost:** {c['adoption_cost']}")
        a("")
        a(f"**Adoption trigger:** {c['trigger']}")
        a("")
    a("## 4. Overall Recommendation")
    a("")
    a(cfg.recommendation)
    a("")
    a("## 5. Action Items for This Quarter")
    a("")
    for item in cfg.action_items:
        a(f"- [ ] {item}")
    a("")
    a("---")
    a("")
    a(
        "*Generated by `scripts/polyglot_assess.py` in ops-automation. To tailor it, "
        "add `.github/polyglot-assessment.json` to this repository.*"
    )
    return "\n".join(lines) + "\n"


def render_json(
    cfg: Config,
    lang_counts: dict[str, int],
    cpu_candidates: list[dict[str, Any]],
    git_info: dict[str, str],
    quarter: str,
) -> str:
    return json.dumps(
        {
            "quarter": quarter,
            "repo": os.environ.get("GITHUB_REPOSITORY") or cfg.root.name,
            "generated": datetime.date.today().isoformat(),
            "config": cfg.source,
            "git": git_info,
            "language_composition": lang_counts,
            "cpu_candidates": cpu_candidates[:10],
            "decisions": cfg.candidates,
            "recommendation": cfg.recommendation,
        },
        indent=2,
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Polyglot language assessment")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of Markdown")
    parser.add_argument("--out", metavar="FILE", help="Write output to FILE instead of stdout")
    parser.add_argument("--repo-root", type=Path, help="Repository to assess (default: $OPS_REPO_ROOT)")
    parser.add_argument(
        "--hot-path-dir", help="Folder to scan for CPU-heavy functions (default: $OPS_HOT_PATH_DIR)"
    )
    parser.add_argument(
        "--config", help="JSON config (default: $OPS_POLYGLOT_CONFIG or .github/polyglot-assessment.json)"
    )
    args = parser.parse_args(argv)

    root = args.repo_root or Path(os.environ.get("OPS_REPO_ROOT") or Path(__file__).resolve().parent.parent)
    cfg = load_config(
        root,
        hot_path=args.hot_path_dir or os.environ.get("OPS_HOT_PATH_DIR") or ".",
        config_path=args.config or os.environ.get("OPS_POLYGLOT_CONFIG") or None,
    )
    quarter = current_quarter()
    lang_counts = count_lines_by_language(cfg)
    cpu_candidates = find_cpu_candidates(cfg)
    git_info = git_summary(cfg.root)

    render = render_json if args.json else render_markdown
    output = render(cfg, lang_counts, cpu_candidates, git_info, quarter)

    if args.out:
        Path(args.out).write_text(output, encoding="utf-8")
        print(f"Assessment written to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
