import NextAuth from "next-auth";
import type { Provider } from "next-auth/providers";
import GitHub from "next-auth/providers/github";

import { log } from "@/lib/log";
import { roleForGithubId } from "@/lib/roles";
import { githubConfigured, serverEnv } from "@/lib/server-env";

const SESSION_MAX_AGE_SECONDS = 8 * 60 * 60;
const SESSION_UPDATE_AGE_SECONDS = 60 * 60;

function providers(): Provider[] {
  const env = serverEnv();
  if (!githubConfigured(env)) return [];
  return [
    GitHub({
      clientId: env.AUTH_GITHUB_ID,
      clientSecret: env.AUTH_GITHUB_SECRET,
      // Least privilege: identity only. We never need the user's email addresses.
      authorization: { params: { scope: "read:user" } },
    }),
  ];
}

export const { handlers, auth, signIn, signOut } = NextAuth(() => {
  const env = serverEnv();
  return {
    providers: providers(),
    // trustHost is derived by Auth.js from AUTH_URL / AUTH_TRUST_HOST / VERCEL - never forced on.
    session: { strategy: "jwt", maxAge: SESSION_MAX_AGE_SECONDS, updateAge: SESSION_UPDATE_AGE_SECONDS },
    pages: { error: "/auth/error" },
    callbacks: {
      // Only allowlisted owners may create a session. Everyone else browses anonymously
      // as the read-only public demo, so a session would grant them nothing.
      signIn({ account, profile }) {
        const githubId = account?.provider === "github" && profile?.id != null ? String(profile.id) : null;
        const allowed = roleForGithubId(githubId, env.OWNER_GITHUB_IDS) === "OWNER";
        if (!allowed) log("warn", "auth.sign_in_denied", { github_id: githubId, login: profile?.login ?? null });
        return allowed;
      },
      jwt({ token, account, profile }) {
        if (account?.provider === "github" && profile?.id != null) {
          token.githubId = String(profile.id);
          token.login = typeof profile.login === "string" ? profile.login : undefined;
        }
        // Re-evaluated on every request: removing an id from OWNER_GITHUB_IDS revokes
        // owner access on the next request instead of when the session expires.
        token.role = roleForGithubId(token.githubId, env.OWNER_GITHUB_IDS);
        return token;
      },
      session({ session, token }) {
        // Narrow explicitly: token contents come from a (signed) cookie.
        session.user.githubId = typeof token.githubId === "string" ? token.githubId : undefined;
        session.user.login = typeof token.login === "string" ? token.login : undefined;
        session.user.role = token.role === "OWNER" ? "OWNER" : "PUBLIC_DEMO";
        return session;
      },
    },
    events: {
      signIn({ profile }) {
        log("info", "auth.sign_in", { github_id: profile?.id != null ? String(profile.id) : null, login: profile?.login ?? null });
      },
      signOut() {
        log("info", "auth.sign_out");
      },
    },
  };
});
