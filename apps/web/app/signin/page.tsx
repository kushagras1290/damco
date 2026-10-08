import type { Metadata } from "next";

import { PageHeader } from "@/components/common";
import { type SignInOption, SignInView } from "@/features/auth/signin-view";
import { githubConfigured, googleConfigured, microsoftConfigured, serverEnv } from "@/lib/server-env";

export const metadata: Metadata = { title: "Sign in" };

type Props = { searchParams: Promise<{ callbackUrl?: string }> };

/** Same-origin relative paths only: never redirect to an attacker-supplied absolute URL. */
function safeCallback(value: string | undefined): string {
  return value && value.startsWith("/") && !value.startsWith("//") ? value : "/dashboard";
}

export default async function Page({ searchParams }: Props) {
  const env = serverEnv();
  const options: SignInOption[] = [
    ...(githubConfigured(env) ? [{ id: "github" as const, label: "GitHub" }] : []),
    ...(googleConfigured(env) ? [{ id: "google" as const, label: "Google" }] : []),
    ...(microsoftConfigured(env) ? [{ id: "microsoft-entra-id" as const, label: "Microsoft" }] : []),
  ];
  const { callbackUrl } = await searchParams;
  return (
    <>
      <PageHeader title="Sign in" description="GitHub, Google, Microsoft or a one-time email link." />
      <SignInView options={options} callbackUrl={safeCallback(callbackUrl)} />
    </>
  );
}
