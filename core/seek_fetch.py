"""Job input layer.

Three sources, all returning the same Job dict shape:
- load_mock()            -> built-in sample
- fetch_from_url(url)    -> real GET + JSON-LD JobPosting extraction (M3)
- from_pasted_text(...)  -> wrap a pasted JD

Network use is limited to ONE thing: a GET request to the supplied SEEK URL.
The app never POSTs anything to seek.com.au or any employer endpoint.
"""

from __future__ import annotations

import json
from typing import Iterator, TypedDict

import requests
from bs4 import BeautifulSoup


class Job(TypedDict):
    source: str          # "mock" | "url" | "paste"
    source_ref: str      # URL or ""
    title: str
    company: str
    location: str
    description: str
    salary: str          # empty if not available
    employment_type: str  # empty if not available
    # Company profile (populated from SEEK redux when available; otherwise empty)
    company_industry: str
    company_size: str
    company_profile_url: str   # SEEK page about the company
    company_jobs_url: str      # SEEK listing of all the company's open roles


class FetchError(Exception):
    """Raised when we cannot extract a JobPosting from the URL."""


REQUIRED_FIELDS: tuple[str, ...] = ("title", "company", "location", "description")

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


# --- Public API ---------------------------------------------------------

_MOCK_JOB: Job = {
    "source": "mock",
    "source_ref": "",
    "title": "Marketing Manager",
    "company": "Acme Pty Ltd",
    "location": "Sydney NSW 2000",
    "description": (
        "We are looking for a Marketing Manager to lead campaigns across digital and "
        "traditional channels. You will own the content calendar, run paid social and "
        "Google Ads, manage SEO, and report on performance to the leadership team.\n\n"
        "Requirements:\n"
        "- 4+ years in B2C marketing\n"
        "- Hands-on experience with Google Ads and Meta Ads\n"
        "- Strong copywriting skills, AU English\n"
        "- Comfortable with GA4 and basic SQL\n"
        "- Bonus: agency or e-commerce background"
    ),
    "salary": "AUD 90,000-110,000 per year",
    "employment_type": "Full-time",
    "company_industry": "Advertising, Marketing & Communications Services",
    "company_size": "11-50 employees",
    "company_profile_url": "",
    "company_jobs_url": "",
}


def load_mock() -> Job:
    """Return a built-in sample job for offline testing."""
    return dict(_MOCK_JOB)  # type: ignore[return-value]


def fetch_from_url(url: str, timeout: float = 15.0) -> tuple[Job, list[str]]:
    """Fetch a SEEK job page and extract the JobPosting via JSON-LD.

    Returns (job, missing_fields). `missing_fields` is empty when extraction
    was clean, or lists which REQUIRED_FIELDS came back empty (partial extraction).

    Raises FetchError on network failure or when no JobPosting JSON-LD can be
    located on the page. The UI catches this and offers the manual paste fallback.
    """
    url = url.strip()
    if not url:
        raise FetchError("No URL provided.")
    try:
        resp = requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": _UA, "Accept-Language": "en-AU,en;q=0.9"},
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        raise FetchError(f"Network error: {e}") from e

    # Force UTF-8: SEEK serves utf-8 but a missing/odd charset header can confuse requests.
    resp.encoding = resp.encoding or "utf-8"
    html = resp.text

    soup = BeautifulSoup(html, "html.parser")
    posting = _find_best_job_posting(soup)
    if posting is not None:
        job = _normalize_posting(posting, url)
    else:
        # Fallback: SEEK now embeds the job in window.SEEK_REDUX_DATA instead of JSON-LD.
        job = _extract_from_seek_redux(html, url)
        if job is None:
            raise FetchError(
                "Could not find a JobPosting on the page (neither JSON-LD nor "
                "SEEK_REDUX_DATA). SEEK may have changed its layout, or the URL "
                "may not be a job ad. Use the manual paste fallback."
            )

    missing = [f for f in REQUIRED_FIELDS if not job[f]]  # type: ignore[literal-required]
    return job, missing


def from_pasted_text(
    pasted: str,
    title: str = "Untitled role",
    company: str = "Unknown company",
    location: str = "",
    source_ref: str = "",
) -> Job:
    """Wrap a pasted job description into the same Job shape.

    `source_ref` is optionally carried so a failed URL stays on record.
    """
    return {
        "source": "paste",
        "source_ref": source_ref.strip(),
        "title": title.strip() or "Untitled role",
        "company": company.strip() or "Unknown company",
        "location": location.strip(),
        "description": pasted.strip(),
        "salary": "",
        "employment_type": "",
        "company_industry": "",
        "company_size": "",
        "company_profile_url": "",
        "company_jobs_url": "",
    }


# --- JSON-LD discovery --------------------------------------------------

def _iter_json_ld(soup: BeautifulSoup) -> Iterator[dict]:
    """Yield every dict found in any <script type="application/ld+json"> tag."""
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        yield from _unwrap_ld(data)


def _unwrap_ld(data: object) -> Iterator[dict]:
    """JSON-LD can be a dict, a list, or contain @graph. Yield every dict node."""
    if isinstance(data, list):
        for item in data:
            yield from _unwrap_ld(item)
    elif isinstance(data, dict):
        graph = data.get("@graph")
        if isinstance(graph, list):
            yield from _unwrap_ld(graph)
        yield data


def _is_job_posting(item: dict) -> bool:
    t = item.get("@type")
    if isinstance(t, list):
        return any(str(x) == "JobPosting" for x in t)
    return t == "JobPosting"


def _score_posting(item: dict) -> int:
    """Rank candidate JobPosting blocks by how many useful fields they populate."""
    keys = (
        "title",
        "description",
        "hiringOrganization",
        "jobLocation",
        "baseSalary",
        "employmentType",
    )
    return sum(1 for k in keys if item.get(k))


def _find_best_job_posting(soup: BeautifulSoup) -> dict | None:
    candidates = [item for item in _iter_json_ld(soup) if _is_job_posting(item)]
    if not candidates:
        return None
    return max(candidates, key=_score_posting)


# --- Normalization ------------------------------------------------------

def _normalize_posting(item: dict, source_url: str) -> Job:
    return {
        "source": "url",
        "source_ref": source_url,
        "title": _clean_text(item.get("title", "")),
        "company": _extract_company(item.get("hiringOrganization")),
        "location": _extract_location(item.get("jobLocation")),
        "description": _strip_html(item.get("description", "")),
        "salary": _extract_salary(item.get("baseSalary")),
        "employment_type": _extract_employment_type(item.get("employmentType")),
        # JSON-LD JobPosting rarely has structured company-profile data, so leave empty.
        "company_industry": "",
        "company_size": "",
        "company_profile_url": "",
        "company_jobs_url": "",
    }


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split()).strip()


def _strip_html(value: object) -> str:
    """Convert the JobPosting description HTML into readable plain text."""
    if not value:
        return ""
    soup = BeautifulSoup(str(value), "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln).strip()


def _extract_company(org: object) -> str:
    if not org:
        return ""
    if isinstance(org, str):
        return _clean_text(org)
    if isinstance(org, dict):
        return _clean_text(org.get("name", ""))
    if isinstance(org, list):
        for entry in org:
            name = _extract_company(entry)
            if name:
                return name
    return ""


def _extract_location(loc: object) -> str:
    if not loc:
        return ""
    if isinstance(loc, list):
        parts = [_extract_location(item) for item in loc]
        joined = " / ".join(p for p in parts if p)
        return joined
    if isinstance(loc, dict):
        addr = loc.get("address")
        if isinstance(addr, dict):
            bits = [
                addr.get("addressLocality"),
                addr.get("addressRegion"),
                addr.get("postalCode"),
                addr.get("addressCountry") if isinstance(addr.get("addressCountry"), str) else None,
            ]
            joined = " ".join(_clean_text(b) for b in bits if b)
            if joined:
                return joined
        if isinstance(addr, str):
            return _clean_text(addr)
        name = loc.get("name")
        if name:
            return _clean_text(name)
    if isinstance(loc, str):
        return _clean_text(loc)
    return ""


def _extract_salary(salary: object) -> str:
    if not salary:
        return ""
    if isinstance(salary, str):
        return _clean_text(salary)
    if isinstance(salary, list):
        for entry in salary:
            out = _extract_salary(entry)
            if out:
                return out
        return ""
    if not isinstance(salary, dict):
        return ""

    currency = _clean_text(salary.get("currency", ""))
    value = salary.get("value")
    unit = ""
    amount = ""

    if isinstance(value, dict):
        unit = _clean_text(value.get("unitText", ""))
        min_v = value.get("minValue")
        max_v = value.get("maxValue")
        val = value.get("value")
        if min_v and max_v:
            amount = f"{min_v}-{max_v}"
        elif val:
            amount = str(val)
        elif min_v:
            amount = f"from {min_v}"
        elif max_v:
            amount = f"up to {max_v}"
    elif isinstance(value, (int, float, str)):
        amount = str(value)

    parts = [p for p in (currency, amount, unit and f"per {unit.lower()}") if p]
    return " ".join(parts).strip()


# --- SEEK_REDUX_DATA fallback -------------------------------------------
#
# Modern SEEK pages no longer ship a JobPosting JSON-LD block. The full job
# object is embedded in a `window.SEEK_REDUX_DATA = {...};` assignment, with the
# job sitting at `jobdetails.result.job`. We grab the JSON via brace-counting
# (the blob contains nested objects/strings, so a regex won't do).

_REDUX_MARKER = "window.SEEK_REDUX_DATA"


def _extract_redux_blob(html: str) -> dict | None:
    """Return the SEEK_REDUX_DATA object as a dict, or None if not present/parseable."""
    start = html.find(_REDUX_MARKER)
    if start == -1:
        return None
    eq = html.find("=", start)
    if eq == -1:
        return None
    brace_start = html.find("{", eq)
    if brace_start == -1:
        return None

    depth = 0
    in_str = False
    esc = False
    i = brace_start
    n = len(html)
    while i < n:
        c = html[i]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"':
            in_str = not in_str
        elif not in_str:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(html[brace_start : i + 1])
                    except json.JSONDecodeError:
                        return None
        i += 1
    return None


def _extract_from_seek_redux(html: str, source_url: str) -> Job | None:
    """Try to build a Job dict from window.SEEK_REDUX_DATA. Returns None if missing."""
    data = _extract_redux_blob(html)
    if not isinstance(data, dict):
        return None
    jobdetails = data.get("jobdetails") if isinstance(data.get("jobdetails"), dict) else {}
    result = jobdetails.get("result", {}) if isinstance(jobdetails, dict) else {}
    job_node = result.get("job", {}) if isinstance(result, dict) else {}
    if not isinstance(job_node, dict) or not job_node.get("title"):
        return None

    title = _clean_text(job_node.get("title", ""))
    advertiser = job_node.get("advertiser") or {}
    company = _clean_text(advertiser.get("name", "") if isinstance(advertiser, dict) else "")
    loc = job_node.get("location") or {}
    location = _clean_text(loc.get("label", "") if isinstance(loc, dict) else "")
    description = _strip_html(job_node.get("content", "") or job_node.get("abstract", ""))
    salary_obj = job_node.get("salary") or {}
    salary = _clean_text(salary_obj.get("label", "") if isinstance(salary_obj, dict) else "")
    wt = job_node.get("workTypes") or {}
    employment_type = _clean_text(wt.get("label", "") if isinstance(wt, dict) else "")

    # Company profile (SEEK ships this alongside the job)
    profile = result.get("companyProfile", {}) if isinstance(result, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    overview = profile.get("overview", {}) if isinstance(profile.get("overview"), dict) else {}
    industry = _clean_text(overview.get("industry", "")) if isinstance(overview, dict) else ""
    size_obj = overview.get("size", {}) if isinstance(overview, dict) else {}
    size = _clean_text(size_obj.get("description", "")) if isinstance(size_obj, dict) else ""
    slug = profile.get("companyNameSlug") if isinstance(profile, dict) else None
    profile_url = f"https://au.seek.com/companies/{slug}" if isinstance(slug, str) and slug else ""
    jobs_url = _clean_text(result.get("companySearchUrl", "")) if isinstance(result, dict) else ""

    return {
        "source": "url",
        "source_ref": source_url,
        "title": title,
        "company": company,
        "location": location,
        "description": description,
        "salary": salary,
        "employment_type": employment_type,
        "company_industry": industry,
        "company_size": size,
        "company_profile_url": profile_url,
        "company_jobs_url": jobs_url,
    }


def _extract_employment_type(et: object) -> str:
    if not et:
        return ""
    if isinstance(et, list):
        return ", ".join(_clean_text(item) for item in et if item)
    return _clean_text(et)
