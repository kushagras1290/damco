"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect } from "react";
import { useForm } from "react-hook-form";

import { ErrorState, LoadingRows, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardContent, CardDescription, CardHeader, CardTitle, Input, Label, Select, Textarea } from "@/components/ui/primitives";
import { useIsOwner, useProfile, useUpdateProfile } from "@/lib/api/hooks";
import type { Profile } from "@/lib/api/types";
import { type ProfileFormInput, type ProfileFormValues, profileFormSchema } from "@/lib/schemas";

const SENIORITIES = ["intern", "junior", "mid", "senior", "staff", "principal", "lead", "manager", "director"] as const;
const WORK_MODELS = ["remote", "hybrid", "onsite"] as const;

export function toFormInput(profile: Profile): ProfileFormInput {
  return {
    display_name: profile.display_name,
    target_roles: profile.target_roles.join(", "),
    skills: profile.skills.join(", "),
    years_experience: profile.years_experience,
    seniority: profile.seniority === "unknown" ? "mid" : profile.seniority,
    timezone: profile.timezone,
    summary: profile.summary,
    notification_email: profile.notification_email ?? "",
    webhook_url: profile.webhook_url ?? "",
    notify_min_score: profile.notify_min_score,
    notifications_enabled: profile.notifications_enabled,
    policy: {
      allowed_locations: profile.policy.allowed_locations.join(", "),
      allowed_work_models: profile.policy.allowed_work_models.filter(
        (model): model is (typeof WORK_MODELS)[number] => model !== "unknown",
      ),
      allowed_timezones: profile.policy.allowed_timezones.join(", "),
      excluded_regions: profile.policy.excluded_regions.join(", "),
      experience: { min: profile.policy.experience.min, max: profile.policy.experience.max },
      experience_tolerance_years: profile.policy.experience_tolerance_years,
    },
  };
}

function Field({ id, label, error, children }: { id: string; label: string; error?: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {error ? (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      ) : null}
    </div>
  );
}

export function ProfileView() {
  const { data: profile, error, isPending } = useProfile();
  const isOwner = useIsOwner();
  const update = useUpdateProfile();
  const form = useForm<ProfileFormInput, unknown, ProfileFormValues>({ resolver: zodResolver(profileFormSchema) });
  const errors = form.formState.errors;

  useEffect(() => {
    if (profile) form.reset(toFormInput(profile));
  }, [profile, form]);

  if (isPending) return <LoadingRows rows={8} />;
  if (error) return <ErrorState error={error} />;

  const submit = form.handleSubmit((values) => update.mutate(values));

  return (
    <>
      <PageHeader
        title="Profile & eligibility policy"
        description="Hard constraints are enforced deterministically before any AI runs."
        actions={profile.has_embedding ? <Badge tone="success">profile embedded</Badge> : <Badge>no embedding yet</Badge>}
      />
      <form onSubmit={submit} noValidate>
        <fieldset disabled={!isOwner || update.isPending} className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Candidate</CardTitle>
              <CardDescription>Drives ranking: skills, roles and seniority fit.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3">
              <Field id="display_name" label="Display name" error={errors.display_name?.message}>
                <Input id="display_name" {...form.register("display_name")} />
              </Field>
              <Field id="target_roles" label="Target roles (comma-separated)">
                <Input id="target_roles" {...form.register("target_roles")} placeholder="Senior AI Engineer, Backend Engineer" />
              </Field>
              <Field id="skills" label="Skills (comma-separated)">
                <Textarea id="skills" {...form.register("skills")} placeholder="Python, FastAPI, PostgreSQL, RAG" />
              </Field>
              <div className="grid grid-cols-3 gap-3">
                <Field id="years" label="Years exp." error={errors.years_experience?.message}>
                  <Input id="years" type="number" step="0.5" {...form.register("years_experience")} />
                </Field>
                <Field id="seniority" label="Seniority">
                  <Select id="seniority" {...form.register("seniority")}>
                    {SENIORITIES.map((value) => (
                      <option key={value} value={value}>
                        {value}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field id="timezone" label="Timezone" error={errors.timezone?.message}>
                  <Input id="timezone" {...form.register("timezone")} />
                </Field>
              </div>
              <Field id="summary" label="Summary (used for semantic matching)">
                <Textarea id="summary" rows={4} {...form.register("summary")} />
              </Field>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Eligibility policy</CardTitle>
              <CardDescription>A job failing any of these is rejected — AI cannot override.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3">
              <Field id="allowed_locations" label="Allowed locations">
                <Input id="allowed_locations" {...form.register("policy.allowed_locations")} placeholder="India, Worldwide" />
              </Field>
              <fieldset className="space-y-1">
                <legend className="text-xs font-medium text-muted-foreground">Allowed work models</legend>
                <div className="flex gap-4">
                  {WORK_MODELS.map((model) => (
                    <label key={model} className="flex items-center gap-1.5 text-sm">
                      <input type="checkbox" value={model} {...form.register("policy.allowed_work_models")} />
                      {model}
                    </label>
                  ))}
                </div>
                {errors.policy?.allowed_work_models ? (
                  <p role="alert" className="text-xs text-destructive">
                    {errors.policy.allowed_work_models.message}
                  </p>
                ) : null}
              </fieldset>
              <Field id="allowed_timezones" label="Allowed timezones">
                <Input id="allowed_timezones" {...form.register("policy.allowed_timezones")} placeholder="IST, GMT, CET" />
              </Field>
              <Field id="excluded_regions" label="Excluded regions">
                <Input id="excluded_regions" {...form.register("policy.excluded_regions")} placeholder="US-only, Canada-only" />
              </Field>
              <div className="grid grid-cols-3 gap-3">
                <Field id="exp-min" label="Exp. min (yrs)">
                  <Input id="exp-min" type="number" {...form.register("policy.experience.min")} />
                </Field>
                <Field id="exp-max" label="Exp. max (yrs)" error={errors.policy?.experience?.max?.message}>
                  <Input id="exp-max" type="number" {...form.register("policy.experience.max")} />
                </Field>
                <Field id="tolerance" label="Tolerance (yrs)">
                  <Input id="tolerance" type="number" step="0.5" {...form.register("policy.experience_tolerance_years")} />
                </Field>
              </div>
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Notifications</CardTitle>
              <CardDescription>Sent once per job version when the match score clears the threshold.</CardDescription>
            </CardHeader>
            <CardContent className="grid gap-3 md:grid-cols-4">
              <Field id="email" label="Email (Resend)" error={errors.notification_email?.message}>
                <Input id="email" type="email" {...form.register("notification_email")} />
              </Field>
              <Field id="webhook" label="Webhook URL (HMAC-signed)" error={errors.webhook_url?.message}>
                <Input id="webhook" {...form.register("webhook_url")} placeholder="https://…" />
              </Field>
              <Field id="threshold" label="Min score (0–1)" error={errors.notify_min_score?.message}>
                <Input id="threshold" type="number" step="0.05" {...form.register("notify_min_score")} />
              </Field>
              <label className="flex items-center gap-2 self-end pb-2 text-sm">
                <input type="checkbox" {...form.register("notifications_enabled")} /> Enabled
              </label>
            </CardContent>
          </Card>
        </fieldset>
        {update.error ? (
          <div className="mt-4">
            <ErrorState error={update.error} />
          </div>
        ) : null}
        <div className="mt-4 flex items-center gap-3">
          {isOwner ? (
            <Button type="submit" disabled={update.isPending}>
              {update.isPending ? "Saving…" : "Save profile"}
            </Button>
          ) : (
            <p className="text-sm text-muted-foreground">Read-only: sign in as the owner to edit.</p>
          )}
          {update.isSuccess ? <span className="text-sm text-success">Saved.</span> : null}
        </div>
      </form>
    </>
  );
}
