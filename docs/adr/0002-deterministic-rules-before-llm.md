# ADR 0002 — Deterministic hard rules before any LLM

**Status:** Accepted

## Context
Users must never be shown a job they are legally or practically barred from (for example
"Candidates must reside in the United States"), no matter how good the semantic match is.
LLM output is probabilistic and costs money.

## Decision
- Every eligibility rule is deterministic and returns `pass`, `fail` or `unknown` with quoted
  evidence. `pass`/`fail` require positive evidence; absence of evidence is `unknown`.
- The LLM runs only when at least one rule is `unknown`.
- `apply_intelligence()` may resolve `unknown` rules. It **never** modifies a deterministic
  `pass` or `fail`, and ignores extractions below 0.6 confidence.
- A job is ineligible if any rule fails; ineligible jobs are scored but marked non-actionable.

## Consequences
- Hard constraints are auditable and unit-testable.
- Some ambiguous postings stay `unknown` in deterministic-only mode; they remain eligible but
  receive partial location/timezone credit in ranking.
