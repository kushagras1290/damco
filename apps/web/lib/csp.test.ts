import { buildCsp, sentryOrigin } from "@/lib/csp";

const directive = (csp: string, name: string) => csp.split("; ").find((part) => part.startsWith(`${name} `)) ?? "";

describe("buildCsp", () => {
  it("allows scripts only via the per-request nonce in production", () => {
    const csp = buildCsp({ nonce: "abc123", development: false });
    const scripts = directive(csp, "script-src");
    expect(scripts).toContain("'nonce-abc123'");
    expect(scripts).toContain("'strict-dynamic'");
    expect(scripts).not.toContain("unsafe-inline");
    expect(scripts).not.toContain("unsafe-eval");
    expect(directive(csp, "frame-ancestors")).toBe("frame-ancestors 'none'");
    expect(directive(csp, "object-src")).toBe("object-src 'none'");
    expect(csp).toContain("upgrade-insecure-requests");
  });

  it("permits eval only in development (React debugging)", () => {
    expect(directive(buildCsp({ nonce: "n", development: true }), "script-src")).toContain("'unsafe-eval'");
  });

  it("adds extra connect origins such as Sentry ingest", () => {
    const csp = buildCsp({ nonce: "n", development: false, connectOrigins: ["https://o1.ingest.sentry.io"] });
    expect(directive(csp, "connect-src")).toBe("connect-src 'self' https://o1.ingest.sentry.io");
  });
});

describe("sentryOrigin", () => {
  it("extracts the origin from a DSN", () => {
    expect(sentryOrigin("https://key@o1.ingest.sentry.io/123")).toBe("https://o1.ingest.sentry.io");
    expect(sentryOrigin(undefined)).toBeNull();
    expect(sentryOrigin("not a url")).toBeNull();
  });
});
