"use client";

import { CheckCircle2, XCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { EligibilityBadge, EmptyState, ErrorState, LoadingRows, OutcomeBadge, PageHeader } from "@/components/common";
import { Badge, Card, CardContent, CardHeader, CardTitle, Select } from "@/components/ui/primitives";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useApplications, useDecisions, useIsOwner, useRuns, useSystem, useUpdateApplication } from "@/lib/api/hooks";
import type { ApplicationStatus, Run } from "@/lib/api/types";
import { humanize, timeAgo } from "@/lib/utils";

const PAGE = 50;
const APPLICATION_STATUSES: ApplicationStatus[] = ["interested", "applied", "interviewing", "offer", "rejected", "withdrawn"];
const RUN_TONE = { running: "info", completed: "success", failed: "danger", cancelled: "neutral" } as const;

function duration(run: Run): string {
  if (!run.finished_at) return "—";
  const ms = new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`;
}

export function RunsView() {
  const [type, setType] = useState("");
  const [status, setStatus] = useState("");
  const { data, error, isPending } = useRuns({ workflow_type: type || undefined, status: status || undefined, limit: PAGE });
  return (
    <>
      <PageHeader
        title="Workflow runs"
        description="Durable Temporal executions recorded for audit."
        actions={
          <>
            <Select aria-label="Workflow type" value={type} onChange={(e) => setType(e.target.value)}>
              <option value="">All workflows</option>
              <option value="SourceDiscoveryWorkflow">Discovery</option>
              <option value="JobEvaluationWorkflow">Evaluation</option>
            </Select>
            <Select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">Any status</option>
              <option value="running">Running</option>
              <option value="completed">Completed</option>
              <option value="failed">Failed</option>
            </Select>
          </>
        }
      />
      {error ? <ErrorState error={error} /> : null}
      {isPending ? (
        <LoadingRows />
      ) : !data?.items.length ? (
        <EmptyState title="No runs recorded yet" />
      ) : (
        <Card>
          <Table>
            <THead>
              <TR>
                <TH>Workflow</TH>
                <TH>Status</TH>
                <TH>Subject</TH>
                <TH>Stats</TH>
                <TH>Started</TH>
                <TH>Duration</TH>
              </TR>
            </THead>
            <TBody>
              {data.items.map((run) => (
                <TR key={run.id}>
                  <TD>
                    <span className="font-medium">{run.workflow_type.replace("Workflow", "")}</span>
                    <span className="block max-w-56 truncate font-mono text-[11px] text-muted-foreground">{run.workflow_id}</span>
                  </TD>
                  <TD>
                    <Badge tone={RUN_TONE[run.status]}>{run.status}</Badge>
                  </TD>
                  <TD className="text-xs">
                    {run.job_id ? (
                      <Link className="hover:underline" href={`/jobs/${run.job_id}`}>
                        job
                      </Link>
                    ) : run.source_id ? (
                      <Link className="hover:underline" href={`/sources/${run.source_id}`}>
                        source
                      </Link>
                    ) : (
                      "—"
                    )}
                  </TD>
                  <TD className="max-w-72 text-xs text-muted-foreground">
                    {run.error ? (
                      <span className="text-destructive">{run.error}</span>
                    ) : (
                      Object.entries(run.stats)
                        .filter(([, v]) => v !== null && v !== "")
                        .map(([k, v]) => `${humanize(k)}: ${String(v)}`)
                        .join(" · ")
                    )}
                  </TD>
                  <TD className="text-xs whitespace-nowrap">{timeAgo(run.started_at)}</TD>
                  <TD className="text-xs tabular-nums">{duration(run)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </Card>
      )}
    </>
  );
}

export function DecisionsView() {
  const [status, setStatus] = useState<"" | "eligible" | "ineligible">("ineligible");
  const { data, error, isPending } = useDecisions({ status: status || undefined, limit: PAGE });
  return (
    <>
      <PageHeader
        title="Eligibility decisions"
        description="Append-only audit log of every hard-rule evaluation and its evidence."
        actions={
          <Select aria-label="Status" value={status} onChange={(e) => setStatus(e.target.value as typeof status)}>
            <option value="">All</option>
            <option value="ineligible">Rejected</option>
            <option value="eligible">Eligible</option>
          </Select>
        }
      />
      {error ? <ErrorState error={error} /> : null}
      {isPending ? (
        <LoadingRows />
      ) : !data?.items.length ? (
        <EmptyState title="No decisions yet" />
      ) : (
        <Card>
          <Table>
            <THead>
              <TR>
                <TH>Job</TH>
                <TH>Stage</TH>
                <TH>Result</TH>
                <TH>Failed rules / evidence</TH>
                <TH>When</TH>
              </TR>
            </THead>
            <TBody>
              {data.items.map((decision) => (
                <TR key={decision.id}>
                  <TD>
                    <Link href={`/jobs/${decision.job_id}`} className="font-medium hover:underline">
                      {decision.job_title}
                    </Link>
                    <span className="block text-xs text-muted-foreground">{decision.company}</span>
                  </TD>
                  <TD className="text-xs">{humanize(decision.stage)}</TD>
                  <TD>
                    <EligibilityBadge status={decision.status} />
                  </TD>
                  <TD className="max-w-xl space-y-1">
                    {decision.failed_rules.map((rule) => (
                      <div key={rule.rule} className="flex items-start gap-2 text-xs">
                        <OutcomeBadge outcome={rule.outcome} />
                        <span>
                          <strong className="font-medium">{humanize(rule.rule)}:</strong> {rule.evidence}
                        </span>
                      </div>
                    ))}
                    {decision.unresolved.length ? (
                      <p className="text-xs text-muted-foreground">Unresolved: {decision.unresolved.join(", ")}</p>
                    ) : null}
                  </TD>
                  <TD className="text-xs whitespace-nowrap">{timeAgo(decision.created_at)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </Card>
      )}
    </>
  );
}

export function ApplicationsView() {
  const { data, error, isPending } = useApplications({ limit: PAGE });
  const update = useUpdateApplication();
  const isOwner = useIsOwner();
  return (
    <>
      <PageHeader title="Applications" description="Track where you applied and how it is going." />
      {error ? <ErrorState error={error} /> : null}
      {update.error ? <ErrorState error={update.error} /> : null}
      {isPending ? (
        <LoadingRows />
      ) : !data?.items.length ? (
        <EmptyState title="No tracked applications">Open a job and choose “Track application”.</EmptyState>
      ) : (
        <Card>
          <Table>
            <THead>
              <TR>
                <TH>Job</TH>
                <TH>Status</TH>
                <TH>Updated</TH>
              </TR>
            </THead>
            <TBody>
              {data.items.map((application) => (
                <TR key={application.id}>
                  <TD>
                    <Link href={`/jobs/${application.job_id}`} className="font-medium hover:underline">
                      {application.job_title}
                    </Link>
                    <span className="block text-xs text-muted-foreground">{application.company}</span>
                  </TD>
                  <TD>
                    {isOwner ? (
                      <Select
                        aria-label={`Status for ${application.job_title}`}
                        className="w-40"
                        value={application.status}
                        onChange={(e) => update.mutate({ id: application.id, status: e.target.value as ApplicationStatus })}
                      >
                        {APPLICATION_STATUSES.map((status) => (
                          <option key={status} value={status}>
                            {humanize(status)}
                          </option>
                        ))}
                      </Select>
                    ) : (
                      <Badge>{application.status}</Badge>
                    )}
                  </TD>
                  <TD className="text-xs">{timeAgo(application.updated_at)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </Card>
      )}
    </>
  );
}

export function SystemView() {
  const { data, error, isPending } = useSystem();
  if (isPending) return <LoadingRows />;
  if (error) return <ErrorState error={error} />;
  return (
    <>
      <PageHeader title="System" description={`Environment ${data.environment} · API v${data.version}`} />
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Dependencies</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {data.dependencies.map((dep) => (
              <div key={dep.name} className="flex items-center justify-between text-sm">
                <span className="flex items-center gap-2">
                  {dep.ok ? (
                    <CheckCircle2 className="size-4 text-success" aria-label="healthy" />
                  ) : (
                    <XCircle className="size-4 text-destructive" aria-label="unhealthy" />
                  )}
                  {dep.name}
                </span>
                <span className="text-xs text-muted-foreground">{dep.detail}</span>
              </div>
            ))}
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <CardTitle>Intelligence & storage</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span>AI enrichment</span>
              <Badge tone={data.intelligence_enabled ? "success" : "warning"}>
                {data.intelligence_enabled ? "enabled" : "deterministic only"}
              </Badge>
            </div>
            {Object.entries(data.models).map(([role, model]) => (
              <div key={role} className="flex justify-between">
                <span>{humanize(role)} model</span>
                <code className="text-xs">{model ?? "—"}</code>
              </div>
            ))}
            <div className="flex justify-between">
              <span>Snapshot storage</span>
              <code className="text-xs">{data.storage_backend}</code>
            </div>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
