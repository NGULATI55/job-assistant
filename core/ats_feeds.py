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
    # Lisnic — AU/NZ widget; data embedded in Next.js __NEXT_DATA__ block
    (r"(?:www\.)?lisnic\.com/widget/jobs/([\w.-]+)", "lisnic"),
    (r"(?:www\.)?lisnic\.com/jobs/business/([\w.-]+)", "lisnic"),
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
        "lisnic": _fetch_lisnic,
    }
    fn = handlers.get(platform)
    if not fn:
        raise ATSError("This careers page isn't supported yet.")
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
        raise ATSError("Couldn't reach the careers page. Try again in a moment.") from e
    except ValueError as e:  # JSON decode
        raise ATSError("Couldn't read the response from the careers page.") from e


def _fetch_greenhouse(slug: str) -> tuple[str, list[CompanyJobResult]]:
    """Greenhouse: GET /v1/boards/{slug}/jobs?content=true returns all open roles."""
    data = _safe_get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if not isinstance(data, dict):
        raise ATSError("Couldn't read the careers page right now.")
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
        raise ATSError("Couldn't read the careers page right now.")
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
        raise ATSError("Couldn't read the careers page right now.")
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
        raise ATSError("Couldn't read the careers page right now.")
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


def _fetch_lisnic(slug: str) -> tuple[str, list[CompanyJobResult]]:
    """Lisnic (Australian/NZ job widget). Data lives in <script id="__NEXT_DATA__">."""
    url = f"https://www.lisnic.com/widget/jobs/{slug}"
    try:
        resp = requests.get(url, headers={"User-Agent": _USER_AGENT}, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ATSError("Couldn't reach that careers page. Try again in a moment.") from e

    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>', resp.text, re.DOTALL)
    if not m:
        raise ATSError("Couldn't read the careers page right now.")
    try:
        import json as _json
        data = _json.loads(m.group(1))
    except Exception as e:  # noqa: BLE001
        raise ATSError("Couldn't read the careers page right now.") from e

    page_props = (data.get("props") or {}).get("pageProps") or {}
    business = page_props.get("business") or {}
    posts = page_props.get("posts") or []
    company_name = business.get("name") or slug.replace("-", " ").title()

    work_type_label = {
        "FULL_TIME": "Full-time", "PART_TIME": "Part-time",
        "CONTRACT": "Contract", "CASUAL": "Casual", "INTERNSHIP": "Internship",
    }

    results: list[CompanyJobResult] = []
    for p in posts:
        # Location string
        loc_info = p.get("post_location_info") or {}
        if isinstance(loc_info, dict):
            loc = ", ".join(filter(None, [
                loc_info.get("city"), loc_info.get("state"), loc_info.get("country"),
            ]))
        else:
            loc = ""
        remote_type = (p.get("remote_type") or "").upper()
        if remote_type and remote_type != "ONSITE":
            loc = f"{loc} ({remote_type.title()})" if loc else remote_type.title()

        # Salary string
        salary = ""
        if not p.get("is_salary_hidden"):
            smin, smax = p.get("salary_range_min"), p.get("salary_range_max")
            if smin and smax and int(smin) > 0:
                disp = (p.get("salary_display_type") or "ANNUAL").upper()
                period = "per year" if disp == "ANNUAL" else disp.lower()
                salary = f"AUD {int(smin):,} - {int(smax):,} {period}"

        # Description from post_metum
        post_meta = p.get("post_metum") or {}
        description = ""
        if isinstance(post_meta, dict):
            description = post_meta.get("description") or ""
        description = _strip_html_text(description)

        # Public job page URL
        job_id = p.get("id")
        job_url = f"https://www.lisnic.com/jobs/{job_id}" if job_id else url

        result: CompanyJobResult = {
            "title": str(p.get("title", "")).strip(),
            "company": company_name,
            "location": loc.strip(),
            "department": "",
            "posted": str(p.get("published_at") or p.get("created_at", ""))[:10],
            "url": job_url,
            "source": "Lisnic",
            "description": description,
        }
        # Stash extras for result_to_job() — TypedDict tolerates extra keys at runtime.
        result["salary"] = salary  # type: ignore[typeddict-unknown-key]
        result["employment_type"] = work_type_label.get(p.get("work_type", ""), "")  # type: ignore[typeddict-unknown-key]
        results.append(result)
    return company_name, results


def _fetch_smartrecruiters(slug: str) -> tuple[str, list[CompanyJobResult]]:
    """SmartRecruiters: GET /v1/companies/{slug}/postings"""
    data = _safe_get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    if not isinstance(data, dict):
        raise ATSError("Couldn't read the careers page right now.")
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


# --- Universal LLM-based job extractor ----------------------------------
# Used when no known ATS is detected. Claude reads any careers page and
# returns a structured list of jobs.

_LLM_EXTRACT_JOBS_SYSTEM = """You extract currently-open job postings from a company's careers page HTML.

Return ONE JSON object only. No prose, no code fences. Schema:
{
  "company": "Company name as shown on the page (string)",
  "jobs": [
    {
      "title": "Job title",
      "location": "City / Remote / Hybrid etc, or empty string",
      "department": "Team or department if visible, or empty string",
      "url": "Direct link to the job posting / apply page if visible, or empty string. Must be a full URL starting with https://, NOT a relative path.",
      "description": "1-3 sentence summary of the role if visible, or empty string"
    }
  ]
}

Rules:
- Only include POSITIONS BEING ACTIVELY ADVERTISED — real job titles with details.
- DON'T include "send us your CV", "future opportunities", "general application" CTAs.
- DON'T include role categories ("Engineering", "Marketing") without a specific position name.
- DON'T invent jobs or URLs. If the page has no clear listings, return jobs: [].
- For URLs: if you see a relative path like "/jobs/123", combine with the base URL to make it absolute. If you can't determine the absolute URL, leave it empty.
- Limit output to the 50 most prominent / recent jobs on the page.
"""


def find_company_jobs(
    url: str,
    api_key: str | None = None,
) -> tuple[str, list[CompanyJobResult], str]:
    """Universal entry point — get jobs for any company URL.

    Returns (company_name, jobs, method_used) where method_used is one of:
      "ats:<platform>"   — direct ATS API
      "llm"              — Claude extraction from page HTML
    Raises ATSError if everything fails.
    """
    # 1) ATS detection + direct API (fast, free, structured)
    detection = detect_ats_from_homepage(url, api_key=api_key)
    if detection:
        platform, slug = detection
        name, jobs = fetch_jobs(platform, slug)
        return name, jobs, f"ats:{platform}"

    # 2) LLM extraction fallback
    if not api_key or not api_key.strip():
        raise ATSError(
            "Couldn't read jobs from that careers page. "
            "Try pasting the company's direct careers page URL."
        )
    name, jobs = _fetch_jobs_via_llm(url, api_key.strip())
    return name, jobs, "llm"


def _fetch_jobs_via_llm(url: str, api_key: str) -> tuple[str, list[CompanyJobResult]]:
    """Generic scraper: find the careers page, send to Claude, parse jobs."""
    # Find the careers page (reuse the discovery logic from detect_ats_from_homepage)
    headers = {"User-Agent": _USER_AGENT, "Accept-Language": "en-AU,en;q=0.9"}

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    final_url = url
    final_html = ""
    try:
        r = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
        if r.status_code == 200:
            final_url = r.url
            final_html = r.text
    except requests.RequestException as e:
        raise ATSError("Couldn't reach that page. Check the URL and try again.") from e

    if not final_html:
        raise ATSError("That page didn't return any readable content.")

    # If the supplied URL clearly isn't already a careers page, follow careers links
    looks_like_careers = any(s in final_url.lower() for s in ("career", "job", "join", "opportun"))
    if not looks_like_careers:
        candidates = _find_careers_links(final_html, base_url=final_url)
        from urllib.parse import urlparse  # noqa: PLC0415
        parsed = urlparse(final_url)
        if parsed.netloc:
            candidates.extend([
                f"{parsed.scheme}://{parsed.netloc}/careers",
                f"{parsed.scheme}://{parsed.netloc}/jobs",
                f"{parsed.scheme}://careers.{parsed.netloc}",
                f"{parsed.scheme}://jobs.{parsed.netloc}",
            ])
        for cand in candidates[:5]:
            try:
                cr = requests.get(cand, timeout=15, headers=headers, allow_redirects=True)
                if cr.status_code == 200 and len(cr.text) > 1000:
                    final_url = cr.url
                    final_html = cr.text
                    break
            except requests.RequestException:
                continue

    # Strip noise from HTML before sending to Claude
    soup = BeautifulSoup(final_html, "html.parser")
    for tag in soup(["script", "style", "noscript", "form", "iframe"]):
        tag.decompose()
    text_blob = str(soup)[:18000]  # ~5k tokens, keeps Claude cost predictable

    if len(text_blob.strip()) < 200:
        raise ATSError(
            "Couldn't read jobs from that careers page. "
            "Try the company's direct careers page URL."
        )

    try:
        import anthropic  # lazy
    except ImportError as e:
        raise ATSError("Job search isn't available right now.") from e

    client = anthropic.Anthropic(api_key=api_key)
    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4000,
            system=_LLM_EXTRACT_JOBS_SYSTEM,
            messages=[{"role": "user", "content": f"CAREERS PAGE URL: {final_url}\n\nHTML:\n{text_blob}"}],
        )
    except anthropic.APIError as e:
        raise ATSError("Couldn't read jobs from that careers page right now. Try again in a moment.") from e
    except Exception as e:  # noqa: BLE001
        raise ATSError("Couldn't read jobs from that careers page right now. Try again in a moment.") from e

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
            raise ATSError("Couldn't read jobs from that careers page. Try the company's direct careers page URL.")
        try:
            data = _json.loads(raw[s : e + 1])
        except _json.JSONDecodeError as err:
            raise ATSError("Couldn't read jobs from that careers page. Try the company's direct careers page URL.") from err

    if not isinstance(data, dict):
        raise ATSError("Couldn't read jobs from that careers page. Try the company's direct careers page URL.")

    company_name = str(data.get("company") or "").strip()
    if not company_name:
        # Fall back to the page <title> or domain name
        try:
            company_name = (BeautifulSoup(final_html, "html.parser").title.text or "").strip()
        except Exception:  # noqa: BLE001
            company_name = ""
        if not company_name:
            from urllib.parse import urlparse  # noqa: PLC0415
            company_name = urlparse(final_url).netloc.replace("www.", "").split(".")[0].title()

    raw_jobs = data.get("jobs") or []
    if not isinstance(raw_jobs, list):
        raise ATSError("Couldn't read jobs from that careers page. Try the company's direct careers page URL.")

    results: list[CompanyJobResult] = []
    for j in raw_jobs:
        if not isinstance(j, dict):
            continue
        title = str(j.get("title") or "").strip()
        if not title:
            continue
        job_url = str(j.get("url") or "").strip()
        # Guard: if Claude returned a relative path, drop it (the prompt requested absolute)
        if job_url and not job_url.startswith(("http://", "https://")):
            job_url = ""
        result: CompanyJobResult = {
            "title": title,
            "company": company_name,
            "location": str(j.get("location") or "").strip(),
            "department": str(j.get("department") or "").strip(),
            "posted": "",
            "url": job_url or final_url,
            "source": "Web (Claude)",
            "description": str(j.get("description") or "").strip(),
        }
        results.append(result)

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
        "salary": r.get("salary", "") or "",  # populated by Lisnic when public
        "employment_type": r.get("employment_type", "") or "",  # populated by Lisnic / Workable
        "company_industry": r.get("department", ""),
        "company_size": "",
        "company_profile_url": "",
        "company_jobs_url": "",
    }
