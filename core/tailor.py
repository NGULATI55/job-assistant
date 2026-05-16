"""Tailoring layer — real Claude API (M2) with mock fallback for debugging.

Single public entry point: `tailor(job, master_resume_md, *, use_mock=False, api_key=None)`.
Always returns the same `TailoredResult` shape, so the UI doesn't branch.

Network use: only POST to api.anthropic.com when `use_mock=False`. Nothing else.
"""

from __future__ import annotations

import json
import os
from typing import TypedDict

from .seek_fetch import Job


DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 4096


class TailoredResult(TypedDict):
    tailored_resume_md: str
    cover_note_md: str
    match_summary: str
    missing_requirements: list[str]
    is_mock: bool


class TailorError(Exception):
    """Raised when tailoring cannot complete. UI catches this and shows a banner."""


# --- System prompt ------------------------------------------------------

SYSTEM_PROMPT = """You tailor resumes and short cover notes for Australian job applications.

OUTPUT FORMAT (mandatory)
Return ONE JSON object and nothing else. No prose before or after. No code fences. No commentary.
Required keys:
- "tailored_resume_md": string (Markdown)
- "cover_note_md": string (Markdown)
- "match_summary": string (one paragraph)
- "missing_requirements": array of short strings

TRUTHFULNESS (mandatory)
- Use ONLY facts present in the master resume. Never invent experience, tools, qualifications, dates, achievements, or metrics.
- You may rephrase, reorder, and prioritise what already exists.
- If the job requires something not in the master resume, list it in missing_requirements. Never fabricate it into the resume or cover note.
- Keep employer names, role titles, dates, and education entries exactly as in the master resume.

STYLE
- Australian English spelling and tone.
- Plain, concise, ATS-friendly Markdown. Use standard headings (#, ##) and "- " bullet lists only. No tables, no emoji, no fancy formatting.
- Do not use long dashes (em-dash or en-dash) anywhere. Use commas, full stops, or "—" (NOT dashes) parentheses if you need a break.
- No AI cliches, no recruiter fluff, no LinkedIn-speak. Avoid these phrases entirely:
  "in today's fast-paced world", "tailored solutions", "comprehensive solutions", "unlock", "elevate", "delve into", "seamless experience", "top-notch", "passionate", "go-getter", "self-starter", "results-driven", "proven track record", "synergy", "leverage", "leverage(d)", "make an impact", "sharpen my skills", "take ownership", "drive results", "drive impact", "deep dive", "cutting-edge", "innovative", "spearhead(ed)", "robust", "dynamic", "agile mindset", "passionate about", "wealth of experience", "demonstrated ability to", "I am writing to express my interest in".

HUMAN VOICE (mandatory — recruiters use AI-detection tools)
- Grammar self-check before returning: "a" vs "an" before consonant vs vowel SOUND (a Melbourne-based, a university, an hour, an honest), subject-verb agreement, plural agreement, tense consistency.
- Do NOT use formulaic openers. Forbidden opening patterns for the cover note: "I am a [adjective]-based [role] with [N] years of experience in...", "As a [role], I have always been passionate about...", "I am writing to express...", "I am excited to apply...". Open with a concrete fact specific to this role or company instead.
- Vary sentence length. Mix short sentences (5 to 10 words) with longer ones. AI detectors flag uniform sentence rhythm; humans are uneven.
- Use specific, direct verbs, not generic ones. "Audited 40+ pages" beats "delivered measurable improvements". Cite tools by name, name the work, skip filler adverbs.
- Do not echo the company's marketing copy back at them. If the ad says "we are a specialised SEO agency with no generalists", do not paraphrase that line in the cover note.
- Contractions are fine and sound more human ("I've", "I'm", "don't"). Use a few.
- If a sentence sounds like it could appear in any cover letter for any role, rewrite it.

TAILORED RESUME
- Mirror the structure of the master resume.
- Reorder bullets within each role to lead with the most job-relevant ones.
- Drop irrelevant bullets quietly. Do not pad.
- Preserve dates, role titles, and employers verbatim from the master resume.
- Bullets should start with a strong verb in past tense for past roles, present tense for the current role.

COVER NOTE
- 4 to 6 sentences total. First-person, plain prose, no headings, no bullet points.
- Open with "Hi [Company] team," then a sentence that ties something concrete in the candidate's history to something concrete the role needs. Not their job title; not their years of experience.
- Avoid the "I am a..." or "As a..." opener entirely.
- End with a short sign-off ("Cheers, [Your name]" is fine).
- No subject line, no address block.

MATCH SUMMARY
- One paragraph. State plainly how the candidate fits, naming 2 to 3 concrete strengths and any notable gaps.
- Refer to the candidate by first name (from the resume) or "they". Refer to their resume content as "his/her/their background", "the experience listed", "what they have done". NEVER write "the master resume", "the source document", "the candidate's CV", or any phrase that exposes that this is an automated review.

MISSING REQUIREMENTS
- Short strings listing job requirements that the candidate's listed experience does not evidence. Empty array if there are none.
- Write naturally: "Screaming Frog not listed in experience", not "Screaming Frog not in master resume".
"""


# --- Public API ---------------------------------------------------------

def tailor(
    job: Job,
    master_resume_md: str,
    *,
    use_mock: bool = False,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
) -> TailoredResult:
    """Generate a tailored resume + cover note + match summary + missing requirements.

    Raises TailorError on any failure (missing API key, empty master resume, API
    error, unparseable output). The UI catches this and shows a banner without
    crashing the app.
    """
    if use_mock:
        return _tailor_mock(job, master_resume_md)
    return _tailor_with_claude(job, master_resume_md, api_key=api_key, model=model)


# --- Real Claude call ---------------------------------------------------

def _tailor_with_claude(
    job: Job,
    master_resume_md: str,
    *,
    api_key: str | None,
    model: str,
) -> TailoredResult:
    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not resolved_key.strip():
        raise TailorError(
            "ANTHROPIC_API_KEY is not set. Add it to your environment or to "
            ".streamlit/secrets.toml. See README for setup."
        )
    if not master_resume_md.strip():
        raise TailorError(
            "Master resume is empty. Fill in data/master_resume.md before tailoring."
        )

    try:
        import anthropic  # lazy import so offline tests run without the package
    except ImportError as e:
        raise TailorError(
            "The 'anthropic' package is not installed. Run: pip install -r requirements.txt"
        ) from e

    client = anthropic.Anthropic(api_key=resolved_key)
    user_prompt = _build_user_prompt(job, master_resume_md)

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": user_prompt},
            ],
        )
    except anthropic.APIError as e:
        raise TailorError(f"Anthropic API error: {e}") from e
    except Exception as e:  # noqa: BLE001 — surface anything else as a clean banner
        raise TailorError(f"Unexpected error calling Anthropic: {e}") from e

    raw = "".join(
        getattr(block, "text", "") for block in msg.content
        if getattr(block, "type", None) == "text"
    )
    # Defensive parser tolerates code fences and stray prose, so we rely on the
    # system prompt + parser rather than assistant-message prefill (which newer
    # Claude models like sonnet-4-6 don't support).
    return _parse_json_response(raw, is_mock=False)


def _build_user_prompt(job: Job, master_resume_md: str) -> str:
    return (
        "JOB\n"
        f"Title: {job.get('title', '')}\n"
        f"Company: {job.get('company', '')}\n"
        f"Location: {job.get('location', '')}\n"
        f"Salary: {job.get('salary') or '(not specified)'}\n"
        f"Employment type: {job.get('employment_type') or '(not specified)'}\n\n"
        "Description:\n"
        f"{job.get('description', '')}\n\n"
        "---\n\n"
        "MASTER RESUME (source of truth — do not invent beyond this):\n"
        f"{master_resume_md}\n"
    )


# --- Parsing + validation -----------------------------------------------

def _parse_json_response(raw: str, *, is_mock: bool) -> TailoredResult:
    text = raw.strip()
    # Strip code fences if the model ignored the instruction.
    if text.startswith("```"):
        # Drop the first fence line.
        if "\n" in text:
            text = text.split("\n", 1)[1]
        if text.endswith("```"):
            text = text.rsplit("```", 1)[0]
        text = text.strip()

    data: object
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Last-ditch recovery: take the substring between the first '{' and last '}'.
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise TailorError("Model output was not parseable as JSON.")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise TailorError(f"Model output was not parseable as JSON: {e}") from e

    return _validate_result(data, is_mock=is_mock)


def _validate_result(data: object, *, is_mock: bool) -> TailoredResult:
    if not isinstance(data, dict):
        raise TailorError("Model output was JSON but not an object.")

    required = ("tailored_resume_md", "cover_note_md", "match_summary", "missing_requirements")
    for key in required:
        if key not in data:
            raise TailorError(f"Model output is missing required key: {key}")

    resume = data["tailored_resume_md"]
    cover = data["cover_note_md"]
    summary = data["match_summary"]
    missing = data["missing_requirements"]

    if not isinstance(resume, str) or not resume.strip():
        raise TailorError("tailored_resume_md must be a non-empty string.")
    if not isinstance(cover, str) or not cover.strip():
        raise TailorError("cover_note_md must be a non-empty string.")
    if not isinstance(summary, str):
        raise TailorError("match_summary must be a string.")
    if not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
        raise TailorError("missing_requirements must be a list of strings.")

    return {
        "tailored_resume_md": resume.strip(),
        "cover_note_md": cover.strip(),
        "match_summary": summary.strip(),
        "missing_requirements": [m.strip() for m in missing if m.strip()],
        "is_mock": is_mock,
    }


# --- Mock fallback (debug only) -----------------------------------------

def _tailor_mock(job: Job, master_resume_md: str) -> TailoredResult:
    """Return a recognisable hardcoded draft. Used by the sidebar debug toggle."""
    title = job["title"]
    company = job["company"]
    location = job.get("location", "")

    resume_preview = (master_resume_md.strip().splitlines() or ["(master_resume.md is empty)"])[0]

    return {
        "tailored_resume_md": (
            f"# Tailored Resume — {title} at {company}\n\n"
            f"*MOCK draft. The real Claude call is bypassed.*\n\n"
            f"## Summary\n"
            f"Marketing professional with hands-on experience across paid social, Google Ads,\n"
            f"SEO, and content. Based near {location or 'Sydney'}.\n\n"
            f"## Highlights\n"
            f"- Led paid social + Google Ads campaigns end-to-end\n"
            f"- Built and maintained the content calendar across blog, email and social\n"
            f"- Lifted organic traffic via on-page SEO\n\n"
            f"## Source line from master resume\n> {resume_preview}\n"
        ),
        "cover_note_md": (
            f"Hi {company} team,\n\n"
            f"I'd love to be considered for the {title} role. My background lines up neatly "
            f"with what you're after: I've owned content calendars, run paid social and "
            f"Google Ads, and reported on performance to leadership.\n\n"
            f"Happy to share examples in a quick chat.\n\n"
            f"Cheers,\n(Your name)\n\n"
            f"*MOCK cover note. The real Claude call is bypassed.*"
        ),
        "match_summary": (
            "MOCK match summary. Real summaries come from Claude based on the master resume."
        ),
        "missing_requirements": ["(mock) any requirement Claude would normally flag goes here"],
        "is_mock": True,
    }
