import type { Problem } from "@/lib/api/types";

const BASE = "/api/backend";
/** Retries for one logical mutation, all with the same Idempotency-Key (safe by construction). */
const MAX_MUTATION_RETRIES = 2;
const RETRYABLE_STATUSES = new Set([409, 502, 503, 504]);
const MAX_RETRY_DELAY_MS = 5_000;
const BASE_RETRY_DELAY_MS = 400;

type Method = "GET" | "POST" | "PATCH" | "DELETE";

export class ApiError extends Error {
  readonly status: number;
  readonly problem: Problem | null;
  readonly retryAfterSeconds: number | null;

  constructor(status: number, problem: Problem | null, retryAfterSeconds: number | null = null) {
    super(problem?.detail ?? `Request failed with status ${status}`);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
    this.retryAfterSeconds = retryAfterSeconds;
  }

  get rateLimited(): boolean {
    return this.status === 429;
  }
}

export type QueryParams = Record<string, string | number | boolean | null | undefined>;

export function buildQuery(params: QueryParams = {}): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== "") search.set(key, String(value));
  }
  const query = search.toString();
  return query ? `?${query}` : "";
}

export function retryAfterSeconds(header: string | null): number | null {
  if (!header) return null;
  const seconds = Number(header);
  return Number.isFinite(seconds) && seconds >= 0 ? seconds : null;
}

/** 409 is only retryable when it means "same key still in flight", not a domain conflict. */
export function isRetryable(error: unknown): boolean {
  if (error instanceof ApiError) {
    if (!RETRYABLE_STATUSES.has(error.status)) return false;
    return error.status !== 409 || error.problem?.type?.endsWith("/idempotency_in_progress") === true;
  }
  return error instanceof TypeError; // fetch network failure
}

export function retryDelayMs(attempt: number, error: unknown): number {
  const hinted = error instanceof ApiError && error.retryAfterSeconds !== null ? error.retryAfterSeconds * 1000 : null;
  return Math.min(MAX_RETRY_DELAY_MS, hinted ?? BASE_RETRY_DELAY_MS * 2 ** attempt);
}

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

async function send<T>(method: Method, path: string, body: unknown, idempotencyKey: string | null): Promise<T> {
  const headers: Record<string, string> = {};
  if (body !== undefined) headers["content-type"] = "application/json";
  if (idempotencyKey) headers["idempotency-key"] = idempotencyKey;
  const response = await fetch(`${BASE}${path}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = null;
    }
  }
  if (!response.ok) {
    throw new ApiError(response.status, (payload as Problem | null) ?? null, retryAfterSeconds(response.headers.get("retry-after")));
  }
  return payload as T;
}

async function mutate<T>(method: Exclude<Method, "GET">, path: string, body: unknown): Promise<T> {
  const idempotencyKey = crypto.randomUUID();
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await send<T>(method, path, body, idempotencyKey);
    } catch (error) {
      if (attempt >= MAX_MUTATION_RETRIES || !isRetryable(error)) throw error;
      await sleep(retryDelayMs(attempt, error));
    }
  }
}

export const api = {
  get: <T>(path: string, params?: QueryParams) => send<T>("GET", `${path}${buildQuery(params)}`, undefined, null),
  post: <T>(path: string, body?: unknown) => mutate<T>("POST", path, body ?? {}),
  patch: <T>(path: string, body: unknown) => mutate<T>("PATCH", path, body),
  delete: (path: string) => mutate<null>("DELETE", path, undefined),
};
