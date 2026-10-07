import type { DefaultSession } from "next-auth";

import type { Role } from "@/lib/roles";

declare module "next-auth" {
  interface Session {
    user: {
      login?: string;
      role: Role;
    } & DefaultSession["user"];
  }
  interface Profile {
    login?: string;
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    login?: string;
    role?: Role;
  }
}
