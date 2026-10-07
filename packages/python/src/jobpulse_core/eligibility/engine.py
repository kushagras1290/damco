"""Deterministic eligibility engine.

Contract
--------
* Every rule yields PASS / FAIL / UNKNOWN with human-readable evidence.
* PASS and FAIL require *positive evidence* found in the posting; absence of
  evidence is UNKNOWN.
* AI enrichment may only resolve UNKNOWN rules (see :func:`apply_intelligence`).
  A deterministic FAIL is final: an LLM can never make an ineligible job eligible.
* A job is INELIGIBLE iff any rule FAILs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum

from jobpulse_core.domain.models import (
    EligibilityPolicy,
    EligibilityStatus,
    JobIntelligence,
    NormalizedJob,
    RemotePolicy,
)
from jobpulse_core.eligibility import geo

AI_MIN_CONFIDENCE = 0.6
MAX_PLAUSIBLE_YEARS = 20
EVIDENCE_WINDOW = 80


class RuleName(StrEnum):
    WORK_MODEL = "work_model"
    EXCLUDED_REGION = "excluded_region"
    LOCATION = "location"
    EXPERIENCE = "experience"
    TIMEZONE = "timezone"


class RuleOutcome(StrEnum):
    PASS = "pass"  # noqa: S105 - rule outcome, not a credential
    FAIL = "fail"
    UNKNOWN = "unknown"


class RuleSource(StrEnum):
    DETERMINISTIC = "deterministic"
    AI = "ai"


@dataclass(frozen=True, slots=True)
class RuleResult:
    rule: RuleName
    outcome: RuleOutcome
    evidence: str
    source: RuleSource = RuleSource.DETERMINISTIC

    def to_dict(self) -> dict[str, str]:
        return {
            "rule": self.rule.value,
            "outcome": self.outcome.value,
            "evidence": self.evidence,
            "source": self.source.value,
        }


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    status: EligibilityStatus
    rules: tuple[RuleResult, ...]
    policy_hash: str
    unresolved: tuple[RuleName, ...] = field(default=())

    @property
    def eligible(self) -> bool:
        return self.status is EligibilityStatus.ELIGIBLE

    @property
    def needs_enrichment(self) -> bool:
        return self.eligible and bool(self.unresolved)

    def rule(self, name: RuleName) -> RuleResult:
        return next(result for result in self.rules if result.rule is name)

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "policy_hash": self.policy_hash,
            "unresolved": [name.value for name in self.unresolved],
            "rules": [rule.to_dict() for rule in self.rules],
        }


# --------------------------------------------------------------------------- patterns

_COUNTRY_GROUP = (
    r"(?:the\s+)?(?P<place>u\.?s\.?a?\.?|united\s+states(?:\s+of\s+america)?|america|canada|"
    r"u\.?k\.?|united\s+kingdom|europe|eu|[A-Z][a-z]+)"
)

RESIDENCY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"\bmust\s+(?:currently\s+)?(?:reside|live|be\s+(?:based|located))\s+in\s+{_COUNTRY_GROUP}", re.I),
    re.compile(
        rf"\b(?:only\s+)?(?:open\s+to|accepting|considering)\s+(?:candidates|applicants)\s+(?:who\s+(?:are|reside)\s+)?(?:based\s+|located\s+|residing\s+)?in\s+{_COUNTRY_GROUP}",
        re.I,
    ),
    re.compile(rf"\b(?:legally\s+)?authori[sz]ed\s+to\s+work\s+in\s+{_COUNTRY_GROUP}", re.I),
    re.compile(rf"\b{_COUNTRY_GROUP}[\s-]+(?:only|based\s+only|residents?\s+only|citizens?\s+only)\b", re.I),
    re.compile(r"\b(?P<place>u\.?s\.?)\s+citizenship\s+(?:is\s+)?required\b", re.I),
    re.compile(r"\b(?:active\s+)?(?P<place>u\.?s\.?)\s+(?:security\s+)?clearance\b", re.I),
)

EXPERIENCE_RE = re.compile(
    r"(?P<min>\d{1,2}(?:\.\d)?)\s*(?:\+|plus)?\s*(?:(?:-|\u2013|\u2014|to)\s*(?P<max>\d{1,2}(?:\.\d)?)\s*\+?)?\s*"
    r"(?:years?|yrs?)(?:\s+of)?(?:\s+\w+){0,6}?\s+(?:experience|exp\b)",
    re.I,
)
PREFERRED_LOOKBEHIND_CHARS = 400
PREFERRED_LOOKAHEAD_CHARS = 60
PREFERRED_MARKER_RE = re.compile(r"preferred|nice[\s-]to[\s-]have|bonus points|desirable|ideally")
REQUIRED_MARKER_RE = re.compile(
    r"required|requirements|must have|you must|minimum qualifications|basic qualifications|you(?:'ll| will) need"
)
TRAILING_PREFERRED_RE = re.compile(r"^[^.\n]{0,40}\b(?:is a plus|a plus|preferred|nice to have|is a bonus)\b")
EXPERIENCE_MIN_PHRASE_RE = re.compile(
    r"(?:at\s+least|minimum(?:\s+of)?|min\.?)\s+(?P<min>\d{1,2})\s*(?:years?|yrs?)",
    re.I,
)

TZ_TOKENS: dict[str, str] = {
    "pst": "PST",
    "pdt": "PST",
    "pt": "PST",
    "pacific": "PST",
    "est": "EST",
    "edt": "EST",
    "et": "EST",
    "eastern": "EST",
    "cst": "CST",
    "cdt": "CST",
    "central": "CST",
    "mst": "MST",
    "mdt": "MST",
    "mountain": "MST",
    "gmt": "GMT",
    "utc": "GMT",
    "bst": "BST",
    "cet": "CET",
    "cest": "CET",
    "eet": "EET",
    "eest": "EET",
    "ist": "IST",
    "sgt": "SGT",
    "aest": "AEST",
    "jst": "JST",
}
TZ_REGIONAL_PHRASES: dict[str, str] = {
    "us hours": "EST",
    "us business hours": "EST",
    "us time zones": "EST",
    "us timezones": "EST",
    "north american time zones": "EST",
    "european hours": "CET",
    "european time zones": "CET",
    "uk hours": "GMT",
}
TZ_CONTEXT_RE = re.compile(
    r"(?P<before>[^.\n]{0,60})\b(?P<tz>PST|PDT|PT|EST|EDT|ET|CST|CDT|MST|MDT|GMT|UTC|BST|CET|CEST|EET|EEST|IST|SGT|AEST|JST|"
    r"pacific|eastern|central|mountain)\b(?P<after>[^.\n]{0,40})",
    re.I,
)
TZ_TIME_WORDS_RE = re.compile(r"time\s*zone|timezone|hours|overlap|working\s+hours|business\s+hours|\btime\b", re.I)
TZ_STRICT_WORDS_RE = re.compile(r"\b(must|required|requires|need\s+to|only|strictly|mandatory)\b", re.I)
# Offsets used to accept nearby zones (within 3.5h of an allowed zone).
TZ_OFFSETS: dict[str, float] = {
    "PST": -8,
    "MST": -7,
    "CST": -6,
    "EST": -5,
    "GMT": 0,
    "BST": 1,
    "CET": 1,
    "EET": 2,
    "IST": 5.5,
    "SGT": 8,
    "JST": 9,
    "AEST": 10,
}
TZ_NEARBY_HOURS = 3.5


def policy_hash(policy: EligibilityPolicy) -> str:
    return hashlib.sha256(policy.model_dump_json().encode("utf-8")).hexdigest()


def _snippet(text: str, start: int, end: int) -> str:
    lo = max(0, start - EVIDENCE_WINDOW // 2)
    hi = min(len(text), end + EVIDENCE_WINDOW // 2)
    return " ".join(text[lo:hi].split())


def _excluded_countries(policy: EligibilityPolicy) -> set[str]:
    excluded: set[str] = set()
    for entry in policy.excluded_regions:
        name = re.sub(r"[-\s]*only$", "", entry.strip(), flags=re.I)
        excluded.add(geo.canonical_country(name) or name.lower())
    return excluded


def _place_to_country(place: str) -> str | None:
    cleaned = place.strip().lower().replace(" of america", "")
    return geo.canonical_country(cleaned) or geo.canonical_country(cleaned.replace(".", ""))


# --------------------------------------------------------------------------- rules


def _rule_work_model(job: NormalizedJob, policy: EligibilityPolicy) -> RuleResult:
    if job.remote_policy is RemotePolicy.UNKNOWN:
        return RuleResult(RuleName.WORK_MODEL, RuleOutcome.UNKNOWN, "work model not stated")
    allowed = set(policy.allowed_work_models)
    outcome = RuleOutcome.PASS if job.remote_policy in allowed else RuleOutcome.FAIL
    return RuleResult(
        RuleName.WORK_MODEL,
        outcome,
        f"job is {job.remote_policy.value}; allowed: {sorted(m.value for m in allowed)}",
    )


def _rule_excluded_region(job: NormalizedJob, policy: EligibilityPolicy) -> RuleResult:
    allowed_countries, _ = geo.expand_allowed(policy.allowed_locations)
    excluded = _excluded_countries(policy)
    text = job.description_text
    for pattern in RESIDENCY_PATTERNS:
        for match in pattern.finditer(text):
            country = _place_to_country(match.group("place"))
            if country is None or country in allowed_countries:
                continue
            evidence = _snippet(text, match.start(), match.end())
            if country in excluded:
                return RuleResult(
                    RuleName.EXCLUDED_REGION, RuleOutcome.FAIL, f"excluded region '{country}': \"{evidence}\""
                )
            return RuleResult(
                RuleName.EXCLUDED_REGION, RuleOutcome.FAIL, f"residency restricted to '{country}': \"{evidence}\""
            )
    return RuleResult(RuleName.EXCLUDED_REGION, RuleOutcome.UNKNOWN, "no residency restriction detected")


def _rule_location(job: NormalizedJob, policy: EligibilityPolicy) -> RuleResult:
    allowed_countries, worldwide_ok = geo.expand_allowed(policy.allowed_locations)
    location = job.location or ""
    if not location.strip():
        return RuleResult(RuleName.LOCATION, RuleOutcome.UNKNOWN, "no location listed")

    regions = geo.regions_in(location)
    countries = geo.countries_in(location)
    if countries & allowed_countries:
        hit = sorted(countries & allowed_countries)
        return RuleResult(RuleName.LOCATION, RuleOutcome.PASS, f"location '{location}' includes allowed {hit}")
    if "worldwide" in regions:
        if worldwide_ok:
            return RuleResult(RuleName.LOCATION, RuleOutcome.PASS, f"location '{location}' is worldwide")
        return RuleResult(RuleName.LOCATION, RuleOutcome.UNKNOWN, "worldwide role but policy lacks 'Worldwide'")
    matching_regions = sorted(r for r in regions if geo.region_includes_any(r, allowed_countries))
    if matching_regions:
        return RuleResult(
            RuleName.LOCATION, RuleOutcome.PASS, f"location region {matching_regions} covers allowed countries"
        )
    if countries or regions:
        listed = sorted(countries | regions)
        return RuleResult(RuleName.LOCATION, RuleOutcome.FAIL, f"location '{location}' limited to {listed}")
    return RuleResult(RuleName.LOCATION, RuleOutcome.UNKNOWN, f"location '{location}' not recognised")


def _is_preferred(text: str, start: int, end: int) -> bool:
    """True when the requirement sits under "preferred / nice to have" wording, not a hard requirement.

    Looks back for the nearest section marker (preferred beats required only if it is closer)
    and a short distance ahead for trailing qualifiers like "... is a plus".
    """
    before = text[max(0, start - PREFERRED_LOOKBEHIND_CHARS) : start].lower()
    last_preferred = max((m.end() for m in PREFERRED_MARKER_RE.finditer(before)), default=-1)
    last_required = max((m.end() for m in REQUIRED_MARKER_RE.finditer(before)), default=-1)
    if last_preferred > last_required:
        return True
    after = text[end : end + PREFERRED_LOOKAHEAD_CHARS].lower()
    return bool(TRAILING_PREFERRED_RE.search(after))


def extract_experience(text: str) -> tuple[float | None, float | None, str]:
    """Return (min_years, max_years, evidence) for the headline *required* experience.

    Preferred/"nice to have" requirements are ignored: they must never hard-reject a job.
    """
    candidates: list[tuple[float, float | None, str]] = []
    for match in EXPERIENCE_RE.finditer(text):
        low = float(match.group("min"))
        high = float(match.group("max")) if match.group("max") else None
        if low > MAX_PLAUSIBLE_YEARS or (high is not None and (high > MAX_PLAUSIBLE_YEARS or high < low)):
            continue
        if _is_preferred(text, match.start(), match.end()):
            continue
        candidates.append((low, high, _snippet(text, match.start(), match.end())))
    for match in EXPERIENCE_MIN_PHRASE_RE.finditer(text):
        low = float(match.group("min"))
        if low <= MAX_PLAUSIBLE_YEARS and not _is_preferred(text, match.start(), match.end()):
            candidates.append((low, None, _snippet(text, match.start(), match.end())))
    if not candidates:
        return None, None, ""
    # Requirements like "5+ years Python, 2+ years Go": the headline requirement is the
    # largest minimum; we pick it to avoid under-estimating seniority.
    low, high, evidence = max(candidates, key=lambda item: item[0])
    return low, high, evidence


def _experience_outcome(
    low: float | None,
    high: float | None,
    policy: EligibilityPolicy,
) -> tuple[RuleOutcome, str]:
    tolerance = policy.experience_tolerance_years
    bounds = policy.experience
    if low is not None and low > bounds.max + tolerance:
        return RuleOutcome.FAIL, f"requires {low:g}+ years; policy max {bounds.max:g} (+{tolerance:g} tolerance)"
    if high is not None and high < bounds.min - tolerance:
        return RuleOutcome.FAIL, f"targets up to {high:g} years; policy min {bounds.min:g} (-{tolerance:g} tolerance)"
    if low is None and high is None:
        return RuleOutcome.UNKNOWN, "no experience requirement stated"
    span = f"{low:g}" if low is not None else "?"
    span += f"-{high:g}" if high is not None else "+"
    return RuleOutcome.PASS, f"requires {span} years; policy {bounds.min:g}-{bounds.max:g}"


def _rule_experience(job: NormalizedJob, policy: EligibilityPolicy) -> RuleResult:
    low, high, evidence = extract_experience(job.description_text)
    outcome, reason = _experience_outcome(low, high, policy)
    detail = f'{reason}: "{evidence}"' if evidence else reason
    return RuleResult(RuleName.EXPERIENCE, outcome, detail)


def _tz_allowed(zone: str, allowed: set[str]) -> bool:
    if zone in allowed:
        return True
    offset = TZ_OFFSETS.get(zone)
    if offset is None:
        return False
    return any(abs(offset - TZ_OFFSETS[other]) <= TZ_NEARBY_HOURS for other in allowed if other in TZ_OFFSETS)


def detect_timezones(text: str) -> tuple[set[str], bool, str]:
    """Return (zones, strict, evidence) for timezone requirements mentioned in ``text``."""
    zones: set[str] = set()
    strict = False
    evidence = ""
    lowered = text.lower()
    for phrase, zone in TZ_REGIONAL_PHRASES.items():
        idx = lowered.find(phrase)
        if idx >= 0:
            zones.add(zone)
            window = lowered[max(0, idx - 60) : idx + len(phrase)]
            strict = strict or bool(TZ_STRICT_WORDS_RE.search(window))
            evidence = evidence or _snippet(text, idx, idx + len(phrase))
    for match in TZ_CONTEXT_RE.finditer(text):
        token = match.group("tz")
        context = f"{match.group('before')} {match.group('after')}"
        # Bare words like "central" or "ET" need temporal context to count.
        if not TZ_TIME_WORDS_RE.search(context):
            continue
        mapped = TZ_TOKENS.get(token.lower())
        if mapped is None:
            continue
        zones.add(mapped)
        strict = strict or bool(TZ_STRICT_WORDS_RE.search(context))
        evidence = evidence or _snippet(text, match.start(), match.end())
    return zones, strict, evidence


def _rule_timezone(job: NormalizedJob, policy: EligibilityPolicy) -> RuleResult:
    allowed = {zone.upper() for zone in policy.allowed_timezones}
    zones, strict, evidence = detect_timezones(job.description_text)
    if not zones:
        return RuleResult(RuleName.TIMEZONE, RuleOutcome.UNKNOWN, "no timezone requirement detected")
    compatible = sorted(zone for zone in zones if _tz_allowed(zone, allowed))
    if compatible:
        return RuleResult(RuleName.TIMEZONE, RuleOutcome.PASS, f'compatible timezone {compatible}: "{evidence}"')
    if strict:
        return RuleResult(RuleName.TIMEZONE, RuleOutcome.FAIL, f'requires {sorted(zones)}: "{evidence}"')
    return RuleResult(RuleName.TIMEZONE, RuleOutcome.UNKNOWN, f"mentions {sorted(zones)} without strict requirement")


RULES = (_rule_work_model, _rule_excluded_region, _rule_location, _rule_experience, _rule_timezone)


def _summarise(rules: tuple[RuleResult, ...], phash: str) -> EligibilityResult:
    failed = any(rule.outcome is RuleOutcome.FAIL for rule in rules)
    unresolved = tuple(rule.rule for rule in rules if rule.outcome is RuleOutcome.UNKNOWN)
    status = EligibilityStatus.INELIGIBLE if failed else EligibilityStatus.ELIGIBLE
    return EligibilityResult(status=status, rules=rules, policy_hash=phash, unresolved=unresolved)


def evaluate(job: NormalizedJob, policy: EligibilityPolicy) -> EligibilityResult:
    """Run every deterministic rule. Pure and side-effect free."""
    return _summarise(tuple(rule(job, policy) for rule in RULES), policy_hash(policy))


# --------------------------------------------------------------------------- AI merge


def _ai_rule(intel: JobIntelligence, name: RuleName, policy: EligibilityPolicy) -> RuleResult | None:
    allowed_countries, worldwide_ok = geo.expand_allowed(policy.allowed_locations)
    excluded = _excluded_countries(policy)
    permitted = {geo.canonical_country(c) or c.lower() for c in intel.permitted_countries}
    banned = {geo.canonical_country(c) or c.lower() for c in intel.excluded_countries}

    match name:
        case RuleName.WORK_MODEL:
            if intel.remote_policy is RemotePolicy.UNKNOWN:
                return None
            ok = intel.remote_policy in set(policy.allowed_work_models)
            return RuleResult(
                name,
                RuleOutcome.PASS if ok else RuleOutcome.FAIL,
                f"AI: work model {intel.remote_policy.value}",
                RuleSource.AI,
            )
        case RuleName.EXCLUDED_REGION:
            if intel.residency_requirement:
                country = _place_to_country(intel.residency_requirement)
                if country and country not in allowed_countries:
                    return RuleResult(
                        name,
                        RuleOutcome.FAIL,
                        f"AI: residency required in {intel.residency_requirement}",
                        RuleSource.AI,
                    )
            if permitted and permitted <= excluded:
                return RuleResult(name, RuleOutcome.FAIL, f"AI: only {sorted(permitted)} permitted", RuleSource.AI)
            if allowed_countries & banned:
                return RuleResult(
                    name, RuleOutcome.FAIL, f"AI: excludes {sorted(allowed_countries & banned)}", RuleSource.AI
                )
            return RuleResult(name, RuleOutcome.PASS, "AI: no conflicting residency restriction", RuleSource.AI)
        case RuleName.LOCATION:
            if not permitted:
                return None
            if "worldwide" in permitted or "anywhere" in permitted:
                outcome = RuleOutcome.PASS if worldwide_ok else RuleOutcome.UNKNOWN
                return (
                    RuleResult(name, outcome, "AI: worldwide", RuleSource.AI) if outcome is RuleOutcome.PASS else None
                )
            ok = bool(permitted & allowed_countries)
            return RuleResult(
                name,
                RuleOutcome.PASS if ok else RuleOutcome.FAIL,
                f"AI: permitted countries {sorted(permitted)}",
                RuleSource.AI,
            )
        case RuleName.EXPERIENCE:
            if intel.minimum_experience is None and intel.maximum_experience is None:
                return None
            outcome, reason = _experience_outcome(intel.minimum_experience, intel.maximum_experience, policy)
            return RuleResult(name, outcome, f"AI: {reason}", RuleSource.AI)
        case RuleName.TIMEZONE:
            zones = {TZ_TOKENS.get(z.strip().lower(), z.strip().upper()) for z in intel.timezone_requirements}
            if not zones:
                return None
            allowed = {zone.upper() for zone in policy.allowed_timezones}
            ok = any(_tz_allowed(zone, allowed) for zone in zones)
            return RuleResult(
                name,
                RuleOutcome.PASS if ok else RuleOutcome.FAIL,
                f"AI: timezone requirements {sorted(zones)}",
                RuleSource.AI,
            )
    return None


def apply_intelligence(
    result: EligibilityResult,
    intel: JobIntelligence,
    policy: EligibilityPolicy,
) -> EligibilityResult:
    """Resolve UNKNOWN rules using AI-extracted facts.

    Deterministic PASS/FAIL results are never altered: hard constraints win.
    Low-confidence extractions are ignored entirely.
    """
    if intel.confidence < AI_MIN_CONFIDENCE:
        return result
    merged: list[RuleResult] = []
    for rule in result.rules:
        if rule.outcome is not RuleOutcome.UNKNOWN:
            merged.append(rule)
            continue
        resolved = _ai_rule(intel, rule.rule, policy)
        merged.append(resolved if resolved is not None else replace(rule))
    return _summarise(tuple(merged), result.policy_hash)
