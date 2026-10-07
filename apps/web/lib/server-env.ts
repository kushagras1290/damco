import "server-only";

import { z } from "zod";

const MIN_SECRET_LENGTH = 32;

const schema = z.object({
  API_BASE_URL: z.url().default("http://localhost:8000"),
  API_JWT_SECRET: z.string().min(MIN_SECRET_LENGTH, "API_JWT_SECRET must be at least 32 characters"),
  API_JWT_AUDIENCE: z.string().default("jobpulse-api"),
  API_JWT_ISSUER: z.string().default("jobpulse-web"),
  OWNER_GITHUB_LOGINS: z
    .string()
    .default("")
    .transform((value) =>
      value
        .split(",")
        .map((login) => login.trim().toLowerCase())
        .filter(Boolean),
    ),
  AUTH_GITHUB_ID: z.string().optional(),
  AUTH_GITHUB_SECRET: z.string().optional(),
  BACKEND_TIMEOUT_MS: z.coerce.number().int().min(1000).max(60_000).default(15_000),
});

export type ServerEnv = z.infer<typeof schema>;

let cached: ServerEnv | undefined;

/** Validated server environment. Throws (fails fast) on misconfiguration. */
export function serverEnv(): ServerEnv {
  if (!cached) {
    const parsed = schema.safeParse(process.env);
    if (!parsed.success) {
      throw new Error(`Invalid server environment: ${z.prettifyError(parsed.error)}`);
    }
    cached = parsed.data;
  }
  return cached;
}

export function githubConfigured(env: ServerEnv = serverEnv()): boolean {
  return Boolean(env.AUTH_GITHUB_ID && env.AUTH_GITHUB_SECRET);
}
