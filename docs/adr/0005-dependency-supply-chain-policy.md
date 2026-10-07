# ADR 0005 — Dependency supply-chain policy

**Status:** Accepted

## Context
JobPulse fetches untrusted content and holds API keys, so a compromised dependency or CI
action is a direct path to secrets. Recent ecosystem incidents (hijacked maintainer accounts,
re-pointed action tags such as the trivy-action compromise) are typically caught within days.

## Decision

| Control | Where | Setting |
|---|---|---|
| Release cooldown | `apps/web/pnpm-workspace.yaml` | `minimumReleaseAge: 10080` (7 days) |
| Release cooldown | `pyproject.toml` | `[tool.uv] exclude-newer = "7 days"` |
| Release cooldown | `.github/dependabot.yml` | `cooldown: { default-days: 7 }` per ecosystem |
| Trust downgrade guard | pnpm | `trustPolicy: no-downgrade` for releases < 30 days old (`trustPolicyIgnoreAfter: 43200`) |
| No exotic sources | pnpm | `blockExoticSubdeps: true` |
| Install scripts allowlist | pnpm | `allowBuilds` (only `unrs-resolver`, `sharp`) |
| Immutable CI actions | `.github/workflows/*` | every action pinned to a full commit SHA (version in a comment; Dependabot updates both) |
| Scanning | CI | Gitleaks, Semgrep, CodeQL, Trivy (fs + images), pip-audit, pnpm audit |

Dependency ranges are expressed at major (npm) or minor (Python) level so the resolver can
choose the newest release that satisfies the cooldown; the lockfiles record exact versions.

## Reviewed exceptions

- **`next-auth@5.0.0-beta.32` (pinned).** The npm publisher changed when Auth.js moved to the
  Better Auth team (announced 2025-09-22: https://www.better-auth.com/blog/authjs-joins-better-auth).
  That is a legitimate ownership transfer, and the release is older than the 30-day trust window.
  Auth.js is now in security-patch mode and its maintainers recommend Better Auth for new
  projects; migrating is a candidate follow-up, kept out of scope because the spec mandates Auth.js.
- **`source-map-js@1.2.2` (override + cooldown exclusion).** Fixes CVE-2026-93749 (DoS) and was
  published inside the cooldown window. Reviewed: same maintainer as 1.2.1, no new dependencies,
  no install scripts, small source diff with tests.

## Consequences
New upstream releases arrive about a week late. Urgent security fixes need an explicit, reviewed
exclusion, as above; that friction is intentional.
