from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jobpulse_core.domain.models import (
    CandidateProfile,
    EligibilityPolicy,
    JobIntelligence,
    NormalizedJob,
    RemotePolicy,
    Seniority,
)
from jobpulse_core.eligibility import evaluate
from jobpulse_core.polling import (
    CIRCUIT_BREAKER_THRESHOLD,
    INTERVAL_LADDER_SECONDS,
    PollOutcome,
    next_poll,
)
from jobpulse_core.ranking import DEFAULT_WEIGHTS, cosine_similarity, extract_skills, score_job

NOW = datetime(2026, 10, 6, tzinfo=UTC)


class TestRanking:
    def test_components_are_all_persisted_and_weights_sum_to_one(
        self,
        make_job: Callable[..., NormalizedJob],
        profile: CandidateProfile,
        policy: EligibilityPolicy,
    ) -> None:
        job = make_job()
        result = score_job(profile=profile, job=job, eligibility=evaluate(job, policy), now=NOW)
        assert {c.name for c in result.components} == set(DEFAULT_WEIGHTS)
        assert sum(result.effective_weights.values()) == pytest.approx(1.0, abs=1e-5)
        assert 0 <= result.final_score <= 1
        assert result.actionable

    def test_missing_semantic_component_is_renormalised(
        self,
        make_job: Callable[..., NormalizedJob],
        profile: CandidateProfile,
        policy: EligibilityPolicy,
    ) -> None:
        job = make_job()
        result = score_job(profile=profile, job=job, eligibility=evaluate(job, policy), now=NOW)
        semantic = next(c for c in result.components if c.name == "semantic_similarity")
        assert semantic.value is None
        assert semantic.weight == 0.0
        assert "semantic_similarity" not in result.effective_weights

    def test_ineligible_job_is_not_actionable(
        self,
        make_job: Callable[..., NormalizedJob],
        profile: CandidateProfile,
        policy: EligibilityPolicy,
    ) -> None:
        job = make_job(location="Remote", description="Must reside in the United States. Python, FastAPI, RAG.")
        result = score_job(profile=profile, job=job, eligibility=evaluate(job, policy), now=NOW)
        assert result.actionable is False

    def test_ai_skills_drive_skill_match(
        self,
        make_job: Callable[..., NormalizedJob],
        profile: CandidateProfile,
        policy: EligibilityPolicy,
    ) -> None:
        job = make_job()
        intelligence = JobIntelligence(
            remote_policy=RemotePolicy.REMOTE,
            permitted_countries=[],
            excluded_countries=[],
            minimum_experience=4,
            maximum_experience=None,
            required_skills=["Python", "Kubernetes"],
            preferred_skills=["postgres"],
            seniority=Seniority.SENIOR,
            sponsorship_available=None,
            timezone_requirements=[],
            residency_requirement=None,
            confidence=0.9,
        )
        result = score_job(
            profile=profile, job=job, eligibility=evaluate(job, policy), intelligence=intelligence, now=NOW
        )
        assert result.matched_skills == ["python", "postgresql"]
        assert result.missing_skills == ["kubernetes"]
        skill = next(c for c in result.components if c.name == "skill_match")
        assert skill.value == pytest.approx(1.5 / 2.5)

    def test_freshness_decays(
        self,
        make_job: Callable[..., NormalizedJob],
        profile: CandidateProfile,
        policy: EligibilityPolicy,
    ) -> None:
        fresh = make_job(published_at=NOW)
        stale = make_job(published_at=NOW - timedelta(days=30))
        f = score_job(profile=profile, job=fresh, eligibility=evaluate(fresh, policy), now=NOW)
        s = score_job(profile=profile, job=stale, eligibility=evaluate(stale, policy), now=NOW)
        assert f.final_score > s.final_score

    def test_semantic_similarity_used_when_vectors_present(
        self,
        make_job: Callable[..., NormalizedJob],
        profile: CandidateProfile,
        policy: EligibilityPolicy,
    ) -> None:
        job = make_job()
        result = score_job(
            profile=profile,
            job=job,
            eligibility=evaluate(job, policy),
            profile_embedding=[1.0, 0.0, 0.0],
            job_embedding=[0.9, 0.1, 0.0],
            now=NOW,
        )
        assert "semantic_similarity" in result.effective_weights

    def test_cosine_rejects_mismatched_vectors(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            cosine_similarity([1.0], [1.0, 2.0])

    def test_keyword_skill_extraction_avoids_false_positives(self) -> None:
        skills = extract_skills(
            "We go to great lengths. Experience with Python, Postgres and RAG pipelines. Go is a plus."
        )
        assert "python" in skills
        assert "postgresql" in skills
        assert "rag" in skills
        assert "go" in skills  # "Go is a plus" (capitalised)

    @given(st.lists(st.floats(min_value=-10, max_value=10, allow_nan=False), min_size=1, max_size=16))
    def test_cosine_self_similarity(self, vector: list[float]) -> None:
        if all(v == 0 for v in vector):
            assert cosine_similarity(vector, vector) == 0.0
        else:
            assert cosine_similarity(vector, vector) == pytest.approx(1.0)


class TestPolling:
    def test_new_jobs_poll_fastest(self) -> None:
        decision = next_poll(
            outcome=PollOutcome.NEW_JOBS,
            current_interval=1800,
            min_interval=300,
            max_interval=3600,
            consecutive_failures=0,
        )
        assert decision.next_interval_seconds == 300

    def test_quiet_source_backs_off_one_step(self) -> None:
        decision = next_poll(
            outcome=PollOutcome.NO_CHANGE,
            current_interval=600,
            min_interval=300,
            max_interval=3600,
            consecutive_failures=0,
        )
        assert decision.next_interval_seconds == 900

    def test_quiet_source_capped_at_max(self) -> None:
        decision = next_poll(
            outcome=PollOutcome.NO_CHANGE,
            current_interval=3600,
            min_interval=300,
            max_interval=3600,
            consecutive_failures=0,
        )
        assert decision.next_interval_seconds == 3600

    def test_rate_limit_honours_retry_after(self) -> None:
        decision = next_poll(
            outcome=PollOutcome.RATE_LIMITED,
            current_interval=300,
            min_interval=300,
            max_interval=3600,
            consecutive_failures=1,
            retry_after_seconds=7200,
        )
        assert decision.sleep_seconds >= 7200
        assert not decision.circuit_open

    def test_circuit_breaker_opens(self) -> None:
        decision = next_poll(
            outcome=PollOutcome.FAILED,
            current_interval=900,
            min_interval=300,
            max_interval=3600,
            consecutive_failures=CIRCUIT_BREAKER_THRESHOLD,
        )
        assert decision.circuit_open
        assert decision.next_interval_seconds == 3600

    @given(
        outcome=st.sampled_from(list(PollOutcome)),
        current=st.integers(min_value=60, max_value=10_000),
        failures=st.integers(min_value=0, max_value=20),
    )
    def test_interval_always_within_bounds(self, outcome: PollOutcome, current: int, failures: int) -> None:
        decision = next_poll(
            outcome=outcome,
            current_interval=current,
            min_interval=300,
            max_interval=3600,
            consecutive_failures=failures,
        )
        assert 300 <= decision.next_interval_seconds <= 3600
        assert decision.sleep_seconds > 0

    def test_ladder_matches_architecture_doc(self) -> None:
        assert INTERVAL_LADDER_SECONDS == (300, 600, 900, 1800, 3600)
