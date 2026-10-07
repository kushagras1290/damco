import type { Metadata } from "next";
import Link from "next/link";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/primitives";

export const metadata: Metadata = { title: "Sign-in" };

// Auth.js error codes: https://authjs.dev/reference/core/errors
const MESSAGES: Record<string, string> = {
  AccessDenied:
    "This GitHub account is not an owner of this JobPulse instance. You can keep browsing the public demo without signing in.",
  Configuration: "Sign-in is temporarily unavailable. Please try again later.",
  Verification: "The sign-in link is no longer valid. Please try again.",
};

export default async function AuthErrorPage({ searchParams }: { searchParams: Promise<{ error?: string }> }) {
  const { error } = await searchParams;
  const message = (error && MESSAGES[error]) ?? "Sign-in failed. Please try again.";
  return (
    <div className="mx-auto mt-16 max-w-md">
      <Card>
        <CardHeader>
          <CardTitle>Couldn&apos;t sign you in</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">{message}</p>
          <Button asChild>
            <Link href="/dashboard">Continue to the public demo</Link>
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
