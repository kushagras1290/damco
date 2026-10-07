"use client";

import { Plus } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { SourceForm } from "@/features/sources/source-form";
import { useIsOwner, useSources } from "@/lib/api/hooks";
import type { Source } from "@/lib/api/types";
import { formatSeconds, timeAgo } from "@/lib/utils";

export function SourceHealth({ source }: { source: Source }) {
  if (!source.enabled) return <Badge>disabled</Badge>;
  if (source.circuit_open_until && new Date(source.circuit_open_until) > new Date()) {
    return <Badge tone="danger">circuit open</Badge>;
  }
  if (source.consecutive_failures > 0) return <Badge tone="warning">{source.consecutive_failures} failures</Badge>;
  return <Badge tone="success">healthy</Badge>;
}

export function SourcesView() {
  const { data, error, isPending } = useSources();
  const isOwner = useIsOwner();
  const [adding, setAdding] = useState(false);

  return (
    <>
      <PageHeader
        title="Sources"
        description="Each source runs a durable Temporal polling workflow with adaptive intervals."
        actions={
          isOwner ? (
            <Button onClick={() => setAdding((value) => !value)}>
              <Plus aria-hidden /> {adding ? "Cancel" : "Add source"}
            </Button>
          ) : null
        }
      />
      {adding ? (
        <Card className="mb-4">
          <CardHeader>
            <CardTitle>New source</CardTitle>
          </CardHeader>
          <CardContent>
            <SourceForm onCreated={() => setAdding(false)} />
          </CardContent>
        </Card>
      ) : null}
      {error ? <ErrorState error={error} /> : null}
      {isPending ? (
        <LoadingRows />
      ) : !data?.items.length ? (
        <EmptyState title="No sources yet">{isOwner ? "Add a Greenhouse, Lever or Ashby board to begin." : null}</EmptyState>
      ) : (
        <Card>
          <Table>
            <THead>
              <TR>
                <TH>Source</TH>
                <TH>Type</TH>
                <TH>Health</TH>
                <TH>Open jobs</TH>
                <TH>Interval</TH>
                <TH>Last success</TH>
              </TR>
            </THead>
            <TBody>
              {data.items.map((source) => (
                <TR key={source.id}>
                  <TD>
                    <Link href={`/sources/${source.id}`} className="font-medium hover:underline">
                      {source.name}
                    </Link>
                    <span className="block text-xs text-muted-foreground">{source.company_domain}</span>
                  </TD>
                  <TD>
                    <Badge>{source.kind}</Badge>
                  </TD>
                  <TD>
                    <SourceHealth source={source} />
                  </TD>
                  <TD className="tabular-nums">{source.open_jobs}</TD>
                  <TD className="tabular-nums">{formatSeconds(source.poll_interval_seconds)}</TD>
                  <TD className="text-xs text-muted-foreground">{timeAgo(source.last_success_at)}</TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </Card>
      )}
    </>
  );
}
