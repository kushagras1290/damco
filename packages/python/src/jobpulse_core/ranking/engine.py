"""Explainable match scoring.

Final score = sum(weight_i * component_i) over *available* components, with weights
re-normalised when a component cannot be computed (e.g. no embeddings). Every
component, its raw inputs and the effective weights are returned for persistence so
the UI never shows "only a magical percentage".

Hard eligibility always wins: an ineligible job's score is marked non-actionable.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from difflib import SequenceMatcher

from jobpulse_core.domain.models import (
    SENIORITY_RANK,
    CandidateProfile,
    JobIntelligence,
    NormalizedJob,
    RemotePolicy,
    Seniority,
)
from jobpulse_core.eligibility.engine import EligibilityResult, RuleName, RuleOutcome
from jobpulse_core.ingestion.normalize import infer_seniority, normalize_title
from jobpulse_core.ranking.skills import extract_skills, normalize_skill

DEFAULT_WEIGHTS: dict[str, float] = {
    "skill_match": 0.30,
    "semantic_similarity": 0.20,
    "role_similarity": 0.15,
    "seniority_fit": 0.15,
    "location_timezone_fit": 0.10,
    "freshness": 0.10,
}
FRESHNESS_HALF_LIFE_DAYS = 7.0
PREFERRED_SKILL_WEIGHT = 0.5
UNKNOWN_RULE_CREDIT = 0.6
UNKNOWN_SENIORITY_CREDIT = 0.6
UNKNOWN_FRESHNESS_CREDIT = 0.5


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    value: float | None
    weight: float
    detail: str

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "value": self.value, "weight": self.weight, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class MatchResult:
    final_score: float
    actionable: bool
    components: tuple[ScoreComponent, ...]
    effective_weights: dict[str, float]
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "final_score": self.final_score,
            "actionable": self.actionable,
            "components": [component.to_dict() for component in self.components],
            "effective_weights": self.effective_weights,
            "matched_skills": self.matched_skills,
            "missing_skills": self.missing_skills,
        }


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b) or not a:
        msg = "vectors must be non-empty and equal length"
        raise ValueError(msg)
    scale_a = max(abs(x) for x in a)
    scale_b = max(abs(y) for y in b)
    if scale_a == 0 or scale_b == 0:
        return 0.0
    # Cosine is scale-invariant; pre-scaling avoids under/overflow for extreme magnitudes.
    xs = [x / scale_a for x in a]
    ys = [y / scale_b for y in b]
    dot = math.fsum(x * y for x, y in zip(xs, ys, strict=True))
    norm = math.sqrt(math.fsum(x * x for x in xs)) * math.sqrt(math.fsum(y * y for y in ys))
    return max(-1.0, min(1.0, dot / norm))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _skill_match(
    profile: CandidateProfile,
    job: NormalizedJob,
    intel: JobIntelligence | None,
) -> tuple[ScoreComponent, list[str], list[str]]:
    have = {normalize_skill(skill) for skill in profile.skills}
    required: list[str]
    preferred: list[str]
    if intel is not None and (intel.required_skills or intel.preferred_skills):
        required = list(dict.fromkeys(normalize_skill(s) for s in intel.required_skills))
        preferred = [s for s in dict.fromkeys(normalize_skill(s) for s in intel.preferred_skills) if s not in required]
        origin = "AI-extracted"
    else:
        required = extract_skills(f"{job.title}\n{job.description_text}")
        preferred = []
        origin = "keyword-extracted"
    if not required and not preferred:
        return ScoreComponent("skill_match", None, 0.0, "no skills detected in posting"), [], []

    matched_required = [s for s in required if s in have]
    matched_preferred = [s for s in preferred if s in have]
    total = len(required) + PREFERRED_SKILL_WEIGHT * len(preferred)
    got = len(matched_required) + PREFERRED_SKILL_WEIGHT * len(matched_preferred)
    value = _clamp(got / total) if total else 0.0
    missing = [s for s in required if s not in have]
    detail = (
        f"{origin}: {len(matched_required)}/{len(required)} required, "
        f"{len(matched_preferred)}/{len(preferred)} preferred"
    )
    return ScoreComponent("skill_match", round(value, 4), 0.0, detail), matched_required + matched_preferred, missing


def _semantic(profile_embedding: Sequence[float] | None, job_embedding: Sequence[float] | None) -> ScoreComponent:
    if not profile_embedding or not job_embedding:
        return ScoreComponent("semantic_similarity", None, 0.0, "embeddings unavailable")
    similarity = cosine_similarity(profile_embedding, job_embedding)
    # text-embedding cosine for related texts sits in ~[0.1, 0.7]; rescale to [0, 1].
    value = _clamp((similarity - 0.1) / 0.6)
    return ScoreComponent("semantic_similarity", round(value, 4), 0.0, f"cosine={similarity:.3f}")


def _role_similarity(profile: CandidateProfile, job: NormalizedJob) -> ScoreComponent:
    if not profile.target_roles:
        return ScoreComponent("role_similarity", None, 0.0, "no target roles configured")
    best_role, best = "", 0.0
    job_tokens = set(job.normalized_title.split())
    for role in profile.target_roles:
        normalized = normalize_title(role)
        ratio = SequenceMatcher(None, normalized, job.normalized_title).ratio()
        role_tokens = set(normalized.split())
        overlap = len(role_tokens & job_tokens) / len(role_tokens) if role_tokens else 0.0
        score = max(ratio, overlap)
        if score > best:
            best_role, best = role, score
    return ScoreComponent("role_similarity", round(_clamp(best), 4), 0.0, f"closest target role: {best_role!r}")


def _seniority_fit(profile: CandidateProfile, job: NormalizedJob, intel: JobIntelligence | None) -> ScoreComponent:
    level = job.seniority
    if level is Seniority.UNKNOWN and intel is not None:
        level = intel.seniority
    if level is Seniority.UNKNOWN:
        level = infer_seniority(job.title)
    if level is Seniority.UNKNOWN or profile.seniority is Seniority.UNKNOWN:
        return ScoreComponent("seniority_fit", UNKNOWN_SENIORITY_CREDIT, 0.0, "seniority not stated")
    gap = abs(SENIORITY_RANK.get(level, 2) - SENIORITY_RANK.get(profile.seniority, 2))
    value = {0: 1.0, 1: 0.6, 2: 0.2}.get(gap, 0.0)
    return ScoreComponent("seniority_fit", value, 0.0, f"job {level.value} vs profile {profile.seniority.value}")


def _location_timezone_fit(eligibility: EligibilityResult, job: NormalizedJob) -> ScoreComponent:
    credits: list[float] = []
    notes: list[str] = []
    for name in (RuleName.LOCATION, RuleName.TIMEZONE, RuleName.WORK_MODEL):
        outcome = eligibility.rule(name).outcome
        credit = {RuleOutcome.PASS: 1.0, RuleOutcome.UNKNOWN: UNKNOWN_RULE_CREDIT, RuleOutcome.FAIL: 0.0}[outcome]
        credits.append(credit)
        notes.append(f"{name.value}={outcome.value}")
    if job.remote_policy is RemotePolicy.REMOTE:
        notes.append("fully remote")
    value = sum(credits) / len(credits)
    return ScoreComponent("location_timezone_fit", round(value, 4), 0.0, ", ".join(notes))


def _freshness(job: NormalizedJob, now: datetime) -> ScoreComponent:
    if job.published_at is None:
        return ScoreComponent("freshness", UNKNOWN_FRESHNESS_CREDIT, 0.0, "publish date unknown")
    published = job.published_at if job.published_at.tzinfo else job.published_at.replace(tzinfo=UTC)
    age_days = max(0.0, (now - published).total_seconds() / 86400)
    value = 0.5 ** (age_days / FRESHNESS_HALF_LIFE_DAYS)
    return ScoreComponent("freshness", round(value, 4), 0.0, f"{age_days:.1f} days old")


def score_job(
    *,
    profile: CandidateProfile,
    job: NormalizedJob,
    eligibility: EligibilityResult,
    intelligence: JobIntelligence | None = None,
    profile_embedding: Sequence[float] | None = None,
    job_embedding: Sequence[float] | None = None,
    weights: dict[str, float] | None = None,
    now: datetime | None = None,
) -> MatchResult:
    """Compute an explainable match score. Pure function."""
    base_weights = weights or DEFAULT_WEIGHTS
    moment = now or datetime.now(tz=UTC)

    skill_component, matched, missing = _skill_match(profile, job, intelligence)
    raw_components = [
        skill_component,
        _semantic(profile_embedding, job_embedding),
        _role_similarity(profile, job),
        _seniority_fit(profile, job, intelligence),
        _location_timezone_fit(eligibility, job),
        _freshness(job, moment),
    ]
    available = {c.name: base_weights[c.name] for c in raw_components if c.value is not None}
    weight_sum = sum(available.values())
    effective = {name: round(w / weight_sum, 6) for name, w in available.items()} if weight_sum else {}

    components = tuple(ScoreComponent(c.name, c.value, effective.get(c.name, 0.0), c.detail) for c in raw_components)
    final = sum((c.value or 0.0) * c.weight for c in components)
    return MatchResult(
        final_score=round(_clamp(final), 4),
        actionable=eligibility.eligible,
        components=components,
        effective_weights=effective,
        matched_skills=matched,
        missing_skills=missing,
    )
