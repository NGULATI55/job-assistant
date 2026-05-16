# Job Application Assistant (private, local)

A private local web app that drafts a tailored resume + short cover note from a job ad. **It never submits to SEEK or any employer.** You always review, approve, and apply manually.

## Status: v1.2 — multi-user / shareable mode

The app runs in one of two modes, chosen by env var:

| Mode | When to use | Storage | API key |
|------|-------------|---------|---------|
| **Local** (default) | Your own machine, personal use | `data/resumes/` + `data/applications/` on disk | Env var or `.streamlit/secrets.toml` |
| **Multi-user** (`MULTI_USER=1`) | Shareable hosted URL for a small group | Session memory only — resumes and saved outputs vanish when the tab closes | Each visitor pastes their own in the sidebar |

In **multi-user** mode, *Approve & Save* becomes *Approve & Download* — the app builds the same bundle of files (`job.json`, `application_meta.json`, `tailored_resume.md/.docx`, `cover_note.md`, `match_summary.md`, `missing_requirements.md`) into a zip and offers it via a download button. Nothing is written to the host's disk.

### Deploying as a shareable link (Streamlit Cloud)

1. **Push this repo to GitHub.**
2. **Go to [share.streamlit.io](https://share.streamlit.io/)** and connect the repo.
3. **In the app's *Settings → Advanced*** add an environment variable:
   ```
   MULTI_USER=1
   ```
4. **Deploy.** Streamlit Cloud picks up:
   - `requirements.txt` — Python packages
   - `packages.txt` — apt packages for Chromium (already in the repo)
   - First load runs `playwright install chromium` automatically (handled inside `app.py`)
5. **Share the resulting `*.streamlit.app` URL.**

Each visitor:
- Pastes their own [Anthropic API key](https://console.anthropic.com/settings/keys) in the sidebar (you don't pay for their tokens).
- Uploads their resume in **PDF, Word (.docx), plain text (.txt), or Markdown (.md)** — text is extracted automatically.
- Pastes a SEEK URL or job description, generates a tailored draft, and downloads the PDFs.
- Nothing they upload is stored on the host — everything stays in their session memory.

### Supported resume formats

| Format | How it's handled |
|---|---|
| **PDF** (`.pdf`) | Text extracted with PyMuPDF. Image-only / scanned PDFs are rejected with a clear error. |
| **Word** (`.docx`) | Parsed with python-docx; headings and bullet lists are preserved as Markdown. |
| **Plain text** (`.txt`) | Read as UTF-8. |
| **Markdown** (`.md`) | Pass-through. |

All uploads are normalised to a `.md` file internally so the rest of the pipeline (Claude tailoring prompt) only ever sees plain text.

What works now:
- **Multi-resume library** at `data/resumes/`. The sidebar lists every `*.md` file in that folder and lets you pick which one to tailor against. You can also drop a `.md` file onto the upload widget — it lands in `data/resumes/` with a sanitised filename. Path traversal is blocked. (The old `data/master_resume.md` from v1 is no longer read — move it into `data/resumes/` if you want to use it.)
- Three input modes: **Mock**, **Live SEEK URL** (real `requests` + JSON-LD parse), **Manual paste**.
- Real Claude-backed tailoring (`claude-sonnet-4-6` by default), strict JSON output with `tailored_resume_md`, `cover_note_md`, `match_summary`, `missing_requirements`. Defensive parser tolerates code fences and stray prose.
- Sidebar toggle keeps a **mock tailoring** path available for debugging — no API call, no tokens spent.
- Truthfulness rules baked into the system prompt: never invents experience, tools, dates, qualifications, or metrics; flags unmet requirements; AU English; no AI cliches; no long dashes.
- Mandatory approval gate. Nothing is saved to disk until you click **Approve & Save**.
- On approval, saves to a per-application folder under `data/applications/`:
  - `job.json`
  - `application_meta.json` *(new — includes `resume_used`, `saved_at`, `is_mock`, source, company, job title)*
  - `tailored_resume.md`
  - `tailored_resume.docx`
  - `cover_note.md`
  - `match_summary.md` (when present)
  - `missing_requirements.md` (when present)
- **Past applications** panel at the bottom of the page: lists saved runs newest-first, surfaces the resume used and timestamp from `application_meta.json`, opens the folder in Explorer, previews the markdown inline, and offers a download button for each file.

What is deliberately **not** built:
- No PDF export. Use Word's *Save As PDF* on the `.docx` if you need one.
- No submission, no browser automation, no email sending. **Ever.**

### Limitations of the .docx exporter
- Supported markdown: `#`, `##`, `###` headings (deeper levels are clamped to Heading 3); plain paragraphs; `- ` and `* ` bullet lists; `> ` blockquotes (rendered as italic paragraphs); inline `**bold**`, `*italic*`, `` `code` ``; fenced code blocks (rendered as a plain Consolas paragraph).
- Unsupported markdown (tables, nested lists, links, images) degrades to plain text rather than being interpreted.
- Output uses python-docx defaults (Calibri 11 pt, standard margins). Deliberately plain for ATS compatibility.
- Only the tailored resume is exported to `.docx`; the cover note stays as `.md` (paste it into the application form).

## API key setup

The real tailoring path needs an Anthropic API key in `ANTHROPIC_API_KEY`. Two ways to provide it:

**Option A — environment variable (simplest):**

```powershell
$env:ANTHROPIC_API_KEY = "sk-ant-..."
streamlit run app.py
```

**Option B — Streamlit secrets file (persists across sessions):**

Create `.streamlit/secrets.toml` (already gitignored):

```toml
ANTHROPIC_API_KEY = "sk-ant-..."
```

Then just `streamlit run app.py`. The sidebar should report `API key detected (…last4)`.

Without a key, you can still use the **mock tailoring** toggle in the sidebar for offline UI testing.

### Fetcher limitations to be aware of
- SEEK occasionally returns 403/HTML challenge pages for non-browser traffic. The fetcher uses a realistic User-Agent but isn't a headless browser. If that happens, the paste fallback kicks in automatically.
- Only `<script type="application/ld+json">` JobPosting blocks are read — no scraping of rendered DOM, no Apollo-state JSON parsing.
- Description HTML is flattened to plain text with newlines preserved between block elements; complex nested formatting is intentionally lost.
- Salary parsing handles `MonetaryAmount` with `value` as `QuantitativeValue` (min/max/unit). Unusual shapes may return an empty salary string.

## Run

From this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
streamlit run app.py
```

Streamlit opens `http://localhost:8501` in your browser.

## Smoke test

1. Open the app. The **Resume** section in the sidebar should list `example.md` and offer an upload widget. Pick a resume.
2. **Mock** input mode → *Load mock job* → confirm the summary shows title, company, location, salary, employment type.
3. **Live SEEK URL** mode → paste a current `seek.com.au` job URL → *Fetch job*. Expect green / amber / red banner depending on extraction.
4. **Manual paste** mode → paste a job ad → *Use this job*.
5. With a job loaded, click *Generate with Claude*. Confirm the match summary, missing requirements, tailored resume, and cover note render. Nothing written to disk yet.
6. Click **Approve & Save** → confirm `data/applications/<timestamp>_<slug>/` contains `job.json`, `application_meta.json`, `tailored_resume.md`, `tailored_resume.docx`, `cover_note.md`, plus `match_summary.md` / `missing_requirements.md` when present. `application_meta.json` should show the resume filename you picked.
7. Open the saved `.docx` in Word — headings, bullets, and bold/italic should render as expected.
8. Scroll to the **Past applications** panel at the bottom. Pick a saved run — caption should show `Resume: <name>` and timestamp. Preview the markdown inline, hit *Download* on any file, or *Open folder in Explorer*.

### Tests / debugging

Offline tests (no network, no API key, no Word required):

```powershell
python tests\test_seek_fetch.py
python tests\test_tailor.py
python tests\test_exporter.py
python tests\test_resume_loader.py
```

CLI fetcher (real SEEK URL, no Anthropic call):

```powershell
python scripts\test_fetch.py "https://www.seek.com.au/job/12345678"
```

## Layout

```
job-assistant/
├── app.py
├── core/
│   ├── seek_fetch.py    # load_mock / fetch_from_url (real, JSON-LD) / from_pasted_text
│   ├── resume_loader.py # list_resumes / load_resume_text / save_uploaded_resume
│   ├── tailor.py        # tailor() — real Claude call + mock toggle
│   ├── saver.py         # approval-gated writer (job.json + meta + markdown + docx)
│   └── exporter.py      # markdown -> .docx (headings, bullets, paragraphs, inline)
├── data/
│   ├── resumes/         # *.md library — sidebar lists them and lets you upload
│   │   └── example.md
│   └── applications/    # one folder per approved application
├── scripts/
│   └── test_fetch.py    # CLI helper for testing the fetcher against a real URL
├── tests/
│   ├── test_seek_fetch.py     # offline parser tests (synthetic HTML)
│   ├── test_tailor.py         # offline parser/validator/dispatch tests
│   ├── test_exporter.py       # offline docx tests (re-opens with python-docx)
│   └── test_resume_loader.py  # list, sanitisation, traversal-block tests
├── requirements.txt
├── .gitignore
└── README.md
```
