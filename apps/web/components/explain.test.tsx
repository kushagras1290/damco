import { render, screen, within } from "@testing-library/react";

import { RuleList, ScoreBreakdown } from "@/components/explain";
import type { Score } from "@/lib/api/types";

const score: Score = {
  final_score: 0.82,
  actionable: true,
  components: [
    { name: "skill_match", value: 0.75, weight: 0.375, detail: "AI-extracted: 3/4 required" },
    { name: "semantic_similarity", value: null, weight: 0, detail: "embeddings unavailable" },
    { name: "freshness", value: 1, weight: 0.125, detail: "0.5 days old" },
  ],
  weights: { skill_match: 0.375, freshness: 0.125 },
  matched_skills: ["python", "rag"],
  missing_skills: ["kubernetes"],
  workflow_id: "wf",
  created_at: "2026-10-06T10:00:00Z",
};

describe("ScoreBreakdown", () => {
  it("shows every component with weight and explanation, never just a number", () => {
    render(<ScoreBreakdown score={score} />);
    expect(screen.getByText("82%")).toBeInTheDocument();
    const list = screen.getByRole("list", { name: "Score components" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByText("AI-extracted: 3/4 required")).toBeInTheDocument();
    expect(screen.getByText("not available (weight redistributed)")).toBeInTheDocument();
    expect(screen.getByRole("meter", { name: "Skill match" })).toHaveAttribute("aria-valuenow", "75");
    expect(screen.getByText("kubernetes")).toBeInTheDocument();
  });

  it("flags non-actionable scores", () => {
    render(<ScoreBreakdown score={{ ...score, actionable: false }} />);
    expect(screen.getByText(/not actionable/)).toBeInTheDocument();
  });
});

describe("RuleList", () => {
  it("renders outcome, evidence and whether a rule came from AI", () => {
    render(
      <RuleList
        rules={[
          { rule: "excluded_region", outcome: "fail", evidence: "must reside in the United States", source: "deterministic" },
          { rule: "timezone", outcome: "pass", evidence: "AI: timezone requirements ['CET']", source: "ai" },
        ]}
      />,
    );
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]!).getByText("fail")).toBeInTheDocument();
    expect(within(items[0]!).getByText("Rule")).toBeInTheDocument();
    expect(within(items[1]!).getByText("AI")).toBeInTheDocument();
  });
});
