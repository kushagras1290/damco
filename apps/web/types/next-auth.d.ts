import type { DefaultSession } from "next-auth";

declare module "next-auth" {
  interface Session {
    user: {
      githubId?: string;
      login?: string;
    } & DefaultSession["user"];
  }
  interface Profile {
    id?: number | string;
    login?: string;
  }
}

// Auth.js v5: the JWT interface is declared in @auth/core (re-exported by next-auth/jwt).
declare module "@auth/core/jwt" {
  interface JWT {
    githubId?: string;
    login?: string;
  }
}
