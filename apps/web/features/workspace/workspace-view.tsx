"use client";

import { useQueryClient } from "@tanstack/react-query";
import { Check, Copy, Link2, Plus, Trash2, UserMinus } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { EmptyState, ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Label, Select } from "@/components/ui/primitives";
import { TBody, TD, TH, THead, TR, Table } from "@/components/ui/table";
import {
  useAcceptInvitation,
  useCan,
  useChangeMemberRole,
  useCreateWorkspace,
  useInvitations,
  useInvite,
  useMe,
  useMembers,
  useRemoveMember,
  useRenameWorkspace,
  useRevokeInvitation,
} from "@/lib/api/hooks";
import type { InvitableRole, Member } from "@/lib/api/types";
import { useLiveFeed } from "@/lib/stores/live-feed";
import { timeAgo } from "@/lib/utils";
import { rememberWorkspace } from "@/lib/workspace-cookie";

const ROLES: InvitableRole[] = ["member", "admin", "owner"];
const ROLE_HELP: Record<InvitableRole, string> = {
  member: "Profile, applications, re-evaluations",
  admin: "+ sources, members, invitations",
  owner: "+ workspace settings and ownership",
};

function useSwitchTo() {
  const client = useQueryClient();
  const router = useRouter();
  return (workspaceId: string, path = "/dashboard") => {
    rememberWorkspace(workspaceId);
    useLiveFeed.getState().clear();
    client.clear();
    router.push(path);
    router.refresh();
  };
}

function RenameCard({ name }: { name: string }) {
  const rename = useRenameWorkspace();
  const [value, setValue] = useState(name);
  function submit(event: FormEvent) {
    event.preventDefault();
    if (value.trim() && value !== name) rename.mutate(value.trim());
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>Workspace name</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex gap-2">
          <Input value={value} onChange={(event) => setValue(event.target.value)} maxLength={200} aria-label="Workspace name" />
          <Button type="submit" disabled={rename.isPending || !value.trim() || value === name}>
            Save
          </Button>
        </form>
        {rename.error ? <ErrorState error={rename.error} /> : null}
      </CardContent>
    </Card>
  );
}

function MemberRow({ member, canManage, isOwner }: { member: Member; canManage: boolean; isOwner: boolean }) {
  const changeRole = useChangeMemberRole();
  const remove = useRemoveMember();
  // Owners manage everyone; admins manage members and admins but never owners.
  const editable = canManage && !member.you && (isOwner || member.role !== "owner");
  const options = isOwner ? ROLES : ROLES.filter((role) => role !== "owner");
  return (
    <TR>
      <TD>
        <span className="font-medium">{member.display_name || "Unnamed user"}</span>
        {member.you ? <Badge className="ml-2">you</Badge> : null}
      </TD>
      <TD>
        {editable ? (
          <Select
            value={member.role}
            disabled={changeRole.isPending}
            onChange={(event) => changeRole.mutate({ userId: member.user_id, role: event.target.value as InvitableRole })}
            aria-label={`Role for ${member.display_name}`}
            className="h-8 w-28"
          >
            {options.map((role) => (
              <option key={role} value={role}>
                {role}
              </option>
            ))}
          </Select>
        ) : (
          <Badge tone={member.role === "member" ? "neutral" : "info"}>{member.role}</Badge>
        )}
        {changeRole.error ? <ErrorState error={changeRole.error} /> : null}
      </TD>
      <TD className="text-xs text-muted-foreground">{timeAgo(member.joined_at)}</TD>
      <TD className="text-right">
        {editable || member.you ? (
          <Button
            variant="ghost"
            size="sm"
            disabled={remove.isPending}
            onClick={() => remove.mutate(member.user_id)}
            aria-label={member.you ? "Leave workspace" : `Remove ${member.display_name}`}
          >
            <UserMinus aria-hidden /> {member.you ? "Leave" : "Remove"}
          </Button>
        ) : null}
        {remove.error ? <ErrorState error={remove.error} /> : null}
      </TD>
    </TR>
  );
}

function InviteCard({ isOwner }: { isOwner: boolean }) {
  const invite = useInvite();
  const revoke = useRevokeInvitation();
  const invitations = useInvitations(true);
  const [role, setRole] = useState<InvitableRole>("member");
  const [email, setEmail] = useState("");
  const [copied, setCopied] = useState(false);

  function submit(event: FormEvent) {
    event.preventDefault();
    setCopied(false);
    invite.mutate({ role, ...(email.trim() ? { email: email.trim() } : {}) });
  }

  async function copy(url: string) {
    await navigator.clipboard.writeText(url);
    setCopied(true);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Invite people</CardTitle>
        <CardDescription>Invitation links work once and expire after 7 days.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <form onSubmit={submit} className="grid gap-3 sm:grid-cols-[1fr_9rem_auto] sm:items-end">
          <div className="grid gap-1">
            <Label htmlFor="invite-email">Email (optional, for your records)</Label>
            <Input id="invite-email" type="email" value={email} onChange={(event) => setEmail(event.target.value)} />
          </div>
          <div className="grid gap-1">
            <Label htmlFor="invite-role">Role</Label>
            <Select id="invite-role" value={role} onChange={(event) => setRole(event.target.value as InvitableRole)}>
              {(isOwner ? ROLES : ROLES.filter((r) => r !== "owner")).map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </Select>
          </div>
          <Button type="submit" disabled={invite.isPending}>
            <Link2 aria-hidden /> Create link
          </Button>
        </form>
        <p className="text-xs text-muted-foreground">{ROLE_HELP[role]}</p>
        {invite.error ? <ErrorState error={invite.error} /> : null}
        {invite.data ? (
          <div className="rounded-md border border-primary/30 bg-primary/5 p-3 text-sm" data-testid="invite-link">
            <p className="mb-2 font-medium">Share this link - it is shown only once:</p>
            <div className="flex gap-2">
              <Input readOnly value={invite.data.invite_url} aria-label="Invitation link" className="font-mono text-xs" />
              <Button type="button" variant="outline" onClick={() => void copy(invite.data.invite_url)}>
                {copied ? <Check aria-hidden /> : <Copy aria-hidden />} {copied ? "Copied" : "Copy"}
              </Button>
            </div>
          </div>
        ) : null}

        <div>
          <h4 className="mb-2 text-sm font-medium">Pending invitations</h4>
          {invitations.isPending ? (
            <LoadingRows rows={2} />
          ) : invitations.data?.length ? (
            <ul className="divide-y text-sm">
              {invitations.data.map((item) => (
                <li key={item.id} className="flex items-center justify-between gap-2 py-2">
                  <span>
                    {item.email ?? "Link invitation"} · <Badge>{item.role}</Badge>
                    <span className="ml-2 text-xs text-muted-foreground">expires {timeAgo(item.expires_at)}</span>
                  </span>
                  <Button variant="ghost" size="sm" onClick={() => revoke.mutate(item.id)} disabled={revoke.isPending}>
                    <Trash2 aria-hidden /> Revoke
                  </Button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">No pending invitations.</p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function CreateWorkspaceCard() {
  const create = useCreateWorkspace();
  const switchTo = useSwitchTo();
  const [name, setName] = useState("");
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!name.trim()) return;
    create.mutate(name.trim(), { onSuccess: (workspace) => switchTo(workspace.id, "/workspace") });
  }
  return (
    <Card>
      <CardHeader>
        <CardTitle>New team workspace</CardTitle>
        <CardDescription>For recruiting teams: shared sources, several candidate profiles, members.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={submit} className="flex gap-2">
          <Input placeholder="Acme Talent" value={name} onChange={(event) => setName(event.target.value)} maxLength={200} aria-label="New workspace name" />
          <Button type="submit" disabled={create.isPending || !name.trim()}>
            <Plus aria-hidden /> Create
          </Button>
        </form>
        {create.error ? <ErrorState error={create.error} /> : null}
      </CardContent>
    </Card>
  );
}

export function WorkspaceView() {
  const { data: me, error, isPending } = useMe();
  const canSeeMembers = useCan("member");
  const isAdmin = useCan("admin");
  const isOwner = useCan("owner");
  const members = useMembers(canSeeMembers);

  if (isPending) return <LoadingRows rows={6} />;
  if (error) return <ErrorState error={error} />;
  if (!me.authenticated || !me.user_id) {
    return (
      <>
        <PageHeader title="Workspace" description="Sign in to create a workspace or join one by invitation." />
        <EmptyState title="You are browsing the public demo" />
      </>
    );
  }

  return (
    <>
      <PageHeader
        title={me.workspace?.name ?? "Workspace"}
        description={me.workspace ? `Plan: ${me.workspace.plan} · your role: ${me.workspace.role}` : "Join or create a workspace."}
      />
      <div className="grid gap-4 lg:grid-cols-2">
        {canSeeMembers ? (
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Members</CardTitle>
            </CardHeader>
            <CardContent>
              {members.isPending ? (
                <LoadingRows rows={3} />
              ) : members.error ? (
                <ErrorState error={members.error} />
              ) : (
                <Table>
                  <THead>
                    <TR>
                      <TH>Name</TH>
                      <TH>Role</TH>
                      <TH>Joined</TH>
                      <TH className="text-right">
                        <span className="sr-only">Actions</span>
                      </TH>
                    </TR>
                  </THead>
                  <TBody>
                    {members.data.map((member) => (
                      <MemberRow key={member.user_id} member={member} canManage={isAdmin} isOwner={isOwner} />
                    ))}
                  </TBody>
                </Table>
              )}
            </CardContent>
          </Card>
        ) : null}
        {isAdmin ? <InviteCard isOwner={isOwner} /> : null}
        <div className="grid content-start gap-4">
          {isOwner && me.workspace ? <RenameCard name={me.workspace.name} /> : null}
          <CreateWorkspaceCard />
        </div>
      </div>
    </>
  );
}

export function AcceptInvitation({ token, signedIn }: { token: string; signedIn: boolean }) {
  const accept = useAcceptInvitation();
  const switchTo = useSwitchTo();
  if (!signedIn) {
    return <EmptyState title="Sign in to accept this invitation">Use the sign-in button, then open this link again.</EmptyState>;
  }
  return (
    <Card className="mx-auto max-w-md">
      <CardHeader>
        <CardTitle>Join workspace</CardTitle>
        <CardDescription>You were invited to a JobPulse workspace.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <Button
          className="w-full"
          disabled={accept.isPending}
          onClick={() => accept.mutate(token, { onSuccess: ({ workspace_id }) => switchTo(workspace_id) })}
        >
          Accept invitation
        </Button>
        {accept.error ? <ErrorState error={accept.error} /> : null}
      </CardContent>
    </Card>
  );
}
