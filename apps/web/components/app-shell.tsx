import { LogIn, LogOut, Zap } from "lucide-react";
import type { ReactNode } from "react";

import { auth, signOut } from "@/auth";
import { LiveStatusBadge } from "@/components/live-events";
import { NavLinks } from "@/components/nav-links";
import { WorkspaceSwitcher } from "@/components/workspace-switcher";
import { Button } from "@/components/ui/button";
import Link from "next/link";

export async function AppShell({ children }: { children: ReactNode }) {
  const session = await auth();
  const user = session?.user;

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
                <span className="truncate text-xs text-muted-foreground">Signed in as {user.login ?? user.name}</span>
                <WorkspaceSwitcher />
                <form action={logout}>
                  <Button variant="ghost" size="sm" className="w-full justify-start" type="submit">
                    <LogOut aria-hidden /> Sign out
                  </Button>
                </form>
              </>
            ) : (
              <Link
                href="/signin"
                className="inline-flex h-8 w-full items-center justify-center gap-2 rounded-md border px-3 text-sm font-medium hover:bg-muted"
              >
                <LogIn aria-hidden className="size-4" /> Sign in
              </Link>
            )}
            {user ? null : <WorkspaceSwitcher />}
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
