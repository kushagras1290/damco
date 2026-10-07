import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { JobDetailView } from "@/features/jobs/job-detail-view";

export const metadata: Metadata = { title: "Job" };

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID_RE.test(id)) notFound();
  return <JobDetailView id={id} />;
}
