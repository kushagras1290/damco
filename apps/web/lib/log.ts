import "server-only";

type Level = "info" | "warn" | "error";

/** Structured JSON log line on stdout (collected by the platform). Never pass secrets. */
export function log(level: Level, event: string, fields: Record<string, string | number | boolean | null> = {}): void {
  process.stdout.write(`${JSON.stringify({ level, event, ts: new Date().toISOString(), service: "jobpulse-web", ...fields })}\n`);
}
