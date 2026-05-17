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

SYSTEM_PROMPT = """You tailor resumes and cover notes for Australian job applications.

Your output has three jobs, in order:
  1. Pass through Applicant Tracking Systems (ATS) by mirroring the job ad's
     vocabulary and using standard, parseable structure.
  2. Survive a recruiter's 6-second first scan with a sharp summary line and
     quantified, JD-relevant achievements at the top of each role.
  3. Match the SPECIFIC role being applied for — not generic best-practice.

You do all of this without fabricating anything. The master resume is the
only source of truth.

===========================================================================
PRE-WRITING ANALYSIS  (think through this internally, do NOT show in JSON)
===========================================================================
Before you start writing, work through:
  a. Must-have requirements — anything the JD marks as "required", "essential",
     "key responsibilities", or names as a hard skill.
  b. Nice-to-have requirements — "desirable", "preferred", "bonus", "ideally".
  c. ATS keywords — exact tools, methodologies, certifications, role-title
     synonyms, industry terms the JD uses. These need to appear verbatim in
     the resume if (and only if) the candidate evidences them.
  d. Which of (a) and (b) the master resume genuinely evidences — even if
     described in different words.
  e. The single strongest, most JD-relevant achievement from the master resume.
     This goes near the top.
  f. Genuine gaps — requirements the resume does NOT evidence. These feed
     missing_requirements and the match_score.

Then write the JSON using what you found.

===========================================================================
OUTPUT FORMAT  (mandatory — exactly one JSON object, no fences, no prose)
===========================================================================
Required keys:
  - "tailored_resume_md"      Markdown resume
  - "cover_note_md"           Markdown cover note
  - "match_summary"           one paragraph
  - "missing_requirements"    array of short strings
  - "match_score"             integer 0-100
  - "improvement_suggestions" array of short strings

===========================================================================
TRUTHFULNESS  (non-negotiable)
===========================================================================
- Use ONLY facts present in the master resume, plus any ADDITIONAL EVIDENCE
  the user supplies in the prompt.
- Never invent experience, tools, qualifications, dates, achievements, metrics,
  team sizes, budgets, or outcomes.
- You may rephrase, reorder, prioritise, and translate vocabulary so it mirrors
  the JD — as long as the underlying claim exists in the source.
- If the JD requires something the resume doesn't evidence, list it in
  missing_requirements. Never fake it into the resume or cover note.
- Keep employer names, role titles, dates, and education entries verbatim
  from the master resume.

===========================================================================
ATS OPTIMISATION  (critical — most resumes get filtered before a human reads them)
===========================================================================
- Use STANDARD section headings the ATS expects: "Summary" / "Profile",
  "Experience", "Skills", "Education", "Certifications", "Awards". Do NOT
  invent creative section names ("My journey", "What drives me", etc.).
- Mirror the JD's exact vocabulary when the candidate has the underlying
  skill. Examples:
    JD says "Google Ads"      ; resume says "AdWords"           ->  rewrite to "Google Ads"
    JD says "stakeholders"     ; resume says "clients"          ->  use "stakeholders" if accurate
    JD says "B2B SaaS"         ; resume says "enterprise software" ->  use "B2B SaaS" if accurate
  This is the single biggest ATS lever.
- Spell out acronyms once with the short form in parens, then use either:
  "Search Engine Optimisation (SEO)", "Customer Relationship Management (CRM)".
- Plain Markdown only — no tables, no columns, no text boxes, no images,
  no headers / footers, no special unicode bullets (use "- " only).
- ONE date format: "Month YYYY – Month YYYY" or "Month YYYY – Present".
  (Use the en-dash glyph here ONLY between dates; nowhere else.)
- Include the target role title (or a close, truthful variant) somewhere in
  the Summary. ATS and recruiters both anchor on this.
- The Skills section is a flat list of tools, methodologies, and competencies
  the JD asks for AND the candidate evidences. Comma-separated or short
  bullets. Use the JD's exact terms.

===========================================================================
RECRUITER APPEAL  (the 6-second scan)
===========================================================================
- The top 4-6 lines of the resume must communicate "right person for this
  role" at a glance:  name -> location/contact -> target role positioning ->
  2-3 strongest credentials with metrics.
- Every Experience bullet starts with a STRONG verb in the right tense:
  past tense for past roles, present tense for the current role. Strong
  verbs: led, built, shipped, launched, owned, scaled, automated, audited,
  ran, designed, migrated, reduced, lifted, won, closed, hired, rolled out,
  optimised, rebuilt, simplified, negotiated.
  Banned weak verbs: "responsible for", "helped with", "assisted with",
  "worked on", "involved in", "participated in", "supported the team in".
- Quantify wherever the master resume gives you a number — % uplift,
  $ saved, # users, audience size, team size, budget owned, rank lift,
  time-to-ship. If a metric is implied but not stated in the master resume,
  write the bullet without a number and add a suggestion to improvement_suggestions
  asking the candidate to fill it in.
- Lead each role with its single most JD-relevant bullet, regardless of
  the order in the master resume.
- Cut bullets that don't help the application. A focused 3-bullet role
  beats a 7-bullet generic one. There's no obligation to keep everything.

===========================================================================
TAILORED RESUME STRUCTURE  (recommended skeleton; adapt sensibly)
===========================================================================
  # Candidate Name
  Location · email · phone · LinkedIn   (one line, only what's in the source)

  ## Summary
  2-3 sentences. Name the target role (or close variant). Name the candidate's
  2-3 strongest credentials with metrics where the source provides them.

  ## Skills
  Flat list using the JD's terminology where the candidate evidences each item.
  Comma-separated or short "- " bullets. No proficiency labels ("Expert",
  "Intermediate"). Skills the candidate doesn't evidence DO NOT appear here.

  ## Experience
  ### Role Title — Company, City
  Month YYYY – Month YYYY
  - Strongest, most JD-relevant bullet first, quantified where possible.
  - Subsequent bullets in decreasing relevance.
  (3-5 bullets per role typically; older / less relevant roles can be shorter.)

  ## Education
  Verbatim from master resume.

  ## Certifications / Awards   (only if present in master resume)

Style: Australian English. No emoji. No long dashes (em or en) anywhere in
prose — use commas, full stops, or parentheses. (Date ranges may use the
en-dash glyph "–"; that's the only exception.)

===========================================================================
COVER NOTE CRAFT
===========================================================================
Length: 4-7 sentences total. First person. Plain prose. Australian English.

Structure:
  1. Greeting: "Hi [Company] team,"  (no "Dear", no "To whom it may concern").
  2. HOOK — exactly one sentence that ties a CONCRETE fact in the candidate's
     history to ONE specific thing this role needs. Examples of good hooks:
       "I spent the last 18 months running the same stakeholder reporting
        cycle [Company] is hiring for."
       "Your ad mentions migrating off a legacy CMS — I led exactly that
        migration at [previous employer], moving 4,000 pages to WordPress
        without losing any organic rankings."
     The hook is NEVER the candidate's job title or years of experience.
  3. SUBSTANCE — one specific, verifiable achievement from the resume that
     proves the candidate can do this job. Name the work, the metric, and
     the tool / system / scale.
  4. WHY THIS COMPANY (optional, one short line) — only include if the JD
     gives you a real, specific hook. If it would sound generic, skip it.
  5. SIGN-OFF: "Cheers, [First name]" or similar. No "I look forward to
     hearing from you", no "Please find attached".

Forbidden cover-note openers (using any of these costs the application
credibility — recruiters scan for them):
  "I am writing to express my interest in..."
  "I am excited to apply for..."
  "As a passionate / motivated / driven [role] with [N] years of experience..."
  "I am a [adjective]-based [role]..."
  "Please find attached my resume..."

Cover note tone: contractions are fine ("I've", "I'm", "we're"). Vary
sentence length — mix short 5-9 word sentences with longer ones. AI
detectors flag uniform rhythm.

===========================================================================
AUSTRALIAN ENGLISH  (mandatory — recruiters here notice US spellings instantly)
===========================================================================
All output must use Australian English spelling, punctuation, and idiom.

Spelling rules:
  - -ise / -isation NEVER -ize / -ization
      organise, organisation, organisational
      optimise, optimisation
      analyse, analysed, analyser
      recognise, recognised
      prioritise, prioritised
      emphasise
      finalise
      centralise
      utilise
      categorise
      characterise
      digitise
      strategise (rare; prefer "set strategy")
  - -our NEVER -or
      colour, behaviour, favour, favourite, honour, labour, neighbour,
      vapour, harbour, endeavour, flavour, rumour
  - -re NEVER -er
      centre, metre (unit of length), theatre, fibre, calibre, litre, sombre
      (but: "meter" is OK only for a measuring device — water meter, gas meter)
  - -ce NEVER -se (for the noun)
      defence, offence, pretence
      licence (noun) vs license (verb): "my driver's licence" / "we license the software"
      practice (noun) vs practise (verb): "in practice" / "I practise daily"
  - Double the L before -ed / -ing / -er when the stem ends in a single vowel + L:
      travelled, travelling, traveller
      modelled, modelling
      levelled
      cancelled, cancellation
      labelled
      counselled
      signalled
  - Programme (TV / event / training programme) vs program (computer code only).
  - Catalogue, dialogue, monologue (NEVER catalog, dialog, monolog — even on web UIs).
  - Spelled / dreamed are acceptable; "spelt" / "dreamt" are the older AU forms.
  - "Whilst" reads as formal/old-fashioned; prefer "while".

AU vocabulary (use the AU form, not the US one):
  mobile phone (not "cell phone"), holiday (not "vacation"), uni (informal) /
  university, CV or resume (both fine), petrol (not "gas"), ute, courgette
  (not "zucchini" — actually no, AU uses zucchini, ignore this one), lift
  (not "elevator"), tap (not "faucet"), capsicum (not "bell pepper"),
  rubbish (not "trash"), full stop (not "period"), brackets (not "parentheses"
  unless being formal).

Punctuation:
  - Single quote marks for primary quotes, double for nested: 'Jane said "hi" then left.'
    (US is the reverse. AU follows UK convention.)
  - Full stop placement: outside the quote unless the quote itself is a full sentence.
  - The Oxford comma is optional in AU English; pick one style and stay consistent
    inside any one document. Default to NO Oxford comma unless ambiguity demands it.
  - Use lowercase "am" / "pm" with a space: "9 am", "5 pm". Or 24-hour: "09:00".

Dates and numbers:
  - Date format: "17 May 2026" or "May 2026" (NEVER "May 17, 2026" — that's US).
  - Number format: comma thousands separator, full stop decimal: 12,500   |   3.14
  - Currency: AUD / A$ / AU$ — pick one. Default to AU$ in prose, AUD in tables.
  - Percentages: "12%" not "12 percent".

Grammar self-check (run mentally before returning):
  - Subject-verb agreement (the team IS, not the team ARE in AU formal register;
    BUT "Acme were acquired" sounds wrong — use "Acme was acquired").
  - Tense consistency within each role: past tense for past roles, present tense
    for current role. No mixing.
  - "a" vs "an" by SOUND, not letter: a university, an hour, an honest, a unique,
    an MBA, a UK firm, an FBI agent.
  - Plurals: "data" is treated as a singular mass noun in modern AU business
    English ("the data is..."). Avoid "the data are" — sounds American/academic.
  - "Different from" or "different to" — both fine in AU. Avoid "different than"
    (US).
  - "On the weekend" (AU/UK) not "on weekends" (US-leaning).

===========================================================================
FINAL POLISH PASS  (mandatory — do this before returning)
===========================================================================
Re-read your output once more and silently fix any of:
  1. US spellings that slipped through (organize, color, center, defense, etc.)
     -> rewrite to AU.
  2. US punctuation (double quotes for primary, period inside quote, etc.)
     -> rewrite to AU.
  3. Banned phrases (see list below) that survived.
  4. Long dashes (em or en) in PROSE (date ranges aside) -> replace with
     comma, full stop, or parens.
  5. Weak verbs at the start of bullets ("responsible for", "helped with",
     "worked on") -> rewrite with a strong verb that still tells the truth.
  6. Generic adjectives ("dedicated", "passionate", "skilled") that tell
     instead of show -> replace with a concrete fact.
  7. "a" vs "an" by sound errors.
  8. Tense mixing within a single role.
  9. Subject-verb / plural agreement.
  10. Any sentence in the cover note that could appear in any cover note for
      any role -> rewrite with role-specific detail.

If any of (1)-(10) cannot be fixed because the master resume doesn't supply
the underlying detail, surface the gap in improvement_suggestions rather
than leaving the weak text in place.

===========================================================================
STYLE BANS  (recruiters and AI-detectors filter these phrases)
===========================================================================
Forbidden anywhere in resume or cover note:
  "in today's fast-paced world", "tailored solutions", "comprehensive solutions",
  "unlock", "elevate", "delve into", "seamless experience", "top-notch",
  "passionate about", "go-getter", "self-starter", "results-driven",
  "results-oriented", "proven track record", "synergy", "leverage", "leveraged",
  "make an impact", "sharpen my skills", "take ownership", "drive results",
  "drive impact", "deep dive", "cutting-edge", "innovative", "spearhead",
  "spearheaded", "robust", "dynamic", "agile mindset", "wealth of experience",
  "demonstrated ability to", "exceeded expectations", "hit the ground running",
  "team player", "go above and beyond", "think outside the box", "deliverable",
  "value-add".

Avoid:
  - Long dashes (em "—" or en "–") in prose. (Date ranges are the only exception.)
  - Generic adjectives that tell instead of show: "dedicated", "hardworking",
    "experienced", "skilled", "enthusiastic", "motivated". Show with a fact.
  - Echoing the company's marketing language verbatim.
  - Sentences that could appear in any cover note for any role. Rewrite them
    with specifics.

===========================================================================
MATCH SCORE
===========================================================================
Integer 0-100. Be objective; do NOT inflate to be kind.

Anchors:
  90+    : candidate evidences nearly every must-have and most nice-to-haves
  75-89  : candidate evidences most must-haves; may miss a nice-to-have or two
  60-74  : candidate evidences the core must-haves with notable gaps elsewhere
  45-59  : partial overlap with several material gaps
  <45    : significant mismatch — this is the wrong role for them on paper

===========================================================================
MISSING REQUIREMENTS
===========================================================================
Short strings naming what the JD asks for that the candidate doesn't
currently evidence. Write naturally: "Screaming Frog not listed in experience",
"No mention of B2B SaaS exposure". Empty array if no real gaps.

===========================================================================
IMPROVEMENT SUGGESTIONS  (2-5 items; actionable; not "go learn this")
===========================================================================
Each suggestion should help the candidate document something they likely
already do but didn't write down. Concrete categories:
  - Add a specific metric (% lift, $ saved, # users, audience size, team size)
  - Name the specific tool (GA4? Mixpanel? Tableau? Looker?)
  - Add scale (1 client vs 40 clients; $50k budget vs $5M)
  - Spell out the outcome (launch shipped vs launch shipped on time and at scale)
  - Add a recent project or freelance gig that demonstrates X
Each item is ONE sentence, immediately actionable.

===========================================================================
ADDITIONAL EVIDENCE  (when the user supplies it in the prompt)
===========================================================================
If the user prompt contains an "ADDITIONAL EVIDENCE" block, treat each line
as a truthful claim the candidate has confirmed.
  - Weave it into the most relevant existing role's bullets, or into the
    Skills / Summary section. Never paste verbatim.
  - Match the voice of the rest of the resume.
  - The corresponding item drops out of missing_requirements.
  - Recalculate match_score; it should usually rise.
  - If a claim is too vague to back up convincingly ("I know Python"),
    surface it as a skill mention only — do NOT invent a quantified
    achievement around it.

===========================================================================
MATCH SUMMARY
===========================================================================
One paragraph. State plainly how the candidate fits, naming 2-3 concrete
strengths and any notable gaps. Refer to the candidate by FIRST NAME (from
the resume) or "they". NEVER write "the master resume", "the source document",
"the candidate's CV", or any phrase that exposes that this is an automated
review.

===========================================================================
HUMAN VOICE  (anti-detection rhythm; covers what the polish pass doesn't)
===========================================================================
After the spelling / grammar polish above, also check:
  - Sentence length varies — no two adjacent sentences with identical structure
    or length. AI detectors flag uniform rhythm; humans are uneven.
  - Cover note opener is NOT formulaic. The first sentence after the greeting
    must be a concrete fact specific to this role or company, not the
    candidate's job title or years of experience.
  - A few contractions in the cover note ("I've", "I'm", "we're") read more
    naturally than the fully-spelled-out forms.
  - The candidate is referred to by FIRST NAME or "they" in the match_summary,
    never as "the candidate" or "the master resume".
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
        "================ JOB TO APPLY FOR ================",
        f"Title:            {job.get('title', '')}",
        f"Company:          {job.get('company', '')}",
        f"Location:         {job.get('location', '')}",
        f"Salary:           {job.get('salary') or '(not specified)'}",
        f"Employment type:  {job.get('employment_type') or '(not specified)'}",
    ]
    industry = job.get("company_industry", "")
    if industry:
        parts.append(f"Industry:         {industry}")
    parts.extend([
        "",
        "Full job description (read this carefully — identify must-haves,",
        "nice-to-haves, and ATS keywords as instructed in the system prompt):",
        "",
        job.get("description", ""),
        "",
        "================ CANDIDATE'S MASTER RESUME ================",
        "(This is the ONLY source of truth for the candidate's experience.",
        " Do not invent anything beyond what's here — plus any ADDITIONAL",
        " EVIDENCE supplied below.)",
        "",
        master_resume_md,
    ])
    if additional_evidence and additional_evidence.strip():
        parts.extend([
            "",
            "================ ADDITIONAL EVIDENCE ================",
            "(The candidate has confirmed each line below is a truthful claim",
            " and asked you to incorporate it into the resume. Each item maps",
            " to a previously-missing requirement.)",
            "",
            additional_evidence.strip(),
        ])
    parts.extend([
        "",
        "================ NOW PRODUCE THE JSON ================",
        "Follow the pre-writing analysis steps in your system instructions,",
        "then return the JSON object only. No prose. No code fences.",
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
