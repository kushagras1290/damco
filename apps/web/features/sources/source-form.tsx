"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useForm, useWatch } from "react-hook-form";

import { ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Input, Label, Select } from "@/components/ui/primitives";
import { useCreateSource } from "@/lib/api/hooks";
import { SOURCE_KINDS, type SourceFormInput, type SourceFormValues, sourceFormSchema } from "@/lib/schemas";
import { humanize } from "@/lib/utils";

const TOKEN_KINDS = new Set(["greenhouse", "lever", "ashby"]);

function FieldError({ message }: { message?: string }) {
  return message ? (
    <p role="alert" className="text-xs text-destructive">
      {message}
    </p>
  ) : null;
}

export function SourceForm({ onCreated }: { onCreated: () => void }) {
  const create = useCreateSource();
  const form = useForm<SourceFormInput, unknown, SourceFormValues>({
    resolver: zodResolver(sourceFormSchema),
    defaultValues: {
      kind: "greenhouse",
      name: "",
      company_name: "",
      company_domain: "",
      board_token: "",
      url: "",
      poll_interval_seconds: 300,
      min_poll_interval_seconds: 120,
      max_poll_interval_seconds: 3600,
    },
  });
  const kind = useWatch({ control: form.control, name: "kind" });
  const errors = form.formState.errors;

  const submit = form.handleSubmit(async (values) => {
    await create.mutateAsync(values);
    form.reset();
    onCreated();
  });

  return (
    <form onSubmit={submit} className="grid gap-3 sm:grid-cols-2" noValidate>
      <div className="space-y-1">
        <Label htmlFor="kind">Source type</Label>
        <Select id="kind" {...form.register("kind")}>
          {SOURCE_KINDS.map((value) => (
            <option key={value} value={value}>
              {humanize(value)}
            </option>
          ))}
        </Select>
      </div>
      <div className="space-y-1">
        <Label htmlFor="name">Name</Label>
        <Input id="name" aria-invalid={Boolean(errors.name)} {...form.register("name")} placeholder="Acme careers" />
        <FieldError message={errors.name?.message} />
      </div>
      <div className="space-y-1">
        <Label htmlFor="company_name">Company</Label>
        <Input id="company_name" aria-invalid={Boolean(errors.company_name)} {...form.register("company_name")} />
        <FieldError message={errors.company_name?.message} />
      </div>
      <div className="space-y-1">
        <Label htmlFor="company_domain">Company domain</Label>
        <Input
          id="company_domain"
          aria-invalid={Boolean(errors.company_domain)}
          {...form.register("company_domain")}
          placeholder="acme.io"
        />
        <FieldError message={errors.company_domain?.message} />
      </div>
      {TOKEN_KINDS.has(kind) ? (
        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor="board_token">Board token</Label>
          <Input id="board_token" aria-invalid={Boolean(errors.board_token)} {...form.register("board_token")} placeholder="acme" />
          <FieldError message={errors.board_token?.message} />
        </div>
      ) : (
        <div className="space-y-1 sm:col-span-2">
          <Label htmlFor="url">URL (https only)</Label>
          <Input id="url" aria-invalid={Boolean(errors.url)} {...form.register("url")} placeholder="https://acme.io/jobs.rss" />
          <FieldError message={errors.url?.message} />
        </div>
      )}
      <div className="space-y-1">
        <Label htmlFor="poll">Initial poll interval (s)</Label>
        <Input id="poll" type="number" {...form.register("poll_interval_seconds")} />
        <FieldError message={errors.poll_interval_seconds?.message} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <div className="space-y-1">
          <Label htmlFor="min-poll">Min (s)</Label>
          <Input id="min-poll" type="number" {...form.register("min_poll_interval_seconds")} />
        </div>
        <div className="space-y-1">
          <Label htmlFor="max-poll">Max (s)</Label>
          <Input id="max-poll" type="number" {...form.register("max_poll_interval_seconds")} />
        </div>
      </div>
      {create.error ? (
        <div className="sm:col-span-2">
          <ErrorState error={create.error} />
        </div>
      ) : null}
      <div className="sm:col-span-2">
        <Button type="submit" disabled={form.formState.isSubmitting}>
          {form.formState.isSubmitting ? "Adding…" : "Add source"}
        </Button>
      </div>
    </form>
  );
}
