import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { SourceDetailView } from "@/features/sources/source-detail-view";

export const metadata: Metadata = { title: "Source" };

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  if (!UUID_RE.test(id)) notFound();
  return <SourceDetailView id={id} />;
}
