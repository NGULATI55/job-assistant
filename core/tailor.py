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
    match_score: int                       # 0-100 fit percentage
    improvement_suggestions: list[str]     # actionable additions to lift the score
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
- "match_score": integer 0 to 100
- "improvement_suggestions": array of short strings

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

MATCH SCORE
- "match_score": integer 0 to 100 representing how well the candidate's listed experience evidences the role's requirements.
- Anchor points: 90+ = strong fit, hits almost every stated requirement; 70 to 89 = good fit with a few gaps; 50 to 69 = some fit, several gaps; below 50 = significant mismatch.
- Be objective. Do not inflate to be kind. The score should match what an experienced recruiter would give on a first read.

IMPROVEMENT SUGGESTIONS
- "improvement_suggestions": array of 2 to 5 short, actionable strings explaining what the candidate could add to their resume to lift the score.
- Each item should suggest a SKILL, TOOL, METRIC, or PROJECT DETAIL they could surface — not advice that requires acquiring new experience.
- Examples: "Add specific Python frameworks you've used (Django? Flask?)", "Quantify the SEO traffic uplift with a percentage", "Mention the team size you've managed".
- The point is to help the candidate write down things they likely already know but haven't documented.
- Return [] only if the resume is already a strong match.

ADDITIONAL EVIDENCE (when supplied by the user)
- If the user prompt contains an "ADDITIONAL EVIDENCE" block, treat each line as a truthful claim the candidate vouches for.
- Incorporate the evidence naturally into the most appropriate section of the resume (skills list, a specific role's bullets, summary). Rewrite to match the voice of the rest of the resume — never paste it verbatim.
- After incorporation, the corresponding item should disappear from missing_requirements.
- Recalculate match_score to reflect the post-incorporation reality (it should typically rise).
- If a claim is too vague to back up convincingly, add it as a brief skill mention only; do NOT invent a quantified achievement around it.
"""


# --- Public API ---------------------------------------------------------

def tailor(
    job: Job,
    master_resume_md: str,
    *,
    use_mock: bool = False,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    additional_evidence: str | None = None,
) -> TailoredResult:
    """Generate a tailored resume + cover note + match summary + missing requirements.

    Pass `additional_evidence` (free-form text) on a refinement pass to feed
    Claude extra details the candidate has supplied for missing requirements.
    Claude will weave them into the resume and recompute the match score.

    Raises TailorError on any failure (missing API key, empty master resume, API
    error, unparseable output). The UI catches this and shows a banner without
    crashing the app.
    """
    if use_mock:
        return _tailor_mock(job, master_resume_md, additional_evidence=additional_evidence)
    return _tailor_with_claude(
        job,
        master_resume_md,
        api_key=api_key,
        model=model,
        additional_evidence=additional_evidence,
    )


# --- Real Claude call ---------------------------------------------------

def _tailor_with_claude(
    job: Job,
    master_resume_md: str,
    *,
    api_key: str | None,
    model: str,
    additional_evidence: str | None = None,
) -> TailoredResult:
    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
    if not resolved_key.strip():
        raise TailorError(
            "No API key set. Paste your key in the sidebar, or enable Mock mode for a sample draft."
        )
    if not master_resume_md.strip():
        raise TailorError(
            "Upload a resume before generating a draft."
        )

    try:
        import anthropic  # lazy import so offline tests run without the package
    except ImportError as e:
        raise TailorError(
            "Tailoring engine isn't available right now."
        ) from e

    client = anthropic.Anthropic(api_key=resolved_key)
    user_prompt = _build_user_prompt(job, master_resume_md, additional_evidence)

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
        raise TailorError("Tailoring service is unavailable right now. Try again in a moment.") from e
    except Exception as e:  # noqa: BLE001 — surface anything else as a clean banner
        raise TailorError("Tailoring service is unavailable right now. Try again in a moment.") from e

    raw = "".join(
        getattr(block, "text", "") for block in msg.content
        if getattr(block, "type", None) == "text"
    )
    # Defensive parser tolerates code fences and stray prose, so we rely on the
    # system prompt + parser rather than assistant-message prefill (which newer
    # Claude models like sonnet-4-6 don't support).
    return _parse_json_response(raw, is_mock=False)


def _build_user_prompt(
    job: Job,
    master_resume_md: str,
    additional_evidence: str | None = None,
) -> str:
    parts = [
        "JOB",
        f"Title: {job.get('title', '')}",
        f"Company: {job.get('company', '')}",
        f"Location: {job.get('location', '')}",
        f"Salary: {job.get('salary') or '(not specified)'}",
        f"Employment type: {job.get('employment_type') or '(not specified)'}",
        "",
        "Description:",
        job.get("description", ""),
        "",
        "---",
        "",
        "MASTER RESUME (source of truth — do not invent beyond this):",
        master_resume_md,
    ]
    if additional_evidence and additional_evidence.strip():
        parts.extend([
            "",
            "---",
            "",
            "ADDITIONAL EVIDENCE (the candidate has confirmed these are true and asked you to incorporate them):",
            additional_evidence.strip(),
        ])
    return "\n".join(parts) + "\n"


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
            raise TailorError("The draft came back in an unexpected format. Try again in a moment.")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise TailorError("The draft came back in an unexpected format. Try again in a moment.") from e

    return _validate_result(data, is_mock=is_mock)


def _validate_result(data: object, *, is_mock: bool) -> TailoredResult:
    if not isinstance(data, dict):
        raise TailorError("The draft came back in an unexpected format. Try again in a moment.")

    required = ("tailored_resume_md", "cover_note_md", "match_summary", "missing_requirements")
    for key in required:
        if key not in data:
            raise TailorError("The draft came back incomplete. Try again in a moment.")

    resume = data["tailored_resume_md"]
    cover = data["cover_note_md"]
    summary = data["match_summary"]
    missing = data["missing_requirements"]

    if not isinstance(resume, str) or not resume.strip():
        raise TailorError("The draft came back without a resume. Try again in a moment.")
    if not isinstance(cover, str) or not cover.strip():
        raise TailorError("The draft came back without a cover note. Try again in a moment.")
    if not isinstance(summary, str):
        raise TailorError("The draft came back in an unexpected format. Try again in a moment.")
    if not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
        raise TailorError("The draft came back in an unexpected format. Try again in a moment.")

    # Optional new fields — accept missing/bad shapes gracefully so older
    # responses or mid-stream changes don't break the parse.
    raw_score = data.get("match_score", 0)
    try:
        match_score = int(raw_score)
    except (TypeError, ValueError):
        match_score = 0
    match_score = max(0, min(100, match_score))

    raw_suggestions = data.get("improvement_suggestions", [])
    if isinstance(raw_suggestions, list):
        suggestions = [str(s).strip() for s in raw_suggestions if str(s).strip()]
    else:
        suggestions = []

    return {
        "tailored_resume_md": resume.strip(),
        "cover_note_md": cover.strip(),
        "match_summary": summary.strip(),
        "missing_requirements": [m.strip() for m in missing if m.strip()],
        "match_score": match_score,
        "improvement_suggestions": suggestions,
        "is_mock": is_mock,
    }


# --- Mock fallback (debug only) -----------------------------------------

def _tailor_mock(
    job: Job,
    master_resume_md: str,
    additional_evidence: str | None = None,
) -> TailoredResult:
    """Return a recognisable hardcoded draft. Used by the sidebar debug toggle."""
    title = job["title"]
    company = job["company"]
    location = job.get("location", "")

    resume_preview = (master_resume_md.strip().splitlines() or ["(master_resume.md is empty)"])[0]
    refined = bool(additional_evidence and additional_evidence.strip())

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
            f"- Lifted organic traffic via on-page SEO\n"
            + ("- (Mock) Incorporated your additional evidence into a relevant bullet\n" if refined else "")
            + f"\n## Source line from master resume\n> {resume_preview}\n"
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
        "missing_requirements": (
            [] if refined else ["(mock) any requirement Claude would normally flag goes here"]
        ),
        "match_score": 82 if refined else 68,
        "improvement_suggestions": (
            [
                "(mock) Quantify the SEO bullet with a traffic percentage",
                "(mock) Name the specific paid-social platforms (Meta, TikTok, LinkedIn)",
            ]
            if not refined
            else ["(mock) Resume now aligns more closely with the role's must-haves"]
        ),
        "is_mock": True,
    }
