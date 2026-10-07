import { SignJWT } from "jose";

import type { Role } from "@/lib/roles";

export const ALLOWED_ROOTS = new Set([
  "jobs",
  "sources",
  "runs",
  "profile",
  "applications",
  "decisions",
  "dashboard",
  "system",
  "me",
]);
export const ALLOWED_METHODS = new Set(["GET", "POST", "PATCH"]);
export const MAX_BODY_BYTES = 256 * 1024;
const SEGMENT_RE = /^[A-Za-z0-9_-]{1,100}$/;
const TOKEN_TTL_SECONDS = 300;

/** Validate the catch-all path; returns the backend path or null when not allowed. */
export function backendPath(segments: readonly string[]): string | null {
  if (segments.length === 0 || segments.length > 4) return null;
  const [root] = segments;
  if (!root || !ALLOWED_ROOTS.has(root)) return null;
  if (!segments.every((segment) => SEGMENT_RE.test(segment))) return null;
  return `/api/v1/${segments.join("/")}`;
}

export interface TokenClaims {
  subject: string;
  role: Role;
  secret: string;
  audience: string;
  issuer: string;
}

/** Short-lived HS256 token for one backend call. Minted server-side only. */
export async function mintBackendToken(claims: TokenClaims): Promise<string> {
  const key = new TextEncoder().encode(claims.secret);
  return new SignJWT({ role: claims.role })
    .setProtectedHeader({ alg: "HS256", typ: "JWT" })
    .setSubject(claims.subject)
    .setAudience(claims.audience)
    .setIssuer(claims.issuer)
    .setIssuedAt()
    .setExpirationTime(`${TOKEN_TTL_SECONDS}s`)
    .sign(key);
}
