import { describeEvent, parseLiveEvent, staleQueryKeys } from "@/lib/api/events";
import { MAX_FEED_ITEMS, MAX_TOASTS, SYNC_EXPIRY_MS, useLiveFeed } from "@/lib/stores/live-feed";

const TS = "2026-10-07T10:00:00+00:00";
const raw = (type: string, data: Record<string, unknown>) => JSON.stringify({ type, ts: TS, data });

const matched = raw("job.matched", { job_id: "j1", title: "Staff Engineer", company: "Acme", eligible: true, score: 0.914 });
const discovered = raw("jobs.discovered", {
  source_id: "s1",
  source: "Demo board",
  stage: "stored",
  new: 3,
  updated: 0,
  closed: 0,
  evaluating: 3,
  jobs: [{ id: "j1", title: "Staff Engineer", company: "Acme" }],
});

describe("parseLiveEvent", () => {
  it("parses known events", () => {
    const event = parseLiveEvent(matched);
    expect(event?.type).toBe("job.matched");
  });

  it.each([
    ["not json", "{"],
    ["unknown type", raw("job.teleported", { job_id: "j1" })],
    ["missing fields", raw("job.matched", { job_id: "j1" })],
    ["wrong types", raw("source.polled", { source_id: "s1", source: "x", success: "yes", next_poll_seconds: 60, circuit_open: false })],
  ])("ignores %s", (_, payload) => {
    expect(parseLiveEvent(payload)).toBeNull();
  });
});

describe("describeEvent", () => {
  it("summarises matches with a link and score", () => {
    const summary = describeEvent(parseLiveEvent(matched)!);
    expect(summary).toEqual({ text: "Strong match: Staff Engineer at Acme · 91%", tone: "success", href: "/jobs/j1" });
  });

  it("summarises discoveries", () => {
    expect(describeEvent(parseLiveEvent(discovered)!).text).toBe("Demo board: 3 new jobs");
  });

  it("flags failed polls and open circuits", () => {
    const failed = parseLiveEvent(
      raw("source.polled", { source_id: "s1", source: "Acme", success: false, next_poll_seconds: 600, circuit_open: true, error: "boom" }),
    )!;
    expect(describeEvent(failed)).toMatchObject({ tone: "danger", href: "/sources/s1" });
  });

  it("marks failed runs", () => {
    const run = parseLiveEvent(raw("run.finished", { workflow_type: "SourceDiscoveryWorkflow", workflow_id: "w", status: "failed", error: "timeout" }))!;
    expect(describeEvent(run)).toMatchObject({ text: "SourceDiscoveryWorkflow failed: timeout", tone: "danger", href: "/runs" });
  });
});

describe("staleQueryKeys", () => {
  it("invalidates job lists and the dashboard on discovery", () => {
    expect(staleQueryKeys(parseLiveEvent(discovered)!)).toEqual([["jobs"], ["dashboard"], ["sources"], ["source", "s1"]]);
  });

  it("does nothing for in-flight progress", () => {
    const progress = parseLiveEvent(raw("source.progress", { source_id: "s1", source: "Acme", stage: "fetching" }))!;
    expect(staleQueryKeys(progress)).toEqual([]);
  });
});

describe("useLiveFeed", () => {
  beforeEach(() => useLiveFeed.getState().clear());

  it("keeps a bounded newest-first feed and toasts only matches", () => {
    const { push } = useLiveFeed.getState();
    for (let i = 0; i < MAX_FEED_ITEMS + 5; i += 1) push(parseLiveEvent(discovered)!);
    for (let i = 0; i < MAX_TOASTS + 2; i += 1) push(parseLiveEvent(matched)!);
    const state = useLiveFeed.getState();
    expect(state.items).toHaveLength(MAX_FEED_ITEMS);
    expect(state.items[0]?.event.type).toBe("job.matched");
    expect(state.toasts).toHaveLength(MAX_TOASTS);
    expect(state.sourceProgress.s1?.stage).toBe("stored");
  });

  it("tracks per-source sync stage and expires a sync whose completion was never seen", () => {
    vi.useFakeTimers();
    try {
      useLiveFeed.getState().push(parseLiveEvent(raw("source.progress", { source_id: "s2", source: "B", stage: "fetching" }))!);
      expect(useLiveFeed.getState().sourceProgress.s2?.stage).toBe("fetching");
      vi.advanceTimersByTime(SYNC_EXPIRY_MS);
      expect(useLiveFeed.getState().sourceProgress.s2).toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
  });

  it("does not let an old expiry timer clear a newer sync", () => {
    vi.useFakeTimers();
    try {
      const fetching = parseLiveEvent(raw("source.progress", { source_id: "s3", source: "C", stage: "fetching" }))!;
      useLiveFeed.getState().push(fetching);
      vi.advanceTimersByTime(SYNC_EXPIRY_MS / 2);
      useLiveFeed.getState().push(fetching);
      vi.advanceTimersByTime(SYNC_EXPIRY_MS / 2);
      expect(useLiveFeed.getState().sourceProgress.s3?.stage).toBe("fetching");
    } finally {
      vi.useRealTimers();
    }
  });
});
