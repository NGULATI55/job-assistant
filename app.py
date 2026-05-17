"""Job Application Assistant.

Two run modes:
- Personal (default): resumes live in data/resumes/, applications saved to disk.
- Multi-user (MULTI_USER=1): session-isolated. Resumes in memory, saved outputs
  delivered as a zip download. Each visitor pastes their own Anthropic API key.

Run locally:
    streamlit run app.py
Run as a shareable hosted instance (e.g. on Streamlit Cloud):
    Set env var MULTI_USER=1, then deploy.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from pathlib import Path

import streamlit as st

from core import seek_fetch, resume_loader, tailor, saver, exporter, job_search, ats_feeds


# --- Playwright bootstrap (for hosted deployments like Streamlit Cloud) ---
# On a fresh container the Chromium binary isn't installed yet. Run the install
# once per session and cache the result. Local dev where chromium is already
# installed hits this in <100ms.

@st.cache_resource(show_spinner=False)
def _ensure_chromium_installed() -> bool:
    import subprocess  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415
    try:
        from playwright.sync_api import sync_playwright  # noqa: PLC0415
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:  # noqa: BLE001
        # Either Playwright is missing or the browser isn't downloaded yet.
        try:
            subprocess.run(
                [_sys.executable, "-m", "playwright", "install", "chromium"],
                check=False,
                capture_output=True,
                timeout=600,
            )
            return True
        except Exception:  # noqa: BLE001
            return False


_ensure_chromium_installed()


# --- Template preview helpers (PDF -> PNG, cached) ----------------------

_PREVIEW_SAMPLE_MD = """# Sample Name
Sydney NSW · sample@email.com · +61 412 345 678

## Summary
Senior marketing specialist with eight years across paid social, SEO, content
strategy and analytics. Comfortable working with stakeholders and reporting
results to leadership in plain English.

## Experience

### Marketing Lead, Acme Pty Ltd
Jan 2022 – Present

- Led paid social and Google Ads end-to-end across five client accounts.
- Built and maintained the content calendar across blog, email and social.
- Reported weekly performance to the executive team via GA4 dashboards.

### Senior Marketing Specialist, Foo Digital
Mar 2018 – Dec 2021

- Owned the brand voice across all customer-facing channels.
- Lifted organic traffic by 64% via on-page SEO and topical clusters.

## Skills
- Paid social and Google Ads
- Content strategy and copywriting
- GA4 and basic SQL

## Education
- BCom Marketing, University of Sydney (2014)

## Tools
- GA4, Search Console, SEMrush
"""


@st.cache_data(show_spinner=False, max_entries=8)
def _render_all_template_previews(md_text: str, dpi: int = 90) -> dict[str, bytes]:
    """Render thumbnails for all 4 styles in a single Playwright session. Cached.

    One browser launch for the whole grid instead of four — drops initial load
    from ~8 seconds to ~3.
    """
    import fitz
    from core import html_templates
    blocks = exporter._parse_md_blocks(md_text)
    htmls = {
        key: html_templates.render_html(blocks, key)
        for key in html_templates._BUILDERS.keys()
    }
    pdfs = exporter.batch_html_to_pdf_bytes(htmls)
    pngs: dict[str, bytes] = {}
    for key, pdf in pdfs.items():
        doc = fitz.open(stream=pdf, filetype="pdf")
        try:
            pngs[key] = doc[0].get_pixmap(dpi=dpi).tobytes("png")
        finally:
            doc.close()
    return pngs


@st.cache_data(show_spinner=False, max_entries=32)
def _render_template_preview(md_text: str, style_key: str, dpi: int = 130) -> bytes:
    """Render a single style's first page as PNG bytes. Cached. Used for the larger expanded preview."""
    import fitz
    pdf_bytes = exporter.markdown_to_pdf_bytes(md_text, style=style_key)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        return doc[0].get_pixmap(dpi=dpi).tobytes("png")
    finally:
        doc.close()

# --- Mode + paths --------------------------------------------------------
MULTI_USER = os.environ.get("MULTI_USER", "").strip().lower() in ("1", "true", "yes")

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "data"
RESUMES_DIR = DATA_DIR / "resumes"
APPLICATIONS_DIR = DATA_DIR / "applications"
if not MULTI_USER:
    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    APPLICATIONS_DIR.mkdir(parents=True, exist_ok=True)


# --- Page setup ----------------------------------------------------------
st.set_page_config(
    page_title="Job Application Assistant",
    page_icon="📝",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Small CSS pass — tighten top spacing and improve readability without going deep.
st.markdown(
    """
    <style>
      .block-container { padding-top: 2rem; padding-bottom: 4rem; }
      h1 { letter-spacing: -0.02em; }
      .privacy-badge {
        display: inline-block;
        padding: 2px 10px;
        border-radius: 999px;
        background: #e8f5e9;
        color: #1b5e20;
        font-size: 0.78rem;
        font-weight: 600;
        margin-left: 0.5rem;
        vertical-align: middle;
      }
      .step-num {
        display: inline-block;
        width: 1.6rem;
        height: 1.6rem;
        line-height: 1.6rem;
        text-align: center;
        background: #1a73e8;
        color: white;
        border-radius: 999px;
        font-size: 0.85rem;
        margin-right: 0.5rem;
        vertical-align: middle;
      }
      .footer-note {
        margin-top: 3rem;
        padding-top: 1rem;
        border-top: 1px solid #eee;
        color: #666;
        font-size: 0.85rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# --- Header --------------------------------------------------------------
mode_label = "Hosted · session-only" if MULTI_USER else "Local · private"
st.markdown(
    f"""
    <h1>Job Application Assistant
        <span class="privacy-badge">Private</span>
    </h1>
    <p style="color: #555; font-size: 1.05rem; margin-top: -0.5rem;">
        Upload your resume, pick a job, and get a tailored draft + short cover note for your review.
        Nothing is submitted on your behalf.
    </p>
    """,
    unsafe_allow_html=True,
)


# --- Session state -------------------------------------------------------
ss = st.session_state
ss.setdefault("auth_ok", False)


# --- Access gate (email OTP > password > open) --------------------------
# Email OTP activates when SMTP_USER + SMTP_PASS + ALLOWED_EMAILS are all set.
# Password gate activates when only APP_PASSWORD is set.
# Otherwise the app is open (for local dev).

def _secret(name: str) -> str:
    """Read a value from Streamlit secrets, fall back to env var."""
    try:
        if hasattr(st, "secrets"):
            v = st.secrets.get(name)
            if v:
                return str(v).strip()
    except Exception:  # noqa: BLE001 — st.secrets raises when secrets file missing
        pass
    return os.environ.get(name, "").strip()


def _resolve_otp_config() -> dict | None:
    smtp_user = _secret("SMTP_USER")
    smtp_pass = _secret("SMTP_PASS")
    allowed = _secret("ALLOWED_EMAILS")
    if not (smtp_user and smtp_pass and allowed):
        return None
    return {"smtp_user": smtp_user, "smtp_pass": smtp_pass, "allowed": allowed}


def _email_otp_gate(cfg: dict) -> bool:
    """Two-step email OTP login with rolling 5-min session."""
    import time as _t
    from core import auth as _auth

    now = _t.time()
    # Valid session? Roll the expiry forward 5 min on this rerun (active use extends it).
    if ss.get("auth_ok") and ss.get("auth_expires_at", 0) > now:
        ss["auth_expires_at"] = now + _auth.SESSION_TTL_SECONDS
        return True
    # Was authenticated but session lapsed — drop it back to login screen.
    if ss.get("auth_ok"):
        ss["auth_ok"] = False
        ss.pop("auth_expires_at", None)
        ss["just_expired"] = True

    st.markdown(
        """
        <div style="max-width: 460px; margin: 4.5rem auto 0; text-align: center;">
          <h1 style="margin-bottom: 0.4rem;">Job Application Assistant</h1>
          <p style="color: #666; margin-bottom: 1.5rem;">Private deployment. Sign in with your email.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if ss.pop("just_expired", False):
        with st.container():
            _l, _m, _r = st.columns([1, 2, 1])
            with _m:
                st.info("Your session timed out after 5 minutes. Sign in again.")

    _l, _m, _r = st.columns([1, 2, 1])
    with _m:
        if "otp_pending_email" not in ss:
            # Step 1 — request a code
            with st.form("otp_request_form"):
                email = st.text_input("Your email", placeholder="you@example.com")
                requested = st.form_submit_button("Send access code", type="primary", use_container_width=True)
            if requested:
                if not email.strip():
                    st.error("Enter your email address.")
                elif not _auth.is_email_allowed(email, cfg["allowed"]):
                    st.error("This email isn't on the allowlist. Ask the admin to add it.")
                else:
                    code = _auth.generate_otp()
                    ss["otp_pending_email"] = email.strip()
                    ss["otp_code"] = code
                    ss["otp_expires_at"] = now + _auth.OTP_TTL_SECONDS
                    try:
                        _auth.send_otp_email(email, code, cfg["smtp_user"], cfg["smtp_pass"])
                        st.success(f"Code sent to {email}. Check your inbox (and spam).")
                        st.rerun()
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Could not send email: {e}")
                        ss.pop("otp_pending_email", None)
                        ss.pop("otp_code", None)
        else:
            # Step 2 — enter the code
            st.markdown(f"Code sent to **{ss['otp_pending_email']}**. Valid for 5 minutes.")
            with st.form("otp_verify_form"):
                code_input = st.text_input("6-digit code", max_chars=6, placeholder="123456")
                verified = st.form_submit_button("Log in", type="primary", use_container_width=True)
            if verified:
                if now > ss.get("otp_expires_at", 0):
                    st.error("Code expired. Request a new one.")
                    ss.pop("otp_code", None)
                    ss.pop("otp_pending_email", None)
                elif code_input.strip() == ss.get("otp_code", ""):
                    ss["auth_ok"] = True
                    ss["auth_email"] = ss["otp_pending_email"]
                    ss["auth_expires_at"] = now + _auth.SESSION_TTL_SECONDS
                    for k in ("otp_code", "otp_pending_email", "otp_expires_at"):
                        ss.pop(k, None)
                    st.rerun()
                else:
                    st.error("Incorrect code. Try again.")
            if st.button("Use a different email", use_container_width=True):
                for k in ("otp_code", "otp_pending_email", "otp_expires_at"):
                    ss.pop(k, None)
                st.rerun()
    return False


def _password_gate() -> bool:
    """Fallback shared-password gate. Active only if APP_PASSWORD is set."""
    expected = _secret("APP_PASSWORD")
    if not expected:
        return True
    if ss.get("auth_ok"):
        return True
    st.markdown(
        """
        <div style="max-width: 480px; margin: 6rem auto 0; text-align: center;">
          <h1 style="margin-bottom: 0.4rem;">Job Application Assistant</h1>
          <p style="color: #666; margin-bottom: 2rem;">
            Private deployment. Enter the access password to continue.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    _l, _m, _r = st.columns([1, 2, 1])
    with _m:
        with st.form("auth_form", clear_on_submit=False):
            pw = st.text_input("Password", type="password", label_visibility="collapsed",
                               placeholder="Access password")
            submitted = st.form_submit_button("Enter", use_container_width=True, type="primary")
        if submitted:
            if pw == expected:
                ss["auth_ok"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
    return False


def _check_access_gate() -> bool:
    """Pick the strongest configured gate. Open if nothing's configured."""
    otp_cfg = _resolve_otp_config()
    if otp_cfg:
        return _email_otp_gate(otp_cfg)
    return _password_gate()


if not _check_access_gate():
    st.stop()


ss.setdefault("job", None)
ss.setdefault("search_results", None)  # list of JobSearchResult dicts
ss.setdefault("search_query", "")
ss.setdefault("watched_companies", {})  # slug -> {platform, company_name, jobs[]}
ss.setdefault("draft", None)
ss.setdefault("saved", None)            # local mode: SavedApplication dict
ss.setdefault("download_bundle", None)  # multi-user mode: (filename, bytes, warning)
ss.setdefault("fetch_status", None)
ss.setdefault("tailor_error", None)
ss.setdefault("last_upload_marker", None)
ss.setdefault("selected_style", exporter.DEFAULT_STYLE)
# Multi-user mode keeps resumes in memory keyed by filename.
# We start empty — visitors must upload their own resume before tailoring
# or searching. The built-in template was confusing on the hosted version.
if MULTI_USER:
    ss.setdefault("resumes", {})


# --- API key resolution -------------------------------------------------

def _format_resume_label(name: str) -> str:
    """Friendly display for the resume picker. Strips .md, tags the built-in."""
    display = name[:-3] if name.endswith(".md") else name
    if name == "example.md":
        display += "  (built-in template)"
    return display


def _resolve_api_key(user_input: str) -> str:
    """User input wins. Otherwise: env var; otherwise: st.secrets.

    In MULTI_USER mode we still allow operator-provided defaults (helpful for testing),
    but the UI strongly encourages each visitor to paste their own.
    """
    if user_input.strip():
        return user_input.strip()
    env_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env_key:
        return env_key
    try:
        return (st.secrets.get("ANTHROPIC_API_KEY") if hasattr(st, "secrets") else "") or ""
    except Exception:  # noqa: BLE001 — st.secrets raises if no secrets file exists
        return ""


# --- Sidebar -----------------------------------------------------------
with st.sidebar:
    st.markdown("### Setup")

    # 1) Resume
    st.markdown("**1. Your resume**")
    uploaded = st.file_uploader(
        "Upload your resume",
        type=["pdf", "docx", "txt", "md"],
        key="resume_upload",
        help=(
            "PDF, Word (.docx), plain text (.txt), or Markdown (.md). "
            "Text is extracted automatically and stored as Markdown."
        ),
    )

    chosen_name = ""
    master_md = ""

    if MULTI_USER:
        # Session-only resume storage — extract text from PDF/DOCX/TXT/MD in memory.
        if uploaded is not None:
            marker = f"{uploaded.name}:{uploaded.size}"
            if ss["last_upload_marker"] != marker:
                try:
                    text = resume_loader.extract_text(uploaded.name, uploaded.getvalue())
                    safe_name = resume_loader.safe_resume_name(uploaded.name)
                    ss["resumes"][safe_name] = text
                    ss["last_upload_marker"] = marker
                    ss["resume_pick"] = safe_name  # auto-select the new upload
                    st.success(f"Resume uploaded ({len(text):,} chars)")
                except ValueError as e:
                    st.error(f"Could not accept upload: {e}")

        names = sorted(ss["resumes"].keys())
        if names:
            chosen_name = st.selectbox(
                "Your resumes",
                options=names,
                format_func=_format_resume_label,
                key="resume_pick",
            )
            master_md = ss["resumes"].get(chosen_name, "")
        else:
            chosen_name = ""
            master_md = ""
    else:
        # Disk-based personal mode
        if uploaded is not None:
            marker = f"{uploaded.name}:{uploaded.size}"
            if ss["last_upload_marker"] != marker:
                try:
                    saved_path = resume_loader.save_uploaded_resume(
                        RESUMES_DIR, uploaded.name, uploaded.getvalue()
                    )
                    ss["last_upload_marker"] = marker
                    ss["resume_pick"] = saved_path.name  # auto-select the new upload
                    st.success("Resume uploaded")
                except (ValueError, OSError) as e:
                    st.error(f"Could not save: {e}")

        available = resume_loader.list_resumes(RESUMES_DIR)
        if not available:
            st.warning("No resumes uploaded yet. Use the upload field above.")
        else:
            chosen_name = st.selectbox(
                "Your resumes",
                options=available,
                format_func=_format_resume_label,
                key="resume_pick",
            )
            master_md = resume_loader.load_resume_text(RESUMES_DIR / chosen_name)

    if master_md.strip():
        st.success(f"Resume loaded ({len(master_md):,} chars)")
    elif chosen_name:
        st.warning("Resume is empty — edit it before tailoring")

    # If a fallback API key is preconfigured (env or Streamlit Secrets), use it
    # silently and hide the API key + debug toggle from visitors.
    _preconfigured_key = _resolve_api_key("")

    if _preconfigured_key:
        api_key = _preconfigured_key
        use_mock = False
    else:
        st.divider()
        st.markdown("**Anthropic API key**")
        api_key_input = st.text_input(
            "API key",
            type="password",
            placeholder="sk-ant-...",
            help="Get one at console.anthropic.com. Your key is used only for your session and never stored.",
            key="api_key_input",
            label_visibility="collapsed",
        )
        api_key = _resolve_api_key(api_key_input)
        if api_key:
            st.success(f"Key set (…{api_key[-4:]})")
        else:
            st.warning("No API key. Paste one above to enable tailoring.")

        st.divider()
        use_mock = st.checkbox(
            "Use sample output (no API call)",
            value=False,
            help="Skip the Anthropic API and return a hardcoded draft. For testing only.",
        )

    # 4) Sign out (only shown when the OTP gate is active)
    if ss.get("auth_ok") and ss.get("auth_email"):
        import time as _t
        st.divider()
        remaining = max(0, int(ss.get("auth_expires_at", 0) - _t.time()))
        mins, secs = divmod(remaining, 60)
        st.caption(f"Signed in as **{ss['auth_email']}** · session expires in {mins}:{secs:02d}")
        if st.button("Sign out", use_container_width=True):
            for k in ("auth_ok", "auth_email", "auth_expires_at"):
                ss.pop(k, None)
            st.rerun()

    # 5) About
    with st.expander("Privacy"):
        st.markdown(
            (
                "Your resume and any generated drafts stay in your browser session. "
                "Nothing you upload is stored. Closing the tab clears everything.\n\n"
                if MULTI_USER
                else "Your resume and saved drafts are stored locally on this machine.\n\n"
            )
            + "**This tool never submits anything to SEEK or any employer.** "
            "You always review the draft and apply manually."
        )


# --- Onboarding banner (when no API key set) ----------------------------
if not api_key:
    st.info(
        "**Quick start** — paste your API key in the sidebar to enable tailoring."
    )


# --- Helpers ------------------------------------------------------------

def _step_header(num: int, title: str) -> None:
    st.markdown(
        f'<h3 style="margin-top:1.2rem;">'
        f'<span class="step-num">{num}</span>{title}</h3>',
        unsafe_allow_html=True,
    )


def _render_paste_form(key_prefix: str, source_ref: str = "") -> dict | None:
    col_a, col_b, col_c = st.columns(3)
    p_title = col_a.text_input("Title", key=f"{key_prefix}_title", placeholder="e.g. Marketing Manager")
    p_company = col_b.text_input("Company", key=f"{key_prefix}_company", placeholder="e.g. Acme Pty Ltd")
    p_location = col_c.text_input("Location", key=f"{key_prefix}_location", placeholder="e.g. Sydney NSW 2000")
    pasted = st.text_area(
        "Job description",
        key=f"{key_prefix}_jd",
        height=200,
        placeholder="Paste the full job ad here...",
    )
    if st.button("Use this job", key=f"{key_prefix}_submit", disabled=not pasted.strip()):
        return seek_fetch.from_pasted_text(
            pasted,
            title=p_title,
            company=p_company,
            location=p_location,
            source_ref=source_ref,
        )
    return None


def _build_zip(bundle: dict[str, bytes], folder_name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in bundle.items():
            zf.writestr(f"{folder_name}/{name}", data)
    return buf.getvalue()


# --- Step 1: provide the job --------------------------------------------
_step_header(1, "Provide the job")

with st.container(border=True):
    mode = st.radio(
        "Input mode",
        options=["Sample job", "Search jobs", "Job URL", "Manual paste"],
        horizontal=True,
        key="mode",
    )

    job: dict | None = None

    if mode == "Sample job":
        st.caption("A built-in sample so you can try the flow without uploading anything else.")
        if st.button("Load sample"):
            job = seek_fetch.load_mock()
            ss["fetch_status"] = None

    elif mode == "Search jobs":
        adzuna_id = _secret("ADZUNA_APP_ID")
        adzuna_key = _secret("ADZUNA_APP_KEY")
        if not (adzuna_id and adzuna_key):
            st.info(
                "Job search isn't available right now. "
                "Use the Job URL or Manual paste option instead."
            )
        else:
            # Seed defaults using the heuristic extractor (regex). The smart button below
            # uses Claude for a much better suggestion.
            heur_q = job_search.suggest_keywords_from_resume(master_md) if master_md else ""
            heur_loc = job_search.suggest_location_from_resume(master_md) if master_md else "Australia"
            ss.setdefault("search_q_input", heur_q)
            ss.setdefault("search_loc_input", heur_loc)

            def _on_resume_suggest():
                """Callback: ask Claude for the best search terms for this resume."""
                if not master_md.strip():
                    ss["suggest_error"] = "Upload a resume first."
                    return
                if not api_key:
                    ss["suggest_error"] = "Set an API key in the sidebar first."
                    return
                try:
                    out = job_search.suggest_search_terms_from_resume(master_md, api_key)
                    # Setting these BEFORE the widgets render this turn (via callback) works.
                    ss["search_q_input"] = out["keywords"]
                    ss["search_loc_input"] = out["location"]
                    ss["suggest_reasoning"] = out["reasoning"]
                    ss["suggest_error"] = ""
                except job_search.JobSearchError as e:
                    ss["suggest_error"] = str(e)

            sb_col, _ = st.columns([1, 2])
            with sb_col:
                st.button(
                    "🪄 Suggest from my resume",
                    on_click=_on_resume_suggest,
                    disabled=not master_md.strip(),
                    help="Picks the best search terms based on your resume.",
                    use_container_width=True,
                )
            if ss.get("suggest_error"):
                st.error(ss["suggest_error"])
            if ss.get("suggest_reasoning"):
                st.caption(f"💡 {ss['suggest_reasoning']}")

            col_a, col_b = st.columns([2, 1])
            with col_a:
                q = st.text_input(
                    "Keywords",
                    placeholder="e.g. SEO specialist, marketing manager",
                    key="search_q_input",
                )
            with col_b:
                loc = st.text_input(
                    "Location",
                    placeholder="Sydney, Melbourne, Australia",
                    key="search_loc_input",
                )

            public_only = st.checkbox(
                "Government / public sector only",
                value=False,
                help=(
                    "Filters results to roles that mention government, council, "
                    "department, ministry, or public service."
                ),
            )

            search_clicked = st.button("Search jobs", type="primary",
                                       disabled=not q.strip())
            if search_clicked:
                try:
                    label = "public sector" if public_only else "jobs"
                    with st.spinner(f"Searching {label} for '{q}' in {loc or 'Australia'}..."):
                        results = job_search.search_adzuna(
                            adzuna_id, adzuna_key,
                            what=q, where=loc or "Australia",
                            results_per_page=20,
                            public_sector_only=public_only,
                        )
                    ss["search_results"] = results
                    ss["search_query"] = q
                    if not results:
                        st.warning("No jobs matched. Try different keywords or a broader location.")
                except job_search.JobSearchError as e:
                    st.error(str(e))
                    ss["search_results"] = None

            # --- Watched companies (ATS feeds) ---
            st.divider()
            st.markdown("##### Watch specific companies")
            st.caption(
                "Paste any company URL — their homepage (e.g. `canva.com`), their careers "
                "page, or a job board link. We'll find their open roles automatically."
            )
            wc_col_a, wc_col_b = st.columns([4, 1])
            with wc_col_a:
                new_company_url = st.text_input(
                    "Company URL",
                    placeholder="canva.com  or  company-careers-page-url",
                    key="watch_company_url",
                    label_visibility="collapsed",
                )
            with wc_col_b:
                add_clicked = st.button(
                    "Add to watchlist",
                    disabled=not new_company_url.strip(),
                    use_container_width=True,
                )

            if add_clicked:
                try:
                    with st.spinner("Finding and reading the company's careers feed..."):
                        company_name, ats_results, method = ats_feeds.find_company_jobs(
                            new_company_url,
                            api_key=api_key or None,
                        )
                    # Pick a stable storage key: ATS slug if known, otherwise domain
                    if method.startswith("ats:"):
                        platform = method.split(":", 1)[1]
                        # Derive slug from the first job URL or fall back to company name
                        det = ats_feeds.detect_ats_from_homepage(new_company_url, api_key=api_key or None)
                        slug_key = det[1] if det else company_name
                    else:
                        platform = "web"
                        from urllib.parse import urlparse  # noqa: PLC0415
                        slug_key = urlparse(
                            new_company_url if new_company_url.startswith(("http://", "https://"))
                            else "https://" + new_company_url
                        ).netloc or company_name
                    ss["watched_companies"][slug_key] = {
                        "platform": platform,
                        "company_name": company_name,
                        "jobs": ats_results,
                    }
                    if ats_results:
                        st.success(f"Added **{company_name}** — {len(ats_results)} open role(s).")
                    else:
                        st.info(f"Added **{company_name}** but no open roles were found.")
                except ats_feeds.ATSError as e:
                    st.error(str(e))

            # Render watched companies
            if ss.get("watched_companies"):
                for slug, entry in list(ss["watched_companies"].items()):
                    label = f"{entry['company_name']} — {len(entry['jobs'])} role(s)"
                    with st.expander(label, expanded=True):
                        rem_col, _ = st.columns([1, 5])
                        with rem_col:
                            if st.button("Remove", key=f"wc_remove_{slug}"):
                                del ss["watched_companies"][slug]
                                st.rerun()
                        if not entry["jobs"]:
                            st.caption("No open roles right now.")
                            continue
                        for jx, j in enumerate(entry["jobs"]):
                            with st.container(border=True):
                                st.markdown(f"**{j['title']}**")
                                sub_bits = [b for b in (j.get("location"), j.get("department"),
                                                         j.get("posted"), j.get("source")) if b]
                                if sub_bits:
                                    st.caption(" · ".join(sub_bits))
                                snip = (j.get("description") or "")[:250]
                                if snip:
                                    st.markdown(
                                        f"<small>{snip}{'...' if len(j.get('description', '')) > 250 else ''}</small>",
                                        unsafe_allow_html=True,
                                    )
                                c1, c2 = st.columns([1, 4])
                                with c1:
                                    if st.button("Tailor this", key=f"ats_pick_{slug}_{jx}",
                                                 type="primary"):
                                        job = ats_feeds.result_to_job(j)
                                        ss["fetch_status"] = None
                                with c2:
                                    if j.get("url"):
                                        st.markdown(
                                            f"<small>[Open original posting]({j['url']})</small>",
                                            unsafe_allow_html=True,
                                        )

            # --- Adzuna keyword search results ---
            if ss.get("search_results"):
                results = ss["search_results"]
                st.caption(f"Found {len(results)} job(s). Click 'Tailor for this job' to load one.")
                for idx, r in enumerate(results):
                    with st.container(border=True):
                        line1 = f"**{r['title']}** — {r['company'] or 'Unknown'}"
                        st.markdown(line1)
                        sub_bits = [b for b in (r["location"], r["salary"], r["source"], r["posted"]) if b]
                        if sub_bits:
                            st.caption(" · ".join(sub_bits))
                        snippet = (r["description_snippet"] or "")[:300]
                        if snippet:
                            st.markdown(f"<small>{snippet}{'...' if len(r['description_snippet']) > 300 else ''}</small>",
                                        unsafe_allow_html=True)
                        c1, c2 = st.columns([1, 4])
                        with c1:
                            if st.button("Tailor this", key=f"pick_{idx}", type="primary"):
                                job = job_search.result_to_job(r)
                                ss["fetch_status"] = None
                        with c2:
                            if r["url"]:
                                st.markdown(f"<small>[Open original posting]({r['url']})</small>",
                                            unsafe_allow_html=True)

    elif mode == "Job URL":
        st.caption(
            "Paste any job URL — SEEK, Indeed, Glassdoor, a company's careers page, anything "
            "public. LinkedIn URLs typically need a login — use Manual paste for those."
        )
        url = st.text_input(
            "Job URL",
            placeholder="https://www.seek.com.au/job/... or any job posting URL",
        )
        if st.button("Fetch job", disabled=not url.strip()):
            try:
                with st.spinner("Fetching job details..."):
                    fetched_job, missing = seek_fetch.fetch_from_url(url, api_key=api_key or None)
                kind = "ok" if not missing else "partial"
                ss["fetch_status"] = (kind, missing, None, url)
                job = fetched_job
            except seek_fetch.FetchError as e:
                ss["fetch_status"] = ("failed", [], str(e), url)
                job = None

        status = ss.get("fetch_status")
        if status:
            kind, missing, err, attempted_url = status
            if kind == "ok":
                st.success("Fetched successfully — all required fields present.")
            elif kind == "partial":
                st.warning(
                    "Partial extraction — missing: " + ", ".join(missing)
                    + ". Continue, or switch to Manual paste."
                )
            elif kind == "failed":
                st.error(err)
                low = (attempted_url or "").lower()
                hint = ""
                if "linkedin" in low:
                    hint = " (LinkedIn requires a login — please paste the job details below.)"
                elif "indeed" in low:
                    hint = " (Indeed often blocks direct fetches — please paste the job details below.)"
                st.info(f"Paste the job description below to continue.{hint}")
                pasted_job = _render_paste_form("fallback", source_ref=attempted_url)
                if pasted_job is not None:
                    job = pasted_job
                    ss["fetch_status"] = ("paste_fallback", [], None, attempted_url)

    else:  # Manual paste
        st.caption("Paste the job details below. Edit title/company/location if needed.")
        pasted_job = _render_paste_form("paste")
        if pasted_job is not None:
            job = pasted_job
            ss["fetch_status"] = None

if job is not None:
    ss["job"] = job
    ss["draft"] = None
    ss["saved"] = None
    ss["download_bundle"] = None
    ss["tailor_error"] = None


# --- Step 2: job summary + company panel --------------------------------
if ss["job"]:
    job = ss["job"]
    _step_header(2, "Job loaded")

    with st.container(border=True):
        col_job, col_company = st.columns([2, 1])

        with col_job:
            st.markdown(f"### {job['title']}")
            st.markdown(f"**{job['company']}**")
            sub_bits = [b for b in (job.get("location"), job.get("employment_type"), job.get("salary")) if b]
            if sub_bits:
                st.markdown("*" + " · ".join(sub_bits) + "*")
            with st.expander("Job description", expanded=False):
                st.write(job["description"])
            if job.get("source_ref"):
                st.caption(f"Source: {job['source_ref']}")

        with col_company:
            st.markdown("##### Company profile")
            if job.get("company"):
                st.markdown(f"**{job['company']}**")
            rows = []
            if job.get("company_industry"):
                rows.append(("Industry", job["company_industry"]))
            if job.get("company_size"):
                rows.append(("Size", job["company_size"]))
            for label, value in rows:
                st.markdown(f"- **{label}**: {value}")
            links = []
            if job.get("company_profile_url"):
                ref = job["company_profile_url"]
                # SEEK redux fills these with seek.com.au URLs; LLM extraction returns the company's own site.
                label = "Company page on SEEK" if "seek.com.au" in ref else "Company website"
                links.append(f"[{label}]({ref})")
            if job.get("company_jobs_url"):
                links.append(f"[All open roles on SEEK]({job['company_jobs_url']})")
            if links:
                st.markdown("**Links**")
                for link in links:
                    st.markdown(f"- {link}")
            if not rows and not links:
                st.caption("No company profile details available.")

    # --- Step 3: generate -----------------------------------------------
    _step_header(3, "Generate tailored draft")
    with st.container(border=True):
        btn_label = "Generate (sample)" if use_mock else "Generate tailored draft"
        if st.button(btn_label, type="primary", disabled=not master_md.strip()):
            ss["tailor_error"] = None
            ss["draft"] = None
            with st.spinner("Tailoring..."):
                try:
                    ss["draft"] = tailor.tailor(
                        job,
                        master_md,
                        use_mock=use_mock,
                        api_key=api_key or None,
                    )
                    ss["saved"] = None
                    ss["download_bundle"] = None
                except tailor.TailorError as e:
                    ss["tailor_error"] = str(e)

        if not master_md.strip():
            st.caption("Pick or upload a resume in the sidebar to enable Generate.")
        if ss["tailor_error"]:
            st.error(ss["tailor_error"])
            st.caption("Fix the issue above and click Generate again.")


# --- Step 4: review + approval gate -------------------------------------
if ss["draft"]:
    draft = ss["draft"]
    _step_header(4, "Review")

    with st.container(border=True):
        if draft.get("is_mock"):
            st.info("**Sample draft** — this is a placeholder, not a real Claude-generated tailoring.")
        st.warning(
            "This is a draft. Nothing has been saved yet — click **Approve** below to keep the output."
        )

        summary = draft.get("match_summary", "").strip()
        missing = draft.get("missing_requirements") or []

        if summary:
            st.markdown("##### Match summary")
            st.write(summary)
        if missing:
            st.markdown("##### Missing requirements (not evidenced in your resume)")
            for item in missing:
                st.markdown(f"- {item}")

        left, right = st.columns(2)
        with left:
            st.markdown("##### Tailored resume")
            st.markdown(draft["tailored_resume_md"])
        with right:
            st.markdown("##### Cover note")
            st.markdown(draft["cover_note_md"])

    # --- Step 5: approve ---
    _step_header(5, "Approve")
    with st.container(border=True):
        st.markdown("##### Choose a template")
        style_keys = list(exporter.STYLES.keys())

        # Thumbnail row — render the actual tailored resume in each template
        # (one Playwright session for all 4).
        preview_md = draft.get("tailored_resume_md", "") or _PREVIEW_SAMPLE_MD
        try:
            with st.spinner("Rendering template previews..."):
                all_thumbs = _render_all_template_previews(preview_md, dpi=90)
        except Exception as e:  # noqa: BLE001
            all_thumbs = {}
            st.caption(f"(template previews unavailable: {e})")
        thumb_cols = st.columns(len(style_keys))
        for col, key in zip(thumb_cols, style_keys):
            with col:
                if key in all_thumbs:
                    st.image(all_thumbs[key], use_container_width=True)
                else:
                    st.caption("(no preview)")
                st.caption(f"**{exporter.STYLES[key]['name']}**")

        picked_style = st.radio(
            "Template",
            options=style_keys,
            format_func=lambda k: exporter.STYLES[k]["name"],
            horizontal=True,
            key="selected_style",
            label_visibility="collapsed",
        )
        st.caption(exporter.STYLES[picked_style]["description"])

        # Larger preview of the currently-selected template.
        with st.expander(f"Full preview: {exporter.STYLES[picked_style]['name']}", expanded=True):
            try:
                png = _render_template_preview(preview_md, picked_style, dpi=130)
                st.image(png, use_container_width=True)
            except Exception as e:  # noqa: BLE001
                st.warning(f"Preview unavailable: {e}")

        approve_label = "Approve & Download" if MULTI_USER else "Approve & Save"
        if st.button(approve_label, type="primary"):
            if MULTI_USER:
                bundle, warnings = saver.build_application_bundle(
                    ss["job"], draft, resume_used=chosen_name, style=picked_style
                )
                folder_name = saver.application_folder_name(ss["job"])
                zip_bytes = _build_zip(bundle, folder_name)
                ss["download_bundle"] = (f"{folder_name}.zip", zip_bytes, bundle, warnings)
                ss["saved"] = None
            else:
                ss["saved"] = saver.save_application(
                    job=ss["job"],
                    draft=draft,
                    applications_root=APPLICATIONS_DIR,
                    resume_used=chosen_name,
                    style=picked_style,
                )
                ss["download_bundle"] = None


# --- Step 6: download / save confirmation -------------------------------

def _quick_downloads(bundle: dict[str, bytes], key_prefix: str) -> None:
    """Render four side-by-side download buttons for the key deliverables."""
    st.markdown("**Quick downloads**")
    cols = st.columns(4)
    specs = [
        ("tailored_resume.pdf", "Resume (PDF)", "application/pdf"),
        ("tailored_resume.docx", "Resume (DOCX)", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("cover_note.pdf", "Cover (PDF)", "application/pdf"),
        ("cover_note.docx", "Cover (DOCX)", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ]
    for col, (fname, label, mime) in zip(cols, specs):
        with col:
            if fname in bundle:
                st.download_button(
                    label=f"⬇ {label}",
                    data=bundle[fname],
                    file_name=fname,
                    mime=mime,
                    key=f"{key_prefix}_{fname}",
                    use_container_width=True,
                )
            else:
                st.button(label, disabled=True, key=f"{key_prefix}_{fname}_disabled", use_container_width=True)


def _rebuild_current_bundle(style: str) -> tuple[dict[str, bytes], dict[str, str]]:
    """Rebuild the bundle for the active job + draft at a given style. Fast (~50ms)."""
    return saver.build_application_bundle(
        ss["job"], ss["draft"], resume_used=chosen_name, style=style,
    )


if MULTI_USER and ss["download_bundle"]:
    fname_original, _, _, _ = ss["download_bundle"]
    current_style = ss.get("selected_style", exporter.DEFAULT_STYLE)
    bundle, warnings = _rebuild_current_bundle(current_style)
    folder_name = saver.application_folder_name(ss["job"])
    zip_bytes = _build_zip(bundle, folder_name)

    with st.container(border=True):
        st.success("Your application bundle is ready.")
        st.caption(f"Template: **{exporter.STYLES[current_style]['name']}** — change above to regenerate.")
        if warnings.get("docx"):
            st.warning(warnings["docx"])
        if warnings.get("pdf"):
            st.warning(warnings["pdf"])

        _quick_downloads(bundle, key_prefix=f"mu_{current_style}")

        st.divider()
        st.download_button(
            label=f"⬇ Download everything as zip ({folder_name}.zip)",
            data=zip_bytes,
            file_name=f"{folder_name}.zip",
            mime="application/zip",
        )
        st.caption(
            "The zip also contains `match_summary.docx`, `missing_requirements.docx`, "
            "`job.json`, and `application_meta.json`."
        )

if (not MULTI_USER) and ss["saved"]:
    saved = ss["saved"]
    current_style = ss.get("selected_style", exporter.DEFAULT_STYLE)
    # Rebuild fresh in the currently-selected style for the download buttons.
    fresh_bundle, _ = _rebuild_current_bundle(current_style)

    with st.container(border=True):
        st.success(f"Saved · `{saved['folder'].name}`")
        st.caption(
            f"Downloads use the **{exporter.STYLES[current_style]['name']}** template — "
            "change above to regenerate."
        )
        _quick_downloads(fresh_bundle, key_prefix=f"local_{current_style}")

        st.caption(
            f"Full archive on disk · `{saved['folder'].relative_to(ROOT)}`"
        )
        if saved.get("docx_warning"):
            st.warning(saved["docx_warning"])
        if saved.get("pdf_warning"):
            st.warning(saved["pdf_warning"])


# --- Past applications (personal mode only) -----------------------------

def _list_past_applications(root: Path, limit: int = 50) -> list[Path]:
    if not root.exists():
        return []
    folders = [p for p in root.iterdir() if p.is_dir()]
    folders.sort(key=lambda p: p.name, reverse=True)
    return folders[:limit]


def _read_meta(folder: Path) -> dict:
    meta_path = folder / "application_meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _render_past_applications():
    folders = _list_past_applications(APPLICATIONS_DIR)
    st.divider()
    st.subheader(f"Past applications ({len(folders)})")
    if not folders:
        st.caption("None yet. Saved applications will appear here.")
        return

    choice = st.selectbox(
        "Open a past application",
        options=folders,
        format_func=lambda p: p.name,
        key="past_app_pick",
    )
    if choice is None:
        return

    meta = _read_meta(choice)
    if meta:
        bits = []
        if meta.get("resume_used"):
            bits.append(f"Resume: `{meta['resume_used']}`")
        if meta.get("saved_at"):
            bits.append(f"Saved: {meta['saved_at']}")
        if meta.get("is_mock"):
            bits.append("Mock: yes")
        if bits:
            st.caption(" · ".join(bits))

    # Quick downloads + open folder, mirroring the post-save panel.
    bundle = {p.name: p.read_bytes() for p in choice.iterdir() if p.is_file()}
    _quick_downloads(bundle, key_prefix=f"past_{choice.name}")

    col_a, col_b = st.columns([1, 3])
    with col_a:
        if st.button("Open folder", key="open_folder_btn"):
            try:
                os.startfile(str(choice))  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                st.error(f"Could not open folder: {e}")
    with col_b:
        st.caption(f"`{choice.relative_to(ROOT)}`")

    st.caption("Download files above to view contents (open .docx in Word, .pdf in any PDF viewer).")


if not MULTI_USER:
    _render_past_applications()


# --- Footer --------------------------------------------------------------
st.markdown(
    """
    <div class="footer-note">
      Private review workflow · this tool never submits anything to SEEK or any employer ·
      every application is sent by you, manually.
    </div>
    """,
    unsafe_allow_html=True,
)
