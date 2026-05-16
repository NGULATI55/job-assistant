"""HTML + CSS resume templates rendered to PDF via Playwright (Chromium).

Designed to push close to commercial Etsy / Canva template quality within
single-page web design constraints:
- Google Fonts for premium typography (Inter, Playfair Display, Lora)
- Inline SVG icons for contact lines
- Per-template visual decoration (banners, accent bars, side stripes)
- Proper grid layouts via CSS Grid
"""

from __future__ import annotations

import html as _html
import re


# --- Inline SVG icons used in contact lines ----------------------------

_ICON_MAIL = """<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5" width="18" height="14" rx="2"/><polyline points="3 7 12 13 21 7"/></svg>"""
_ICON_PHONE = """<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.94.36 1.86.7 2.74a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.34-1.34a2 2 0 0 1 2.11-.45c.88.34 1.8.57 2.74.7A2 2 0 0 1 22 16.92z"/></svg>"""
_ICON_LOCATION = """<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/></svg>"""
_ICON_LINK = """<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>"""
_ICON_GLOBE = """<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="2" y1="12" x2="22" y2="12"/><path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>"""


# --- markdown -> HTML helpers ------------------------------------------

_INLINE_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_INLINE_ITALIC = re.compile(r"\*([^*\n]+)\*")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")

_EMAIL_RE = re.compile(r"\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?:\+?\d[\d\s\-()]{7,}\d)")
_URL_RE = re.compile(r"\b(?:https?://|www\.)[^\s,·•|]+", re.IGNORECASE)
_LINKEDIN_RE = re.compile(r"linkedin\.com/in/[\w-]+", re.IGNORECASE)


def _inline_md_to_html(text: str) -> str:
    escaped = _html.escape(text)
    escaped = _INLINE_BOLD.sub(r"<strong>\1</strong>", escaped)
    escaped = _INLINE_ITALIC.sub(r"<em>\1</em>", escaped)
    escaped = _INLINE_CODE.sub(r"<code>\1</code>", escaped)
    return escaped


def _split_contact(contact: str) -> list[tuple[str, str]]:
    """Split a contact line ('Sydney · email · phone') into typed parts.

    Returns a list of (kind, value) where kind is 'email', 'phone', 'location', 'link', 'text'.
    Robust against various separators (·, |, comma).
    """
    # Common separators in contact lines
    raw_parts = re.split(r"\s*[·•|]\s*", contact.strip()) if contact else []
    typed: list[tuple[str, str]] = []
    for part in raw_parts:
        p = part.strip()
        if not p:
            continue
        if _EMAIL_RE.fullmatch(p) or _EMAIL_RE.search(p):
            m = _EMAIL_RE.search(p)
            typed.append(("email", m.group(0) if m else p))
        elif _LINKEDIN_RE.search(p):
            m = _LINKEDIN_RE.search(p)
            typed.append(("link", m.group(0) if m else p))
        elif _URL_RE.search(p):
            m = _URL_RE.search(p)
            typed.append(("link", m.group(0) if m else p))
        elif _PHONE_RE.fullmatch(p) or _PHONE_RE.search(p):
            m = _PHONE_RE.search(p)
            typed.append(("phone", m.group(0).strip() if m else p))
        else:
            typed.append(("location", p))
    return typed


_ICON_MAP = {
    "email": _ICON_MAIL,
    "phone": _ICON_PHONE,
    "location": _ICON_LOCATION,
    "link": _ICON_LINK,
    "text": "",
}


def _render_contact_with_icons(contact: str, css_class: str = "contact-row") -> str:
    parts = _split_contact(contact)
    if not parts:
        return ""
    items = []
    for kind, value in parts:
        icon = _ICON_MAP.get(kind, "")
        items.append(f'<span class="contact-item"><span class="ci-icon">{icon}</span>{_html.escape(value)}</span>')
    return f'<div class="{css_class}">' + "".join(items) + "</div>"


def _blocks_to_html(blocks: list[tuple]) -> str:
    parts: list[str] = []
    for blk in blocks:
        kind = blk[0]
        if kind == "h":
            level = blk[1]
            parts.append(f"<h{level}>{_inline_md_to_html(blk[2])}</h{level}>")
        elif kind == "p":
            parts.append(f"<p>{_inline_md_to_html(blk[1])}</p>")
        elif kind == "bullets":
            items = "".join(f"<li>{_inline_md_to_html(b)}</li>" for b in blk[1])
            parts.append(f"<ul>{items}</ul>")
        elif kind == "quote":
            parts.append(f"<blockquote>{_inline_md_to_html(blk[1])}</blockquote>")
        elif kind == "code":
            parts.append(f"<pre>{_html.escape(blk[1])}</pre>")
    return "\n".join(parts)


def _split_header(blocks: list[tuple]) -> tuple[str, str, list[tuple]]:
    name = ""
    contact = ""
    i = 0
    if blocks and blocks[0][0] == "h" and blocks[0][1] == 1:
        name = blocks[0][2]
        i = 1
    if i < len(blocks) and blocks[i][0] == "p":
        contact = blocks[i][1]
        i += 1
    return name, contact, blocks[i:]


_SIDEBAR_SECTIONS = {
    "skills", "education", "tools", "languages", "contact",
    "certifications", "awards", "interests", "references", "other",
    "core skills", "key skills", "technical skills", "tools & technologies",
    "tech stack", "expertise",
}


_TOP_MAIN_SECTIONS = {
    "summary", "profile", "about", "objective", "profile summary", "about me",
}


def _route_main_sidebar(blocks: list[tuple]) -> tuple[list[tuple], list[tuple]]:
    main: list[tuple] = []
    sidebar: list[tuple] = []
    current = main
    for blk in blocks:
        if blk[0] == "h" and blk[1] == 2:
            current = sidebar if blk[2].strip().lower() in _SIDEBAR_SECTIONS else main
        current.append(blk)
    return main, sidebar


def _route_three_way(blocks: list[tuple]) -> tuple[list[tuple], list[tuple], list[tuple]]:
    """Split into (sidebar, top_main, bottom_main).

    sidebar    -> Skills/Education/Tools/Contact/Languages/Awards/etc.
    top_main   -> Summary/Profile/About (shown next to sidebar on page 1)
    bottom_main -> Everything else (Experience, Projects, etc.) — full-width,
                   page-breaks cleanly because there's no parallel sidebar.
    """
    sidebar: list[tuple] = []
    top_main: list[tuple] = []
    bottom: list[tuple] = []
    current = top_main  # before any H2, treat content as top-main
    for blk in blocks:
        if blk[0] == "h" and blk[1] == 2:
            name = blk[2].strip().lower()
            if name in _SIDEBAR_SECTIONS:
                current = sidebar
            elif name in _TOP_MAIN_SECTIONS:
                current = top_main
            else:
                current = bottom
        current.append(blk)
    return sidebar, top_main, bottom


# --- Shared resets + Google Fonts --------------------------------------

_GOOGLE_FONTS = (
    "<link rel=\"preconnect\" href=\"https://fonts.googleapis.com\">"
    "<link rel=\"preconnect\" href=\"https://fonts.gstatic.com\" crossorigin>"
    "<link href=\"https://fonts.googleapis.com/css2?"
    "family=Inter:wght@300;400;500;600;700;800;900&"
    "family=Playfair+Display:wght@400;700;900&"
    "family=Lora:wght@400;500;600;700&"
    "family=Crimson+Text:ital,wght@0,400;0,600;1,400&"
    "family=Inter+Tight:wght@400;500;600;700;800;900&"
    "display=swap\" rel=\"stylesheet\">"
)

_CSS_RESET = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; -webkit-font-smoothing: antialiased; }
p, ul, ol, h1, h2, h3, h4 { margin: 0; padding: 0; }
ul { list-style: none; }
strong { font-weight: 700; }
em { font-style: italic; }
code { font-family: "Consolas", "Courier New", monospace; font-size: 0.95em; }
.ci-icon { display: inline-flex; align-items: center; vertical-align: middle; margin-right: 5px; opacity: 0.8; }
.ci-icon svg { display: block; }
.contact-item { display: inline-flex; align-items: center; margin-right: 12px; white-space: nowrap; }
@page { size: A4; margin: 0; }
/* Pagination control: avoid orphan headings + keep single bullets together,
   but allow long bullet lists to split across pages so the layout doesn't bail. */
h2, h3 { break-after: avoid-page; page-break-after: avoid; }
h3 + p { break-after: avoid-page; page-break-after: avoid; }
li { break-inside: avoid-page; page-break-inside: avoid; }
.top-section { break-inside: auto; }
.bottom-section { break-before: auto; }
"""


# ========================================================================
# 1) Editorial Bold  (Samuel "I'm Samuel, I'm a Graphic Designer" reference)
# ========================================================================

_CSS_EDITORIAL = _CSS_RESET + """
body {
  font-family: 'Inter', -apple-system, sans-serif;
  color: #1a1a1a;
  background: #fff;
  font-size: 10.5pt;
  line-height: 1.55;
  padding: 22mm 22mm 18mm;
}
.header-row { display: flex; align-items: flex-end; justify-content: space-between; gap: 18mm; }
.name {
  font-family: 'Inter Tight', 'Inter', sans-serif;
  font-size: 46pt;
  font-weight: 900;
  letter-spacing: -2px;
  line-height: 0.92;
}
.name .accent { color: #e89827; }
.contact-row { display: flex; flex-direction: column; gap: 3mm; font-size: 9.5pt; color: #555; text-align: right; align-items: flex-end; }
.contact-row .contact-item { margin-right: 0; }
.contact-row .ci-icon { color: #e89827; }
.rule-thick { height: 4px; background: #1a1a1a; margin: 8mm 0 9mm; }
h2 {
  font-family: 'Inter Tight', sans-serif;
  font-size: 15pt;
  font-weight: 800;
  letter-spacing: -0.3px;
  margin: 7mm 0 3mm;
}
h3 {
  font-family: 'Inter', sans-serif;
  font-size: 11.5pt;
  font-weight: 700;
  margin: 4mm 0 0.5mm;
  padding-left: 6mm;
  position: relative;
}
h3::before {
  content: "";
  position: absolute;
  left: 0;
  top: 1.5mm;
  bottom: 1.5mm;
  width: 3px;
  background: #e89827;
  border-radius: 1px;
}
p { margin: 1.5mm 0; }
h3 + p { color: #888; font-size: 9.5pt; margin: 0 0 1.5mm 6mm; font-weight: 500; }
h2 + p { color: #444; line-height: 1.65; }
ul { margin: 2.5mm 0 3mm 6mm; padding: 0; }
ul li {
  position: relative;
  padding-left: 6mm;
  margin-bottom: 2mm;
  line-height: 1.5;
}
ul li::before {
  content: "";
  position: absolute;
  left: 0;
  top: 2.2mm;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: #e89827;
}
"""


def _build_editorial_bold(name: str, contact: str, blocks: list[tuple]) -> str:
    first, _, rest = name.partition(" ")
    if rest:
        name_html = f'<span class="accent">{_html.escape(first)}</span> {_html.escape(rest)}'
    else:
        name_html = _html.escape(name)
    contact_html = _render_contact_with_icons(contact)
    body_html = _blocks_to_html(blocks)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">{_GOOGLE_FONTS}<style>{_CSS_EDITORIAL}</style></head><body>
<div class="header-row">
  <h1 class="name">{name_html}</h1>
  {contact_html}
</div>
<div class="rule-thick"></div>
<main>{body_html}</main>
</body></html>"""


# ========================================================================
# 2) Executive Banner  (Tracy Hall navy + gold serif reference)
# ========================================================================

_CSS_EXECUTIVE = _CSS_RESET + """
body {
  font-family: 'Lora', Georgia, serif;
  color: #222;
  background: #fff;
  font-size: 10.5pt;
  line-height: 1.6;
}
.banner {
  background: #1a3a6c;
  padding: 22mm 25mm 18mm;
  text-align: center;
  color: #c9a96e;
  position: relative;
}
.banner::after {
  content: "";
  position: absolute;
  bottom: 6mm;
  left: 50%;
  transform: translateX(-50%);
  width: 28mm;
  height: 1px;
  background: #c9a96e;
  opacity: 0.6;
}
.banner .name {
  font-family: 'Playfair Display', Georgia, serif;
  font-size: 38pt;
  font-weight: 700;
  letter-spacing: 3px;
  line-height: 1;
}
.banner .tag {
  margin-top: 6mm;
  font-family: 'Lora', serif;
  font-size: 10pt;
  text-transform: uppercase;
  letter-spacing: 6px;
  color: #c9a96e;
}
.contact-bar {
  text-align: center;
  font-size: 9.5pt;
  color: #555;
  padding: 5mm 25mm 0;
  display: flex;
  justify-content: center;
}
.contact-bar .contact-row { display: flex; flex-direction: row; gap: 0; flex-wrap: wrap; justify-content: center; }
.contact-bar .ci-icon { color: #c9a96e; }
.body {
  padding: 10mm 22mm 18mm;
  overflow: hidden;
}
.body .sidebar {
  float: left;
  width: 32%;
  border-right: 1px solid #d8d4c4;
  padding-right: 8mm;
  margin-right: 4mm;
}
.body .main {
  margin-left: 38%;
}
h2 {
  font-family: 'Playfair Display', Georgia, serif;
  color: #1a3a6c;
  font-size: 12pt;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 3px;
  margin: 7mm 0 3mm;
  padding-bottom: 2mm;
  border-bottom: 1.5px solid #c9a96e;
}
.sidebar h2:first-child, .main h2:first-child { margin-top: 0; }
h3 {
  font-family: 'Lora', serif;
  font-size: 11.5pt;
  font-weight: 600;
  color: #1a3a6c;
  margin: 4mm 0 0.5mm;
}
p { margin: 1.5mm 0; }
h3 + p { color: #888; font-size: 9.5pt; font-style: italic; margin: 0 0 1.5mm; }
h2 + p { line-height: 1.65; }
ul { margin: 2mm 0 3mm; padding: 0; }
ul li {
  position: relative;
  padding-left: 5mm;
  margin-bottom: 1.5mm;
  line-height: 1.55;
  font-size: 10pt;
}
ul li::before {
  content: "◆";
  position: absolute;
  left: 0;
  color: #c9a96e;
  font-size: 8pt;
  top: 0.5mm;
}
"""


def _build_executive_banner(name: str, contact: str, blocks: list[tuple]) -> str:
    main_blocks, sidebar_blocks = _route_main_sidebar(blocks)
    contact_html = _render_contact_with_icons(contact)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">{_GOOGLE_FONTS}<style>{_CSS_EXECUTIVE}</style></head><body>
<header class="banner">
  <h1 class="name">{_html.escape(name)}</h1>
</header>
<div class="contact-bar">{contact_html}</div>
<div class="body">
  <aside class="sidebar">{_blocks_to_html(sidebar_blocks)}</aside>
  <main class="main">{_blocks_to_html(main_blocks)}</main>
</div>
</body></html>"""


# ========================================================================
# 3) Grid Modern  (Veselin cream + thick purple rules reference)
# ========================================================================

_CSS_GRID = _CSS_RESET + """
body {
  font-family: 'Inter Tight', 'Inter', sans-serif;
  color: #1c1c1c;
  background: #faf6ef;
  font-size: 10pt;
  line-height: 1.5;
}
.page-bg {
  background: #faf6ef;
  min-height: 297mm;
  padding: 14mm 22mm 18mm;
}
.top-rule, .bottom-rule { height: 5px; background: #6c46b8; }
.name-block { text-align: center; padding: 9mm 0 6mm; }
.name {
  font-family: 'Inter Tight', sans-serif;
  font-size: 38pt;
  font-weight: 900;
  letter-spacing: 2px;
  line-height: 1;
}
.tag {
  margin-top: 4mm;
  font-size: 10pt;
  text-transform: uppercase;
  letter-spacing: 7px;
  color: #6c46b8;
  font-weight: 600;
}
.contact-bar { text-align: center; font-size: 9.5pt; color: #444; padding: 6mm 0 0; display: flex; justify-content: center; }
.contact-bar .contact-row { display: flex; flex-direction: row; flex-wrap: wrap; justify-content: center; gap: 0; }
.contact-bar .ci-icon { color: #6c46b8; }
.body {
  margin-top: 9mm;
  overflow: hidden;
}
.body .sidebar { float: left; width: 42%; padding-right: 8mm; }
.body .main { margin-left: 46%; }
h2 {
  font-family: 'Inter Tight', sans-serif;
  font-size: 13pt;
  font-weight: 900;
  text-transform: uppercase;
  letter-spacing: 2px;
  margin: 6mm 0 3mm;
  color: #1c1c1c;
  display: flex;
  align-items: center;
  gap: 4mm;
}
h2::after {
  content: "";
  flex: 1;
  height: 2px;
  background: #1c1c1c;
}
.sidebar h2::after, .main h2::after { background: #1c1c1c; }
h3 { font-size: 11pt; font-weight: 700; margin: 3mm 0 0.5mm; }
p { margin: 1.5mm 0; }
h3 + p { color: #777; font-size: 9.5pt; margin: 0 0 1.5mm; }
ul { margin: 2mm 0 3mm; padding: 0; }
ul li {
  position: relative;
  padding-left: 5mm;
  margin-bottom: 1.5mm;
  font-size: 9.5pt;
  line-height: 1.5;
}
ul li::before {
  content: "";
  position: absolute;
  left: 0;
  top: 1.8mm;
  width: 3px;
  height: 3px;
  background: #6c46b8;
}
"""


def _build_grid_modern(name: str, contact: str, blocks: list[tuple]) -> str:
    main_blocks, sidebar_blocks = _route_main_sidebar(blocks)
    contact_html = _render_contact_with_icons(contact)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">{_GOOGLE_FONTS}<style>{_CSS_GRID}</style></head><body>
<div class="page-bg">
<div class="top-rule"></div>
<div class="name-block">
  <h1 class="name">{_html.escape(name)}</h1>
</div>
<div class="bottom-rule"></div>
<div class="contact-bar">{contact_html}</div>
<div class="body">
  <aside class="sidebar">{_blocks_to_html(sidebar_blocks)}</aside>
  <main class="main">{_blocks_to_html(main_blocks)}</main>
</div>
</div>
</body></html>"""


# ========================================================================
# 4) Minimal Sidebar  (Resume Worded clean two-column reference)
# ========================================================================

_CSS_MINIMAL = _CSS_RESET + """
body {
  font-family: 'Inter', -apple-system, sans-serif;
  color: #1c1c1c;
  background: #fff;
  font-size: 10pt;
  line-height: 1.55;
  padding: 16mm 20mm;
}
.header { padding-bottom: 6mm; border-bottom: 1px solid #e5e5e5; margin-bottom: 7mm; }
.name {
  font-family: 'Inter Tight', sans-serif;
  font-size: 28pt;
  font-weight: 800;
  letter-spacing: -1px;
  line-height: 1;
  margin-bottom: 3mm;
}
.contact-row { display: flex; flex-wrap: wrap; gap: 0; font-size: 9.5pt; color: #555; }
.contact-row .ci-icon { color: #1a73e8; }
.body { overflow: hidden; }
.body .sidebar { float: right; width: 32%; padding-left: 8mm; }
.body .main { margin-right: 36%; }
h2 {
  font-family: 'Inter Tight', sans-serif;
  font-size: 11pt;
  color: #1a73e8;
  text-transform: uppercase;
  letter-spacing: 1.8px;
  font-weight: 700;
  margin: 6mm 0 3mm;
  padding-bottom: 1.5mm;
  border-bottom: 1.5px solid #1a73e8;
}
.main > :first-child, .sidebar > :first-child { margin-top: 0; }
h3 {
  font-family: 'Inter', sans-serif;
  font-size: 11pt;
  font-weight: 700;
  margin: 3.5mm 0 0.5mm;
}
p { margin: 1.5mm 0; }
h3 + p { color: #888; font-size: 9.5pt; margin: 0 0 1.5mm; font-weight: 500; }
ul { margin: 2mm 0 3mm; padding: 0; }
ul li {
  position: relative;
  padding-left: 5mm;
  margin-bottom: 1.5mm;
  line-height: 1.5;
  font-size: 9.5pt;
}
ul li::before {
  content: "";
  position: absolute;
  left: 0;
  top: 1.5mm;
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: #1a73e8;
}
"""


def _build_minimal_sidebar(name: str, contact: str, blocks: list[tuple]) -> str:
    main_blocks, sidebar_blocks = _route_main_sidebar(blocks)
    contact_html = _render_contact_with_icons(contact)
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">{_GOOGLE_FONTS}<style>{_CSS_MINIMAL}</style></head><body>
<div class="header">
  <h1 class="name">{_html.escape(name)}</h1>
  {contact_html}
</div>
<div class="body">
  <main class="main">{_blocks_to_html(main_blocks)}</main>
  <aside class="sidebar">{_blocks_to_html(sidebar_blocks)}</aside>
</div>
</body></html>"""


# --- Dispatcher ---------------------------------------------------------

_BUILDERS = {
    "editorial_bold": _build_editorial_bold,
    "executive_banner": _build_executive_banner,
    "grid_modern": _build_grid_modern,
    "minimal_sidebar": _build_minimal_sidebar,
}


def render_html(md_blocks: list[tuple], style: str) -> str:
    name, contact, remaining = _split_header(md_blocks)
    builder = _BUILDERS.get(style, _build_editorial_bold)
    return builder(name, contact, remaining)
