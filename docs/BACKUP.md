# Backing up repositories outside GitHub

Git is distributed, but a GitHub account is still a single point of failure: a
lockout, a compromised account, a billing problem or a deleted repository can
cut off every repo at once. `scripts/backup_repos.py` keeps an independent copy
that restores without GitHub.

## What it saves

| Saved | How | Not saved |
| --- | --- | --- |
| Every branch, tag and pull-request ref | `git bundle --all`, from a local `--mirror` clone | Actions secrets and variables |
| Issues, pull requests and their comments | GitHub API, as JSON (`--metadata`, on by default) | Repository settings, rulesets, webhooks |
| Releases (notes and asset links) | GitHub API, as JSON | Release asset files, wikis, Actions artifacts and logs |

Each run writes a dated set, for example `2026-12-01/`, holding one `.bundle`
per repo, the JSON files, `MANIFEST.json` and `SHA256SUMS`. Before a set is
reported as good, each bundle is restored into a scratch repository, checked
with `git fsck`, and its default branch compared with the mirror.

## Running it

Run it on your own computer (it doesn't need GitHub Actions):

```bash
gh auth login                       # once; or export GH_TOKEN=<fine-grained token>
python scripts/backup_repos.py --dest /path/to/encrypted/backup --owner <user-or-org>
```

- `--repo owner/name` (repeatable) backs up named repos instead of, or as well
  as, everything an owner has.
- `--keep 12` (default) keeps a year of monthly sets. Mirrors are kept and
  updated in place, so later runs are fast.
- `--no-metadata` skips the API export.
- The exit code is 1 if any repo failed, so it can run from cron or a scheduled
  task.

A read-only token is enough: a fine-grained token with **Contents: read**,
**Issues: read**, **Pull requests: read** and **Metadata: read** on the repos
to back up. The token is passed to git through environment config, never on the
command line or in a saved remote URL.

## Where to keep the copies

The script doesn't encrypt. Follow 3-2-1:

1. Keep the working set on an **encrypted** disk (FileVault, BitLocker, LUKS) or
   in an encrypted cloud folder.
2. Copy the newest set to a **second place** that isn't the same disk or account,
   for example an encrypted USB drive kept elsewhere or a different cloud
   provider.
3. Check the second copy without GitHub:

   ```bash
   python scripts/backup_repos.py --verify /path/to/second/copy/2026-12-01
   ```

## Restoring

```bash
git clone owner__repo.bundle repo           # working copy from the bundle
cd repo && git remote set-url origin <new host URL>
git push --mirror origin                    # all branches and tags to the new host
```

Issues and pull requests come back as JSON for reference; importing them into a
new host is a manual step.
