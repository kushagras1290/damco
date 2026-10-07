export interface CspOptions {
  nonce: string;
  development: boolean;
  /** Extra origins the browser may call (e.g. the Sentry ingest host). */
  connectOrigins?: readonly string[];
}

/**
 * Strict, nonce-based Content-Security-Policy. Scripts run only with the per-request
 * nonce ('strict-dynamic' lets Next.js load its chunks); no inline/eval in production.
 * Inline *style attributes* are allowed (charts/progress bars set widths) - they cannot
 * execute code, unlike inline scripts.
 */
export function buildCsp({ nonce, development, connectOrigins = [] }: CspOptions): string {
  const directives: Record<string, string[]> = {
    "default-src": ["'self'"],
    "script-src": ["'self'", `'nonce-${nonce}'`, "'strict-dynamic'", ...(development ? ["'unsafe-eval'"] : [])],
    "style-src": ["'self'", `'nonce-${nonce}'`],
    "style-src-attr": ["'unsafe-inline'"],
    "img-src": ["'self'", "data:", "blob:"],
    "font-src": ["'self'"],
    "connect-src": ["'self'", ...connectOrigins],
    "object-src": ["'none'"],
    "base-uri": ["'self'"],
    "form-action": ["'self'", "https://github.com"],
    "frame-ancestors": ["'none'"],
  };
  const policy = Object.entries(directives).map(([name, values]) => `${name} ${values.join(" ")}`);
  if (!development) policy.push("upgrade-insecure-requests");
  return policy.join("; ");
}

/** Origin of a Sentry DSN (e.g. https://o1.ingest.sentry.io), or null if absent/invalid. */
export function sentryOrigin(dsn: string | undefined): string | null {
  if (!dsn) return null;
  try {
    return new URL(dsn).origin;
  } catch {
    return null;
  }
}
