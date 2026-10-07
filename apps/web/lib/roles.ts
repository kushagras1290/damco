export type Role = "PUBLIC_DEMO" | "OWNER";

/** Owner role is granted only to explicitly allowlisted GitHub logins. */
export function roleForLogin(login: string | null | undefined, owners: readonly string[]): Role {
  if (!login) return "PUBLIC_DEMO";
  return owners.includes(login.toLowerCase()) ? "OWNER" : "PUBLIC_DEMO";
}
