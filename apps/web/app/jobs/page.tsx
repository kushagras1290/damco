import type { Metadata } from "next";

import { JobsView } from "@/features/jobs/jobs-view";

export const metadata: Metadata = { title: "Jobs" };

export default function Page() {
  return <JobsView />;
}
