"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Building2, Settings } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";

import { Badge, Select } from "@/components/ui/primitives";
import { useMe } from "@/lib/api/hooks";
import { useLiveFeed } from "@/lib/stores/live-feed";
import { rememberWorkspace } from "@/lib/workspace-cookie";

const ROLE_TONE = { owner: "info", admin: "info", member: "neutral", viewer: "neutral" } as const;

/** Workspace picker + current role. Switching resets all cached data for the new tenant. */
export function WorkspaceSwitcher() {
  const { data: me } = useMe();
  const client = useQueryClient();
  const router = useRouter();

  if (!me?.workspace) return null;
  const current = me.workspace;

  function select(id: string) {
    if (id === current.id) return;
    rememberWorkspace(id);
    useLiveFeed.getState().clear();
    client.clear(); // never show one workspace's cached data under another
    router.refresh();
  }

  return (
    <div className="flex flex-col gap-1.5" data-testid="workspace-switcher">
      {me.workspaces.length > 1 ? (
        <label className="flex items-center gap-1.5 text-xs">
          <Building2 className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          <span className="sr-only">Workspace</span>
          <Select
            value={current.id}
            onChange={(event) => select(event.target.value)}
            className="h-7 min-w-0 flex-1 text-xs"
            aria-label="Workspace"
          >
            {me.workspaces.map((workspace) => (
              <option key={workspace.id} value={workspace.id}>
                {workspace.name}
              </option>
            ))}
          </Select>
        </label>
      ) : (
        <p className="flex items-center gap-1.5 truncate text-xs font-medium">
          <Building2 className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
          {current.name}
        </p>
      )}
      <div className="flex items-center justify-between gap-2">
        <Badge tone={ROLE_TONE[current.role]}>{current.role === "viewer" ? "Read-only demo" : current.role}</Badge>
        {me.authenticated && me.user_id ? (
          <Link href="/workspace" className="inline-flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <Settings className="size-3" aria-hidden /> Manage
          </Link>
        ) : null}
      </div>
    </div>
  );
}
