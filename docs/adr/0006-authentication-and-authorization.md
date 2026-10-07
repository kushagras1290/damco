# ADR 0006 — Authentication and authorization

**Status:** Accepted (supersedes the shared-HS256-secret design in the first release)

## Context
The web app (Auth.js + GitHub OAuth) calls the API on behalf of the user. The first release
used a shared HS256 secret and trusted a `role` claim minted by the web app, and identified
owners by GitHub *login*.

Problems: any holder of the shared secret (including the API) could forge tokens; the API
delegated authorization to the web tier; GitHub logins are mutable and can be re-registered
by someone else after a rename; a role baked into an 8-hour session could not be revoked.

## Decision

1. **Asymmetric tokens.** The web signs per-request access tokens with Ed25519 (`EdDSA`,
   5-minute TTL, `kid`, `jti`, `nbf`). The API holds only public keys (`API_JWT_JWKS`) and
   refuses to start if the set contains private material. Multiple `kid`s enable rotation.
2. **Server-side authorization.** Tokens carry the immutable GitHub id (`sub=github:<id>`)
   and a display login - no role. The API derives the role from its own `OWNER_GITHUB_IDS`.
3. **Immutable identity.** Owners are allowlisted by numeric GitHub user id, never by login.
4. **Owner-only sign-in.** Only allowlisted owners can create a session; everyone else browses
   anonymously as the read-only public demo (a session would grant nothing extra).
5. **Per-request role evaluation.** The Auth.js `jwt` callback recomputes the role on every
   request, so removing an owner takes effect immediately.
6. **Hardening.** `read:user` OAuth scope only; `trustHost` derived from `AUTH_URL`
   (never forced); strict production config validation; same-origin check on proxied writes
   (in addition to SameSite=Lax cookies); structured sign-in/deny audit logs; `AUTH_SECRET_1`
   for zero-downtime session-secret rotation.

## Consequences
Two key materials to manage (documented in `docs/runbook.md`). The public demo needs no
account. Auth.js is in security-patch mode under the Better Auth team (ADR 0005); this design
keeps the API independent of the web auth library, so a later migration only touches the web tier.
