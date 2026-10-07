from __future__ import annotations

from collections.abc import Callable

import pytest

from jobpulse_core.domain.models import (
    EligibilityPolicy,
    EligibilityStatus,
    JobIntelligence,
    NormalizedJob,
    RemotePolicy,
    Seniority,
)
from jobpulse_core.eligibility import (
    RuleName,
    RuleOutcome,
    RuleSource,
    apply_intelligence,
    evaluate,
    load_policy_yaml,
)
from jobpulse_core.eligibility.engine import detect_timezones, extract_experience
from jobpulse_core.eligibility.geo import countries_in
from jobpulse_core.errors import ConfigurationError

JobFactory = Callable[..., NormalizedJob]


def intel(**overrides: object) -> JobIntelligence:
    base: dict[str, object] = {
        "remote_policy": RemotePolicy.REMOTE,
        "permitted_countries": [],
        "excluded_countries": [],
        "minimum_experience": None,
        "maximum_experience": None,
        "required_skills": [],
        "preferred_skills": [],
        "seniority": Seniority.SENIOR,
        "sponsorship_available": None,
        "timezone_requirements": [],
        "residency_requirement": None,
        "confidence": 0.9,
    }
    base.update(overrides)
    return JobIntelligence.model_validate(base)


class TestDeterministicRules:
    def test_worldwide_remote_role_is_eligible(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(), policy)
        assert result.status is EligibilityStatus.ELIGIBLE
        assert result.rule(RuleName.LOCATION).outcome is RuleOutcome.PASS
        assert result.rule(RuleName.WORK_MODEL).outcome is RuleOutcome.PASS
        assert result.rule(RuleName.EXPERIENCE).outcome is RuleOutcome.PASS

    def test_us_residency_requirement_is_hard_fail(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        job = make_job(location="Remote", description="Fully remote. Candidates must reside in the United States.")
        result = evaluate(job, policy)
        assert result.status is EligibilityStatus.INELIGIBLE
        rule = result.rule(RuleName.EXCLUDED_REGION)
        assert rule.outcome is RuleOutcome.FAIL
        assert "united states" in rule.evidence

    @pytest.mark.parametrize(
        "phrase",
        [
            "This role is US-only.",
            "You must be authorized to work in the US.",
            "Only open to candidates based in Canada.",
            "Active U.S. security clearance required.",
        ],
    )
    def test_residency_variants(self, make_job: JobFactory, policy: EligibilityPolicy, phrase: str) -> None:
        result = evaluate(make_job(location="Remote", description=f"Remote role. {phrase}"), policy)
        assert result.rule(RuleName.EXCLUDED_REGION).outcome is RuleOutcome.FAIL

    def test_authorized_in_allowed_country_is_not_a_failure(
        self, make_job: JobFactory, policy: EligibilityPolicy
    ) -> None:
        result = evaluate(make_job(description="Remote. Must be authorized to work in India."), policy)
        assert result.rule(RuleName.EXCLUDED_REGION).outcome is RuleOutcome.UNKNOWN

    def test_location_restricted_to_other_country(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(location="Remote - United States"), policy)
        assert result.rule(RuleName.LOCATION).outcome is RuleOutcome.FAIL
        assert result.status is EligibilityStatus.INELIGIBLE

    def test_us_city_with_state_code(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(location="Austin, TX", description="Remote friendly."), policy)
        assert result.rule(RuleName.LOCATION).outcome is RuleOutcome.FAIL

    def test_apac_region_covers_india(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(location="Remote (APAC)"), policy)
        assert result.rule(RuleName.LOCATION).outcome is RuleOutcome.PASS

    @pytest.mark.parametrize("location", ["Remote - European Union", "Remote (EEA)", "DACH region", "Remote, Nordics"])
    def test_european_regions_excluded_for_india_policy(
        self, make_job: JobFactory, policy: EligibilityPolicy, location: str
    ) -> None:
        # Regression: real Ashby postings use "Remote - European Union".
        assert evaluate(make_job(location=location), policy).rule(RuleName.LOCATION).outcome is RuleOutcome.FAIL

    @pytest.mark.parametrize("location", ["Seoul, South Korea", "Ontario, CAN", "Toronto, Canada", "Taipei"])
    def test_real_world_locations_outside_policy_fail(
        self, make_job: JobFactory, policy: EligibilityPolicy, location: str
    ) -> None:
        # Regression: real Anthropic Greenhouse locations.
        assert evaluate(make_job(location=location), policy).rule(RuleName.LOCATION).outcome is RuleOutcome.FAIL

    def test_lowercase_can_is_not_canada(self) -> None:
        assert countries_in("Remote (can be anywhere in India)") == {"india"}

    def test_unrecognised_location_is_unknown_not_fail(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(location="Atlantis"), policy)
        assert result.rule(RuleName.LOCATION).outcome is RuleOutcome.UNKNOWN

    def test_onsite_fails_work_model(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(location="Bangalore, India", description="On-site role in our office."), policy)
        assert result.rule(RuleName.WORK_MODEL).outcome is RuleOutcome.FAIL

    def test_experience_too_senior(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(description="Remote. 10+ years of experience required."), policy)
        assert result.rule(RuleName.EXPERIENCE).outcome is RuleOutcome.FAIL

    def test_experience_within_tolerance(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(description="Remote. 7+ years of professional experience."), policy)
        assert result.rule(RuleName.EXPERIENCE).outcome is RuleOutcome.PASS

    def test_experience_too_junior(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        result = evaluate(make_job(description="Remote. 0-1 years of experience, new grads welcome."), policy)
        assert result.rule(RuleName.EXPERIENCE).outcome is RuleOutcome.FAIL

    def test_strict_us_timezone_fails(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        job = make_job(description="Remote. You must work PST business hours.")
        assert evaluate(job, policy).rule(RuleName.TIMEZONE).outcome is RuleOutcome.FAIL

    def test_soft_timezone_mention_is_unknown(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        job = make_job(description="Remote. Our team is mostly on Eastern time.")
        assert evaluate(job, policy).rule(RuleName.TIMEZONE).outcome is RuleOutcome.UNKNOWN

    def test_compatible_timezone_passes(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        job = make_job(description="Remote. Must overlap 4 hours with CET working hours.")
        assert evaluate(job, policy).rule(RuleName.TIMEZONE).outcome is RuleOutcome.PASS

    def test_policy_hash_is_stable(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        assert evaluate(make_job(), policy).policy_hash == evaluate(make_job(), policy).policy_hash


class TestExtractors:
    def test_extract_experience_takes_headline_requirement(self) -> None:
        low, high, evidence = extract_experience("5+ years of experience in Python and 2+ years of experience with Go")
        assert (low, high) == (5.0, None)
        assert "5+" in evidence

    def test_extract_experience_range(self) -> None:
        assert extract_experience("3-5 years of relevant experience")[:2] == (3.0, 5.0)

    @pytest.mark.parametrize(
        "text",
        [
            "Requirements: 3+ years of experience. Preferred qualifications: 8+ years of industry experience.",
            "You must have 3+ years of backend experience. 8+ years of Go experience is a plus.",
            "Minimum qualifications: 3+ years of experience. Nice to have: 10+ years of experience leading teams.",
        ],
    )
    def test_preferred_experience_never_counts_as_required(self, text: str) -> None:
        # Regression: real Anthropic postings list "Preferred qualifications 8+ years".
        assert extract_experience(text)[:2] == (3.0, None)

    def test_preferred_only_posting_has_no_requirement(self) -> None:
        assert extract_experience("Preferred qualifications: 8+ years of industry experience.")[:2] == (None, None)

    def test_ignores_company_age(self) -> None:
        assert extract_experience("For 25 years of experience serving customers we...")[:2] == (None, None)

    def test_detect_timezones_requires_temporal_context(self) -> None:
        zones, _, _ = detect_timezones("We are a central team building products.")
        assert zones == set()


class TestAiMerge:
    def test_ai_cannot_override_deterministic_fail(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        job = make_job(location="Remote", description="Candidates must reside in the United States.")
        base = evaluate(job, policy)
        merged = apply_intelligence(base, intel(permitted_countries=["India"], residency_requirement=None), policy)
        assert merged.status is EligibilityStatus.INELIGIBLE
        assert merged.rule(RuleName.EXCLUDED_REGION).source is RuleSource.DETERMINISTIC

    def test_ai_resolves_unknown_to_fail(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        job = make_job(location="Remote", description="Great team, great culture.")
        base = evaluate(job, policy)
        assert base.rule(RuleName.LOCATION).outcome is RuleOutcome.UNKNOWN
        merged = apply_intelligence(base, intel(permitted_countries=["United States"]), policy)
        assert merged.rule(RuleName.LOCATION).outcome is RuleOutcome.FAIL
        assert merged.rule(RuleName.LOCATION).source is RuleSource.AI
        assert merged.status is EligibilityStatus.INELIGIBLE

    def test_ai_resolves_unknown_to_pass(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        job = make_job(location="Remote", description="Great team.")
        merged = apply_intelligence(
            evaluate(job, policy),
            intel(permitted_countries=["India", "Germany"], minimum_experience=4),
            policy,
        )
        assert merged.rule(RuleName.LOCATION).outcome is RuleOutcome.PASS
        assert merged.rule(RuleName.EXPERIENCE).outcome is RuleOutcome.PASS
        assert merged.status is EligibilityStatus.ELIGIBLE

    def test_low_confidence_ai_is_ignored(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        base = evaluate(make_job(location="Remote", description="Great team."), policy)
        merged = apply_intelligence(base, intel(permitted_countries=["United States"], confidence=0.3), policy)
        assert merged == base

    def test_ai_never_changes_deterministic_pass(self, make_job: JobFactory, policy: EligibilityPolicy) -> None:
        base = evaluate(make_job(), policy)
        merged = apply_intelligence(base, intel(remote_policy=RemotePolicy.ONSITE), policy)
        assert merged.rule(RuleName.WORK_MODEL).outcome is RuleOutcome.PASS


class TestPolicyYaml:
    def test_loads_document_example(self) -> None:
        policy = load_policy_yaml(
            """
allowed_locations: [India, Worldwide]
allowed_work_models: [remote]
allowed_timezones: [IST, GMT, BST, CET, EET]
experience: {min: 3, max: 6}
excluded_regions: [US-only, Canada-only]
""",
        )
        assert policy.experience.max == 6
        assert policy.allowed_work_models == [RemotePolicy.REMOTE]

    @pytest.mark.parametrize("bad", ["- just\n- a list", "experience: {min: 9, max: 2}", "!!python/object:os.system x"])
    def test_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(ConfigurationError):
            load_policy_yaml(bad)
