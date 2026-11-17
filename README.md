# ops-automation

Shared GitHub Actions workflows and scripts for the day-to-day upkeep of every
repo in the business. Each repo calls these workflows instead of keeping its own
copy, so a fix here reaches all of them.

This repo is meant to be **public**. It holds tooling only: no business data,
customer data, credentials or internal documents. Repo-specific settings live in
each calling repo.

## Workflows

| Workflow | What it does | Suggested cadence | Caller permissions |
| --- | --- | --- | --- |
| [`triage-emails.yml`](.github/workflows/triage-emails.yml) | Files CI-failure emails (`emails/git_issues/unresolved/*.msg`) into `docs/KNOWN_ISSUES.md`, moves them to `in_progress/` and commits the result | Hourly and on push | `contents: write` |
| [`dependency-audit.yml`](.github/workflows/dependency-audit.yml) | Installs the Python project in a clean virtualenv and runs `pip-audit` on what was installed (task W-01) | Weekly | `contents: read` |
| [`secret-scan.yml`](.github/workflows/secret-scan.yml) | Scans git history with gitleaks for committed credentials | Every push and PR, full history weekly | `contents: read` |
| [`maintenance-reminder.yml`](.github/workflows/maintenance-reminder.yml) | Opens the period's checklist issue from [`MAINTENANCE_CADENCE.md`](docs/MAINTENANCE_CADENCE.md) | Monthly and quarterly | `contents: read`, `issues: write` |
| [`polyglot-assessment.yml`](.github/workflows/polyglot-assessment.yml) | Profiles the language mix and CPU-heavy Python functions and opens the quarterly review issue | Quarterly | `contents: read`, `issues: write` |

## Using them in a repo

1. Copy [`examples/ops.yml`](examples/ops.yml) to `.github/workflows/ops.yml`,
   set `hot-path-dir` to the repo's main package folder, and delete the jobs it
   doesn't need.
2. Copy [`examples/secret-scan.yml`](examples/secret-scan.yml) to
   `.github/workflows/secret-scan.yml`.
3. Optional: add `.github/polyglot-assessment.json` to describe the repo's
   performance profile (see [`examples/polyglot-assessment.json`](examples/polyglot-assessment.json)).

Workflows that run scripts check this repo out into `.ops-automation/`, so it
must be readable by the calling repo's `GITHUB_TOKEN`. Keep it public, or, while
it is private, allow access under *Settings → Actions → General → Access*.

### Inputs

Every input is optional unless marked required.

**triage-emails.yml**: `ops-ref` (default `main`), `python-version` (`3.12`).
Output `changed` is `'true'` when it committed something. Runs one at a time per
branch and rebases and retries if the branch moved during the run.

**dependency-audit.yml**: `install-command` (default `pip install -e .`, runs
inside the project virtualenv), `working-directory` (`.`), `python-version`,
`ignore-vulns` (space-separated IDs; record the reason in KNOWN_ISSUES),
`fail-on-vulnerability` (`true`), `pip-audit-version`. Findings go to the job
summary.

**secret-scan.yml**: `full-history` (default `false`: only the commits in the
push or pull request), `gitleaks-version` and `gitleaks-sha256` (the download is
checksum-verified; change both together). False positives go in the calling
repo's `.gitleaksignore` (one fingerprint per line, shown in the job summary).

**maintenance-reminder.yml**: `cadence` (**required**: `weekly`, `monthly` or
`quarterly`), `cadence-file` (a cadence document in the calling repo; default is
the shared one here), `labels` (`maintenance`), `assignee`, `ops-ref`. It does
nothing if an issue with the same title already exists, open or closed.

**polyglot-assessment.yml**: `hot-path-dir` (`.`), `config-file` (default
`.github/polyglot-assessment.json` if present), `open-issue` (`true`; when false
the report only goes to the job summary), `ops-ref`, `python-version`.

### Versions

Callers use `@main` today. Once a tag such as `v1` exists, pin to it for
stability: change the ref after `@` in each `uses:` line **and** set
`ops-ref: v1`, so the scripts come from the same release as the workflow.

## Scripts

All scripts use the Python standard library only (3.11 or newer) and can be run
locally against any repo:

```bash
OPS_REPO_ROOT=../RuleSet python scripts/triage_emails.py --dry-run
python scripts/triage_emails.py --repo-root ../RuleSet resolve KI-011 --note "Fixed in #42"
python scripts/polyglot_assess.py --repo-root ../RuleSet --hot-path-dir ruleset
python scripts/maintenance_issue.py quarterly --format markdown
```

## Developing

```bash
python -m unittest discover -s tests -t .   # unit tests, no dependencies
ruff check . && ruff format --check .        # lint (ruff 0.17)
```

CI ([`ci.yml`](.github/workflows/ci.yml)) runs the tests on Python 3.11 to 3.13,
ruff, actionlint, and calls the shared secret-scan, polyglot and dependency-audit
workflows on this repo the way a calling repo would. Dependabot keeps the
actions up to date.

## Related repos

- **RuleSet**: the governance engine, and the origin of these scripts.
- **company-records**: the business's own policies and registers, checked by RuleSet.

See [`docs/MAINTENANCE_CADENCE.md`](docs/MAINTENANCE_CADENCE.md) for the weekly,
monthly and quarterly routine, and [`SECURITY.md`](SECURITY.md) to report a problem.
