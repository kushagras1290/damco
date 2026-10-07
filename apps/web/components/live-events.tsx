"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Radio, Sparkles, X } from "lucide-react";
import Link from "next/link";
import { useEffect } from "react";

import { Badge } from "@/components/ui/primitives";
import { describeEvent, parseLiveEvent, staleQueryKeys } from "@/lib/api/events";
import { type LiveStatus, useLiveFeed } from "@/lib/stores/live-feed";
import { cn } from "@/lib/utils";

const STREAM_URL = "/api/backend/events";
const INVALIDATE_THROTTLE_MS = 1_000;
const RECONNECT_BASE_MS = 2_000;
const RECONNECT_MAX_MS = 60_000;
const TOAST_TTL_MS = 8_000;

/**
 * Owns the single EventSource for the tab. The browser retries transient drops itself
 * (server sends `retry:`); when the stream is closed for good (429/503/busy) we back off
 * and reconnect, and after any gap we refetch everything since events may have been missed.
 */
export function LiveEvents() {
  const client = useQueryClient();

  useEffect(() => {
    const { push, setStatus } = useLiveFeed.getState();
    const pending = new Map<string, readonly unknown[]>();
    let source: EventSource | null = null;
    let flushTimer: ReturnType<typeof setTimeout> | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let hadConnection = false;
    let stopped = false;

    const flush = () => {
      flushTimer = null;
      for (const queryKey of pending.values()) void client.invalidateQueries({ queryKey });
      pending.clear();
    };

    const scheduleReconnect = (status: LiveStatus) => {
      source?.close();
      source = null;
      setStatus(status);
      if (stopped || reconnectTimer) return;
      const delay = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** attempt) * (0.5 + Math.random() / 2);
      attempt += 1;
      reconnectTimer = setTimeout(() => {
        reconnectTimer = null;
        connect();
      }, delay);
    };

    function connect() {
      if (stopped) return;
      setStatus(hadConnection ? "offline" : "connecting");
      const stream = new EventSource(STREAM_URL);
      source = stream;

      stream.addEventListener("ready", () => {
        if (hadConnection) void client.invalidateQueries(); // resync anything missed during the gap
        hadConnection = true;
        attempt = 0;
        setStatus("live");
      });
      stream.addEventListener("busy", () => scheduleReconnect("offline"));
      stream.onmessage = (message: MessageEvent<string>) => {
        const event = parseLiveEvent(message.data);
        if (!event) return;
        push(event);
        for (const key of staleQueryKeys(event)) pending.set(JSON.stringify(key), key);
        flushTimer ??= setTimeout(flush, INVALIDATE_THROTTLE_MS);
      };
      stream.onerror = () => {
        if (stream.readyState === EventSource.CLOSED) {
          // Non-200 (rate limited, realtime disabled, API down): the browser will not retry.
          scheduleReconnect(hadConnection ? "offline" : "unavailable");
        } else {
          setStatus("offline"); // browser is reconnecting on its own
        }
      };
    }

    connect();
    return () => {
      stopped = true;
      source?.close();
      if (flushTimer) clearTimeout(flushTimer);
      if (reconnectTimer) clearTimeout(reconnectTimer);
    };
  }, [client]);

  return <MatchToasts />;
}

const STATUS_COPY: Record<LiveStatus, { label: string; className: string }> = {
  live: { label: "Live", className: "bg-success" },
  connecting: { label: "Connecting", className: "bg-warning animate-pulse" },
  offline: { label: "Reconnecting", className: "bg-warning animate-pulse" },
  unavailable: { label: "Polling", className: "bg-muted-foreground" },
};

export function LiveStatusBadge() {
  const status = useLiveFeed((state) => state.status);
  const copy = STATUS_COPY[status];
  return (
    <span
      className="inline-flex items-center gap-1.5 text-xs text-muted-foreground"
      title={status === "live" ? "Receiving realtime updates" : "Falling back to periodic refresh"}
      data-testid="live-status"
    >
      <span className={cn("size-2 rounded-full", copy.className)} aria-hidden />
      {copy.label}
    </span>
  );
}

function MatchToasts() {
  const toasts = useLiveFeed((state) => state.toasts);
  const dismiss = useLiveFeed((state) => state.dismissToast);

  useEffect(() => {
    if (toasts.length === 0) return;
    const timers = toasts.map((toast) => setTimeout(() => dismiss(toast.id), TOAST_TTL_MS));
    return () => timers.forEach(clearTimeout);
  }, [toasts, dismiss]);

  return (
    <div aria-live="polite" className="pointer-events-none fixed right-4 bottom-4 z-50 flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2">
      {toasts.map((toast) => {
        const summary = describeEvent(toast.event);
        return (
          <div key={toast.id} role="status" className="pointer-events-auto flex items-start gap-2 rounded-lg border bg-card p-3 text-sm shadow-lg">
            <Sparkles className="mt-0.5 size-4 shrink-0 text-success" aria-hidden />
            {summary.href ? (
              <Link href={summary.href} className="min-w-0 flex-1 hover:underline" onClick={() => dismiss(toast.id)}>
                {summary.text}
              </Link>
            ) : (
              <span className="min-w-0 flex-1">{summary.text}</span>
            )}
            <button type="button" onClick={() => dismiss(toast.id)} className="text-muted-foreground hover:text-foreground" aria-label="Dismiss">
              <X className="size-4" aria-hidden />
            </button>
          </div>
        );
      })}
    </div>
  );
}

export function LiveFeedList({ limit = 12 }: { limit?: number }) {
  const items = useLiveFeed((state) => state.items).slice(0, limit);
  const status = useLiveFeed((state) => state.status);

  if (items.length === 0) {
    return (
      <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
        <Radio className="size-4" aria-hidden />
        {status === "live" ? "Listening… new activity appears here as it happens." : "Waiting for the live connection…"}
      </p>
    );
  }
  return (
    <ul className="divide-y" data-testid="live-feed">
      {items.map(({ id, event }) => {
        const summary = describeEvent(event);
        return (
          <li key={id} className="flex items-center justify-between gap-3 py-2 text-sm">
            {summary.href ? (
              <Link href={summary.href} className="min-w-0 truncate hover:underline">
                {summary.text}
              </Link>
            ) : (
              <span className="min-w-0 truncate">{summary.text}</span>
            )}
            <span className="flex shrink-0 items-center gap-2">
              {summary.tone !== "neutral" ? <Badge tone={summary.tone}>{event.type.split(".")[1]}</Badge> : null}
              <time className="text-xs tabular-nums text-muted-foreground" dateTime={event.ts}>
                {new Date(event.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}
              </time>
            </span>
          </li>
        );
      })}
    </ul>
  );
}
