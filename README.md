# ops-automation

Shared GitHub Actions and scripts that run the day-to-day upkeep of every repo
in the business. Each repo calls these workflows instead of keeping its own copy,
so a fix here reaches all of them.

## Workflows

| Workflow | What it does | Cadence (suggested) |
| --- | --- | --- |
| `triage-emails.yml` | Files CI-failure emails (`emails/git_issues/unresolved/*.msg`) into the caller's `docs/KNOWN_ISSUES.md` and moves them to `in_progress/` | Hourly and on push |
| `dependency-audit.yml` | Installs the caller's Python project and runs `pip-audit` (maintenance task W-01) | Weekly |
| `polyglot-assessment.yml` | Profiles the caller's language mix and opens a quarterly review issue | Quarterly |

## Using them in a repo

Copy [`examples/ops.yml`](examples/ops.yml) to `.github/workflows/ops.yml` in the
repo, set `hot-path-dir` to its main package folder, and delete any jobs it doesn't need.

The workflows check out this repo to get the scripts, so it must be readable by
the calling repo's `GITHUB_TOKEN`. Keep it **public** (it holds no business data),
or, if it's private, allow access under *Settings → Actions → General → Access*.

## Scripts

Both scripts run on the standard library only and can be run locally against any
repo by setting `OPS_REPO_ROOT`:

```bash
OPS_REPO_ROOT=../RuleSet python scripts/triage_emails.py --dry-run
OPS_REPO_ROOT=../RuleSet OPS_HOT_PATH_DIR=ruleset python scripts/polyglot_assess.py
```

## Related repos

- **RuleSet**: the governance engine. Origin of these scripts.
- **company-records**: the business's own policies and registers, checked by RuleSet.

See [`docs/MAINTENANCE_CADENCE.md`](docs/MAINTENANCE_CADENCE.md) for the weekly, monthly and quarterly routine.
