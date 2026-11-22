# Security policy

This repository holds shared automation only. It must never contain
credentials, customer data or private business records; workflows read secrets
only through GitHub's `secrets` context in the calling repo.

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](../../security/advisories/new) for this
repository, not in a public issue. You should get a reply within five business
days.

## Supply-chain notes

- Third-party binaries (gitleaks, actionlint) are downloaded at pinned versions
  and verified against a pinned SHA-256 before use.
- Workflows request the least `permissions` they need, and checkouts that don't
  push use `persist-credentials: false`.
- Dependabot proposes updates to the actions these workflows use.
- Callers pin release tags such as `v1`. Release tags and `main` should be
  protected by a repository ruleset (no deletion or force-push), so a tag can only
  move by a deliberate release.
