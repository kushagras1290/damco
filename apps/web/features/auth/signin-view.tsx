"use client";

import { Mail } from "lucide-react";
import { signIn } from "next-auth/react";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Label } from "@/components/ui/primitives";
import { api } from "@/lib/api/client";

export interface SignInOption {
  id: "github" | "google" | "microsoft-entra-id";
  label: string;
}

/** OAuth buttons for configured providers plus a passwordless email link. */
export function SignInView({ options, callbackUrl }: { options: SignInOption[]; callbackUrl: string }) {
  const [email, setEmail] = useState("");
  const [state, setState] = useState<"idle" | "sending" | "sent">("idle");
  const [error, setError] = useState<unknown>(null);

  async function sendLink(event: FormEvent) {
    event.preventDefault();
    setState("sending");
    setError(null);
    try {
      await api.post("/auth/email/start", { email: email.trim() });
      setState("sent");
    } catch (caught) {
      setError(caught);
      setState("idle");
    }
  }

  return (
    <Card className="mx-auto max-w-sm">
      <CardHeader>
        <CardTitle>Sign in to JobPulse</CardTitle>
        <CardDescription>New here? Signing in creates your account.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {options.map((option) => (
          <Button key={option.id} variant="outline" className="w-full" onClick={() => void signIn(option.id, { redirectTo: callbackUrl })}>
            Continue with {option.label}
          </Button>
        ))}
        {options.length ? <p className="text-center text-xs text-muted-foreground">or</p> : null}
        {state === "sent" ? (
          <p className="rounded-md border border-success/30 bg-success/10 p-3 text-sm" role="status">
            Check your inbox: we sent a sign-in link that works once and expires in 15 minutes.
          </p>
        ) : (
          <form onSubmit={sendLink} className="space-y-2">
            <Label htmlFor="signin-email">Email</Label>
            <Input id="signin-email" type="email" required autoComplete="email" value={email} onChange={(e) => setEmail(e.target.value)} />
            <Button type="submit" className="w-full" disabled={state === "sending"}>
              <Mail aria-hidden /> Email me a sign-in link
            </Button>
          </form>
        )}
        {error ? <ErrorState error={error} /> : null}
      </CardContent>
    </Card>
  );
}

/** Landing page for the emailed link: exchanges the one-time token for a session. */
export function EmailLinkSignIn({ token }: { token: string }) {
  const router = useRouter();
  const [failed, setFailed] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    if (started.current) return; // single use: never submit the token twice
    started.current = true;
    void signIn("email-link", { token, redirect: false }).then((result) => {
      if (result?.error) setFailed(true);
      else {
        router.replace("/dashboard");
        router.refresh();
      }
    });
  }, [token, router]);

  return failed ? (
    <ErrorState error={new Error("This sign-in link is invalid, expired or already used. Request a new one.")} />
  ) : (
    <p className="text-sm text-muted-foreground">Signing you in…</p>
  );
}
