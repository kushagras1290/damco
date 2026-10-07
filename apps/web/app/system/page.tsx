import type { Metadata } from "next";

import { SystemView } from "@/features/activity/activity-views";

export const metadata: Metadata = { title: "System" };

export default function Page() {
  return <SystemView />;
}
