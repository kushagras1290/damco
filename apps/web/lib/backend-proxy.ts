import { type CryptoKey, type JWK, SignJWT, importJWK } from "jose";

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
const ALGORITHM = "EdDSA";

/** Validate the catch-all path; returns the backend path or null when not allowed. */
export function backendPath(segments: readonly string[]): string | null {
  if (segments.length === 0 || segments.length > 4) return null;
  const [root] = segments;
  if (!root || !ALLOWED_ROOTS.has(root)) return null;
  if (!segments.every((segment) => SEGMENT_RE.test(segment))) return null;
  return `/api/v1/${segments.join("/")}`;
}

/**
 * CSRF defence in depth for state-changing requests (on top of SameSite=Lax cookies):
 * the browser-sent Origin must match this app's canonical origin exactly.
 */
export function isSameOrigin(originHeader: string | null, expectedOrigin: string): boolean {
  if (!originHeader) return false;
  try {
    return new URL(originHeader).origin === new URL(expectedOrigin).origin;
  } catch {
    return false;
  }
}

export interface PrivateSigningJwk extends JWK {
  kid: string;
}

const keyCache = new Map<string, Promise<CryptoKey | Uint8Array>>();

function signingKey(jwk: PrivateSigningJwk): Promise<CryptoKey | Uint8Array> {
  let key = keyCache.get(jwk.kid);
  if (!key) {
    key = importJWK(jwk, ALGORITHM);
    keyCache.set(jwk.kid, key);
  }
  return key;
}

export interface TokenClaims {
  githubId: string;
  login: string | undefined;
  privateJwk: PrivateSigningJwk;
  audience: string;
  issuer: string;
}

/**
 * Short-lived Ed25519 token for one backend call, minted server-side only. Carries the
 * immutable GitHub id as `sub`; it deliberately has NO role claim - the API decides.
 */
export async function mintBackendToken(claims: TokenClaims): Promise<string> {
  const payload = claims.login ? { login: claims.login } : {};
  return new SignJWT(payload)
    .setProtectedHeader({ alg: ALGORITHM, typ: "JWT", kid: claims.privateJwk.kid })
    .setSubject(`github:${claims.githubId}`)
    .setAudience(claims.audience)
    .setIssuer(claims.issuer)
    .setIssuedAt()
    .setNotBefore("0s")
    .setExpirationTime(`${TOKEN_TTL_SECONDS}s`)
    .setJti(crypto.randomUUID())
    .sign(await signingKey(claims.privateJwk));
}
