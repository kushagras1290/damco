import { type CryptoKey, type JWK, SignJWT, importJWK } from "jose";

import { isWorkspaceId } from "@/lib/workspace-cookie";

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
  "events",
  "workspaces",
  "workspace",
  "invitations",
]);
export const ALLOWED_METHODS = new Set(["GET", "POST", "PATCH", "DELETE"]);
export { WORKSPACE_COOKIE } from "@/lib/workspace-cookie";
export const MAX_BODY_BYTES = 256 * 1024;
export const EVENTS_PATH = "/api/v1/events";
const SEGMENT_RE = /^[A-Za-z0-9_-]{1,100}$/;
const IDEMPOTENCY_KEY_RE = /^[A-Za-z0-9_-]{8,255}$/;
const REQUEST_ID_RE = /^[A-Za-z0-9._-]{8,128}$/;
const TOKEN_TTL_SECONDS = 300;
const ALGORITHM = "EdDSA";
const VISITOR_HASH_HEX_CHARS = 32;

/** Upstream response headers the browser may see (everything else is dropped). */
export const PASSTHROUGH_RESPONSE_HEADERS = [
  "content-type",
  "ratelimit-limit",
  "ratelimit-remaining",
  "ratelimit-reset",
  "retry-after",
  "idempotent-replayed",
  "x-request-id",
] as const;

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

/** Forwardable client headers: validated request id and Idempotency-Key (mutations only). */
export function forwardedRequestHeaders(incoming: Headers, method: string): Headers {
  const headers = new Headers({ accept: method === "GET" ? "application/json, text/event-stream" : "application/json" });
  const requestId = incoming.get("x-request-id");
  if (requestId && REQUEST_ID_RE.test(requestId)) headers.set("x-request-id", requestId);
  const idempotencyKey = incoming.get("idempotency-key");
  if (method !== "GET" && idempotencyKey && IDEMPOTENCY_KEY_RE.test(idempotencyKey)) {
    headers.set("idempotency-key", idempotencyKey);
  }
  return headers;
}

export function passthroughResponseHeaders(upstream: Headers): Headers {
  const headers = new Headers({ "cache-control": "no-store" });
  for (const name of PASSTHROUGH_RESPONSE_HEADERS) {
    const value = upstream.get(name);
    if (value !== null) headers.set(name, value);
  }
  if (!headers.has("content-type")) headers.set("content-type", "application/json");
  return headers;
}

/**
 * Client IP from X-Forwarded-For, counting `trustedHops` proxies from the right (the same
 * rule the API uses). 0 = no trusted proxy: the header is attacker-controlled, so ignore it.
 */
export function clientIp(forwardedFor: string | null, trustedHops: number): string | null {
  if (trustedHops <= 0 || !forwardedFor) return null;
  const hops = forwardedFor
    .split(",")
    .map((hop) => hop.trim())
    .filter(Boolean);
  if (hops.length < trustedHops) return null;
  const candidate = hops[hops.length - trustedHops];
  return candidate && /^[0-9A-Fa-f:.]{2,45}$/.test(candidate) ? candidate : null;
}

/**
 * Stable pseudonymous visitor id: keyed HMAC of the IP, so the API can give each anonymous
 * visitor its own rate-limit bucket without ever seeing (or storing) the IP itself.
 */
export async function visitorSubject(ip: string, secret: string): Promise<string> {
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey("raw", encoder.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, [
    "sign",
  ]);
  const mac = new Uint8Array(await crypto.subtle.sign("HMAC", key, encoder.encode(`visitor:${ip}`)));
  const hex = Array.from(mac, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `visitor:${hex.slice(0, VISITOR_HASH_HEX_CHARS)}`;
}

/** Selected workspace (a hint only: the API verifies membership on every request). */
export function selectedWorkspace(cookieValue: string | undefined): string | undefined {
  return isWorkspaceId(cookieValue) ? cookieValue.toLowerCase() : undefined;
}

export interface PrivateSigningJwk extends JWK {
  kid: string;
}

const keyCache = new Map<string, Promise<CryptoKey | Uint8Array>>();

function signingKey(jwk: PrivateSigningJwk): Promise<CryptoKey | Uint8Array> {
  // Keyed by kid AND public key: a rotated key that reuses a kid is never signed with stale material.
  const cacheKey = `${jwk.kid}:${String(jwk.x)}`;
  let key = keyCache.get(cacheKey);
  if (!key) {
    key = importJWK(jwk, ALGORITHM);
    keyCache.set(cacheKey, key);
  }
  return key;
}

export interface TokenClaims {
  /** `github:<numeric id>` for signed-in users, `visitor:<hash>` for anonymous visitors. */
  subject: string;
  login: string | undefined;
  /** Selected workspace id, forwarded as the `wid` claim. */
  workspaceId?: string | undefined;
  privateJwk: PrivateSigningJwk;
  audience: string;
  issuer: string;
}

export function githubSubject(githubId: string): string {
  return `github:${githubId}`;
}

/**
 * Short-lived Ed25519 token for one backend call, minted server-side only. It deliberately
 * has NO role claim - the API derives authorization from `sub` itself.
 */
export async function mintBackendToken(claims: TokenClaims): Promise<string> {
  const payload: Record<string, string> = {};
  if (claims.login) payload.login = claims.login;
  if (claims.workspaceId) payload.wid = claims.workspaceId;
  return new SignJWT(payload)
    .setProtectedHeader({ alg: ALGORITHM, typ: "JWT", kid: claims.privateJwk.kid })
    .setSubject(claims.subject)
    .setAudience(claims.audience)
    .setIssuer(claims.issuer)
    .setIssuedAt()
    .setNotBefore("0s")
    .setExpirationTime(`${TOKEN_TTL_SECONDS}s`)
    .setJti(crypto.randomUUID())
    .sign(await signingKey(claims.privateJwk));
}
