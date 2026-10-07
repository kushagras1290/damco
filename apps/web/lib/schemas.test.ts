import { profileFormSchema, sourceFormSchema } from "@/lib/schemas";

const baseSource = {
  kind: "greenhouse",
  name: "Acme",
  company_name: "Acme",
  company_domain: "Acme.io",
  board_token: "acme",
  url: "",
  poll_interval_seconds: "300",
  min_poll_interval_seconds: "120",
  max_poll_interval_seconds: "3600",
};

describe("sourceFormSchema", () => {
  it("normalises a valid ATS source", () => {
    const parsed = sourceFormSchema.parse(baseSource);
    expect(parsed.company_domain).toBe("acme.io");
    expect(parsed.url).toBeUndefined();
    expect(parsed.poll_interval_seconds).toBe(300);
  });

  it("requires a board token for ATS kinds", () => {
    const result = sourceFormSchema.safeParse({ ...baseSource, board_token: "" });
    expect(result.success).toBe(false);
    expect(result.error?.issues[0]?.path).toEqual(["board_token"]);
  });

  it("requires an https url for feed kinds", () => {
    expect(sourceFormSchema.safeParse({ ...baseSource, kind: "rss", board_token: "" }).success).toBe(false);
    expect(
      sourceFormSchema.safeParse({ ...baseSource, kind: "rss", board_token: "", url: "http://acme.io/feed" }).success,
    ).toBe(false);
    expect(
      sourceFormSchema.safeParse({ ...baseSource, kind: "rss", board_token: "", url: "https://acme.io/feed" }).success,
    ).toBe(true);
  });

  it("rejects intervals outside bounds and unsafe tokens", () => {
    expect(sourceFormSchema.safeParse({ ...baseSource, poll_interval_seconds: "30" }).success).toBe(false);
    expect(sourceFormSchema.safeParse({ ...baseSource, poll_interval_seconds: "7200" }).success).toBe(false);
    expect(sourceFormSchema.safeParse({ ...baseSource, board_token: "../etc" }).success).toBe(false);
  });
});

describe("profileFormSchema", () => {
  const profile = {
    display_name: "Me",
    target_roles: "Senior AI Engineer, ",
    skills: "Python, FastAPI ,  RAG",
    years_experience: "5",
    seniority: "senior",
    timezone: "IST",
    summary: "",
    notification_email: "",
    webhook_url: "",
    notify_min_score: "0.7",
    notifications_enabled: false,
    policy: {
      allowed_locations: "India, Worldwide",
      allowed_work_models: ["remote"],
      allowed_timezones: "IST, CET",
      excluded_regions: "US-only",
      experience: { min: "3", max: "6" },
      experience_tolerance_years: "1",
    },
  };

  it("splits comma lists and nulls empty optionals", () => {
    const parsed = profileFormSchema.parse(profile);
    expect(parsed.skills).toEqual(["Python", "FastAPI", "RAG"]);
    expect(parsed.target_roles).toEqual(["Senior AI Engineer"]);
    expect(parsed.notification_email).toBeNull();
    expect(parsed.webhook_url).toBeNull();
    expect(parsed.policy.experience).toEqual({ min: 3, max: 6 });
  });

  it("enforces experience ordering and https webhooks", () => {
    expect(
      profileFormSchema.safeParse({ ...profile, policy: { ...profile.policy, experience: { min: "8", max: "2" } } }).success,
    ).toBe(false);
    expect(profileFormSchema.safeParse({ ...profile, webhook_url: "http://hooks.example.com" }).success).toBe(false);
  });
});
