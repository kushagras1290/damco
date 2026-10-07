"use client";

import Link from "next/link";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EmptyState, ErrorState, LoadingRows, PageHeader, ScorePill } from "@/components/common";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/primitives";
import { useDashboard } from "@/lib/api/hooks";
import { humanize } from "@/lib/utils";

function Stat({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl tabular-nums">{value}</CardTitle>
      </CardHeader>
      {hint ? <CardContent className="text-xs text-muted-foreground">{hint}</CardContent> : null}
    </Card>
  );
}

const AXIS = { fontSize: 11, fill: "var(--muted-foreground)" };
const TOOLTIP_STYLE = { background: "var(--card)", border: "1px solid var(--border)", borderRadius: 8, fontSize: 12 };

export function DashboardView() {
  const { data, error, isPending } = useDashboard();

  if (isPending) return <LoadingRows rows={8} />;
  if (error) return <ErrorState error={error} />;

  const eligible = data.jobs_by_status.eligible ?? 0;
  const ineligible = data.jobs_by_status.ineligible ?? 0;
  const pending = data.jobs_by_status.pending ?? 0;
  const runsFailed = data.runs_last_24h.failed ?? 0;
  const runsTotal = Object.values(data.runs_last_24h).reduce((sum, n) => sum + n, 0);

  return (
    <>
      <PageHeader title="Dashboard" description="Live view of discovery, eligibility and matching." />
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
        <Stat label="Eligible jobs" value={eligible} />
        <Stat label="Rejected (hard rules)" value={ineligible} />
        <Stat label="Pending evaluation" value={pending} />
        <Stat label="Sources" value={data.sources_total} />
        <Stat label="Runs (24h)" value={runsTotal} hint={runsFailed ? `${runsFailed} failed` : "no failures"} />
      </div>

      <div className="mt-4 grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Jobs discovered per day</CardTitle>
            <CardDescription>Last 14 days</CardDescription>
          </CardHeader>
          <CardContent className="h-56">
            {data.discovered_per_day.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data.discovered_per_day}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                  <XAxis dataKey="day" tick={AXIS} tickFormatter={(day: string) => day.slice(5)} />
                  <YAxis allowDecimals={false} tick={AXIS} width={32} />
                  <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: "var(--muted)" }} />
                  <Bar dataKey="count" name="Jobs" fill="var(--chart-1)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState title="No discoveries yet">Add a source to start polling.</EmptyState>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Match score distribution</CardTitle>
            <CardDescription>Eligible, ranked jobs</CardDescription>
          </CardHeader>
          <CardContent className="h-56">
            {data.score_histogram.length ? (
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={data.score_histogram.map((b) => ({ ...b, label: `${Math.round(b.bucket * 100)}%` }))}>
                  <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" vertical={false} />
                  <XAxis dataKey="label" tick={AXIS} />
                  <YAxis allowDecimals={false} tick={AXIS} width={32} />
                  <Tooltip contentStyle={TOOLTIP_STYLE} cursor={{ fill: "var(--muted)" }} />
                  <Bar dataKey="count" name="Jobs" fill="var(--chart-2)" radius={[4, 4, 0, 0]} />
                </BarChart>
              </ResponsiveContainer>
            ) : (
              <EmptyState title="No scored jobs yet" />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Top matches</CardTitle>
          </CardHeader>
          <CardContent>
            {data.top_matches.length ? (
              <ul className="divide-y">
                {data.top_matches.map((job) => (
                  <li key={job.id} className="flex items-center justify-between gap-3 py-2">
                    <Link href={`/jobs/${job.id}`} className="min-w-0 hover:underline">
                      <p className="truncate text-sm font-medium">{job.title}</p>
                      <p className="truncate text-xs text-muted-foreground">
                        {job.company} · {job.location ?? "Location n/a"}
                      </p>
                    </Link>
                    <ScorePill score={job.match_score} />
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState title="No eligible matches yet" />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Why jobs are rejected</CardTitle>
            <CardDescription>Failing hard rules, last 14 days</CardDescription>
          </CardHeader>
          <CardContent>
            {data.rejection_reasons.length ? (
              <ul className="space-y-2">
                {data.rejection_reasons.map((reason) => {
                  const max = data.rejection_reasons[0]?.count ?? 1;
                  return (
                    <li key={reason.rule} className="text-sm">
                      <div className="flex justify-between">
                        <span>{humanize(reason.rule)}</span>
                        <span className="tabular-nums text-muted-foreground">{reason.count}</span>
                      </div>
                      <div className="mt-1 h-1.5 rounded-full bg-muted">
                        <div className="h-full rounded-full bg-destructive/70" style={{ width: `${(reason.count / max) * 100}%` }} />
                      </div>
                    </li>
                  );
                })}
              </ul>
            ) : (
              <EmptyState title="No rejections recorded" />
            )}
            <p className="mt-4 text-xs text-muted-foreground">
              Estimated LLM spend: ${data.llm_cost_usd.toFixed(4)} · Notifications sent: {data.notifications.sent ?? 0}
            </p>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
