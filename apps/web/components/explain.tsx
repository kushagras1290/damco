import { Bot, Cog } from "lucide-react";

import { OutcomeBadge } from "@/components/common";
import { Badge } from "@/components/ui/primitives";
import type { Rule, Score } from "@/lib/api/types";
import { formatScore, humanize } from "@/lib/utils";

/** Every score component with its weight and reasoning - never just a percentage. */
export function ScoreBreakdown({ score }: { score: Score }) {
  return (
    <div className="space-y-3">
      <div className="flex items-baseline gap-2">
        <span className="text-3xl font-semibold tabular-nums">{formatScore(score.final_score)}</span>
        <span className="text-sm text-muted-foreground">
          {score.actionable ? "final match score" : "not actionable (hard eligibility failed)"}
        </span>
      </div>
      <ul className="space-y-2" aria-label="Score components">
        {score.components.map((component) => {
          const available = component.value !== null;
          const pct = available ? Math.round((component.value ?? 0) * 100) : 0;
          return (
            <li key={component.name} className="space-y-1">
              <div className="flex items-center justify-between gap-2 text-sm">
                <span className="font-medium">{humanize(component.name)}</span>
                <span className="text-xs text-muted-foreground tabular-nums">
                  {available ? `${pct}% × weight ${component.weight.toFixed(2)}` : "not available (weight redistributed)"}
                </span>
              </div>
              <div
                className="h-1.5 overflow-hidden rounded-full bg-muted"
                role="meter"
                aria-label={humanize(component.name)}
                aria-valuemin={0}
                aria-valuemax={100}
                aria-valuenow={pct}
              >
                <div className="h-full rounded-full bg-primary" style={{ width: `${pct}%` }} />
              </div>
              <p className="text-xs text-muted-foreground">{component.detail}</p>
            </li>
          );
        })}
      </ul>
      <SkillChips matched={score.matched_skills} missing={score.missing_skills} />
    </div>
  );
}

export function SkillChips({ matched, missing }: { matched: string[]; missing: string[] }) {
  return (
    <div className="space-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-1">
        <span className="mr-1 text-xs text-muted-foreground">Matched</span>
        {matched.length ? matched.map((skill) => <Badge key={skill} tone="success">{skill}</Badge>) : <span className="text-xs">—</span>}
      </div>
      <div className="flex flex-wrap items-center gap-1">
        <span className="mr-1 text-xs text-muted-foreground">Missing</span>
        {missing.length ? missing.map((skill) => <Badge key={skill} tone="danger">{skill}</Badge>) : <span className="text-xs">—</span>}
      </div>
    </div>
  );
}

/** Deterministic and AI-resolved eligibility rules with their evidence. */
export function RuleList({ rules }: { rules: Rule[] }) {
  return (
    <ul className="divide-y" aria-label="Eligibility rules">
      {rules.map((rule) => (
        <li key={rule.rule} className="flex flex-col gap-1 py-2 sm:flex-row sm:items-start sm:gap-3">
          <div className="flex w-44 shrink-0 items-center gap-2">
            <OutcomeBadge outcome={rule.outcome} />
            <span className="text-sm font-medium">{humanize(rule.rule)}</span>
          </div>
          <p className="flex-1 text-sm break-words text-muted-foreground">{rule.evidence}</p>
          <span className="flex items-center gap-1 text-xs text-muted-foreground" title={`Source: ${rule.source}`}>
            {rule.source === "ai" ? <Bot className="size-3.5" aria-hidden /> : <Cog className="size-3.5" aria-hidden />}
            {rule.source === "ai" ? "AI" : "Rule"}
          </span>
        </li>
      ))}
    </ul>
  );
}
