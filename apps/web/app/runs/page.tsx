import type { Metadata } from "next";

import { RunsView } from "@/features/activity/activity-views";

export const metadata: Metadata = { title: "Runs" };

export default function Page() {
  return <RunsView />;
}
