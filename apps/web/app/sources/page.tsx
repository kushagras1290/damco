import type { Metadata } from "next";

import { SourcesView } from "@/features/sources/sources-view";

export const metadata: Metadata = { title: "Sources" };

export default function Page() {
  return <SourcesView />;
}
