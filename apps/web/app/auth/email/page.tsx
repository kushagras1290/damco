import type { Metadata } from "next";
import { notFound } from "next/navigation";

import { PageHeader } from "@/components/common";
import { EmailLinkSignIn } from "@/features/auth/signin-view";

export const metadata: Metadata = { title: "Signing in", referrer: "no-referrer" };

const TOKEN_RE = /^[A-Za-z0-9_-]{20,200}$/;

type Props = { searchParams: Promise<{ token?: string }> };

export default async function Page({ searchParams }: Props) {
  const { token } = await searchParams;
  if (!token || !TOKEN_RE.test(token)) notFound();
  return (
    <>
      <PageHeader title="Signing in" />
      <EmailLinkSignIn token={token} />
    </>
  );
}
