"""ATS (Applicant Tracking System) public job-feed clients.

Many companies host their careers pages on third-party ATS platforms that expose
public JSON endpoints listing every open role at that company. No auth, no rate
limits, no scraping — just public APIs designed for aggregators.

Supported platforms (paste a careers URL from any of these and the app auto-detects):
- Greenhouse        e.g. https://boards.greenhouse.io/stripe
- Lever             e.g. https://jobs.lever.co/notion
- Workable          e.g. https://apply.workable.com/glasses-com
- Ashby             e.g. https://jobs.ashbyhq.com/cocoonweaver
- SmartRecruiters   e.g. https://jobs.smartrecruiters.com/Coca-Cola
"""

from __future__ import annotations

import re
from typing import TypedDict

import requests
from bs4 import BeautifulSoup

from .seek_fetch import Job


class CompanyJobResult(TypedDict):
    title: str
    company: str
    location: str
    department: str
    posted: str           # YYYY-MM-DD or ""
    url: str
    source: str           # platform name e.g. "Greenhouse"
    description: str      # plain text (HTML stripped); may be empty for some platforms


class ATSError(Exception):
    """Detection / fetch failure for an ATS source."""


# --- Detection ----------------------------------------------------------

_PATTERNS: list[tuple[str, str]] = [
    # Greenhouse — multiple URL forms over the years
    (r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board/?\?for=)?([\w.-]+)", "greenhouse"),
    # Lever
    (r"jobs\.lever\.co/([\w.-]+)", "lever"),
    # Workable (two URL shapes — apply.workable.com/{slug} OR {slug}.workable.com)
    (r"apply\.workable\.com/([\w.-]+)", "workable"),
    (r"https?://([\w.-]+)\.workable\.com", "workable"),
    # Ashby
    (r"jobs\.ashbyhq\.com/([\w.-]+)", "ashby"),
    # SmartRecruiters
    (r"(?:jobs|careers)\.smartrecruiters\.com/([\w.-]+)", "smartrecruiters"),
]


def detect_ats(url: str) -> tuple[str, str] | None:
    """Return (platform, slug) if `url` matches a supported ATS pattern, else None."""
    if not url:
        return None
    u = url.strip()
    for pattern, platform in _PATTERNS:
        m = re.search(pattern, u, re.IGNORECASE)
        if m:
            slug = m.group(1).strip("/").strip()
            if slug:
                return platform, slug
    return None


# --- Homepage → ATS discovery -------------------------------------------

_CAREERS_HINTS = (
    "career", "careers", "jobs", "join-us", "join us", "work-with-us",
    "work with us", "opportunities", "hiring", "vacancies", "open-roles",
    "open roles", "positions",
)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def detect_ats_from_homepage(
    homepage_url: str,
    api_key: str | None = None,
) -> tuple[str, str] | None:
    """Auto-discover a company's ATS from their homepage or careers URL.

    Strategy:
    1. If the URL itself matches an ATS pattern → return immediately.
    2. Fetch the URL, scan its HTML source for any ATS URL.
    3. Find careers-page candidates in the page, follow each, scan again.
    4. Optional LLM fallback (if api_key provided): ask Claude to identify the ATS.

    Returns (platform, slug) on success, or None.
    """
    if not homepage_url or not homepage_url.strip():
        return None

    # 1) Maybe it's already an ATS URL
    direct = detect_ats(homepage_url)
    if direct:
        return direct

    # 2) Normalise URL
    url = homepage_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "en-AU,en;q=0.9"}

    # Fetch the supplied URL
    pages_to_check: list[str] = [url]
    visited: set[str] = set()
    found: tuple[str, str] | None = None
    final_html = ""

    while pages_to_check and not found:
        current = pages_to_check.pop(0)
        if current in visited:
            continue
        visited.add(current)
        try:
            r = requests.get(current, timeout=15, headers=headers, allow_redirects=True)
            if r.status_code != 200:
                continue
            html = r.text
        except requests.RequestException:
            continue

        final_html = html  # keep last successful response for LLM fallback
        found = _scan_ats_urls_in_html(html)
        if found:
            return found

        # On the first hop, also collect careers candidates
        if len(visited) <= 1:
            careers_candidates = _find_careers_links(html, base_url=r.url)
            pages_to_check.extend(careers_candidates[:3])
            # Plus a few hardcoded common careers paths (SPA homepages often don't
            # expose links in plain HTML, but the conventional URLs still work).
            from urllib.parse import urlparse  # noqa: PLC0415
            parsed = urlparse(r.url)
            if parsed.netloc:
                base = f"{parsed.scheme}://{parsed.netloc}"
                pages_to_check.extend([
                    f"{base}/careers",
                    f"{base}/jobs",
                    f"{base}/company/careers",
                    f"{parsed.scheme}://careers.{parsed.netloc}",
                    f"{parsed.scheme}://jobs.{parsed.netloc}",
                ])

    # 3) LLM fallback if a key is available
    if not found and api_key and api_key.strip():
        try:
            candidate = _llm_detect_ats(final_html, url, api_key.strip())
            if candidate:
                # Verify the candidate by actually calling the ATS — Claude may
                # hallucinate a slug, so we only trust it if the fetch works.
                try:
                    _ = fetch_jobs(candidate[0], candidate[1])
                    found = candidate
                except ATSError:
                    found = None
        except Exception:  # noqa: BLE001 — best effort, never raise from detection
            return None

    return found


def _scan_ats_urls_in_html(html: str) -> tuple[str, str] | None:
    """Scan an HTML blob for any URL/string matching a known ATS pattern."""
    if not html:
        return None
    # Look in plain URLs + iframe srcs + script srcs
    candidates = re.findall(r"https?://[^\s\"'<>)]+", html)
    for cand in candidates:
        m = detect_ats(cand)
        if m:
            return m
    # Sometimes the slug is referenced without protocol — try a few common shapes
    for pat, platform in _PATTERNS:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            slug = m.group(1).strip("/").strip()
            if slug:
                return platform, slug
    return None


def _find_careers_links(html: str, base_url: str) -> list[str]:
    """Find anchor tags that look like careers links. Returns absolute URLs, sorted by relevance."""
    from urllib.parse import urljoin
    soup = BeautifulSoup(html, "html.parser")
    scored: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        text = a.get_text(strip=True).lower()
        if not href or href.startswith(("mailto:", "tel:", "javascript:")):
            continue
        score = 0
        href_l = href.lower()
        for hint in _CAREERS_HINTS:
            if hint in href_l:
                score += 2
            if hint in text:
                score += 1
        if score > 0:
            full = urljoin(base_url, href)
            scored.append((score, full))
    scored.sort(key=lambda x: -x[0])
    # Dedupe preserving order
    seen: set[str] = set()
    out: list[str] = []
    for _, u in scored:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


_LLM_ATS_SYSTEM = """You identify which Applicant Tracking System (ATS) a company uses for its public job board.

Supported platforms and their URL shapes:
- greenhouse        → boards.greenhouse.io/{slug} or job-boards.greenhouse.io/{slug}
- lever             → jobs.lever.co/{slug}
- workable          → apply.workable.com/{slug} or {slug}.workable.com
- ashby             → jobs.ashbyhq.com/{slug}
- smartrecruiters   → jobs.smartrecruiters.com/{slug}

Two-step method:
1. **Scan the HTML** for direct evidence (URLs containing greenhouse.io / lever.co / workable.com / ashbyhq.com / smartrecruiters.com). If found, return that platform + slug.
2. **If no direct evidence**, use your training knowledge of where well-known companies host their careers. Many large tech companies use Greenhouse or Lever. The slug is usually a lowercase, dash-separated version of the company name (e.g. "stripe", "anthropic", "canva", "atlassian", "robinhood", "notion", "airbnb").

Output schema (return one JSON object only, no prose, no code fences):
{"platform": "greenhouse"|"lever"|"workable"|"ashby"|"smartrecruiters"|"unknown", "slug": "..."}

Rules:
- Only return platform != "unknown" if at least 60% confident the slug is correct.
- If the company uses Workday, SAP, Taleo, or a fully custom system, return platform="unknown".
- The slug must be lowercase and contain only letters, digits, and dashes.
- Don't invent slugs you're unsure about — "unknown" is better than wrong.
"""


def _llm_detect_ats(html: str, url: str, api_key: str) -> tuple[str, str] | None:
    try:
        import anthropic  # lazy
    except ImportError:
        return None

    # Strip noise from the HTML before sending
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "noscript", "header", "footer"]):
        tag.decompose()
    # Keep <script src=> URLs but drop body text for scripts (lighter payload)
    for tag in soup.find_all("script"):
        if not tag.get("src"):
            tag.decompose()
    text_blob = str(soup)[:8000]

    client = anthropic.Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=200,
            system=_LLM_ATS_SYSTEM,
            messages=[{"role": "user", "content": f"URL: {url}\n\nHTML:\n{text_blob}"}],
        )
    except Exception:  # noqa: BLE001
        return None

    raw = "".join(getattr(b, "text", "") for b in msg.content if getattr(b, "type", None) == "text").strip()
    if raw.startswith("```"):
        if "\n" in raw:
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        raw = raw.strip()

    import json as _json
    try:
        data = _json.loads(raw)
    except _json.JSONDecodeError:
        s, e = raw.find("{"), raw.rfind("}")
        if s == -1 or e <= s:
            return None
        try:
            data = _json.loads(raw[s : e + 1])
        except _json.JSONDecodeError:
            return None

    if not isinstance(data, dict):
        return None
    platform = str(data.get("platform", "")).lower().strip()
    slug = str(data.get("slug", "")).strip()
    if platform in {"greenhouse", "lever", "workable", "ashby", "smartrecruiters"} and slug:
        return platform, slug
    return None


# --- Fetchers (one per platform) ----------------------------------------

def fetch_jobs(platform: str, slug: str) -> tuple[str, list[CompanyJobResult]]:
    """Fetch all current open roles for `slug` on `platform`.

    Returns (display_company_name, list_of_results). Raises ATSError on failure.
    """
    handlers = {
        "greenhouse": _fetch_greenhouse,
        "lever": _fetch_lever,
        "workable": _fetch_workable,
        "ashby": _fetch_ashby,
        "smartrecruiters": _fetch_smartrecruiters,
    }
    fn = handlers.get(platform)
    if not fn:
        raise ATSError(f"Unsupported platform: {platform}")
    return fn(slug)


def _strip_html_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    return "\n".join(ln.strip() for ln in text.splitlines() if ln.strip()).strip()


def _safe_get_json(url: str, *, post_body: dict | None = None, timeout: float = 15.0) -> dict | list:
    try:
        if post_body is not None:
            resp = requests.post(url, json=post_body, timeout=timeout)
        else:
            resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        raise ATSError(f"Network error: {e}") from e
    except ValueError as e:  # JSON decode
        raise ATSError(f"Response wasn't valid JSON: {e}") from e


def _fetch_greenhouse(slug: str) -> tuple[str, list[CompanyJobResult]]:
    """Greenhouse: GET /v1/boards/{slug}/jobs?content=true returns all open roles."""
    data = _safe_get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not isinstance(data, dict):
        raise ATSError("Greenhouse returned unexpected shape")
    company_name = (data.get("meta") or {}).get("title") or slug.replace("-", " ").title()
    results: list[CompanyJobResult] = []
    for j in data.get("jobs", []):
        loc = j.get("location") or {}
        results.append({
            "title": str(j.get("title", "")).strip(),
            "company": str(company_name),
            "location": str(loc.get("name", "")).strip() if isinstance(loc, dict) else "",
            "department": "",
            "posted": str(j.get("updated_at", ""))[:10],
            "url": str(j.get("absolute_url", "")).strip(),
            "source": "Greenhouse",
            "description": _strip_html_text(j.get("content", "")),
        })
    return company_name, results


def _fetch_lever(slug: str) -> tuple[str, list[CompanyJobResult]]:
    """Lever: GET /v0/postings/{slug}?mode=json returns array."""
    data = _safe_get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        raise ATSError("Lever returned unexpected shape")
    company_name = slug.replace("-", " ").title()
    results: list[CompanyJobResult] = []
    for j in data:
        cats = j.get("categories") or {}
        results.append({
            "title": str(j.get("text", "")).strip(),
            "company": str(company_name),
            "location": str(cats.get("location", "")).strip(),
            "department": str(cats.get("department", "") or cats.get("team", "")).strip(),
            "posted": "",
            "url": str(j.get("hostedUrl") or j.get("applyUrl") or "").strip(),
            "source": "Lever",
            "description": _strip_html_text(j.get("description", "")),
        })
    return company_name, results


def _fetch_workable(slug: str) -> tuple[str, list[CompanyJobResult]]:
    """Workable v3: POST /api/v3/accounts/{slug}/jobs with empty body returns paginated list."""
    data = _safe_get_json(
        f"https://apply.workable.com/api/v3/accounts/{slug}/jobs",
        post_body={"query": "", "limit": 100},
    )
    if not isinstance(data, dict):
        raise ATSError("Workable returned unexpected shape")
    results: list[CompanyJobResult] = []
    company_name = slug.replace("-", " ").title()
    for j in data.get("results", []):
        loc_obj = j.get("location") or {}
        if isinstance(loc_obj, dict):
            loc = ", ".join(p for p in [
                loc_obj.get("city"), loc_obj.get("region"), loc_obj.get("country")
            ] if p)
        else:
            loc = str(loc_obj or "")
        results.append({
            "title": str(j.get("title", "")).strip(),
            "company": str(j.get("company", company_name)).strip(),
            "location": loc.strip(),
            "department": str(j.get("department", "")).strip(),
            "posted": str(j.get("published_on", ""))[:10],
            "url": str(j.get("url") or f"https://apply.workable.com/{slug}/j/{j.get('shortcode', '')}").strip(),
            "source": "Workable",
            "description": _strip_html_text(j.get("description", "")),
        })
    if results:
        company_name = results[0]["company"]
    return company_name, results


def _fetch_ashby(slug: str) -> tuple[str, list[CompanyJobResult]]:
    """Ashby: GET /posting-api/job-board/{slug}?includeCompensation=true"""
    data = _safe_get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    if not isinstance(data, dict):
        raise ATSError("Ashby returned unexpected shape")
    company_name = slug.replace("-", " ").title()
    results: list[CompanyJobResult] = []
    for j in data.get("jobs", []):
        results.append({
            "title": str(j.get("title", "")).strip(),
            "company": str(company_name),
            "location": str(j.get("locationName", "")).strip(),
            "department": str(j.get("departmentName", "")).strip(),
            "posted": str(j.get("publishedAt", ""))[:10],
            "url": str(j.get("jobUrl", "")).strip(),
            "source": "Ashby",
            "description": _strip_html_text(j.get("descriptionHtml", "") or ""),
        })
    return company_name, results


def _fetch_smartrecruiters(slug: str) -> tuple[str, list[CompanyJobResult]]:
    """SmartRecruiters: GET /v1/companies/{slug}/postings"""
    data = _safe_get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not isinstance(data, dict):
        raise ATSError("SmartRecruiters returned unexpected shape")
    company_name = slug.replace("-", " ").title()
    results: list[CompanyJobResult] = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        loc_str = ", ".join(p for p in [
            loc.get("city"), loc.get("region"), loc.get("country")
        ] if p) if isinstance(loc, dict) else ""
        results.append({
            "title": str(j.get("name", "")).strip(),
            "company": str(company_name),
            "location": loc_str.strip(),
            "department": "",
            "posted": str(j.get("releasedDate", ""))[:10],
            "url": str(j.get("ref", "")).strip(),
            "source": "SmartRecruiters",
            "description": "",  # full description requires per-posting fetch
        })
    return company_name, results


# --- Conversion to standard Job dict for the tailor pipeline ------------

def result_to_job(r: CompanyJobResult) -> Job:
    return {
        "source": "ats",
        "source_ref": r.get("url", ""),
        "title": r.get("title", "") or "Untitled role",
        "company": r.get("company", "") or "Unknown company",
        "location": r.get("location", ""),
        "description": r.get("description", "") or r.get("title", ""),
        "salary": "",
        "employment_type": "",
        "company_industry": r.get("department", ""),
        "company_size": "",
        "company_profile_url": "",
        "company_jobs_url": "",
    }
