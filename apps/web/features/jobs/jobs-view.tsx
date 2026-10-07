"use client";

import { createColumnHelper, tableFeatures, useTable } from "@tanstack/react-table";
import { ChevronLeft, ChevronRight, RotateCcw } from "lucide-react";
import Link from "next/link";
import { useDeferredValue, useMemo } from "react";

import { EligibilityBadge, EmptyState, ErrorState, LoadingRows, PageHeader, ScorePill } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, Input, Label, Select } from "@/components/ui/primitives";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import { useJobs } from "@/lib/api/hooks";
import type { JobSummary } from "@/lib/api/types";
import { PAGE_SIZE, type JobSort, useJobFilters } from "@/lib/stores/job-filters";
import { timeAgo } from "@/lib/utils";

const features = tableFeatures({});
const helper = createColumnHelper<typeof features, JobSummary>();
const columns = helper.columns([
  helper.accessor("title", {
    header: "Role",
    cell: (info) => {
      const job = info.row.original;
      return (
        <Link href={`/jobs/${job.id}`} className="block min-w-48 hover:underline">
          <span className="font-medium">{job.title}</span>
          <span className="block text-xs text-muted-foreground">
            {job.company} · {job.source_kind}
          </span>
        </Link>
      );
    },
  }),
  helper.accessor("location", { header: "Location", cell: (info) => info.getValue() ?? "—" }),
  helper.accessor("remote_policy", { header: "Work model", cell: (info) => <Badge>{info.getValue()}</Badge> }),
  helper.accessor("eligibility_status", {
    header: "Eligibility",
    cell: (info) => <EligibilityBadge status={info.getValue()} />,
  }),
  helper.accessor("match_score", { header: "Score", cell: (info) => <ScorePill score={info.getValue()} /> }),
  helper.accessor("published_at", {
    header: "Published",
    cell: (info) => <span className="whitespace-nowrap text-xs text-muted-foreground">{timeAgo(info.getValue())}</span>,
  }),
]);

export function JobsView() {
  const filters = useJobFilters();
  const deferredQuery = useDeferredValue(filters.query);
  const params = useMemo(
    () => ({
      q: deferredQuery.trim().length >= 2 ? deferredQuery.trim() : undefined,
      eligibility: filters.eligibility || undefined,
      remote_policy: filters.remotePolicy || undefined,
      min_score: filters.minScore > 0 ? filters.minScore / 100 : undefined,
      sort: filters.sort,
      limit: PAGE_SIZE,
      offset: filters.page * PAGE_SIZE,
    }),
    [deferredQuery, filters.eligibility, filters.remotePolicy, filters.minScore, filters.sort, filters.page],
  );
  const { data, error, isPending, isFetching } = useJobs(params);
  const rows = useMemo(() => data?.items ?? [], [data]);
  const table = useTable({ features, columns, data: rows });
  const total = data?.total ?? 0;
  const lastPage = Math.max(0, Math.ceil(total / PAGE_SIZE) - 1);

  return (
    <>
      <PageHeader title="Jobs" description="Full-text and fuzzy search across every discovered posting." />
      <Card className="mb-4 grid gap-3 p-4 sm:grid-cols-2 lg:grid-cols-6">
        <div className="space-y-1 lg:col-span-2">
          <Label htmlFor="q">Search</Label>
          <Input
            id="q"
            placeholder="Python, RAG, Agentic AI…"
            value={filters.query}
            onChange={(event) => filters.set({ query: event.target.value })}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="eligibility">Eligibility</Label>
          <Select
            id="eligibility"
            value={filters.eligibility}
            onChange={(event) => filters.set({ eligibility: event.target.value as typeof filters.eligibility })}
          >
            <option value="">All</option>
            <option value="eligible">Eligible</option>
            <option value="ineligible">Ineligible</option>
            <option value="pending">Pending</option>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="remote">Work model</Label>
          <Select
            id="remote"
            value={filters.remotePolicy}
            onChange={(event) => filters.set({ remotePolicy: event.target.value as typeof filters.remotePolicy })}
          >
            <option value="">Any</option>
            <option value="remote">Remote</option>
            <option value="hybrid">Hybrid</option>
            <option value="onsite">On-site</option>
            <option value="unknown">Unknown</option>
          </Select>
        </div>
        <div className="space-y-1">
          <Label htmlFor="min-score">Min score: {filters.minScore}%</Label>
          <Input
            id="min-score"
            type="range"
            min={0}
            max={100}
            step={5}
            className="h-9 px-0 shadow-none"
            value={filters.minScore}
            onChange={(event) => filters.set({ minScore: Number(event.target.value) })}
          />
        </div>
        <div className="space-y-1">
          <Label htmlFor="sort">Sort</Label>
          <div className="flex gap-2">
            <Select id="sort" value={filters.sort} onChange={(event) => filters.set({ sort: event.target.value as JobSort })}>
              <option value="score">Best match</option>
              <option value="published">Newest posted</option>
              <option value="discovered">Recently discovered</option>
            </Select>
            <Button variant="ghost" size="icon" aria-label="Reset filters" onClick={filters.reset}>
              <RotateCcw />
            </Button>
          </div>
        </div>
      </Card>

      {error ? <ErrorState error={error} /> : null}
      {isPending ? (
        <LoadingRows rows={8} />
      ) : rows.length === 0 ? (
        <EmptyState title="No jobs match these filters" />
      ) : (
        <Card aria-busy={isFetching}>
          <Table>
            <THead>
              {table.getHeaderGroups().map((group) => (
                <TR key={group.id}>
                  {group.headers.map((header) => (
                    <TH key={header.id}>{header.isPlaceholder ? null : <table.FlexRender header={header} />}</TH>
                  ))}
                </TR>
              ))}
            </THead>
            <TBody>
              {table.getRowModel().rows.map((row) => (
                <TR key={row.id} className={row.original.closed ? "opacity-60" : undefined}>
                  {row.getAllCells().map((cell) => (
                    <TD key={cell.id}>
                      <table.FlexRender cell={cell} />
                    </TD>
                  ))}
                </TR>
              ))}
            </TBody>
          </Table>
          <div className="flex items-center justify-between border-t p-3 text-xs text-muted-foreground">
            <span>
              {filters.page * PAGE_SIZE + 1}–{Math.min(total, (filters.page + 1) * PAGE_SIZE)} of {total}
            </span>
            <div className="flex gap-1">
              <Button
                variant="outline"
                size="icon"
                aria-label="Previous page"
                disabled={filters.page === 0}
                onClick={() => filters.set({ page: filters.page - 1 })}
              >
                <ChevronLeft />
              </Button>
              <Button
                variant="outline"
                size="icon"
                aria-label="Next page"
                disabled={filters.page >= lastPage}
                onClick={() => filters.set({ page: filters.page + 1 })}
              >
                <ChevronRight />
              </Button>
            </div>
          </div>
        </Card>
      )}
    </>
  );
}
