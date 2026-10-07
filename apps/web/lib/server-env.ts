import "server-only";

import { z } from "zod";

const MIN_SECRET_LENGTH = 32;

const csvIds = z
  .string()
  .default("")
  .transform((value) =>
    value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  )
  .pipe(z.array(z.string().regex(/^[1-9][0-9]{0,19}$/, "OWNER_GITHUB_IDS must be numeric GitHub user ids")));

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
    OWNER_GITHUB_IDS: csvIds,
    AUTH_SECRET: z.string().min(MIN_SECRET_LENGTH, `AUTH_SECRET must be at least ${MIN_SECRET_LENGTH} characters`),
    AUTH_URL: z.url().optional(),
    AUTH_GITHUB_ID: z.string().optional(),
    AUTH_GITHUB_SECRET: z.string().optional(),
    BACKEND_TIMEOUT_MS: z.coerce.number().int().min(1000).max(60_000).default(15_000),
  })
  .superRefine((env, ctx) => {
    if (Boolean(env.AUTH_GITHUB_ID) !== Boolean(env.AUTH_GITHUB_SECRET)) {
      ctx.addIssue({ code: "custom", path: ["AUTH_GITHUB_SECRET"], message: "set both AUTH_GITHUB_ID and AUTH_GITHUB_SECRET" });
    }
    if (env.ENVIRONMENT !== "production") return;
    // Production: no implicit host trust, HTTPS everywhere, sign-in must be possible.
    if (!env.AUTH_URL?.startsWith("https://")) {
      ctx.addIssue({ code: "custom", path: ["AUTH_URL"], message: "AUTH_URL must be an https:// URL in production" });
    }
    if (!env.API_BASE_URL.startsWith("https://")) {
      ctx.addIssue({ code: "custom", path: ["API_BASE_URL"], message: "API_BASE_URL must be https:// in production" });
    }
    if (!env.AUTH_GITHUB_ID) {
      ctx.addIssue({ code: "custom", path: ["AUTH_GITHUB_ID"], message: "GitHub OAuth is required in production" });
    }
    if (env.OWNER_GITHUB_IDS.length === 0) {
      ctx.addIssue({ code: "custom", path: ["OWNER_GITHUB_IDS"], message: "at least one owner id is required in production" });
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

export function githubConfigured(env: ServerEnv = serverEnv()): boolean {
  return Boolean(env.AUTH_GITHUB_ID && env.AUTH_GITHUB_SECRET);
}
