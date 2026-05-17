"""Adzuna job search for Australia.

Adzuna aggregates jobs from many AU job boards (including some SEEK + Indeed
indexes) and exposes a free public API:
    https://developer.adzuna.com/docs/search

Free tier: ~25 calls/month per registered app. Keep usage modest.
"""

from __future__ import annotations

import re
from typing import TypedDict

import requests

from .seek_fetch import Job


ADZUNA_BASE = "https://api.adzuna.com/v1/api/jobs/au/search/1"


class JobSearchResult(TypedDict):
    title: str
    company: str
    location: str
    salary: str
    description_snippet: str
    posted: str        # "YYYY-MM-DD"
    url: str           # adzuna redirect URL → ultimately to SEEK/Indeed/etc.
    source: str        # "SEEK" / "Indeed" / "Adzuna" / etc.
    category: str      # Adzuna category label


class JobSearchError(Exception):
    """Network / auth / quota failure on the search backend."""


_PUBLIC_SECTOR_BOOST = '(government OR council OR department OR ministry OR "public service" OR "public sector" OR "APS")'


def search_adzuna(
    app_id: str,
    app_key: str,
    what: str = "",
    where: str = "Australia",
    results_per_page: int = 20,
    max_days_old: int = 30,
    sort_by: str = "relevance",
    public_sector_only: bool = False,
) -> list[JobSearchResult]:
    """Search Adzuna's AU index.

    Args:
        app_id, app_key: Adzuna credentials from https://developer.adzuna.com
        what: search terms (e.g. "marketing manager")
        where: location ("Sydney", "Melbourne", "Australia")
        results_per_page: 1-50
        max_days_old: only return jobs posted within N days (default 30)
        sort_by: "relevance" | "date" | "salary"

    Raises JobSearchError on network / auth failure.
    """
    if not app_id.strip() or not app_key.strip():
        raise JobSearchError(
            "Adzuna API credentials not configured. "
            "Set ADZUNA_APP_ID and ADZUNA_APP_KEY in Streamlit Secrets."
        )
    params: dict = {
        "app_id": app_id.strip(),
        "app_key": app_key.strip(),
        "results_per_page": max(1, min(int(results_per_page), 50)),
        "max_days_old": max(1, int(max_days_old)),
        "sort_by": sort_by,
        "content-type": "application/json",
    }
    query = what.strip()
    if public_sector_only:
        # Append gov-related boost terms so Adzuna prioritises public-sector matches.
        # This is a best-effort filter (no true gov-only feed exists publicly in AU).
        query = f"{query} {_PUBLIC_SECTOR_BOOST}".strip()
    if query:
        params["what"] = query
    if where.strip():
        params["where"] = where.strip()

    try:
        resp = requests.get(ADZUNA_BASE, params=params, timeout=20)
    except requests.RequestException as e:
        raise JobSearchError(f"Network error: {e}") from e
    if resp.status_code == 401:
        raise JobSearchError("Adzuna rejected the credentials. Check your APP_ID / APP_KEY.")
    if resp.status_code == 429:
        raise JobSearchError(
            "Adzuna monthly quota exceeded (free tier = 25 calls/month). "
            "Wait until next month or upgrade your Adzuna plan."
        )
    if resp.status_code >= 400:
        raise JobSearchError(f"Adzuna error {resp.status_code}: {resp.text[:200]}")

    try:
        data = resp.json()
    except ValueError as e:
        raise JobSearchError(f"Could not parse Adzuna response: {e}") from e

    return [_normalise_result(r) for r in data.get("results", [])]


def _normalise_result(r: dict) -> JobSearchResult:
    company = ""
    if isinstance(r.get("company"), dict):
        company = str(r["company"].get("display_name", "")).strip()
    location = ""
    if isinstance(r.get("location"), dict):
        location = str(r["location"].get("display_name", "")).strip()
    category = ""
    if isinstance(r.get("category"), dict):
        category = str(r["category"].get("label", "")).strip()
    url = str(r.get("redirect_url", "")).strip()
    return {
        "title": str(r.get("title", "")).strip(),
        "company": company,
        "location": location,
        "salary": _format_salary(r),
        "description_snippet": str(r.get("description", "")).strip(),
        "posted": str(r.get("created", "")).strip()[:10],
        "url": url,
        "source": _detect_source(url),
        "category": category,
    }


def _format_salary(r: dict) -> str:
    smin = r.get("salary_min")
    smax = r.get("salary_max")
    if not smin and not smax:
        return ""
    cur = "AUD"
    if smin and smax and int(smin) != int(smax):
        s = f"{cur} {int(smin):,} – {int(smax):,}"
    else:
        s = f"{cur} {int(smin or smax):,}"
    if r.get("salary_is_predicted") in (True, "1", 1):
        s += " (est.)"
    return s


def _detect_source(url: str) -> str:
    u = url.lower()
    if "seek.com.au" in u:
        return "SEEK"
    if "indeed." in u:
        return "Indeed"
    if "linkedin.com" in u:
        return "LinkedIn"
    if "jora.com" in u:
        return "Jora"
    if "glassdoor." in u:
        return "Glassdoor"
    if "careerone." in u:
        return "CareerOne"
    if "ethicaljobs." in u:
        return "EthicalJobs"
    return "Adzuna"


def result_to_job(result: JobSearchResult) -> Job:
    """Convert an Adzuna search result into the standard Job dict used by the tailor pipeline."""
    return {
        "source": "search",
        "source_ref": result["url"],
        "title": result["title"] or "Untitled role",
        "company": result["company"] or "Unknown company",
        "location": result["location"],
        "description": result["description_snippet"],
        "salary": result["salary"],
        "employment_type": "",
        "company_industry": result.get("category", ""),
        "company_size": "",
        "company_profile_url": "",
        "company_jobs_url": "",
    }


# --- Resume → search keyword hints --------------------------------------

_ROLE_HINTS = (
    "manager", "specialist", "developer", "engineer", "analyst", "designer",
    "consultant", "lead", "director", "officer", "coordinator", "assistant",
    "marketing", "sales", "data", "product", "project", "operations",
    "support", "administrator", "accountant", "writer", "editor", "teacher",
    "nurse", "therapist", "researcher", "scientist", "architect",
)


def suggest_keywords_from_resume(resume_text: str) -> str:
    """Pull a sensible default search query from the resume content.

    Strategy:
    - First look at H3 headings (typically the role title at top of each Experience block)
    - Then H2 / H1 if they contain role-ish words
    - Fall back to first match against ROLE_HINTS anywhere in the doc
    Returns "" if nothing reasonable found.
    """
    lines = resume_text.splitlines()
    # H3 first (role titles)
    for line in lines[:200]:
        m = re.match(r"^###\s+(.+)$", line)
        if m:
            text = m.group(1).strip()
            # Often "Role title, Company". Take the part before the comma.
            text = text.split(",")[0].strip()
            if any(w in text.lower() for w in _ROLE_HINTS):
                return text
    # H1 / H2 fallback
    for line in lines[:80]:
        m = re.match(r"^#{1,2}\s+(.+)$", line)
        if m:
            text = m.group(1).strip()
            if any(w in text.lower() for w in _ROLE_HINTS):
                return text.split("—")[0].split(",")[0].strip()
    # Body keyword scan
    lower = resume_text.lower()
    common_roles = (
        "seo specialist", "marketing manager", "data analyst",
        "software engineer", "product manager", "ux designer",
        "graphic designer", "project manager", "account manager",
        "business analyst", "content writer",
    )
    for role in common_roles:
        if role in lower:
            return role
    return ""


def suggest_location_from_resume(resume_text: str, default: str = "Australia") -> str:
    """Pull a likely location hint from the resume's contact line."""
    au_cities = (
        "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra",
        "hobart", "darwin", "gold coast", "newcastle", "wollongong",
    )
    head = resume_text[:500].lower()
    for city in au_cities:
        if city in head:
            return city.title()
    return default


# --- Claude-powered resume analysis -------------------------------------

_SUGGEST_SYSTEM_PROMPT = """You analyse resumes to pick the best job search query for the candidate.

Return ONE JSON object only. No prose, no code fences. Schema:
{
  "keywords": "Job-title-style search query, 2 to 6 words. Use the candidate's most recent role title and primary skill. Max 60 chars.",
  "location": "Australian city (Sydney / Melbourne / Brisbane / Perth / Adelaide / Canberra / Hobart / Darwin) or 'Australia' if unclear.",
  "reasoning": "ONE short sentence (max 25 words) explaining why these terms match."
}

Rules:
- Use the candidate's actual recent role title where possible. Don't invent more senior or junior versions.
- Use AU spelling and AU job market vocabulary.
- Keywords should be a phrase a recruiter would search for (e.g. "Senior SEO Specialist", "Marketing Manager", "Data Analyst"), NOT a list of skills.
- If the resume mixes roles, pick the most prominent / most recent one.
"""


class SearchSuggestion(TypedDict):
    keywords: str
    location: str
    reasoning: str


def suggest_search_terms_from_resume(
    resume_text: str,
    api_key: str,
    model: str = "claude-sonnet-4-6",
) -> SearchSuggestion:
    """Ask Claude to suggest the best Adzuna search terms for a given resume.

    Raises JobSearchError on missing input / API error / unparseable output.
    """
    if not resume_text or not resume_text.strip():
        raise JobSearchError("Resume is empty. Upload a resume before asking for suggestions.")
    if not api_key or not api_key.strip():
        raise JobSearchError("Anthropic API key is required for the resume-based suggestion.")

    try:
        import anthropic  # lazy import
    except ImportError as e:
        raise JobSearchError(
            "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"
        ) from e

    client = anthropic.Anthropic(api_key=api_key.strip())
    try:
        msg = client.messages.create(
            model=model,
            max_tokens=300,
            system=_SUGGEST_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": f"RESUME:\n{resume_text}"}],
        )
    except anthropic.APIError as e:
        raise JobSearchError(f"Anthropic API error: {e}") from e
    except Exception as e:  # noqa: BLE001
        raise JobSearchError(f"Unexpected error calling Anthropic: {e}") from e

    raw = "".join(
        getattr(b, "text", "") for b in msg.content
        if getattr(b, "type", None) == "text"
    ).strip()

    # Defensive JSON parse: strip code fences if present, then locate {...} body.
    if raw.startswith("```"):
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

    import json
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            raise JobSearchError(f"Could not parse Claude's suggestion: {raw[:120]!r}")
        try:
            data = json.loads(raw[start : end + 1])
        except json.JSONDecodeError as e:
            raise JobSearchError(f"Could not parse Claude's suggestion: {e}") from e

    if not isinstance(data, dict):
        raise JobSearchError("Claude returned non-object output for the search suggestion.")

    return {
        "keywords": str(data.get("keywords", "")).strip()[:60],
        "location": str(data.get("location", "Australia")).strip() or "Australia",
        "reasoning": str(data.get("reasoning", "")).strip(),
    }
