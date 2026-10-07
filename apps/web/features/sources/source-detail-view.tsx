"use client";

import { LogOut, Pause, Play, RefreshCw } from "lucide-react";
import { useRouter } from "next/navigation";

import { ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { SourceHealth } from "@/features/sources/sources-view";
import { useCan, useRuns, useSource, useSyncSource, useUnfollowSource, useUpdateSource } from "@/lib/api/hooks";
import { formatSeconds, timeAgo } from "@/lib/utils";

export function SourceDetailView({ id }: { id: string }) {
  const { data: source, error, isPending } = useSource(id);
  const runs = useRuns({ source_id: id, limit: 10 });
  const canEdit = useCan("admin");
  const update = useUpdateSource(id);
  const sync = useSyncSource(id);
  const unfollow = useUnfollowSource(id);
  const router = useRouter();

  if (isPending) return <LoadingRows rows={6} />;
  if (error) return <ErrorState error={error} />;

  return (
    <>
      <PageHeader
        title={source.name}
        description={`${source.company} · ${source.kind}`}
        actions={
          canEdit ? (
            <>
              <Button variant="outline" onClick={() => sync.mutate()} disabled={source.paused || sync.isPending}>
                <RefreshCw aria-hidden /> {sync.isSuccess ? "Sync queued" : "Sync now"}
              </Button>
              <Button
                variant="outline"
                onClick={() => update.mutate({ enabled: source.paused })}
                disabled={update.isPending}
                title="Pausing affects only this workspace"
              >
                {source.paused ? <Play aria-hidden /> : <Pause aria-hidden />}
                {source.paused ? "Resume" : "Pause"}
              </Button>
              <Button
                variant="ghost"
                onClick={() => unfollow.mutate(undefined, { onSuccess: () => router.push("/sources") })}
                disabled={unfollow.isPending}
              >
                <LogOut aria-hidden /> Unfollow
              </Button>
            </>
          ) : null
        }
      />
      {sync.error ? <ErrorState error={sync.error} /> : null}
      {update.error ? <ErrorState error={update.error} /> : null}
      {unfollow.error ? <ErrorState error={unfollow.error} /> : null}
      {source.paused ? (
        <p className="mb-4 rounded-md border border-warning/30 bg-warning/10 p-3 text-sm">
          Paused for this workspace: new postings are not evaluated for your profiles until you resume.
        </p>
      ) : null}

      <div className="grid gap-4 lg:grid-cols-3">
        <Card>
          <CardHeader>
            <CardTitle>Adaptive polling</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <div className="flex justify-between">
              <span>Health</span>
              <SourceHealth source={source} />
            </div>
            <div className="flex justify-between">
              <span>Current interval</span>
              <span className="tabular-nums">{formatSeconds(source.poll_interval_seconds)}</span>
            </div>
            <div className="flex justify-between">
              <span>Bounds</span>
              <span className="tabular-nums">
                {formatSeconds(source.min_poll_interval_seconds)} – {formatSeconds(source.max_poll_interval_seconds)}
              </span>
            </div>
            <div className="flex justify-between">
              <span>Last polled</span>
              <span>{timeAgo(source.last_polled_at)}</span>
            </div>
            <div className="flex justify-between">
              <span>Last new job</span>
              <span>{timeAgo(source.last_new_job_at)}</span>
            </div>
            <div className="flex justify-between">
              <span>Open jobs</span>
              <span className="tabular-nums">{source.open_jobs}</span>
            </div>
            {source.last_error ? <p className="rounded-md bg-destructive/5 p-2 text-xs text-destructive">{source.last_error}</p> : null}
          </CardContent>
        </Card>

        <Card className="lg:col-span-2">
          <CardHeader>
            <CardTitle>Recent discovery runs</CardTitle>
          </CardHeader>
          <CardContent>
            {runs.data?.items.length ? (
              <Table>
                <THead>
                  <TR>
                    <TH>Started</TH>
                    <TH>Status</TH>
                    <TH>New</TH>
                    <TH>Updated</TH>
                    <TH>Evaluations</TH>
                  </TR>
                </THead>
                <TBody>
                  {runs.data.items.map((run) => (
                    <TR key={run.id}>
                      <TD className="text-xs">{timeAgo(run.started_at)}</TD>
                      <TD>{run.status}</TD>
                      <TD className="tabular-nums">{String(run.stats.new_jobs ?? "—")}</TD>
                      <TD className="tabular-nums">{String(run.stats.updated_jobs ?? "—")}</TD>
                      <TD className="tabular-nums">{String(run.stats.evaluations_started ?? "—")}</TD>
                    </TR>
                  ))}
                </TBody>
              </Table>
            ) : (
              <p className="text-sm text-muted-foreground">No runs yet.</p>
            )}
          </CardContent>
        </Card>

        <Card className="lg:col-span-3">
          <CardHeader>
            <CardTitle>Configuration</CardTitle>
          </CardHeader>
          <CardContent>
            <pre className="overflow-auto rounded-md bg-muted p-3 text-xs">{JSON.stringify(source.config, null, 2)}</pre>
          </CardContent>
        </Card>
      </div>
    </>
  );
}
