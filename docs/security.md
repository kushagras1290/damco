# Security

## Threat model highlights

| Threat | Control |
|---|---|
| **SSRF** via source URLs, redirects, webhooks or the headless browser | `SafeHttpClient`: http/https only, ports 80/443, no credentials in URLs, DNS resolution with rejection of private/loopback/link-local/CGNAT/reserved IPs (incl. IPv4-mapped IPv6), host allowlist (ATS APIs + each source's own host + `OUTBOUND_ALLOWED_HOSTS`), manual redirect following with re-validation on every hop. Source and webhook URLs are also validated at write time. Playwright routes every sub-request through the same guard. |
| Oversized / slow responses | Streamed byte limit (default 10 MB), connect/read/total timeouts on every outbound call |
| Prompt injection in postings | Posting fenced in `<posting>` tags, closing tag stripped, instructions declare it untrusted data, strict JSON schema output, AI cannot override hard rules |
| Stored XSS from job HTML | `nh3` sanitisation with tag/attribute/URL-scheme allowlists; `rel="noopener noreferrer nofollow"` on links; CSP on the web app |
| AuthN/AuthZ bypass | Backend verifies HS256 JWT signature, `exp`, `iat`, `aud`, `iss`, role enum; OWNER required for every write; tokens minted server-side only, 5-minute TTL |
| Path traversal | Proxy path allowlist + segment regex; snapshot keys validated and resolved inside the storage root |
| SQL injection | SQLAlchemy expressions only (parameterised) |
| Abuse | Per-IP token-bucket rate limit, 256 KB request body limit (declared and streamed), Pydantic `extra="forbid"` on inputs |
| Secrets | Environment only; config validated at startup (secret length ≥ 32); structlog redacts sensitive keys; no secrets in images |
| Webhook spoofing | Payloads signed `HMAC-SHA256("{timestamp}.{body}")` in `X-JobPulse-Signature` |
| robots.txt | Respected per RFC 9309 (4xx = allow, 5xx = disallow and retry later) |

## HTTP hardening
API: `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, restrictive CSP,
`Cache-Control: no-store`, HSTS in production, CORS allowlist without credentials, docs disabled
in production. Web: CSP, frame-ancestors none, `poweredByHeader` off.

## Known residual risks
- **DNS rebinding TOCTOU**: the IP is validated before httpx connects; a hostile resolver could
  return a different address on the second lookup. Mitigated by the host allowlist (on by default).
- The web CSP allows `'unsafe-inline'` scripts for Next.js bootstrap; nonce-based CSP is a follow-up.
- In-process rate limiting is per instance.

## CI scanning
Gitleaks, Semgrep, CodeQL (Python + TS), Trivy (filesystem and images), pip-audit, pnpm audit.
