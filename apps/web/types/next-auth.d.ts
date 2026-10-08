import type { DefaultSession } from "next-auth";

import type { IdentityProvider } from "@/lib/identity";

declare module "next-auth" {
  interface Session {
    user: {
      provider?: IdentityProvider;
      subject?: string;
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
    provider?: string;
    subject?: string;
    login?: string;
    /** Legacy (pre multi-provider) sessions. */
    githubId?: string;
  }
}
