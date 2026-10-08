import NextAuth, { CredentialsSignin } from "next-auth";
import type { Provider } from "next-auth/providers";
import Credentials from "next-auth/providers/credentials";
import GitHub from "next-auth/providers/github";
import Google from "next-auth/providers/google";
import MicrosoftEntraID from "next-auth/providers/microsoft-entra-id";

import { identityFor, isIdentityProvider } from "@/lib/identity";
import { log } from "@/lib/log";
import { type ServerEnv, githubConfigured, googleConfigured, microsoftConfigured, serverEnv } from "@/lib/server-env";

const SESSION_MAX_AGE_SECONDS = 8 * 60 * 60;
const SESSION_UPDATE_AGE_SECONDS = 60 * 60;
const VERIFY_TIMEOUT_MS = 10_000;
const TOKEN_RE = /^[A-Za-z0-9_-]{20,200}$/;

class InvalidLinkError extends CredentialsSignin {
  override code = "invalid_link";
}

/** Exchange a single-use magic-link token with the API for the verified address. */
async function verifyEmailLink(env: ServerEnv, token: unknown): Promise<{ id: string; email: string } | null> {
  if (typeof token !== "string" || !TOKEN_RE.test(token)) throw new InvalidLinkError();
  const response = await fetch(new URL("/api/v1/auth/email/verify", env.API_BASE_URL), {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ token }),
    cache: "no-store",
    redirect: "error",
    signal: AbortSignal.timeout(VERIFY_TIMEOUT_MS),
  });
  if (!response.ok) throw new InvalidLinkError();
  const body = (await response.json()) as { subject?: unknown; email?: unknown };
  if (typeof body.subject !== "string" || !/^[0-9a-f]{64}$/.test(body.subject) || typeof body.email !== "string") {
    throw new InvalidLinkError();
  }
  return { id: body.subject, email: body.email };
}

function providers(env: ServerEnv): Provider[] {
  const list: Provider[] = [];
  if (githubConfigured(env)) {
    // Least privilege: identity only. We never need the user's email addresses.
    list.push(GitHub({ clientId: env.AUTH_GITHUB_ID, clientSecret: env.AUTH_GITHUB_SECRET, authorization: { params: { scope: "read:user" } } }));
  }
  if (googleConfigured(env)) {
    list.push(Google({ clientId: env.AUTH_GOOGLE_ID, clientSecret: env.AUTH_GOOGLE_SECRET }));
  }
  if (microsoftConfigured(env)) {
    list.push(
      MicrosoftEntraID({
        clientId: env.AUTH_MICROSOFT_ENTRA_ID_ID,
        clientSecret: env.AUTH_MICROSOFT_ENTRA_ID_SECRET,
        issuer: env.AUTH_MICROSOFT_ENTRA_ID_ISSUER,
      }),
    );
  }
  list.push(
    Credentials({
      id: "email-link",
      name: "Email link",
      credentials: { token: {} },
      authorize: (credentials) => verifyEmailLink(env, credentials?.token),
    }),
  );
  return list;
}

export const { handlers, auth, signIn, signOut } = NextAuth(() => {
  const env = serverEnv();
  return {
    providers: providers(env),
    // trustHost is derived by Auth.js from AUTH_URL / AUTH_TRUST_HOST / VERCEL - never forced on.
    session: { strategy: "jwt", maxAge: SESSION_MAX_AGE_SECONDS, updateAge: SESSION_UPDATE_AGE_SECONDS },
    pages: { signIn: "/signin", error: "/auth/error" },
    callbacks: {
      // The session only records WHO signed in: an immutable provider subject. Whether they may
      // sign up, and what they can do in each workspace, is decided by the API on every request.
      jwt({ token, account, profile, user }) {
        if (account) {
          const identity = identityFor(account.provider, account.providerAccountId ?? user?.id);
          if (!identity) throw new Error("unsupported sign-in provider");
          token.provider = identity.provider;
          token.subject = identity.subject;
          token.login = typeof profile?.login === "string" ? profile.login : undefined;
        } else if (!token.provider && typeof token.githubId === "string") {
          // Sessions issued before multi-provider sign-in.
          token.provider = "github";
          token.subject = token.githubId;
        }
        return token;
      },
      session({ session, token }) {
        // Narrow explicitly: token contents come from a (signed) cookie.
        session.user.provider = isIdentityProvider(token.provider) ? token.provider : undefined;
        session.user.subject = typeof token.subject === "string" ? token.subject : undefined;
        session.user.login = typeof token.login === "string" ? token.login : undefined;
        return session;
      },
    },
    events: {
      signIn({ account }) {
        log("info", "auth.sign_in", { provider: account?.provider ?? null });
      },
      signOut() {
        log("info", "auth.sign_out");
      },
    },
  };
});
