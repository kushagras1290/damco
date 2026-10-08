import "server-only";

import { z } from "zod";

const MIN_SECRET_LENGTH = 32;

const privateJwk = z
  .string()
  .transform((raw, ctx) => {
    try {
      return JSON.parse(raw) as unknown;
    } catch {
      ctx.addIssue({ code: "custom", message: "API_JWT_PRIVATE_JWK is not valid JSON" });
      return z.NEVER;
    }
  })
  .pipe(
    z.object({
      kty: z.literal("OKP", { error: "API_JWT_PRIVATE_JWK must be an Ed25519 (OKP) key" }),
      crv: z.literal("Ed25519", { error: "API_JWT_PRIVATE_JWK must use crv Ed25519" }),
      d: z.string().min(1, "API_JWT_PRIVATE_JWK must include the private 'd' parameter"),
      x: z.string().min(1),
      kid: z.string().min(1, "API_JWT_PRIVATE_JWK needs a 'kid' matching the API's JWKS"),
    }),
  );

export const serverEnvSchema = z
  .object({
    ENVIRONMENT: z.enum(["local", "test", "staging", "production"]).default("local"),
    API_BASE_URL: z.url().default("http://localhost:8000"),
    API_JWT_PRIVATE_JWK: privateJwk,
    API_JWT_AUDIENCE: z.string().default("jobpulse-api"),
    API_JWT_ISSUER: z.string().default("jobpulse-web"),
    AUTH_SECRET: z.string().min(MIN_SECRET_LENGTH, `AUTH_SECRET must be at least ${MIN_SECRET_LENGTH} characters`),
    AUTH_URL: z.url().optional(),
    AUTH_GITHUB_ID: z.string().optional(),
    AUTH_GITHUB_SECRET: z.string().optional(),
    AUTH_GOOGLE_ID: z.string().optional(),
    AUTH_GOOGLE_SECRET: z.string().optional(),
    AUTH_MICROSOFT_ENTRA_ID_ID: z.string().optional(),
    AUTH_MICROSOFT_ENTRA_ID_SECRET: z.string().optional(),
    // https://login.microsoftonline.com/<tenant-id or common>/v2.0
    AUTH_MICROSOFT_ENTRA_ID_ISSUER: z.url().optional(),
    BACKEND_TIMEOUT_MS: z.coerce.number().int().min(1000).max(60_000).default(15_000),
    // Proxies in front of this app whose X-Forwarded-For is trustworthy. Unset: 1 on Vercel
    // (it overwrites the header with the real client IP), otherwise 0 (header ignored).
    TRUSTED_PROXY_HOPS: z.preprocess((value) => (value === "" ? undefined : value), z.coerce.number().int().min(0).max(5).optional()),
    VERCEL: z.string().optional(),
  })
  .superRefine((env, ctx) => {
    if (Boolean(env.AUTH_GITHUB_ID) !== Boolean(env.AUTH_GITHUB_SECRET)) {
      ctx.addIssue({ code: "custom", path: ["AUTH_GITHUB_SECRET"], message: "set both AUTH_GITHUB_ID and AUTH_GITHUB_SECRET" });
    }
    if (Boolean(env.AUTH_GOOGLE_ID) !== Boolean(env.AUTH_GOOGLE_SECRET)) {
      ctx.addIssue({ code: "custom", path: ["AUTH_GOOGLE_SECRET"], message: "set both AUTH_GOOGLE_ID and AUTH_GOOGLE_SECRET" });
    }
    const microsoft = [env.AUTH_MICROSOFT_ENTRA_ID_ID, env.AUTH_MICROSOFT_ENTRA_ID_SECRET, env.AUTH_MICROSOFT_ENTRA_ID_ISSUER];
    if (microsoft.some(Boolean) && !microsoft.every(Boolean)) {
      ctx.addIssue({
        code: "custom",
        path: ["AUTH_MICROSOFT_ENTRA_ID_ISSUER"],
        message: "set AUTH_MICROSOFT_ENTRA_ID_ID, AUTH_MICROSOFT_ENTRA_ID_SECRET and AUTH_MICROSOFT_ENTRA_ID_ISSUER together",
      });
    }
    if (env.ENVIRONMENT !== "production") return;
    // Production: no implicit host trust, HTTPS everywhere, sign-in must be possible.
    // (Owner/platform-admin ids live only in the API, which is authoritative for roles.)
    if (!env.AUTH_URL?.startsWith("https://")) {
      ctx.addIssue({ code: "custom", path: ["AUTH_URL"], message: "AUTH_URL must be an https:// URL in production" });
    }
    if (!env.API_BASE_URL.startsWith("https://")) {
      ctx.addIssue({ code: "custom", path: ["API_BASE_URL"], message: "API_BASE_URL must be https:// in production" });
    }
    if (!env.AUTH_GITHUB_ID && !env.AUTH_GOOGLE_ID && !env.AUTH_MICROSOFT_ENTRA_ID_ID) {
      ctx.addIssue({ code: "custom", path: ["AUTH_GITHUB_ID"], message: "configure at least one OAuth provider in production" });
    }
  });

export type ServerEnv = z.infer<typeof serverEnvSchema>;

let cached: ServerEnv | undefined;

/** Validated server environment. Throws (fails fast) on misconfiguration; never logs values. */
export function serverEnv(): ServerEnv {
  if (!cached) {
    const parsed = serverEnvSchema.safeParse(process.env);
    if (!parsed.success) {
      const problems = parsed.error.issues.map((issue) => `${issue.path.join(".")}: ${issue.message}`);
      throw new Error(`Invalid server environment:\n  ${problems.join("\n  ")}`);
    }
    cached = parsed.data;
  }
  return cached;
}

/** Resolved proxy-hop trust (explicit setting wins; Vercel's edge is trusted by default). */
export function trustedProxyHops(env: ServerEnv = serverEnv()): number {
  return env.TRUSTED_PROXY_HOPS ?? (env.VERCEL ? 1 : 0);
}

export function googleConfigured(env: ServerEnv = serverEnv()): env is ServerEnv & {
  AUTH_GOOGLE_ID: string;
  AUTH_GOOGLE_SECRET: string;
} {
  return Boolean(env.AUTH_GOOGLE_ID && env.AUTH_GOOGLE_SECRET);
}

export function microsoftConfigured(env: ServerEnv = serverEnv()): env is ServerEnv & {
  AUTH_MICROSOFT_ENTRA_ID_ID: string;
  AUTH_MICROSOFT_ENTRA_ID_SECRET: string;
  AUTH_MICROSOFT_ENTRA_ID_ISSUER: string;
} {
  return Boolean(env.AUTH_MICROSOFT_ENTRA_ID_ID && env.AUTH_MICROSOFT_ENTRA_ID_SECRET && env.AUTH_MICROSOFT_ENTRA_ID_ISSUER);
}

export function githubConfigured(env: ServerEnv = serverEnv()): boolean {
  return Boolean(env.AUTH_GITHUB_ID && env.AUTH_GITHUB_SECRET);
}
