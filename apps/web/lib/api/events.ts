import { z } from "zod";

import { formatScore } from "@/lib/utils";

/** Mirrors apps/api/src/jobpulse/services/events.py (EventType + payloads). */
const text = z.string().max(400);
const id = z.string().max(100);

const sourceProgress = z.object({
  source_id: id,
  source: text,
  stage: z.enum(["fetching", "not_modified", "fetched", "stored"]),
  found: z.number().int().nonnegative().optional(),
  new: z.number().int().nonnegative().optional(),
  updated: z.number().int().nonnegative().optional(),
});

const jobsDiscovered = z.object({
  source_id: id,
  source: text,
  new: z.number().int().nonnegative(),
  updated: z.number().int().nonnegative(),
  closed: z.number().int().nonnegative().optional(),
  evaluating: z.number().int().nonnegative().optional(),
  jobs: z.array(z.object({ id, title: text, company: text })).default([]),
});

const sourcePolled = z.object({
  source_id: id,
  source: text,
  success: z.boolean(),
  next_poll_seconds: z.number().nonnegative(),
  circuit_open: z.boolean(),
  error: text.nullable().optional(),
});

const jobEvaluated = z.object({
  job_id: id,
  title: text,
  company: text,
  eligible: z.boolean(),
  score: z.number().nullable().optional(),
  failed_rules: z.array(text).optional(),
});

const notificationSent = z.object({ job_id: id, title: text, channel: text });

const runStarted = z.object({ workflow_type: text, workflow_id: text, source_id: id.nullable().optional() });

const runFinished = z.object({
  workflow_type: text,
  workflow_id: text,
  status: text,
  error: text.nullable().optional(),
});

const envelope = <T extends string, S extends z.ZodType>(type: T, data: S) =>
  z.object({ type: z.literal(type), ts: z.string(), data });

export const liveEventSchema = z.discriminatedUnion("type", [
  envelope("source.progress", sourceProgress),
  envelope("jobs.discovered", jobsDiscovered),
  envelope("source.polled", sourcePolled),
  envelope("job.evaluated", jobEvaluated),
  envelope("job.matched", jobEvaluated),
  envelope("notification.sent", notificationSent),
  envelope("run.started", runStarted),
  envelope("run.finished", runFinished),
]);

export type LiveEvent = z.infer<typeof liveEventSchema>;
export type LiveEventType = LiveEvent["type"];

/** Parse one SSE `data:` payload; unknown or malformed events are ignored (forward compatible). */
export function parseLiveEvent(raw: string): LiveEvent | null {
  let json: unknown;
  try {
    json = JSON.parse(raw);
  } catch {
    return null;
  }
  const parsed = liveEventSchema.safeParse(json);
  return parsed.success ? parsed.data : null;
}

export type Tone = "neutral" | "info" | "success" | "warning" | "danger";

export interface EventSummary {
  text: string;
  tone: Tone;
  href?: string;
}

const pct = (score: number | null | undefined) => (score == null ? "" : ` · ${formatScore(score)}`);

export function describeEvent(event: LiveEvent): EventSummary {
  switch (event.type) {
    case "source.progress": {
      const { source, stage, found } = event.data;
      const label = {
        fetching: "checking for new jobs…",
        not_modified: "no changes",
        fetched: `fetched ${found ?? 0} postings`,
        stored: "up to date",
      }[stage];
      return { text: `${source}: ${label}`, tone: "neutral", href: `/sources/${event.data.source_id}` };
    }
    case "jobs.discovered": {
      const { source, new: added, updated } = event.data;
      const parts = [added ? `${added} new` : "", updated ? `${updated} updated` : ""].filter(Boolean).join(", ");
      return { text: `${source}: ${parts || "changes"} job${added + updated === 1 ? "" : "s"}`, tone: "info", href: "/jobs" };
    }
    case "source.polled": {
      const { source, success, circuit_open: circuitOpen } = event.data;
      if (success) return { text: `${source} polled`, tone: "neutral", href: `/sources/${event.data.source_id}` };
      return {
        text: `${source} poll failed${circuitOpen ? " (paused after repeated errors)" : ""}`,
        tone: circuitOpen ? "danger" : "warning",
        href: `/sources/${event.data.source_id}`,
      };
    }
    case "job.evaluated": {
      const { title, company, eligible, score } = event.data;
      return {
        text: `${eligible ? "Scored" : "Filtered out"}: ${title} at ${company}${eligible ? pct(score) : ""}`,
        tone: eligible ? "neutral" : "warning",
        href: `/jobs/${event.data.job_id}`,
      };
    }
    case "job.matched":
      return {
        text: `Strong match: ${event.data.title} at ${event.data.company}${pct(event.data.score)}`,
        tone: "success",
        href: `/jobs/${event.data.job_id}`,
      };
    case "notification.sent":
      return { text: `Alert sent via ${event.data.channel}: ${event.data.title}`, tone: "info", href: `/jobs/${event.data.job_id}` };
    case "run.started":
      return { text: `Started ${event.data.workflow_type}`, tone: "neutral", href: "/runs" };
    case "run.finished": {
      const failed = event.data.status !== "completed";
      return {
        text: `${event.data.workflow_type} ${failed ? `failed${event.data.error ? `: ${event.data.error}` : ""}` : "finished"}`,
        tone: failed ? "danger" : "neutral",
        href: "/runs",
      };
    }
  }
}

/** Which cached queries an event makes stale (prefix keys for TanStack Query). */
export function staleQueryKeys(event: LiveEvent): readonly (readonly unknown[])[] {
  switch (event.type) {
    case "source.progress":
      return event.data.stage === "stored" ? [["sources"], ["source", event.data.source_id]] : [];
    case "jobs.discovered":
      return [["jobs"], ["dashboard"], ["sources"], ["source", event.data.source_id]];
    case "source.polled":
      return [["sources"], ["source", event.data.source_id], ["system"]];
    case "job.evaluated":
    case "job.matched":
      return [["jobs"], ["job", event.data.job_id], ["dashboard"], ["decisions"]];
    case "notification.sent":
      return [["job", event.data.job_id]];
    case "run.started":
    case "run.finished":
      return [["runs"], ["dashboard"]];
  }
}
