"""Offline tests for the JSON-LD parsing pipeline.

No network. We feed synthetic HTML into the soup pipeline and check the
normaliser. Run with:  python -m tests.test_seek_fetch
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow running this file directly via `python tests\test_seek_fetch.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bs4 import BeautifulSoup  # noqa: E402

from core import seek_fetch  # noqa: E402


def _make_html(*json_ld_blocks: str) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{block}</script>' for block in json_ld_blocks
    )
    return f"<html><head>{scripts}</head><body></body></html>"


def _normalize(html: str, url: str = "https://www.seek.com.au/job/test"):
    soup = BeautifulSoup(html, "html.parser")
    posting = seek_fetch._find_best_job_posting(soup)
    if posting is None:
        return None
    return seek_fetch._normalize_posting(posting, url)


# --- Tests --------------------------------------------------------------

def test_clean_seek_like_posting():
    block = """{
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "title": "Marketing Manager",
      "description": "<p>Lead campaigns.</p><ul><li>4+ years</li><li>GA4</li></ul>",
      "hiringOrganization": {"@type": "Organization", "name": "Acme Pty Ltd"},
      "jobLocation": {
        "@type": "Place",
        "address": {
          "@type": "PostalAddress",
          "addressLocality": "Sydney",
          "addressRegion": "NSW",
          "postalCode": "2000",
          "addressCountry": "AU"
        }
      },
      "baseSalary": {
        "@type": "MonetaryAmount",
        "currency": "AUD",
        "value": {"@type": "QuantitativeValue", "minValue": 90000, "maxValue": 110000, "unitText": "YEAR"}
      },
      "employmentType": "FULL_TIME"
    }"""
    job = _normalize(_make_html(block))
    assert job is not None
    assert job["title"] == "Marketing Manager"
    assert job["company"] == "Acme Pty Ltd"
    assert "Sydney" in job["location"] and "NSW" in job["location"] and "2000" in job["location"]
    assert "Lead campaigns." in job["description"]
    assert "4+ years" in job["description"]
    assert "<p>" not in job["description"]  # HTML stripped
    assert "AUD" in job["salary"] and "90000-110000" in job["salary"]
    assert job["employment_type"] == "FULL_TIME"
    assert job["source"] == "url"


def test_picks_best_posting_when_multiple_blocks():
    breadcrumb = '{"@context":"https://schema.org","@type":"BreadcrumbList","itemListElement":[]}'
    sparse_posting = '{"@type":"JobPosting","title":"Sparse"}'
    rich_posting = (
        '{"@type":"JobPosting","title":"Rich",'
        '"description":"<p>x</p>",'
        '"hiringOrganization":{"name":"Foo"},'
        '"jobLocation":{"address":{"addressLocality":"Melbourne"}},'
        '"employmentType":"CONTRACT"}'
    )
    job = _normalize(_make_html(breadcrumb, sparse_posting, rich_posting))
    assert job is not None
    assert job["title"] == "Rich"
    assert job["company"] == "Foo"
    assert job["location"] == "Melbourne"


def test_employment_type_list():
    block = '{"@type":"JobPosting","title":"X","employmentType":["FULL_TIME","CONTRACT"]}'
    job = _normalize(_make_html(block))
    assert job is not None
    assert job["employment_type"] == "FULL_TIME, CONTRACT"


def test_graph_wrapper():
    block = """{
      "@context":"https://schema.org",
      "@graph":[
        {"@type":"WebPage","name":"page"},
        {"@type":"JobPosting","title":"GraphRole","hiringOrganization":{"name":"GraphCo"}}
      ]
    }"""
    job = _normalize(_make_html(block))
    assert job is not None
    assert job["title"] == "GraphRole"
    assert job["company"] == "GraphCo"


def test_no_job_posting_returns_none():
    block = '{"@type":"WebPage","name":"Just a page"}'
    job = _normalize(_make_html(block))
    assert job is None


def test_malformed_json_is_skipped():
    bad = "{not json at all"
    good = '{"@type":"JobPosting","title":"Survivor"}'
    job = _normalize(_make_html(bad, good))
    assert job is not None
    assert job["title"] == "Survivor"


def test_missing_fields_reported_via_public_api(monkeypatch):
    """Use monkeypatch to stub requests.get and exercise fetch_from_url end-to-end."""
    html = _make_html('{"@type":"JobPosting","title":"Only Title"}')

    class FakeResp:
        text = html
        encoding = "utf-8"
        def raise_for_status(self):  # noqa: D401
            return None

    def fake_get(url, timeout, headers):  # noqa: ARG001
        return FakeResp()

    monkeypatch.setattr(seek_fetch.requests, "get", fake_get)
    job, missing = seek_fetch.fetch_from_url("https://www.seek.com.au/job/123")
    assert job["title"] == "Only Title"
    assert set(missing) == {"company", "location", "description"}
    assert job["source"] == "url"
    assert job["source_ref"].endswith("/123")


def test_fetch_failure_raises_fetch_error(monkeypatch):
    import requests as _req

    def boom(url, timeout, headers):  # noqa: ARG001
        raise _req.ConnectionError("simulated")

    monkeypatch.setattr(seek_fetch.requests, "get", boom)
    try:
        seek_fetch.fetch_from_url("https://www.seek.com.au/job/123")
    except seek_fetch.FetchError as e:
        assert "simulated" in str(e)
    else:
        raise AssertionError("Expected FetchError")


# --- SEEK_REDUX_DATA fallback -------------------------------------------

def _make_redux_html(job_node: dict, *, with_jsonld: bool = False, company_profile: dict | None = None, company_search_url: str = "") -> str:
    import json as _json
    result: dict = {"job": job_node}
    if company_profile is not None:
        result["companyProfile"] = company_profile
    if company_search_url:
        result["companySearchUrl"] = company_search_url
    blob = _json.dumps({"jobdetails": {"result": result}})
    script = f"<script>window.SEEK_REDUX_DATA = {blob};</script>"
    ld = (
        '<script type="application/ld+json">{"@type":"JobPosting","title":"FromLD"}</script>'
        if with_jsonld
        else ""
    )
    return f"<html><head>{ld}</head><body>{script}</body></html>"


def test_redux_fallback_extracts_full_job():
    html = _make_redux_html({
        "title": "SEO Specialist - Melbourne",
        "advertiser": {"name": "StudioHawk"},
        "location": {"label": "Prahran, Melbourne VIC"},
        "content": "<p>Are you looking for more than just another job?</p><p><strong>What you'll do:</strong></p><ul><li>Run SEO campaigns</li></ul>",
        "salary": {"label": "$65,000 - $80,000 + super"},
        "workTypes": {"label": "Full time"},
    })
    job = seek_fetch._extract_from_seek_redux(html, "https://au.seek.com/job/123")
    assert job is not None
    assert job["title"] == "SEO Specialist - Melbourne"
    assert job["company"] == "StudioHawk"
    assert job["location"] == "Prahran, Melbourne VIC"
    assert "<p>" not in job["description"]
    assert "Run SEO campaigns" in job["description"]
    assert job["salary"] == "$65,000 - $80,000 + super"
    assert job["employment_type"] == "Full time"


def test_redux_fallback_falls_back_to_abstract_when_no_content():
    html = _make_redux_html({
        "title": "Junior Role",
        "advertiser": {"name": "Foo"},
        "location": {"label": "Sydney"},
        "abstract": "Plain text abstract.",
    })
    job = seek_fetch._extract_from_seek_redux(html, "https://x/y")
    assert job is not None
    assert job["description"] == "Plain text abstract."


def test_redux_fallback_returns_none_without_title():
    html = _make_redux_html({"advertiser": {"name": "Foo"}})
    assert seek_fetch._extract_from_seek_redux(html, "https://x/y") is None


def test_redux_fallback_returns_none_when_blob_absent():
    html = "<html><body>nothing useful here</body></html>"
    assert seek_fetch._extract_from_seek_redux(html, "https://x/y") is None


def test_fetch_from_url_uses_redux_when_jsonld_missing(monkeypatch):
    html = _make_redux_html({
        "title": "T", "advertiser": {"name": "C"},
        "location": {"label": "L"}, "content": "D",
        "salary": {"label": "S"}, "workTypes": {"label": "FT"},
    })

    class FakeResp:
        text = html
        encoding = "utf-8"
        def raise_for_status(self):  # noqa: D401
            return None

    def fake_get(url, timeout, headers):  # noqa: ARG001
        return FakeResp()

    monkeypatch.setattr(seek_fetch.requests, "get", fake_get)
    job, missing = seek_fetch.fetch_from_url("https://au.seek.com/job/123")
    assert job["title"] == "T" and job["company"] == "C"
    assert missing == []


def test_redux_extracts_company_profile_fields():
    html = _make_redux_html(
        {"title": "T", "advertiser": {"name": "StudioHawk"}},
        company_profile={
            "companyNameSlug": "studiohawk-123",
            "overview": {
                "industry": "Advertising, Marketing & Communications Services",
                "size": {"description": "101-1,000 employees"},
            },
        },
        company_search_url="https://au.seek.com/StudioHawk-jobs/at-this-company",
    )
    job = seek_fetch._extract_from_seek_redux(html, "https://x/y")
    assert job is not None
    assert job["company_industry"] == "Advertising, Marketing & Communications Services"
    assert job["company_size"] == "101-1,000 employees"
    assert job["company_profile_url"] == "https://au.seek.com/companies/studiohawk-123"
    assert job["company_jobs_url"] == "https://au.seek.com/StudioHawk-jobs/at-this-company"


def test_redux_company_profile_absent_returns_empty_strings():
    html = _make_redux_html({"title": "T", "advertiser": {"name": "Foo"}})
    job = seek_fetch._extract_from_seek_redux(html, "https://x/y")
    assert job is not None
    assert job["company_industry"] == ""
    assert job["company_size"] == ""
    assert job["company_profile_url"] == ""
    assert job["company_jobs_url"] == ""


def test_fetch_from_url_prefers_jsonld_over_redux(monkeypatch):
    """JSON-LD wins when present — we only fall through to Redux as a fallback."""
    html = _make_redux_html(
        {"title": "FromRedux", "advertiser": {"name": "X"}},
        with_jsonld=True,
    )

    class FakeResp:
        text = html
        encoding = "utf-8"
        def raise_for_status(self):  # noqa: D401
            return None

    def fake_get(url, timeout, headers):  # noqa: ARG001
        return FakeResp()

    monkeypatch.setattr(seek_fetch.requests, "get", fake_get)
    job, _missing = seek_fetch.fetch_from_url("https://au.seek.com/job/123")
    assert job["title"] == "FromLD"


# --- Tiny runner so the file works without pytest -----------------------

class _MiniMonkeypatch:
    def __init__(self):
        self._undo: list[tuple] = []

    def setattr(self, target, name, value):
        old = getattr(target, name)
        self._undo.append((target, name, old))
        setattr(target, name, value)

    def undo(self):
        while self._undo:
            target, name, old = self._undo.pop()
            setattr(target, name, old)


def _run_all():
    tests = [
        ("test_clean_seek_like_posting", test_clean_seek_like_posting, False),
        ("test_picks_best_posting_when_multiple_blocks", test_picks_best_posting_when_multiple_blocks, False),
        ("test_employment_type_list", test_employment_type_list, False),
        ("test_graph_wrapper", test_graph_wrapper, False),
        ("test_no_job_posting_returns_none", test_no_job_posting_returns_none, False),
        ("test_malformed_json_is_skipped", test_malformed_json_is_skipped, False),
        ("test_missing_fields_reported_via_public_api", test_missing_fields_reported_via_public_api, True),
        ("test_fetch_failure_raises_fetch_error", test_fetch_failure_raises_fetch_error, True),
        ("test_redux_fallback_extracts_full_job", test_redux_fallback_extracts_full_job, False),
        ("test_redux_fallback_falls_back_to_abstract_when_no_content", test_redux_fallback_falls_back_to_abstract_when_no_content, False),
        ("test_redux_fallback_returns_none_without_title", test_redux_fallback_returns_none_without_title, False),
        ("test_redux_fallback_returns_none_when_blob_absent", test_redux_fallback_returns_none_when_blob_absent, False),
        ("test_redux_extracts_company_profile_fields", test_redux_extracts_company_profile_fields, False),
        ("test_redux_company_profile_absent_returns_empty_strings", test_redux_company_profile_absent_returns_empty_strings, False),
        ("test_fetch_from_url_uses_redux_when_jsonld_missing", test_fetch_from_url_uses_redux_when_jsonld_missing, True),
        ("test_fetch_from_url_prefers_jsonld_over_redux", test_fetch_from_url_prefers_jsonld_over_redux, True),
    ]
    passed = 0
    for name, fn, needs_mp in tests:
        mp = _MiniMonkeypatch() if needs_mp else None
        try:
            if needs_mp:
                fn(mp)  # type: ignore[arg-type]
            else:
                fn()
            print(f"OK  {name}")
            passed += 1
        except AssertionError as e:
            print(f"FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            print(f"ERR  {name}: {type(e).__name__}: {e}")
        finally:
            if mp is not None:
                mp.undo()
    print(f"\n{passed}/{len(tests)} passed")
    return passed == len(tests)


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
