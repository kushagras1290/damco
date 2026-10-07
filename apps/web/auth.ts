import NextAuth from "next-auth";
import type { Provider } from "next-auth/providers";
import GitHub from "next-auth/providers/github";

import { roleForLogin } from "@/lib/roles";
import { githubConfigured, serverEnv } from "@/lib/server-env";

const SESSION_MAX_AGE_SECONDS = 8 * 60 * 60;

function providers(): Provider[] {
  const env = serverEnv();
  return githubConfigured(env)
    ? [GitHub({ clientId: env.AUTH_GITHUB_ID, clientSecret: env.AUTH_GITHUB_SECRET })]
    : [];
}

export const { handlers, auth, signIn, signOut } = NextAuth(() => ({
  providers: providers(),
  session: { strategy: "jwt", maxAge: SESSION_MAX_AGE_SECONDS },
  trustHost: true,
  callbacks: {
    jwt({ token, profile }) {
      if (profile && typeof profile.login === "string") {
        token.login = profile.login;
        token.role = roleForLogin(profile.login, serverEnv().OWNER_GITHUB_LOGINS);
      }
      return token;
    },
    session({ session, token }) {
      session.user.login = typeof token.login === "string" ? token.login : undefined;
      session.user.role = token.role === "OWNER" ? "OWNER" : "PUBLIC_DEMO";
      return session;
    },
  },
}));
