from __future__ import annotations

from collections.abc import Callable

import pytest
from hypothesis import given
from hypothesis import strategies as st

from jobpulse_core.domain.models import NormalizedJob, RemotePolicy, Seniority
from jobpulse_core.errors import ValidationError
from jobpulse_core.ingestion.html import html_to_text, sanitize_html
from jobpulse_core.ingestion.normalize import (
    compute_fingerprint,
    infer_remote_policy,
    infer_seniority,
    normalize_location,
    normalize_title,
)
from jobpulse_core.ingestion.urls import canonicalize_url, registrable_domain


class TestCanonicalUrl:
    def test_strips_tracking_params_and_fragment(self) -> None:
        url = "http://WWW.Example.com:80/jobs/123/?utm_source=x&gh_src=y&b=2&a=1#apply"
        assert canonicalize_url(url) == "https://example.com/jobs/123?a=1&b=2"

    def test_root_path_kept(self) -> None:
        assert canonicalize_url("https://example.com") == "https://example.com/"

    def test_non_default_port_kept(self) -> None:
        assert canonicalize_url("https://example.com:8443/a") == "https://example.com:8443/a"

    @pytest.mark.parametrize("bad", ["ftp://example.com/a", "javascript:alert(1)", "https:///nohost"])
    def test_rejects_unsupported(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            canonicalize_url(bad)

    @given(
        path=st.lists(st.text(alphabet="abcdefghij0123456789-", min_size=1, max_size=8), max_size=4),
        params=st.dictionaries(
            st.sampled_from(["a", "b", "utm_source", "q"]), st.text(alphabet="xyz", min_size=1, max_size=3)
        ),
    )
    def test_idempotent(self, path: list[str], params: dict[str, str]) -> None:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"https://Example.com/{'/'.join(path)}" + (f"?{query}" if query else "")
        once = canonicalize_url(url)
        assert canonicalize_url(once) == once

    def test_registrable_domain(self) -> None:
        assert registrable_domain("https://jobs.boards.acme.io/x") == "acme.io"
        assert registrable_domain("careers.acme.co.uk") == "acme.co.uk"


class TestTitleAndLocation:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Senior AI Engineer", "senior artificial intelligence engineer"),
            ("Sr AI Engineer", "senior artificial intelligence engineer"),
            ("Sr. AI Engineer (Remote)", "senior artificial intelligence engineer"),
            ("Senior Artificial Intelligence Engineer", "senior artificial intelligence engineer"),
        ],
    )
    def test_near_duplicate_titles_converge(self, title: str, expected: str) -> None:
        assert normalize_title(title) == expected

    def test_location_aliases(self) -> None:
        assert normalize_location("USA") == "united states"
        assert normalize_location("Bengaluru, India") == "bangalore, india"
        assert normalize_location("  ") is None

    def test_fingerprint_stable_across_aliases(self) -> None:
        a = compute_fingerprint("jobs.acme.io", normalize_title("Sr AI Engineer"), normalize_location("Anywhere"))
        b = compute_fingerprint("acme.io", normalize_title("Senior AI Engineer"), normalize_location("Global"))
        assert a == b


class TestInference:
    @pytest.mark.parametrize(
        ("texts", "hint", "expected"),
        [
            (("Remote - Worldwide",), None, RemotePolicy.REMOTE),
            (("Hybrid, 3 days a week in-office",), None, RemotePolicy.HYBRID),
            (("Must work on-site in Berlin",), None, RemotePolicy.ONSITE),
            (("Berlin",), None, RemotePolicy.UNKNOWN),
            (("Berlin",), True, RemotePolicy.REMOTE),
            (("Berlin",), False, RemotePolicy.ONSITE),
        ],
    )
    def test_remote_policy(self, texts: tuple[str, ...], hint: bool | None, expected: RemotePolicy) -> None:
        assert infer_remote_policy(*texts, remote_hint=hint) is expected

    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Staff Engineer", Seniority.STAFF),
            ("Sr. Backend Developer", Seniority.SENIOR),
            ("Junior Data Analyst", Seniority.JUNIOR),
            ("Engineering Manager", Seniority.MANAGER),
            ("Software Engineer", Seniority.UNKNOWN),
        ],
    )
    def test_seniority(self, title: str, expected: Seniority) -> None:
        assert infer_seniority(title) is expected


class TestHtml:
    def test_sanitize_removes_scripts_and_handlers(self) -> None:
        dirty = '<p onclick="x()">Hi<script>alert(1)</script><a href="javascript:evil()">x</a></p>'
        clean = sanitize_html(dirty)
        assert "script" not in clean
        assert "onclick" not in clean
        assert "javascript:" not in clean

    def test_sanitize_unescapes_entity_encoded_html(self) -> None:
        assert "<p>" in sanitize_html("&lt;p&gt;Hello&lt;/p&gt;")

    def test_html_to_text_keeps_paragraphs(self) -> None:
        text = html_to_text("<p>One</p><p>Two &amp; three</p><ul><li>a</li><li>b</li></ul>")
        assert "One" in text
        assert "Two & three" in text
        assert "\n" in text


def test_normalize_job_end_to_end(make_job: Callable[..., NormalizedJob]) -> None:
    job = make_job(title="Sr. AI Engineer (Remote)")
    assert job.normalized_title == "senior artificial intelligence engineer"
    assert job.remote_policy is RemotePolicy.REMOTE
    assert job.seniority is Seniority.SENIOR
    assert len(job.content_hash) == 64
    assert make_job(title="Sr. AI Engineer (Remote)").content_hash == job.content_hash
