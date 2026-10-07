// @vitest-environment node
// Server-side module: runs in Node in production, so test it there (jsdom has a separate Uint8Array realm).
import { jwtVerify } from "jose";

import { backendPath, mintBackendToken } from "@/lib/backend-proxy";
import { roleForLogin } from "@/lib/roles";

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

describe("mintBackendToken", () => {
  it("produces a short-lived HS256 token the API can verify", async () => {
    const secret = "x".repeat(40);
    const token = await mintBackendToken({
      subject: "octocat",
      role: "OWNER",
      secret,
      audience: "jobpulse-api",
      issuer: "jobpulse-web",
    });
    const { payload, protectedHeader } = await jwtVerify(token, new TextEncoder().encode(secret), {
      audience: "jobpulse-api",
      issuer: "jobpulse-web",
    });
    expect(protectedHeader.alg).toBe("HS256");
    expect(payload.sub).toBe("octocat");
    expect(payload.role).toBe("OWNER");
    expect((payload.exp ?? 0) - (payload.iat ?? 0)).toBe(300);
  });
});

describe("roleForLogin", () => {
  it("grants OWNER only to allowlisted logins (case-insensitive)", () => {
    expect(roleForLogin("OctoCat", ["octocat"])).toBe("OWNER");
    expect(roleForLogin("someone", ["octocat"])).toBe("PUBLIC_DEMO");
    expect(roleForLogin(undefined, ["octocat"])).toBe("PUBLIC_DEMO");
    expect(roleForLogin("octocat", [])).toBe("PUBLIC_DEMO");
  });
});
