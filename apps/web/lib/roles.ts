export type Role = "PUBLIC_DEMO" | "OWNER";

/**
 * Owner role is granted only to allowlisted, immutable numeric GitHub user ids.
 * Logins are never used: they can be renamed and then re-registered by someone else.
 * This drives the UI only; the API independently re-derives the role from its own list.
 */
export function roleForGithubId(githubId: string | null | undefined, owners: readonly string[]): Role {
  if (!githubId) return "PUBLIC_DEMO";
  return owners.includes(githubId) ? "OWNER" : "PUBLIC_DEMO";
}
