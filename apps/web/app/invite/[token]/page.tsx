import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { auth } from "@/auth";
import { PageHeader } from "@/components/common";
import { AcceptInvitation } from "@/features/workspace/workspace-view";

export const metadata: Metadata = { title: "Invitation", referrer: "no-referrer" };

const TOKEN_RE = /^[A-Za-z0-9_-]{20,200}$/;

type Props = { params: Promise<{ token: string }> };

export default async function Page({ params }: Props) {
  const { token } = await params;
  if (!TOKEN_RE.test(token)) notFound();
  const session = await auth();
  return (
    <>
      <PageHeader title="Invitation" description="Accept to join the workspace with the role you were invited to." />
      <AcceptInvitation token={token} signedIn={Boolean(session?.user?.subject)} />
    </>
  );
}
