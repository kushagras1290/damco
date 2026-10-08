/**
 * Maps an Auth.js provider + account id to the API's identity subject
 * (`<provider>:<immutable id>`). Only these providers and shapes are accepted; the API
 * re-validates the same patterns before trusting a token.
 */
export type IdentityProvider = "github" | "google" | "microsoft" | "email";

const PROVIDERS: Record<string, { provider: IdentityProvider; pattern: RegExp }> = {
  github: { provider: "github", pattern: /^[1-9][0-9]{0,19}$/ },
  google: { provider: "google", pattern: /^[A-Za-z0-9_-]{1,255}$/ },
  "microsoft-entra-id": { provider: "microsoft", pattern: /^[A-Za-z0-9_-]{1,255}$/ },
  "email-link": { provider: "email", pattern: /^[0-9a-f]{64}$/ },
};

export function isIdentityProvider(value: unknown): value is IdentityProvider {
  return value === "github" || value === "google" || value === "microsoft" || value === "email";
}

export interface Identity {
  provider: IdentityProvider;
  subject: string;
}

export function identityFor(authProvider: string, accountId: string | null | undefined): Identity | null {
  const entry = PROVIDERS[authProvider];
  if (!entry || typeof accountId !== "string" || !entry.pattern.test(accountId)) return null;
  return { provider: entry.provider, subject: accountId };
}

/** Inverse of the provider mapping (session stores the API-side provider name). */
export function authProviderOf(provider: IdentityProvider): string {
  return Object.entries(PROVIDERS).find(([, entry]) => entry.provider === provider)?.[0] ?? "";
}

export function backendSubjectOf(identity: Identity): string {
  return `${identity.provider}:${identity.subject}`;
}
