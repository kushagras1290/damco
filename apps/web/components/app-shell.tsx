import { LogIn, LogOut, Zap } from "lucide-react";
import type { ReactNode } from "react";

import { auth, signIn, signOut } from "@/auth";
import { LiveStatusBadge } from "@/components/live-events";
import { NavLinks } from "@/components/nav-links";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/primitives";
import { githubConfigured } from "@/lib/server-env";

export async function AppShell({ children }: { children: ReactNode }) {
  const session = await auth();
  const user = session?.user;

  async function login() {
    "use server";
    await signIn("github");
  }

  async function logout() {
    "use server";
    await signOut({ redirectTo: "/dashboard" });
  }

  return (
    <div className="flex min-h-dvh flex-col md:flex-row">
      <aside className="border-b bg-card md:sticky md:top-0 md:h-dvh md:w-56 md:shrink-0 md:border-r md:border-b-0">
        <div className="flex items-center justify-between gap-2 p-4 md:flex-col md:items-stretch">
          <div className="flex items-center gap-2 font-semibold">
            <Zap className="size-5 text-primary" aria-hidden />
            JobPulse
            <span className="ml-auto md:ml-2">
              <LiveStatusBadge />
            </span>
          </div>
          <div className="flex items-center gap-2 md:mt-2 md:flex-col md:items-stretch">
            {user ? (
              <>
                <div className="flex items-center gap-2 text-xs">
                  <span className="truncate">{user.login ?? user.name}</span>
                  <Badge tone={user.role === "OWNER" ? "info" : "neutral"}>{user.role === "OWNER" ? "Owner" : "Demo"}</Badge>
                </div>
                <form action={logout}>
                  <Button variant="ghost" size="sm" className="w-full justify-start" type="submit">
                    <LogOut aria-hidden /> Sign out
                  </Button>
                </form>
              </>
            ) : githubConfigured() ? (
              <form action={login}>
                <Button variant="outline" size="sm" className="w-full" type="submit">
                  <LogIn aria-hidden /> Sign in with GitHub
                </Button>
              </form>
            ) : (
              <Badge>Public demo (read-only)</Badge>
            )}
          </div>
        </div>
        <div className="px-2 pb-2 md:pb-4">
          <NavLinks />
        </div>
      </aside>
      <main className="min-w-0 flex-1 p-4 md:p-6">{children}</main>
    </div>
  );
}
