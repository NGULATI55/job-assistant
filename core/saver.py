"""Approval-gated saver.

Two public entry points:
- `build_application_bundle(...)` — returns {filename: bytes} in memory. Used
  by the multi-user/hosted mode for zip download (nothing touches disk).
- `save_application(...)` — writes the bundle to a timestamped folder under
  `data/applications/`. Used by the local/personal mode.

Both routes pass through `build_application_bundle`, so they produce
byte-identical artifacts.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from . import exporter
from .seek_fetch import Job
from .tailor import TailoredResult


class SavedApplication(TypedDict, total=False):
    folder: Path
    job_json: Path
    application_meta_json: Path
    tailored_resume_docx: Path
    cover_note_docx: Path
    match_summary_docx: Path
    missing_requirements_docx: Path
    tailored_resume_pdf: Path
    cover_note_pdf: Path
    docx_warning: str
    pdf_warning: str


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 40) -> str:
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return s[:max_len] or "job"


def application_folder_name(job: Job, now: datetime | None = None) -> str:
    """Return the conventional folder name (`<timestamp>_<slug>`) for an application."""
    now = now or datetime.now()
    stamp = now.strftime("%Y-%m-%d_%H%M%S")
    slug = _slugify(f"{job['company']}-{job['title']}")
    return f"{stamp}_{slug}"


def build_application_bundle(
    job: Job,
    draft: TailoredResult,
    resume_used: str = "",
    now: datetime | None = None,
    style: str | None = None,
) -> tuple[dict[str, bytes], dict[str, str]]:
    """Build all application output files as in-memory bytes.

    Returns (bundle, warnings). `bundle` is {filename: bytes}. `warnings` is
    {"docx": msg, "pdf": msg} — keys only present if that export failed; the
    rest of the bundle is unaffected.
    """
    now = now or datetime.now()

    job_bytes = json.dumps(job, indent=2, ensure_ascii=False).encode("utf-8")

    meta = {
        "saved_at": now.isoformat(timespec="seconds"),
        "resume_used": resume_used,
        "is_mock": bool(draft.get("is_mock", False)),
        "input_source": job.get("source", ""),
        "input_ref": job.get("source_ref", ""),
        "job_title": job.get("title", ""),
        "company": job.get("company", ""),
        "style": style or "classic",
    }
    meta_bytes = json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8")

    bundle: dict[str, bytes] = {
        "job.json": job_bytes,
        "application_meta.json": meta_bytes,
    }

    warnings: dict[str, str] = {}

    # DOCX (editable in Word) — replaces the old .md text files
    try:
        bundle["tailored_resume.docx"] = exporter.markdown_to_docx_bytes(
            draft["tailored_resume_md"]
        )
        bundle["cover_note.docx"] = exporter.markdown_to_docx_bytes(
            draft["cover_note_md"]
        )
        summary = draft.get("match_summary", "").strip()
        if summary:
            bundle["match_summary.docx"] = exporter.markdown_to_docx_bytes(
                f"# Match summary\n\n{summary}"
            )
        missing = draft.get("missing_requirements") or []
        if missing:
            body = "# Missing requirements\n\n" + "\n".join(f"- {m}" for m in missing)
            bundle["missing_requirements.docx"] = exporter.markdown_to_docx_bytes(body)
    except Exception as e:  # noqa: BLE001
        warnings["docx"] = f"docx export failed: {e}"

    # PDF (designed, beautiful) — primary deliverable for sending to employers
    pdf_title_resume = f"Tailored Resume — {job.get('title', '')} at {job.get('company', '')}".strip(" —")
    pdf_title_cover = f"Cover Note — {job.get('title', '')} at {job.get('company', '')}".strip(" —")
    try:
        bundle["tailored_resume.pdf"] = exporter.markdown_to_pdf_bytes(
            draft["tailored_resume_md"], title=pdf_title_resume, style=style,
        )
        bundle["cover_note.pdf"] = exporter.markdown_to_pdf_bytes(
            draft["cover_note_md"], title=pdf_title_cover, style=style,
        )
    except Exception as e:  # noqa: BLE001
        warnings["pdf"] = f"pdf export failed: {e}"

    return bundle, warnings


def save_application(
    job: Job,
    draft: TailoredResult,
    applications_root: Path,
    resume_used: str = "",
    now: datetime | None = None,
    style: str | None = None,
) -> SavedApplication:
    """Write the application bundle to disk under `applications_root/<timestamp>_<slug>/`.

    Called ONLY from the approval gate in app.py — never automatically.
    """
    now = now or datetime.now()
    folder_name = application_folder_name(job, now)
    folder = applications_root / folder_name
    folder.mkdir(parents=True, exist_ok=True)

    bundle, warnings = build_application_bundle(job, draft, resume_used, now, style=style)
    for name, data in bundle.items():
        (folder / name).write_bytes(data)

    result: SavedApplication = {
        "folder": folder,
        "job_json": folder / "job.json",
        "application_meta_json": folder / "application_meta.json",
    }
    for fname, key in (
        ("tailored_resume.docx", "tailored_resume_docx"),
        ("cover_note.docx", "cover_note_docx"),
        ("match_summary.docx", "match_summary_docx"),
        ("missing_requirements.docx", "missing_requirements_docx"),
        ("tailored_resume.pdf", "tailored_resume_pdf"),
        ("cover_note.pdf", "cover_note_pdf"),
    ):
        if fname in bundle:
            result[key] = folder / fname  # type: ignore[literal-required]
    if warnings.get("docx"):
        result["docx_warning"] = warnings["docx"]
    if warnings.get("pdf"):
        result["pdf_warning"] = warnings["pdf"]
    return result
