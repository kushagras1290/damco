# Security

## Threat model highlights

| Threat | Control |
|---|---|
| **SSRF** via source URLs, redirects, webhooks or the headless browser | `SafeHttpClient`: http/https only, ports 80/443, no credentials in URLs, DNS resolution with rejection of private/loopback/link-local/CGNAT/reserved IPs (incl. IPv4-mapped IPv6), host allowlist (ATS APIs + each source's own host + `OUTBOUND_ALLOWED_HOSTS`), manual redirect following with re-validation on every hop. Source and webhook URLs are also validated at write time. Playwright routes every sub-request through the same guard. |
| Oversized / slow responses | Streamed byte limit (default 10 MB), connect/read/total timeouts on every outbound call |
| Prompt injection in postings | Posting fenced in `<posting>` tags, closing tag stripped, instructions declare it untrusted data, strict JSON schema output, AI cannot override hard rules |
| Stored XSS from job HTML | `nh3` sanitisation with tag/attribute/URL-scheme allowlists; `rel="noopener noreferrer nofollow"` on links; CSP on the web app |
| AuthN/AuthZ bypass | Ed25519 (EdDSA) tokens minted server-side by the web app, verified by the API with public keys only (`kid` rotation, alg pinned, `exp`/`nbf`/`iat`/`aud`/`iss`/`jti` required, max 15 min lifetime). Tokens identify an immutable provider subject and optionally request a workspace; they never carry a role. The API verifies membership and derives the role from PostgreSQL on every request. `OWNER_GITHUB_IDS` is a platform-admin bootstrap allowlist only. See ADRs 0006 and 0008 |
| CSRF | SameSite=Lax session cookie plus an exact same-origin check on every proxied write |
| Path traversal | Proxy path allowlist + segment regex; snapshot keys validated and resolved inside the storage root |
| SQL injection | SQLAlchemy expressions only (parameterised) |
| Abuse | Redis-shared sliding-window limits keyed by verified user, signed anonymous visitor (`visitor:` HMAC of the client IP - the API never sees the IP) or client IP; separate tighter buckets for writes and realtime stream connects; per-instance fallback if Redis is down. 256 KB request body limit (declared and streamed), 30 s request deadline, max concurrent SSE clients, Pydantic `extra="forbid"` on inputs |
| Duplicate writes on retry | `Idempotency-Key` (client sends one per mutation and reuses it on retry): replay of the stored response, 409 while in flight, 422 on key reuse with a different body; scoped per caller; 503 rather than risking a duplicate when Redis is unavailable |
| Spoofed client IPs | `X-Forwarded-For` is trusted only for an explicit number of proxy hops (`TRUSTED_PROXY_COUNT` on the API, `TRUSTED_PROXY_HOPS` on the web, default 1 on Vercel, otherwise 0) |
| Event stream data leaks | Events carry `workspace_id` for tenant changes or `source_id` for catalogue changes. The hub delivers only matching-workspace events or catalogue events for a source that workspace follows; the stream requires the same reader identity and RLS-scoped visibility as normal endpoints |
| Secrets | Environment only; config validated at startup (secret length ≥ 32); structlog redacts sensitive keys; no secrets in images |
| Webhook spoofing | Payloads signed `HMAC-SHA256("{timestamp}.{body}")` in `X-JobPulse-Signature` |
| robots.txt | Respected per RFC 9309 (4xx = allow, 5xx = disallow and retry later) |

## HTTP hardening
Web: per-request nonce CSP (`script-src 'nonce-…' 'strict-dynamic'`, no inline scripts), HSTS, frame-ancestors none.
API: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, restrictive CSP,
`Cache-Control: no-store`, HSTS in production, **deny-by-default CORS** (browsers only reach the
API through the same-origin web proxy; exact-origin allowlist, no wildcard in production, no
credentials), docs disabled in production. Web: CSP, frame-ancestors none, `poweredByHeader` off.

## Known residual risks
- **DNS rebinding TOCTOU**: the IP is validated before httpx connects; a hostile resolver could
  return a different address on the second lookup. Mitigated by the host allowlist (on by default).
- Inline *style attributes* are allowed by the CSP (charts set widths); scripts are nonce-only.
- Rate limits fall back to per-instance windows while Redis is down (bounded, logged and
  exposed as `rate_limit_backend_fallbacks_total`).
- Shared response caching is bypassed while Redis is down, and writes carrying an
  `Idempotency-Key` return 503 rather than risk a duplicate.
- Visitor buckets rely on the platform's client-IP header; behind an untrusted proxy all
  anonymous visitors share the web server's IP bucket (safe, but coarser).

## CI scanning
Gitleaks, Semgrep, CodeQL (Python + TS), Trivy (filesystem and images), pip-audit, pnpm audit.

## Supply chain
7-day release cooldowns (pnpm, uv, Dependabot), pnpm trust-downgrade guard, no exotic
sub-dependencies, install-script allowlist, and every GitHub Action pinned to a commit SHA.
Reviewed exceptions are listed in [ADR 0005](adr/0005-dependency-supply-chain-policy.md).
