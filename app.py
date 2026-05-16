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

from core import seek_fetch, resume_loader, tailor, saver, exporter


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
        <span class="privacy-badge">{mode_label}</span>
    </h1>
    <p style="color: #555; font-size: 1.05rem; margin-top: -0.5rem;">
        Paste a SEEK job link, pick your resume, and get a tailored draft + short cover note for your review.
        Nothing is submitted to SEEK or any employer.
    </p>
    """,
    unsafe_allow_html=True,
)


# --- Session state -------------------------------------------------------
ss = st.session_state
ss.setdefault("job", None)
ss.setdefault("draft", None)
ss.setdefault("saved", None)            # local mode: SavedApplication dict
ss.setdefault("download_bundle", None)  # multi-user mode: (filename, bytes, warning)
ss.setdefault("fetch_status", None)
ss.setdefault("tailor_error", None)
ss.setdefault("last_upload_marker", None)
ss.setdefault("selected_style", exporter.DEFAULT_STYLE)
# Multi-user mode keeps resumes in memory keyed by filename.
if MULTI_USER:
    ss.setdefault("resumes", {"example.md": resume_loader.builtin_example()})


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
                    st.success(f"Uploaded · `{uploaded.name}` ({len(text):,} chars extracted)")
                except ValueError as e:
                    st.error(f"Could not accept upload: {e}")

        names = sorted(ss["resumes"].keys())
        chosen_name = st.selectbox(
            "Your resumes",
            options=names,
            format_func=_format_resume_label,
            key="resume_pick",
        )
        master_md = ss["resumes"].get(chosen_name, "")
        st.caption(f"In-memory only · {len(ss['resumes'])} resume(s) this session")
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
                    st.success(f"Uploaded · `{uploaded.name}` saved as `{saved_path.name}`")
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
        st.caption(f"`{RESUMES_DIR.relative_to(ROOT)}/`")

    if master_md.strip():
        st.success(f"Loaded `{chosen_name}` ({len(master_md):,} chars)")
    elif chosen_name:
        st.warning(f"`{chosen_name}` is empty — edit it before tailoring")

    st.divider()

    # 2) API key
    st.markdown("**2. Anthropic API key**")
    api_key_input = st.text_input(
        "API key",
        type="password",
        placeholder="sk-ant-...",
        help="Get one at console.anthropic.com. Your key is used only for your session and never stored.",
        key="api_key_input",
    )
    api_key = _resolve_api_key(api_key_input)
    if api_key:
        st.success(f"Key set (…{api_key[-4:]})")
    else:
        st.warning("No API key. Paste one above to enable real tailoring.")

    st.divider()

    # 3) Tailoring options
    st.markdown("**3. Options**")
    use_mock = st.checkbox(
        "Use mock tailoring (debug)",
        value=False,
        help="Bypass the Anthropic API and return a hardcoded draft. Useful for UI testing.",
    )

    # 4) About
    with st.expander("About / privacy"):
        st.markdown(
            f"**Mode:** {mode_label}\n\n"
            + (
                "Each visitor's resume and outputs stay in their session memory. "
                "Nothing is written to the host. Closing the tab clears everything."
                if MULTI_USER
                else "Resumes live in `data/resumes/` and saved applications in "
                "`data/applications/` on this machine. Nothing leaves the host except "
                "the Anthropic API call."
            )
            + "\n\n**No submission**: this tool never posts to SEEK or any employer. "
            "You always apply manually."
        )


# --- Onboarding banner (when no API key set) ----------------------------
if not api_key:
    st.info(
        "**Quick start** — paste an Anthropic API key in the sidebar to enable "
        "real tailoring, or tick *Use mock tailoring (debug)* to try the flow without "
        "spending tokens. Get a key at "
        "[console.anthropic.com](https://console.anthropic.com/settings/keys)."
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
        options=["Mock", "Live SEEK URL", "Manual paste"],
        horizontal=True,
        key="mode",
    )

    job: dict | None = None

    if mode == "Mock":
        st.caption("Built-in sample job. Use this to test the flow with no network.")
        if st.button("Load mock job"):
            job = seek_fetch.load_mock()
            ss["fetch_status"] = None

    elif mode == "Live SEEK URL":
        url = st.text_input("SEEK job URL", placeholder="https://www.seek.com.au/job/...")
        if st.button("Fetch job", disabled=not url.strip()):
            try:
                fetched_job, missing = seek_fetch.fetch_from_url(url)
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
                st.error(f"Fetch failed: {err}")
                st.info("Manual paste fallback — paste the description below.")
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
            src_caption = f"Source: `{job['source']}`"
            if job.get("source_ref"):
                src_caption += f" — {job['source_ref']}"
            st.caption(src_caption)

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
                links.append(f"[Company page on SEEK]({job['company_profile_url']})")
            if job.get("company_jobs_url"):
                links.append(f"[All open roles on SEEK]({job['company_jobs_url']})")
            if links:
                st.markdown("**Links**")
                for link in links:
                    st.markdown(f"- {link}")
            if not rows and not links:
                st.caption("No company profile data available for this source.")

    # --- Step 3: generate -----------------------------------------------
    _step_header(3, "Generate tailored draft")
    with st.container(border=True):
        btn_label = "Generate (mock)" if use_mock else "Generate with Claude"
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
            st.error(f"Tailoring failed: {ss['tailor_error']}")
            st.caption("Fix the issue (or flip the *Use mock tailoring* toggle) and click Generate again.")


# --- Step 4: review + approval gate -------------------------------------
if ss["draft"]:
    draft = ss["draft"]
    _step_header(4, "Review")

    with st.container(border=True):
        if draft.get("is_mock"):
            st.info("**Mock mode** — the Anthropic API was NOT called. This draft is hardcoded.")
        st.warning(
            "This is a DRAFT only. No files have been written yet. "
            "Click **Approve** below to keep the output."
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
        ("cover_note.pdf", "Cover (PDF)", "application/pdf"),
        ("tailored_resume.md", "Resume (MD)", "text/markdown"),
        ("cover_note.md", "Cover (MD)", "text/markdown"),
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
            "The zip also contains `match_summary.md`, `missing_requirements.md`, "
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

    with st.expander("Preview the drafts", expanded=False):
        for name in ("tailored_resume.md", "cover_note.md", "match_summary.md", "missing_requirements.md"):
            path = choice / name
            if path.exists():
                st.markdown(f"**{name}**")
                try:
                    st.markdown(path.read_text(encoding="utf-8"))
                except OSError as e:
                    st.error(f"Could not read {name}: {e}")
                st.divider()


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
