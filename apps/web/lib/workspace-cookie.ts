/**
 * The selected workspace travels as a cookie that the server-side proxy turns into the `wid`
 * claim. It is only a hint: the API checks membership on every request and falls back to
 * one of the caller's own workspaces, so a tampered cookie grants nothing.
 */
export const WORKSPACE_COOKIE = "jp_workspace";
const ONE_YEAR_SECONDS = 365 * 24 * 60 * 60;
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isWorkspaceId(value: string | undefined | null): value is string {
  return typeof value === "string" && UUID_RE.test(value);
}

export function workspaceCookie(id: string, secure: boolean): string {
  if (!isWorkspaceId(id)) throw new Error("invalid workspace id");
  return `${WORKSPACE_COOKIE}=${id.toLowerCase()}; Path=/; Max-Age=${ONE_YEAR_SECONDS}; SameSite=Lax${secure ? "; Secure" : ""}`;
}

/** Browser only: remember the selected workspace for subsequent API calls. */
export function rememberWorkspace(id: string): void {
  document.cookie = workspaceCookie(id, window.location.protocol === "https:");
}
