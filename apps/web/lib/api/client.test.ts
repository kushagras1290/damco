import { ApiError, api, isRetryable, retryAfterSeconds, retryDelayMs } from "@/lib/api/client";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });
}

describe("retry policy", () => {
  it("retries transient failures and in-flight idempotent duplicates only", () => {
    expect(isRetryable(new ApiError(503, null))).toBe(true);
    expect(isRetryable(new ApiError(504, null))).toBe(true);
    expect(isRetryable(new TypeError("Failed to fetch"))).toBe(true);
    expect(isRetryable(new ApiError(409, { type: "https://jobpulse.dev/problems/idempotency_in_progress", title: "", status: 409, detail: "" }))).toBe(true);
    expect(isRetryable(new ApiError(409, { type: "https://jobpulse.dev/problems/conflict", title: "", status: 409, detail: "" }))).toBe(false);
    expect(isRetryable(new ApiError(422, null))).toBe(false);
    expect(isRetryable(new ApiError(429, null))).toBe(false);
  });

  it("honours Retry-After within a cap", () => {
    expect(retryAfterSeconds("7")).toBe(7);
    expect(retryAfterSeconds("soon")).toBeNull();
    expect(retryDelayMs(0, new ApiError(503, null, 2))).toBe(2000);
    expect(retryDelayMs(0, new ApiError(503, null, 120))).toBe(5000);
    expect(retryDelayMs(1, new TypeError("x"))).toBe(800);
  });
});

describe("api mutations", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.useRealTimers();
  });

  it("reuses one Idempotency-Key across retries of the same mutation", async () => {
    vi.useFakeTimers();
    const fetchMock = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(503, { title: "x", status: 503, detail: "busy" }, { "retry-after": "1" }))
      .mockResolvedValueOnce(jsonResponse(201, { id: "s1" }));
    vi.stubGlobal("fetch", fetchMock);

    const pending = api.post<{ id: string }>("/sources", { name: "x" });
    await vi.runAllTimersAsync();
    await expect(pending).resolves.toEqual({ id: "s1" });

    expect(fetchMock).toHaveBeenCalledTimes(2);
    const keys = fetchMock.mock.calls.map(([, init]) => (init?.headers as Record<string, string>)["idempotency-key"]);
    expect(keys[0]).toMatch(/^[0-9a-f-]{36}$/);
    expect(keys[1]).toBe(keys[0]);
  });

  it("uses a fresh key per mutation and none for reads", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);
    await api.patch("/profile", { a: 1 });
    await api.patch("/profile", { a: 1 });
    await api.get("/jobs");
    const keys = fetchMock.mock.calls.map(([, init]) => (init?.headers as Record<string, string>)["idempotency-key"]);
    expect(keys[0]).not.toBe(keys[1]);
    expect(keys[2]).toBeUndefined();
  });

  it("surfaces rate limits with the server's Retry-After and does not retry them", async () => {
    const fetchMock = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(429, { title: "x", status: 429, detail: "too many requests" }, { "retry-after": "12" }));
    vi.stubGlobal("fetch", fetchMock);
    const error = await api.post("/sources", {}).catch((caught: unknown) => caught);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).rateLimited).toBe(true);
    expect((error as ApiError).retryAfterSeconds).toBe(12);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});
