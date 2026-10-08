import { AlertTriangle, Inbox } from "lucide-react";
import type { ReactNode } from "react";

import { Badge, Skeleton } from "@/components/ui/primitives";
import { ApiError } from "@/lib/api/client";
import type { EligibilityStatus, RuleOutcome } from "@/lib/api/types";
import { formatScore } from "@/lib/utils";

export function PageHeader({ title, description, actions }: { title: string; description?: string; actions?: ReactNode }) {
  return (
    <header className="mb-5 flex flex-wrap items-end justify-between gap-3">
      <div>
        <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
        {description ? <p className="mt-1 text-sm text-muted-foreground">{description}</p> : null}
      </div>
      {actions ? <div className="flex flex-wrap gap-2">{actions}</div> : null}
    </header>
  );
}

export function ErrorState({ error }: { error: unknown }) {
  const message =
    error instanceof ApiError
      ? error.status === 403
        ? "You need the Owner role to do this."
        : error.rateLimited
          ? `Too many requests - try again in ${error.retryAfterSeconds ?? 60}s.`
          : error.status === 402
            ? `${error.message} (Workspace → Plan & billing).`
            : error.message
      : "Something went wrong.";
  return (
    <div role="alert" className="flex items-center gap-2 rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
      <AlertTriangle className="size-4 shrink-0" aria-hidden />
      {message}
    </div>
  );
}

export function EmptyState({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center gap-2 rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
      <Inbox className="size-6" aria-hidden />
      <p className="font-medium text-foreground">{title}</p>
      {children}
    </div>
  );
}

export function LoadingRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2" aria-busy="true" aria-label="Loading">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-9 w-full" />
      ))}
    </div>
  );
}

const ELIGIBILITY_TONE = { eligible: "success", ineligible: "danger", pending: "neutral" } as const;

export function EligibilityBadge({ status }: { status: EligibilityStatus }) {
  return <Badge tone={ELIGIBILITY_TONE[status]}>{status}</Badge>;
}

const OUTCOME_TONE = { pass: "success", fail: "danger", unknown: "warning" } as const;

export function OutcomeBadge({ outcome }: { outcome: RuleOutcome }) {
  return <Badge tone={OUTCOME_TONE[outcome]}>{outcome}</Badge>;
}

export function ScorePill({ score }: { score: number | null }) {
  const tone = score === null ? "neutral" : score >= 0.75 ? "success" : score >= 0.5 ? "warning" : "danger";
  return (
    <Badge tone={tone} className="tabular-nums">
      {formatScore(score)}
    </Badge>
  );
}
