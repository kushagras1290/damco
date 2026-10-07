"use client";

import {
  Activity,
  Briefcase,
  ClipboardCheck,
  Gauge,
  type LucideIcon,
  Radar,
  Scale,
  Server,
  UserRound,
  Users,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const LINKS: { href: string; label: string; icon: LucideIcon }[] = [
  { href: "/dashboard", label: "Dashboard", icon: Gauge },
  { href: "/jobs", label: "Jobs", icon: Briefcase },
  { href: "/sources", label: "Sources", icon: Radar },
  { href: "/applications", label: "Applications", icon: ClipboardCheck },
  { href: "/decisions", label: "Decisions", icon: Scale },
  { href: "/runs", label: "Runs", icon: Activity },
  { href: "/profile", label: "Profile", icon: UserRound },
  { href: "/workspace", label: "Workspace", icon: Users },
  { href: "/system", label: "System", icon: Server },
];

export function NavLinks() {
  const pathname = usePathname();
  return (
    <nav aria-label="Main" className="flex gap-1 overflow-x-auto md:flex-col">
      {LINKS.map(({ href, label, icon: Icon }) => {
        const active = pathname === href || pathname.startsWith(`${href}/`);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex items-center gap-2 rounded-md px-3 py-2 text-sm whitespace-nowrap transition-colors",
              active ? "bg-accent font-medium text-accent-foreground" : "text-muted-foreground hover:bg-muted",
            )}
          >
            <Icon className="size-4" aria-hidden />
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
