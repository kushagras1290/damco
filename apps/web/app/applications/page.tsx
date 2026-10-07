import type { Metadata } from "next";

import { ApplicationsView } from "@/features/activity/activity-views";

export const metadata: Metadata = { title: "Applications" };

export default function Page() {
  return <ApplicationsView />;
}
