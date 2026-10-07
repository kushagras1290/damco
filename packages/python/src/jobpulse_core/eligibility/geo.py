"""Tiny gazetteer for deterministic location reasoning.

Intentionally small and explicit: it only needs to recognise the countries and regions
that commonly appear in job postings. Unrecognised locations yield UNKNOWN, never FAIL.
"""

from __future__ import annotations

import re
from functools import lru_cache

_EU = "europe"
_EMEA = "emea"
_APAC = "apac"
_ASIA = "asia"
_NA = "north america"
_AM = "americas"
_LATAM = "latam"

# country -> (aliases incl. major cities/provinces, regions it belongs to)
COUNTRIES: dict[str, tuple[tuple[str, ...], frozenset[str]]] = {
    "united states": (
        ("united states", "usa", "u.s.a", "u.s.", "us", "america", "us-based", "us based"),
        frozenset({_NA, _AM}),
    ),
    "canada": (
        (
            "canada",
            "canadian",
            "ontario",
            "quebec",
            "british columbia",
            "alberta",
            "toronto",
            "vancouver",
            "montreal",
            "ottawa",
            "calgary",
        ),
        frozenset({_NA, _AM}),
    ),
    "mexico": (("mexico", "mexico city", "guadalajara"), frozenset({_NA, _AM, _LATAM})),
    "brazil": (("brazil", "brasil", "sao paulo", "são paulo"), frozenset({_AM, _LATAM})),
    "argentina": (("argentina", "buenos aires"), frozenset({_AM, _LATAM})),
    "colombia": (("colombia", "bogota", "bogotá", "medellin"), frozenset({_AM, _LATAM})),
    "chile": (("chile", "santiago"), frozenset({_AM, _LATAM})),
    "peru": (("peru", "lima"), frozenset({_AM, _LATAM})),
    "costa rica": (("costa rica",), frozenset({_AM, _LATAM})),
    "uruguay": (("uruguay", "montevideo"), frozenset({_AM, _LATAM})),
    "united kingdom": (
        (
            "united kingdom",
            "uk",
            "u.k.",
            "england",
            "scotland",
            "wales",
            "britain",
            "london",
            "manchester",
            "edinburgh",
        ),
        frozenset({_EU, _EMEA}),
    ),
    "ireland": (("ireland", "dublin"), frozenset({_EU, _EMEA})),
    "germany": (("germany", "deutschland", "berlin", "munich", "hamburg", "frankfurt"), frozenset({_EU, _EMEA})),
    "france": (("france", "paris"), frozenset({_EU, _EMEA})),
    "netherlands": (("netherlands", "amsterdam", "rotterdam"), frozenset({_EU, _EMEA})),
    "belgium": (("belgium", "brussels"), frozenset({_EU, _EMEA})),
    "austria": (("austria", "vienna"), frozenset({_EU, _EMEA})),
    "switzerland": (("switzerland", "zurich", "geneva"), frozenset({_EU, _EMEA})),
    "spain": (("spain", "madrid", "barcelona"), frozenset({_EU, _EMEA})),
    "portugal": (("portugal", "lisbon", "porto"), frozenset({_EU, _EMEA})),
    "italy": (("italy", "milan", "rome"), frozenset({_EU, _EMEA})),
    "denmark": (("denmark", "copenhagen"), frozenset({_EU, _EMEA})),
    "sweden": (("sweden", "stockholm"), frozenset({_EU, _EMEA})),
    "norway": (("norway", "oslo"), frozenset({_EU, _EMEA})),
    "finland": (("finland", "helsinki"), frozenset({_EU, _EMEA})),
    "poland": (("poland", "warsaw", "krakow"), frozenset({_EU, _EMEA})),
    "czechia": (("czechia", "czech republic", "prague"), frozenset({_EU, _EMEA})),
    "hungary": (("hungary", "budapest"), frozenset({_EU, _EMEA})),
    "romania": (("romania", "bucharest"), frozenset({_EU, _EMEA})),
    "bulgaria": (("bulgaria", "sofia"), frozenset({_EU, _EMEA})),
    "greece": (("greece", "athens"), frozenset({_EU, _EMEA})),
    "estonia": (("estonia", "tallinn"), frozenset({_EU, _EMEA})),
    "ukraine": (("ukraine", "kyiv"), frozenset({_EU, _EMEA})),
    "serbia": (("serbia", "belgrade"), frozenset({_EU, _EMEA})),
    "turkey": (("turkey", "türkiye", "istanbul"), frozenset({_EMEA})),
    "israel": (("israel", "tel aviv"), frozenset({_EMEA, _ASIA})),
    "united arab emirates": (("united arab emirates", "uae", "dubai", "abu dhabi"), frozenset({_EMEA, _ASIA})),
    "saudi arabia": (("saudi arabia", "riyadh"), frozenset({_EMEA, _ASIA})),
    "south africa": (("south africa", "cape town", "johannesburg"), frozenset({_EMEA})),
    "nigeria": (("nigeria", "lagos"), frozenset({_EMEA})),
    "kenya": (("kenya", "nairobi"), frozenset({_EMEA})),
    "egypt": (("egypt", "cairo"), frozenset({_EMEA})),
    "india": (
        (
            "india",
            "bangalore",
            "bengaluru",
            "mumbai",
            "delhi",
            "new delhi",
            "gurgaon",
            "gurugram",
            "noida",
            "pune",
            "hyderabad",
            "chennai",
            "kolkata",
            "ahmedabad",
        ),
        frozenset({_APAC, _ASIA}),
    ),
    "pakistan": (("pakistan", "karachi", "lahore"), frozenset({_APAC, _ASIA})),
    "bangladesh": (("bangladesh", "dhaka"), frozenset({_APAC, _ASIA})),
    "sri lanka": (("sri lanka", "colombo"), frozenset({_APAC, _ASIA})),
    "singapore": (("singapore",), frozenset({_APAC, _ASIA})),
    "malaysia": (("malaysia", "kuala lumpur"), frozenset({_APAC, _ASIA})),
    "thailand": (("thailand", "bangkok"), frozenset({_APAC, _ASIA})),
    "vietnam": (("vietnam", "ho chi minh", "hanoi"), frozenset({_APAC, _ASIA})),
    "indonesia": (("indonesia", "jakarta"), frozenset({_APAC, _ASIA})),
    "philippines": (("philippines", "manila"), frozenset({_APAC, _ASIA})),
    "china": (("china", "beijing", "shanghai", "shenzhen"), frozenset({_APAC, _ASIA})),
    "hong kong": (("hong kong",), frozenset({_APAC, _ASIA})),
    "taiwan": (("taiwan", "taipei"), frozenset({_APAC, _ASIA})),
    "south korea": (("south korea", "korea", "seoul"), frozenset({_APAC, _ASIA})),
    "japan": (("japan", "tokyo", "osaka"), frozenset({_APAC, _ASIA})),
    "australia": (("australia", "sydney", "melbourne", "brisbane"), frozenset({_APAC})),
    "new zealand": (("new zealand", "auckland"), frozenset({_APAC})),
}

COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {country: aliases for country, (aliases, _) in COUNTRIES.items()}

# Upper-case country codes matched case-sensitively ("CAN" is Canada; "can" is a verb).
COUNTRY_CODES: dict[str, str] = {
    "CAN": "canada",
    "USA": "united states",
    "UK": "united kingdom",
    "UAE": "united arab emirates",
}


def _derive_region_members() -> dict[str, frozenset[str]]:
    members: dict[str, set[str]] = {}
    for country, (_, regions) in COUNTRIES.items():
        for region in regions:
            members.setdefault(region, set()).add(country)
    return {"worldwide": frozenset({"*"}), **{region: frozenset(names) for region, names in members.items()}}


REGION_MEMBERS: dict[str, frozenset[str]] = _derive_region_members()

REGION_ALIASES: dict[str, str] = {
    "worldwide": "worldwide",
    "anywhere": "worldwide",
    "global": "worldwide",
    "globally": "worldwide",
    "north america": "north america",
    "americas": "americas",
    "latam": "latam",
    "latin america": "latam",
    "europe": "europe",
    "eu": "europe",
    "european union": "europe",
    "eea": "europe",
    "dach": "europe",
    "nordics": "europe",
    "benelux": "europe",
    "emea": "emea",
    "apac": "apac",
    "asia pacific": "apac",
    "asia-pacific": "apac",
    "asia": "asia",
}

US_STATE_CODES: frozenset[str] = frozenset(
    [
        "AL",
        "AK",
        "AZ",
        "AR",
        "CA",
        "CO",
        "CT",
        "DE",
        "FL",
        "GA",
        "HI",
        "ID",
        "IL",
        "IN",
        "IA",
        "KS",
        "KY",
        "LA",
        "ME",
        "MD",
        "MA",
        "MI",
        "MN",
        "MS",
        "MO",
        "MT",
        "NE",
        "NV",
        "NH",
        "NJ",
        "NM",
        "NY",
        "NC",
        "ND",
        "OH",
        "OK",
        "OR",
        "PA",
        "RI",
        "SC",
        "SD",
        "TN",
        "TX",
        "UT",
        "VT",
        "VA",
        "WA",
        "WV",
        "WI",
        "WY",
        "DC",
    ],
)
US_CITIES: tuple[str, ...] = (
    "san francisco",
    "new york",
    "nyc",
    "seattle",
    "austin",
    "boston",
    "chicago",
    "los angeles",
    "denver",
    "atlanta",
    "miami",
    "washington dc",
    "palo alto",
    "mountain view",
    "menlo park",
    "sunnyvale",
    "san jose",
    "san diego",
    "portland",
    "philadelphia",
)
_STATE_RE = re.compile(r",\s*([A-Z]{2})\b")


@lru_cache(maxsize=1)
def _alias_patterns() -> tuple[tuple[str, re.Pattern[str]], ...]:
    patterns: list[tuple[str, re.Pattern[str]]] = []
    for country, aliases in COUNTRY_ALIASES.items():
        escaped = "|".join(re.escape(alias) for alias in sorted(aliases, key=len, reverse=True))
        patterns.append((country, re.compile(rf"(?<![a-z]){escaped}(?![a-z])", re.IGNORECASE)))
    return tuple(patterns)


def canonical_country(name: str) -> str | None:
    lowered = name.strip().lower()
    for country, aliases in COUNTRY_ALIASES.items():
        if lowered == country or lowered in aliases:
            return country
    return None


def canonical_region(name: str) -> str | None:
    return REGION_ALIASES.get(name.strip().lower())


def countries_in(text: str | None) -> set[str]:
    """Countries mentioned in ``text`` (aliases, major cities, US state codes)."""
    if not text:
        return set()
    found = {country for country, pattern in _alias_patterns() if pattern.search(text)}
    found |= {
        country for code, country in COUNTRY_CODES.items() if re.search(rf"(?<![A-Za-z]){code}(?![A-Za-z])", text)
    }
    if any(match in US_STATE_CODES for match in _STATE_RE.findall(text)):
        found.add("united states")
    lowered = text.lower()
    if any(city in lowered for city in US_CITIES):
        found.add("united states")
    return found


def regions_in(text: str | None) -> set[str]:
    if not text:
        return set()
    lowered = text.lower()
    return {
        region
        for alias, region in REGION_ALIASES.items()
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", lowered)
    }


def expand_allowed(allowed: list[str]) -> tuple[set[str], bool]:
    """Policy ``allowed_locations`` -> (allowed country set, worldwide_ok)."""
    countries: set[str] = set()
    worldwide = False
    for entry in allowed:
        region = canonical_region(entry)
        if region == "worldwide":
            worldwide = True
        elif region is not None:
            countries |= REGION_MEMBERS[region]
        else:
            countries.add(canonical_country(entry) or entry.strip().lower())
    return countries, worldwide


def region_includes_any(region: str, countries: set[str]) -> bool:
    members = REGION_MEMBERS.get(region, frozenset())
    return "*" in members or bool(members & countries)
