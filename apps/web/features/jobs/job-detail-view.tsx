"use client";

import { ExternalLink, FileJson, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { EligibilityBadge, ErrorState, LoadingRows, PageHeader, ScorePill } from "@/components/common";
import { RuleList, ScoreBreakdown } from "@/components/explain";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Select } from "@/components/ui/primitives";
import { useCan, useJob, useRerunJob, useSnapshot, useTrackApplication } from "@/lib/api/hooks";
import type { ApplicationStatus, JobDetail } from "@/lib/api/types";
import { humanize, timeAgo } from "@/lib/utils";

const APPLICATION_STATUSES: ApplicationStatus[] = ["interested", "applied", "interviewing", "offer", "rejected", "withdrawn"];

function Fact({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="text-sm">{value}</dd>
    </div>
  );
}

function IntelligenceCard({ job }: { job: JobDetail }) {
  const intel = job.intelligence;
  return (
    <Card>
      <CardHeader>
        <CardTitle>AI extraction</CardTitle>
        <CardDescription>
          {intel
            ? `${intel.model}${intel.escalated ? " (escalated)" : ""} · confidence ${Math.round(intel.confidence * 100)}% · $${intel.cost_usd.toFixed(5)}`
            : "Not run: AI is only used for ambiguous postings, or intelligence is disabled."}
        </CardDescription>
      </CardHeader>
      {intel ? (
        <CardContent>
          <dl className="grid grid-cols-2 gap-3">
            {Object.entries(intel.data)
              .filter(([key]) => key !== "confidence")
              .map(([key, value]) => (
                <Fact
                  key={key}
                  label={humanize(key)}
                  value={Array.isArray(value) ? value.join(", ") || "—" : value === null ? "—" : String(value)}
                />
              ))}
          </dl>
        </CardContent>
      ) : null}
    </Card>
  );
}

function SnapshotCard({ jobId, hasSnapshot }: { jobId: string; hasSnapshot: boolean }) {
  const [open, setOpen] = useState(false);
  const snapshot = useSnapshot(jobId, open && hasSnapshot);
  return (
    <Card>
      <CardHeader>
        <CardTitle>Source snapshot</CardTitle>
        <CardDescription>Immutable original payload used for this decision (replayable).</CardDescription>
      </CardHeader>
      <CardContent>
        {!hasSnapshot ? (
          <p className="text-sm text-muted-foreground">No snapshot stored.</p>
        ) : !open ? (
          <Button variant="outline" size="sm" onClick={() => setOpen(true)}>
            <FileJson aria-hidden /> Show original content
          </Button>
        ) : snapshot.error ? (
          <ErrorState error={snapshot.error} />
        ) : snapshot.data ? (
          <>
            <p className="mb-2 text-xs text-muted-foreground break-all">
              {snapshot.data.snapshot.snapshot_key} · sha256 {snapshot.data.snapshot.snapshot_hash.slice(0, 16)}…
            </p>
            <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap break-all">
              {snapshot.data.content}
            </pre>
            {snapshot.data.truncated ? <p className="mt-1 text-xs text-muted-foreground">Truncated preview.</p> : null}
          </>
        ) : (
          <LoadingRows rows={3} />
        )}
      </CardContent>
    </Card>
  );
}

function OwnerActions({ job }: { job: JobDetail }) {
  const rerun = useRerunJob(job.id);
  const track = useTrackApplication(job.id);
  return (
    <>
      <Select
        aria-label="Application status"
        className="w-40"
        value={job.application?.status ?? ""}
        onChange={(event) => track.mutate(event.target.value as ApplicationStatus)}
        disabled={track.isPending}
      >
        <option value="" disabled>
          Track application…
        </option>
        {APPLICATION_STATUSES.map((status) => (
          <option key={status} value={status}>
            {humanize(status)}
          </option>
        ))}
      </Select>
      <Button variant="outline" onClick={() => rerun.mutate()} disabled={rerun.isPending}>
        <RefreshCw aria-hidden /> {rerun.isSuccess ? "Re-run queued" : "Re-run evaluation"}
      </Button>
    </>
  );
}

export function JobDetailView({ id }: { id: string }) {
  const { data: job, error, isPending } = useJob(id);
  const canEdit = useCan("member");

  if (isPending) return <LoadingRows rows={10} />;
  if (error) return <ErrorState error={error} />;

  const decision = job.eligibility;
  return (
    <>
      <PageHeader
        title={job.title}
        description={`${job.company} · ${job.location ?? "Location n/a"} · ${job.source_name}`}
        actions={
          <>
            {canEdit ? <OwnerActions job={job} /> : null}
            <Button asChild>
              <a href={job.url} target="_blank" rel="noopener noreferrer nofollow">
                <ExternalLink aria-hidden /> View posting
              </a>
            </Button>
          </>
        }
      />

      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader>
            <div className="flex flex-wrap items-center gap-2">
              <CardTitle>Eligibility</CardTitle>
              <EligibilityBadge status={job.eligibility_status} />
              {decision ? <Badge>{humanize(decision.stage)}</Badge> : null}
            </div>
            <CardDescription>
              Hard rules run first. AI may only resolve rules marked unknown — it can never override a failed rule.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {decision ? <RuleList rules={decision.rules} /> : <p className="text-sm text-muted-foreground">Not evaluated yet.</p>}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Match score</CardTitle>
          </CardHeader>
          <CardContent>
            {job.score ? <ScoreBreakdown score={job.score} /> : <p className="text-sm text-muted-foreground">Not ranked.</p>}
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Description</CardTitle>
          </CardHeader>
          <CardContent>
            {/* Sanitised server-side with nh3 (strict tag/attribute/URL-scheme allowlist). */}
            <div className="prose-job text-sm" dangerouslySetInnerHTML={{ __html: job.description_html }} />
          </CardContent>
        </Card>

        <div className="space-y-4">
          <Card>
            <CardHeader>
              <CardTitle>Details</CardTitle>
            </CardHeader>
            <CardContent>
              <dl className="grid grid-cols-2 gap-3">
                <Fact label="Work model" value={job.remote_policy} />
                <Fact label="Seniority" value={job.seniority} />
                <Fact label="Published" value={timeAgo(job.published_at)} />
                <Fact label="Discovered" value={timeAgo(job.first_seen_at)} />
                <Fact label="Department" value={job.department ?? "—"} />
                <Fact label="Version" value={`v${job.version}`} />
                <Fact label="State" value={humanize(job.workflow_state)} />
                <Fact label="Score" value={<ScorePill score={job.match_score} />} />
              </dl>
            </CardContent>
          </Card>
          <IntelligenceCard job={job} />
        </div>

        <div className="lg:col-span-2">
          <SnapshotCard jobId={job.id} hasSnapshot={job.snapshot !== null} />
        </div>

        <Card>
          <CardHeader>
            <CardTitle>Decision trace</CardTitle>
            <CardDescription>Every evaluation is append-only and auditable.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            {job.eligibility_history.map((entry) => (
              <div key={entry.id} className="flex items-center justify-between gap-2">
                <span>
                  {humanize(entry.stage)} <EligibilityBadge status={entry.status} />
                </span>
                <span className="text-xs text-muted-foreground">{timeAgo(entry.created_at)}</span>
              </div>
            ))}
            {job.notifications.map((n) => (
              <div key={`${n.channel}-${n.created_at}`} className="flex justify-between text-xs text-muted-foreground">
                <span>
                  Notification via {n.channel}: {n.status}
                </span>
                <span>{timeAgo(n.created_at)}</span>
              </div>
            ))}
            {job.similar.length ? (
              <div className="pt-2">
                <p className="text-xs text-muted-foreground">Similar titles (pg_trgm)</p>
                {job.similar.map((other) => (
                  <Link key={other.id} href={`/jobs/${other.id}`} className="block truncate text-xs hover:underline">
                    {other.title} — {other.company} ({Math.round(other.similarity * 100)}%)
                  </Link>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </>
  );
}
