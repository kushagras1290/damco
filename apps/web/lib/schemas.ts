import { z } from "zod";

export const SOURCE_KINDS = [
  "greenhouse",
  "lever",
  "ashby",
  "rss",
  "generic_json",
  "static_html",
  "dynamic_html",
] as const;
const TOKEN_KINDS = new Set<string>(["greenhouse", "lever", "ashby"]);
const MIN_POLL = 300;
const MAX_POLL = 86_400;

const interval = z.coerce.number().int().min(MIN_POLL).max(MAX_POLL);

export const sourceFormSchema = z
  .object({
    kind: z.enum(SOURCE_KINDS),
    name: z.string().trim().min(1).max(200),
    company_name: z.string().trim().min(1).max(200),
    company_domain: z
      .string()
      .trim()
      .toLowerCase()
      .regex(/^[a-z0-9.-]+\.[a-z]{2,}$/, "Enter a domain like acme.io"),
    board_token: z
      .string()
      .trim()
      .regex(/^[A-Za-z0-9_.-]*$/, "Letters, digits, dot, dash or underscore only")
      .max(200)
      .optional()
      .transform((value) => value || undefined),
    url: z
      .string()
      .trim()
      .optional()
      .transform((value) => value || undefined)
      .pipe(z.url({ protocol: /^https$/, error: "Must be an https:// URL" }).optional()),
    poll_interval_seconds: interval.default(900),
    min_poll_interval_seconds: interval.default(300),
    max_poll_interval_seconds: interval.default(3600),
  })
  .superRefine((value, ctx) => {
    if (TOKEN_KINDS.has(value.kind) && !value.board_token) {
      ctx.addIssue({ code: "custom", path: ["board_token"], message: "Board token is required for this ATS" });
    }
    if (!TOKEN_KINDS.has(value.kind) && !value.url) {
      ctx.addIssue({ code: "custom", path: ["url"], message: "URL is required for this source type" });
    }
    const { min_poll_interval_seconds: min, poll_interval_seconds: current, max_poll_interval_seconds: max } = value;
    if (!(min <= current && current <= max)) {
      ctx.addIssue({ code: "custom", path: ["poll_interval_seconds"], message: "Must be between min and max" });
    }
  });

export type SourceFormInput = z.input<typeof sourceFormSchema>;
export type SourceFormValues = z.output<typeof sourceFormSchema>;

const csv = z
  .string()
  .transform((value) =>
    value
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean),
  );

export const profileFormSchema = z
  .object({
    display_name: z.string().trim().min(1).max(200),
    target_roles: csv,
    skills: csv,
    years_experience: z.coerce.number().min(0).max(60),
    seniority: z.enum(["intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager", "director"]),
    timezone: z.string().trim().min(2).max(10),
    summary: z.string().max(8000),
    notification_email: z
      .string()
      .trim()
      .transform((value) => value || null)
      .pipe(z.email().nullable()),
    webhook_url: z
      .string()
      .trim()
      .transform((value) => value || null)
      .pipe(z.url({ protocol: /^https$/, error: "Must be an https:// URL" }).nullable()),
    notify_min_score: z.coerce.number().min(0).max(1),
    notifications_enabled: z.boolean(),
    policy: z.object({
      allowed_locations: csv,
      allowed_work_models: z.array(z.enum(["remote", "hybrid", "onsite"])).min(1, "Pick at least one"),
      allowed_timezones: csv,
      excluded_regions: csv,
      experience: z.object({ min: z.coerce.number().min(0).max(50), max: z.coerce.number().min(0).max(50) }),
      experience_tolerance_years: z.coerce.number().min(0).max(5),
    }),
  })
  .refine((value) => value.policy.experience.min <= value.policy.experience.max, {
    path: ["policy", "experience", "max"],
    message: "Max must be at least min",
  });

export type ProfileFormInput = z.input<typeof profileFormSchema>;
export type ProfileFormValues = z.output<typeof profileFormSchema>;
