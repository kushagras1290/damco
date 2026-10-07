"""Board identity: equal boards collapse to one locator, different boards never do."""

from __future__ import annotations

import pytest

from jobpulse_core.domain.models import SourceDefinition, SourceKind
from jobpulse_core.sources.locator import normalize_url, source_locator


def definition(kind: SourceKind, **fields: str) -> SourceDefinition:
    return SourceDefinition(kind=kind, company_name="Acme", company_domain="acme.io", **fields)  # type: ignore[arg-type]


def test_ats_tokens_are_case_insensitive() -> None:
    upper = definition(SourceKind.GREENHOUSE, board_token="Acme")
    lower = definition(SourceKind.GREENHOUSE, board_token="acme ")
    assert source_locator(upper) == source_locator(lower) == "acme"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://Acme.io/careers/", "https://acme.io/careers"),
        ("https://acme.io:443/feed.xml", "https://acme.io/feed.xml"),
        ("https://acme.io/jobs#open", "https://acme.io/jobs"),
    ],
)
def test_cosmetic_url_differences_are_the_same_board(left: str, right: str) -> None:
    assert normalize_url(left) == normalize_url(right)


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("https://acme.io/jobs?team=ai", "https://acme.io/jobs?team=sales"),  # query selects a board
        ("http://acme.io/jobs", "https://acme.io/jobs"),
        ("https://acme.io:8443/jobs", "https://acme.io/jobs"),
        ("https://acme.io/jobs", "https://acme.io/Jobs"),  # paths are case-sensitive
    ],
)
def test_different_boards_stay_distinct(left: str, right: str) -> None:
    assert normalize_url(left) != normalize_url(right)


def test_url_sources_use_the_normalized_url() -> None:
    rss = definition(SourceKind.RSS, url="https://Acme.io/feed/")
    assert source_locator(rss) == "https://acme.io/feed"


def test_demo_board_is_a_singleton() -> None:
    assert source_locator(definition(SourceKind.DEMO)) == "demo"
