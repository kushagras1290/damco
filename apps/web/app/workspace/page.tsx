import type { Metadata } from "next";

import { WorkspaceView } from "@/features/workspace/workspace-view";

export const metadata: Metadata = { title: "Workspace" };

export default function Page() {
  return <WorkspaceView />;
}
