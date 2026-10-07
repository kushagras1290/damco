// @vitest-environment node
// Server-side module: runs in Node in production, so test it there (jsdom has a separate Uint8Array realm).
import { exportJWK, generateKeyPair, jwtVerify } from "jose";

import {
  type PrivateSigningJwk,
  backendPath,
  clientIp,
  forwardedRequestHeaders,
  githubSubject,
  isSameOrigin,
  mintBackendToken,
  passthroughResponseHeaders,
  selectedWorkspace,
  visitorSubject,
} from "@/lib/backend-proxy";
import { workspaceCookie } from "@/lib/workspace-cookie";

describe("backendPath", () => {
  it.each([
    [["jobs"], "/api/v1/jobs"],
    [["jobs", "0192f0c4-1111-7000-8000-000000000000"], "/api/v1/jobs/0192f0c4-1111-7000-8000-000000000000"],
    [["sources", "abc", "sync"], "/api/v1/sources/abc/sync"],
    [["me"], "/api/v1/me"],
  ])("allows %j", (segments, expected) => {
    expect(backendPath(segments)).toBe(expected);
  });

  it.each([[[]], [["admin"]], [["jobs", ".."]], [["jobs", "a%2F..%2Fb"]], [["jobs", "a", "b", "c", "d"]], [["health", "ready"]]])(
    "rejects %j",
    (segments) => {
      expect(backendPath(segments)).toBeNull();
    },
  );
});

describe("isSameOrigin", () => {
  it("accepts only the exact canonical origin", () => {
    expect(isSameOrigin("https://jobpulse.example", "https://jobpulse.example")).toBe(true);
    expect(isSameOrigin("https://jobpulse.example", "https://jobpulse.example/")).toBe(true);
    expect(isSameOrigin("https://evil.example", "https://jobpulse.example")).toBe(false);
    expect(isSameOrigin("https://jobpulse.example.evil.com", "https://jobpulse.example")).toBe(false);
    expect(isSameOrigin("http://jobpulse.example", "https://jobpulse.example")).toBe(false);
    expect(isSameOrigin(null, "https://jobpulse.example")).toBe(false);
    expect(isSameOrigin("null", "https://jobpulse.example")).toBe(false);
  });
});

describe("mintBackendToken", () => {
  it("signs a short-lived Ed25519 token the API can verify, with no role claim", async () => {
    const { privateKey, publicKey } = await generateKeyPair("EdDSA", { crv: "Ed25519", extractable: true });
    const privateJwk = { ...(await exportJWK(privateKey)), kid: "k1" } as PrivateSigningJwk;
    const token = await mintBackendToken({
      subject: githubSubject("583231"),
      login: "octocat",
      privateJwk,
      audience: "jobpulse-api",
      issuer: "jobpulse-web",
    });
    const { payload, protectedHeader } = await jwtVerify(token, publicKey, {
      audience: "jobpulse-api",
      issuer: "jobpulse-web",
      algorithms: ["EdDSA"],
    });
    expect(protectedHeader).toMatchObject({ alg: "EdDSA", kid: "k1", typ: "JWT" });
    expect(payload.sub).toBe("github:583231");
    expect(payload.login).toBe("octocat");
    expect(payload).not.toHaveProperty("role");
    expect(payload.jti).toMatch(/^[0-9a-f-]{36}$/);
    expect((payload.exp ?? 0) - (payload.iat ?? 0)).toBe(300);
    expect(payload.nbf).toBeDefined();
  });
});

describe("clientIp", () => {
  it("ignores X-Forwarded-For unless a proxy is trusted", () => {
    expect(clientIp("203.0.113.9", 0)).toBeNull();
    expect(clientIp(null, 1)).toBeNull();
  });

  it("counts trusted hops from the right so clients cannot spoof the left side", () => {
    expect(clientIp("203.0.113.9", 1)).toBe("203.0.113.9");
    expect(clientIp("6.6.6.6, 203.0.113.9", 1)).toBe("203.0.113.9");
    expect(clientIp("6.6.6.6, 203.0.113.9, 10.0.0.2", 2)).toBe("203.0.113.9");
    expect(clientIp("203.0.113.9", 2)).toBeNull();
    expect(clientIp("2001:db8::1", 1)).toBe("2001:db8::1");
    expect(clientIp("<script>", 1)).toBeNull();
  });
});

describe("visitorSubject", () => {
  it("is a stable keyed pseudonym that never contains the IP", async () => {
    const secret = "s".repeat(48);
    const first = await visitorSubject("203.0.113.9", secret);
    expect(first).toMatch(/^visitor:[0-9a-f]{32}$/);
    expect(first).not.toContain("203");
    expect(await visitorSubject("203.0.113.9", secret)).toBe(first);
    expect(await visitorSubject("203.0.113.10", secret)).not.toBe(first);
    expect(await visitorSubject("203.0.113.9", "t".repeat(48))).not.toBe(first);
  });
});

/** Low-entropy fixture (a realistic random key would trip secret scanners). */
const IDEMPOTENCY_KEY = "test".repeat(4);

describe("forwardedRequestHeaders", () => {
  it("forwards only validated request ids and mutation idempotency keys", () => {
    const incoming = new Headers({
      "x-request-id": "req-12345678",
      "idempotency-key": IDEMPOTENCY_KEY,
      cookie: "authjs.session-token=secret",
      authorization: "Bearer forged",
    });
    const post = forwardedRequestHeaders(incoming, "POST");
    expect(post.get("x-request-id")).toBe("req-12345678");
    expect(post.get("idempotency-key")).toBe(IDEMPOTENCY_KEY);
    expect(post.get("cookie")).toBeNull();
    expect(post.get("authorization")).toBeNull();
    expect(forwardedRequestHeaders(incoming, "GET").get("idempotency-key")).toBeNull();
    const bad = forwardedRequestHeaders(new Headers({ "idempotency-key": "bad key!", "x-request-id": "x" }), "POST");
    expect(bad.get("idempotency-key")).toBeNull();
    expect(bad.get("x-request-id")).toBeNull();
  });
});

describe("passthroughResponseHeaders", () => {
  it("exposes rate-limit and replay headers but nothing else", () => {
    const upstream = new Headers({
      "content-type": "application/json",
      "ratelimit-remaining": "3",
      "retry-after": "7",
      "idempotent-replayed": "true",
      "set-cookie": "x=y",
      server: "uvicorn",
    });
    const headers = passthroughResponseHeaders(upstream);
    expect(headers.get("ratelimit-remaining")).toBe("3");
    expect(headers.get("retry-after")).toBe("7");
    expect(headers.get("idempotent-replayed")).toBe("true");
    expect(headers.get("cache-control")).toBe("no-store");
    expect(headers.get("set-cookie")).toBeNull();
    expect(headers.get("server")).toBeNull();
  });
});

describe("workspace selection", () => {
  const WS = "0192f0c4-1111-7000-8000-000000000000";

  it("forwards only well-formed workspace ids", () => {
    expect(selectedWorkspace(WS.toUpperCase())).toBe(WS);
    expect(selectedWorkspace(undefined)).toBeUndefined();
    expect(selectedWorkspace("default")).toBeUndefined();
    expect(selectedWorkspace(`${WS}; admin=true`)).toBeUndefined();
  });

  it("puts the selection in the wid claim, never a role", async () => {
    const { privateKey, publicKey } = await generateKeyPair("EdDSA", { crv: "Ed25519", extractable: true });
    const privateJwk = { ...(await exportJWK(privateKey)), kid: "k1" } as PrivateSigningJwk;
    const token = await mintBackendToken({
      subject: githubSubject("583231"),
      login: "octocat",
      workspaceId: WS,
      privateJwk,
      audience: "jobpulse-api",
      issuer: "jobpulse-web",
    });
    const { payload } = await jwtVerify(token, publicKey, { audience: "jobpulse-api", issuer: "jobpulse-web" });
    expect(payload.wid).toBe(WS);
    expect(payload).not.toHaveProperty("role");
  });

  it("writes a scoped, lax cookie and refuses junk", () => {
    expect(workspaceCookie(WS, true)).toBe(`jp_workspace=${WS}; Path=/; Max-Age=31536000; SameSite=Lax; Secure`);
    expect(() => workspaceCookie("x; Domain=evil.example", false)).toThrow("invalid workspace id");
  });
});
