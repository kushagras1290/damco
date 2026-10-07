// @vitest-environment node
import { serverEnvSchema, trustedProxyHops } from "@/lib/server-env";

vi.mock("server-only", () => ({}));

const PRIVATE_JWK = JSON.stringify({ kty: "OKP", crv: "Ed25519", x: "pub", d: "priv", kid: "k1" });
const BASE = {
  API_JWT_PRIVATE_JWK: PRIVATE_JWK,
  AUTH_SECRET: "s".repeat(48),
};
const PRODUCTION = {
  ...BASE,
  ENVIRONMENT: "production",
  AUTH_URL: "https://jobpulse.example",
  API_BASE_URL: "https://api.jobpulse.example",
  AUTH_GITHUB_ID: "id",
  AUTH_GITHUB_SECRET: "secret",
};

const issues = (input: Record<string, string>) =>
  serverEnvSchema.safeParse(input).error?.issues.map((issue) => issue.message) ?? [];

describe("serverEnvSchema", () => {
  it("accepts a complete production configuration", () => {
    const parsed = serverEnvSchema.parse(PRODUCTION);
    expect(parsed.API_JWT_PRIVATE_JWK.kid).toBe("k1");
  });

  it("is lenient for local development", () => {
    expect(serverEnvSchema.safeParse(BASE).success).toBe(true);
  });

  it.each([
    [{ AUTH_URL: "http://jobpulse.example" }, "AUTH_URL must be an https:// URL in production"],
    [{ API_BASE_URL: "http://api.internal" }, "API_BASE_URL must be https:// in production"],
    [{ AUTH_GITHUB_ID: "", AUTH_GITHUB_SECRET: "" }, "GitHub OAuth is required in production"],
  ])("rejects unsafe production config %j", (override, message) => {
    expect(issues({ ...PRODUCTION, ...override })).toContain(message);
  });

  it.each([
    [{ AUTH_SECRET: "short" }, "AUTH_SECRET must be at least 32 characters"],
    [{ API_JWT_PRIVATE_JWK: "{oops" }, "API_JWT_PRIVATE_JWK is not valid JSON"],
    [{ API_JWT_PRIVATE_JWK: JSON.stringify({ kty: "oct", k: "x", kid: "k" }) }, "API_JWT_PRIVATE_JWK must be an Ed25519 (OKP) key"],
    [{ AUTH_GITHUB_ID: "id" }, "set both AUTH_GITHUB_ID and AUTH_GITHUB_SECRET"],
  ])("rejects invalid values %j", (override, message) => {
    expect(issues({ ...BASE, ...override })).toContain(message);
  });

  it.each([
    [{}, 0],
    [{ TRUSTED_PROXY_HOPS: "" }, 0],
    [{ VERCEL: "1" }, 1],
    [{ VERCEL: "1", TRUSTED_PROXY_HOPS: "" }, 1],
    [{ VERCEL: "1", TRUSTED_PROXY_HOPS: "0" }, 0],
    [{ TRUSTED_PROXY_HOPS: "2" }, 2],
  ])("resolves proxy trust %j -> %i", (override, expected) => {
    expect(trustedProxyHops(serverEnvSchema.parse({ ...BASE, ...override }))).toBe(expected);
  });

  it("requires a kid on the signing key", () => {
    const noKid = JSON.stringify({ kty: "OKP", crv: "Ed25519", x: "pub", d: "priv" });
    expect(serverEnvSchema.safeParse({ ...BASE, API_JWT_PRIVATE_JWK: noKid }).success).toBe(false);
  });
});
