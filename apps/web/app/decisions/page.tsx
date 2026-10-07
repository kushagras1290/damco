import type { Metadata } from "next";

import { DecisionsView } from "@/features/activity/activity-views";

export const metadata: Metadata = { title: "Decisions" };

export default function Page() {
  return <DecisionsView />;
}
