// @vitest-environment node
// Server-side module: runs in Node in production, so test it there (jsdom has a separate Uint8Array realm).
import { exportJWK, generateKeyPair, jwtVerify } from "jose";

import { type PrivateSigningJwk, backendPath, isSameOrigin, mintBackendToken } from "@/lib/backend-proxy";
import { roleForGithubId } from "@/lib/roles";

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
      githubId: "583231",
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

describe("roleForGithubId", () => {
  it("grants OWNER only to allowlisted numeric ids", () => {
    expect(roleForGithubId("583231", ["583231"])).toBe("OWNER");
    expect(roleForGithubId("42", ["583231"])).toBe("PUBLIC_DEMO");
    expect(roleForGithubId(undefined, ["583231"])).toBe("PUBLIC_DEMO");
    expect(roleForGithubId("583231", [])).toBe("PUBLIC_DEMO");
  });
});
