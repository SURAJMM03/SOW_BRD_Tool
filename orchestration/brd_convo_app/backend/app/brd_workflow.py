# app/brd_workflow.py
from __future__ import annotations

import os
import re
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from infrastructure.unified_mcp.MCP_client import (
    mcp_web_search,
    mcp_doc_search,
)
import requests
import json

HTTP_BASE = os.getenv("MCP_WEBTOOLS_BASE_URL", "http://localhost:8000").rstrip("/")
logger = logging.getLogger("BDConvoWorkflow")

def _section_sort_key(section_id: str):
    return [int(p) if p.isdigit() else p for p in section_id.split(".")]


# ================================================================
# SECTION FILTERING UTILITIES
# ================================================================

def filter_sections(
    all_sections: List[Dict],
    selected_ids: List[str],
) -> List[Dict]:
    """
    Return only the section dicts whose ``id`` is explicitly listed in
    *selected_ids*.  No parent auto-inclusion: selecting ``"1"`` does **not**
    pull in ``"1.1"``, ``"1.2"``, etc.  Order is preserved by section-id sort
    key so the result is always numerically ordered regardless of the input
    order of *selected_ids*.

    Args:
        all_sections:  Full list of section dicts, each with at minimum an
                       ``"id"`` key.
        selected_ids:  Explicit list of section ids to keep.

    Returns:
        Filtered, sorted list of section dicts.
    """
    wanted: set = set(selected_ids)
    filtered = [s for s in all_sections if s.get("id") in wanted]
    return sorted(filtered, key=lambda s: _section_sort_key(s.get("id", "")))


# ── Word outline / cross-reference plumbing ──────────────────────────────────
# Section headings in this document are hand-formatted paragraphs (explicit
# font, size and colour runs) rather than Word's built-in Heading styles. That
# looks right on the page but leaves the file with no document structure at
# all: Word's navigation pane is empty, and the Table of Contents could carry
# no page numbers because there was nothing to reference. These helpers attach
# the structure — outline levels and bookmarks — without disturbing any of the
# visual formatting the headings already carry.

def _set_outline_level(paragraph, level: int) -> None:
    """Mark a hand-formatted paragraph as a heading at `level` (1-based).

    Gives the paragraph an outline level so it appears in Word's navigation
    pane and can be picked up by a TOC field, while leaving its manual run
    formatting untouched.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    pPr = paragraph._p.get_or_add_pPr()
    for existing in pPr.findall(qn("w:outlineLvl")):
        pPr.remove(existing)
    node = OxmlElement("w:outlineLvl")
    node.set(qn("w:val"), str(max(0, min(8, level - 1))))
    pPr.append(node)


def _bookmark_paragraph(paragraph, name: str, bookmark_id: int) -> None:
    """Wrap a paragraph's content in a named bookmark so PAGEREF can find it."""
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    start = OxmlElement("w:bookmarkStart")
    start.set(qn("w:id"), str(bookmark_id))
    start.set(qn("w:name"), name)
    end = OxmlElement("w:bookmarkEnd")
    end.set(qn("w:id"), str(bookmark_id))
    paragraph._p.insert(0, start)
    paragraph._p.append(end)


def _toc_bookmark_name(section_id: str) -> str:
    """Bookmark names must be alphanumeric/underscore and start with a letter."""
    return "_Sec_" + re.sub(r"[^0-9A-Za-z]", "_", section_id)


def _add_pageref_run(paragraph, bookmark_name: str) -> None:
    """Append a PAGEREF field that resolves to the page holding `bookmark_name`.

    Emitted with a "1" placeholder as the cached result so the entry is never
    blank if the document is read by something that does not evaluate fields;
    Word replaces it with the true page number on open (see
    _enable_update_fields).
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    run = paragraph.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    instr.text = f" PAGEREF {bookmark_name} \\h "
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "1"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    for node in (fld_begin, instr, fld_sep, placeholder, fld_end):
        run._r.append(node)


def _enable_update_fields(doc) -> None:
    """Ask Word to refresh every field when the document is opened.

    Without this the PAGEREF page numbers in the TOC keep showing their cached
    placeholder until someone manually selects all and presses F9.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    try:
        settings = doc.settings.element
        for existing in settings.findall(qn("w:updateFields")):
            settings.remove(existing)
        node = OxmlElement("w:updateFields")
        node.set(qn("w:val"), "true")
        settings.append(node)
    except Exception as e:
        logger.debug("Could not set updateFields: %s", e)


def build_table_of_contents(
    sections: List[Dict],
) -> List[Dict]:
    """
    Build a structured Table-of-Contents list from the supplied sections.

    The hierarchy is inferred purely from the dot-separated ``id``
    (``"1"`` → depth 1, ``"1.1"`` → depth 2, ``"1.1.2"`` → depth 3, …).
    Parent entries are **not** synthesised — only the sections you explicitly
    pass appear in the TOC.

    Args:
        sections: List of section dicts, each with ``"id"`` and ``"title"``.

    Returns:
        List of dicts ``{ "id", "title", "depth" }`` in ascending section order.

    Example::

        toc = build_table_of_contents([
            {"id": "1",   "title": "Introduction"},
            {"id": "1.1", "title": "Company Information"},
        ])
        # [{"id": "1", "title": "Introduction", "depth": 1},
        #  {"id": "1.1", "title": "Company Information", "depth": 2}]
    """
    toc: List[Dict] = []
    for sec in sorted(sections, key=lambda s: _section_sort_key(s.get("id", ""))):
        sid = sec.get("id", "")
        toc.append({
            "id":    sid,
            "title": sec.get("title", ""),
            "depth": len(sid.split(".")),
        })
    return toc


def generate_sections(
    selected_ids: List[str],
    project_id: str,
    app_base_url: str,
    client_context: Optional[Dict] = None,
    doc_keywords: Optional[Dict[str, List[str]]] = None,
    web_keywords: Optional[Dict[str, List[str]]] = None,
    timeout_per_section: int = 120,
) -> Dict[str, str]:
    """
    Call the ``/api/sections/{id}/generate`` endpoint independently for each
    section in *selected_ids*.  One HTTP request per section — no batching —
    so prompt sizes stay small and TPM limits are never hit by a single call.

    Args:
        selected_ids:         Explicit list of section ids to generate.
        project_id:           Project scoping key passed to the generate API.
        app_base_url:         Base URL of the running FastAPI app,
                              e.g. ``"http://localhost:8001"``.
        client_context:       Optional dict forwarded as ``client_context``
                              in each generate payload.
        doc_keywords:         Optional ``{ section_id: [kw, …] }`` map.
        web_keywords:         Optional ``{ section_id: [kw, …] }`` map.
        timeout_per_section:  Per-request timeout in seconds (default 120).

    Returns:
        ``{ section_id: generated_content }`` — empty string on failure.
    """
    base = app_base_url.rstrip("/")
    results: Dict[str, str] = {}

    for sid in sorted(selected_ids, key=_section_sort_key):
        payload: Dict = {
            "doc_keywords":   (doc_keywords or {}).get(sid, []),
            "web_keywords":   (web_keywords or {}).get(sid, []),
            "client_context": client_context or {},
            "project_id":     project_id,
        }
        try:
            resp = requests.post(
                f"{base}/api/sections/{sid}/generate",
                json=payload,
                timeout=timeout_per_section,
            )
            resp.raise_for_status()
            results[sid] = resp.json().get("content", "")
            logger.info("generate_sections: section %s — %d chars", sid, len(results[sid]))
        except Exception as exc:
            logger.warning("generate_sections: section %s failed: %s", sid, exc)
            results[sid] = ""

    return results


def export_document(
    selected_ids: List[str],
    project_id: str,
    app_base_url: str,
    user_email: str = "",
    revision_description: str = "Partial export",
) -> Dict:
    """
    Call the ``/api/document/export`` endpoint, restricting the exported DOCX
    to *only* the sections in *selected_ids*.  Passes ``section_ids`` so the
    backend applies its strict filter — no ancestor auto-inclusion.

    Args:
        selected_ids:         Explicit list of section ids to include in the
                              exported document.
        project_id:           Project scoping key.
        app_base_url:         Base URL of the running FastAPI app.
        user_email:           Optional author email for the revision history row.
        revision_description: Optional description written into the revision history.

    Returns:
        The JSON response dict from ``/api/document/export``
        (keys: ``ok``, ``docx_url``, ``txt_url``, ``filename``, ``version``).
    """
    base = app_base_url.rstrip("/")
    payload: Dict = {
        "project_id":           project_id,
        "section_ids":          sorted(selected_ids, key=_section_sort_key),
        "user_email":           user_email,
        "user_name":            user_email,
        "revision_description": revision_description,
    }
    resp = requests.post(
        f"{base}/api/document/export",
        json=payload,
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


# ================================================================
# REGEX PATTERNS (REAL TAGS — NO HTML ESCAPING)
# ================================================================



PARA_RE = re.compile(
    r'<PARA\s+id=["\']?(\d+)["\']?>(.*?)</PARA>',
    re.DOTALL | re.IGNORECASE,
)

PRIVATE_RE = re.compile(
    r'<PRIVATE\s+id=["\']?(R\d+)["\']?>(.*?)</PRIVATE>',
    re.DOTALL | re.IGNORECASE,
)

# FACT format expected from research agent:
# FACT # <ID> # <statement> # <url>
FACT_RE = re.compile(
    r'^FACT\s*\\s*(\w+)\s*\\s*(.+?)\s*\\s*(.+?)\s*$',
    re.MULTILINE,
)

# ================================================================
# RESEARCH CONTEXT NORMALIZATION
# ================================================================

def build_question_research_context(question: str, section_id: str = None) -> str:
    context_parts = []

    # Web
    try:
        response = requests.post(f"{HTTP_BASE}/web_search", json={
            "query": question,
            "max_results": 5
        }, timeout=20)

        if response.status_code == 200:
            context_parts.append("## Web Insights\n")
            context_parts.append(response.json().get("result", ""))
    except Exception as e:
        context_parts.append(f"Web error: {e}")

    # Docs — pass section_id so only chunks tagged to this BRD section are returned
    try:
        doc_req: dict = {"query": question, "top_k": 5}
        if section_id:
            doc_req["section_id"] = section_id
        response = requests.post(f"{HTTP_BASE}/doc_search", json=doc_req, timeout=20)

        if response.status_code == 200:
            hits = response.json().get("hits", [])
            context_parts.append("\n## Internal Docs\n")
            for h in hits:
                context_parts.append(
                    f"- [Source: {h['doc_name']} / {h['heading_path']}]: {h['snippet'][:200]}"
                )
    except Exception as e:
        context_parts.append(f"Doc error: {e}")

    return "\n".join(context_parts)

# ================================================================
# GENERATION PARSING
# ================================================================

def parse_generation_blocks(
    gen_text: str,
) -> Tuple[List[Tuple[int, str]], List[Tuple[str, str]]]:
    """Extract <PARA> and <PRIVATE> blocks from generation output."""

    paras = [
        (int(pid), text.strip())
        for pid, text in PARA_RE.findall(gen_text)
    ]

    privates = [
        (rid, q.strip())
        for rid, q in PRIVATE_RE.findall(gen_text)
    ]

    return paras, privates


# ================================================================
# PARAGRAPH REWRITE
# ================================================================

def rewrite_paragraph(
    gen_agent,
    gen_messages: list,
    para_id: int,
    instruction: str | None = None,
) -> str:
    """Ask the generation agent to rewrite a specific paragraph."""

    if instruction:
        prompt = (
            f"Rewrite paragraph {para_id} applying this change: {instruction}. "
            "Use only the research data already provided. "
            "Keep all citations. "
            f'Output only <PARA id="{para_id}">...</PARA>.'
    )
    else:
        prompt = (
            f"Rewrite paragraph {para_id} with different phrasing. "
            "Use only the research data already provided. "
            "Keep all citations. "
            f'Output only <PARA id="{para_id}">...</PARA>.'
        )

    regen = gen_agent.invoke({
        "messages": list(gen_messages)
        + [{"role": "user", "content": prompt}]
    })

    new_text = regen["messages"][-1].content
    matches = PARA_RE.findall(new_text)

    return matches[0][1].strip() if matches else new_text.strip()

# ── Section titles: loaded dynamically from screen3_sections.json ─────────────
# This means any renames/additions made in Screen 3 are automatically reflected
# in the assembled Blueprint Document without restarting the server.
try:
    from app.section_titles import get_titles as _get_titles
except ImportError:
    try:
        from section_titles import get_titles as _get_titles
    except ImportError:
        def _get_titles():
            return {}

def _live_titles():
    """Call each time titles are needed so renames are always fresh."""
    return _get_titles()

# Lazy shims — these are called per-assembly, not at import time
def SECTION_TITLES():    return _live_titles()
def BR_SECTION_TITLES(): return _live_titles()
def SSC_SECTION_TITLES(): return _live_titles()
def DP_SECTION_TITLES(): return _live_titles()
def IP_SECTION_TITLES(): return _live_titles()
def DI_SECTION_TITLES(): return _live_titles()

# ================================================================
# DOCUMENT ASSEMBLY
# ================================================================
def assemble_document(
    sections=None,
    section1_sections=None,
    br_sections=None,
    ssc_sections=None,
    dp_sections=None,
    ip_sections=None,
    di_sections=None,
) -> str:
    """
    Assemble the full BD document in correct section order.
    Safely handles optional sections (BR, SSC, DP).
    """
    section1_sections = sections or section1_sections or {}

    lines = []
    
    # =========================================================
    # SECTION 1 — INTRODUCTION ✅ NEW
    # =========================================================
    if section1_sections:
        lines.append("=" * 60)
        lines.append("Section 1: Introduction")
        lines.append("=" * 60)
        lines.append("")

        for sid in sorted(section1_sections.keys(), key=_section_sort_key):
            if sid == "1":
                lines.append(section1_sections[sid])
                lines.append("")
                continue
            title = _live_titles().get(sid, "")
            lines.append(f"Section {sid}: {title}")
            lines.append(section1_sections[sid])
            lines.append("")
    
    # =========================================================
    # SECTION 2 — BENEFIT REALIZATION
    # =========================================================
    if br_sections:
        lines.append("=" * 60)
        lines.append("Section 2: Benefit Realization")
        lines.append("=" * 60)
        lines.append("")

        for sid in sorted(br_sections.keys(), key=_section_sort_key):
            if sid == "2":
                lines.append(br_sections[sid])
                lines.append("")
                continue
            title = _live_titles().get(sid, "")
            lines.append(f"Section {sid}: {title}" if title else f"Section {sid}")
            lines.append(br_sections[sid])
            lines.append("")
    
    
    # =========================================================
    # SECTION 3 — SUPPLY CHAIN SCOPE
    # =========================================================
    if ssc_sections:
        lines.append("=" * 60)
        lines.append("Section 3: Supply Chain Scope")
        lines.append("=" * 60)
        lines.append("")

        for sid in sorted(ssc_sections.keys(), key=_section_sort_key):
                if sid == "3":
                    lines.append(ssc_sections[sid])
                    lines.append("")
                    continue
                title = _live_titles().get(sid, "")
                lines.append(f"Section {sid}: {title}" if title else f"Section {sid}")
                lines.append(ssc_sections[sid])
                lines.append("")
        
    
    # =========================================================
    # SECTION 4 — DEMAND PLANNING ✅ NEW
    # =========================================================
    if dp_sections:
        lines.append("=" * 60)
        lines.append("Section 4: Demand Planning")
        lines.append("=" * 60)
        lines.append("")

        for sid in sorted(dp_sections.keys(), key=_section_sort_key):
            if sid == "4":
                lines.append(dp_sections[sid])
                lines.append("")
                continue
            title = _live_titles().get(sid, "")
            lines.append(f"Section {sid}: {title}" if title else f"Section {sid}")
            lines.append(dp_sections[sid])
            lines.append("")

    
    # =========================================================
    # SECTION 5 — SUPPLY & INVENTORY PLANNING ✅ NEW
    # =========================================================
    if ip_sections:
        lines.append("=" * 60)
        lines.append("Section 5: Supply & Inventory Planning")
        lines.append("=" * 60)
        lines.append("")

        for sid in sorted(ip_sections.keys(), key=_section_sort_key):
            if sid == "5":
                lines.append(ip_sections[sid])
                lines.append("")
                continue
            title = _live_titles().get(sid, "")
            lines.append(f"Section {sid}: {title}" if title else f"Section {sid}")
            lines.append(ip_sections[sid])
            lines.append("")
    
    # =========================================================
    # SECTION 6 — DATA INTEGRATION
    # =========================================================
    if di_sections:
        lines.append("=" * 60)
        lines.append("Section 6: Data Integration")
        lines.append("=" * 60)
        lines.append("")

        for sid in sorted(di_sections.keys(), key=_section_sort_key):
            if sid == "6":
                lines.append(di_sections[sid])
                lines.append("")
                continue
            title = _live_titles().get(sid, "")
            lines.append(f"Section {sid}: {title}" if title else f"Section {sid}")
            lines.append(di_sections[sid])
            lines.append("")

    return "\n".join(lines)
    
# ================================================================
# FILE OUTPUT
# ================================================================

def save_txt(doc: str, output_dir: Path) -> Path:
    # ✅ If a file path was accidentally passed, fix it
    if output_dir.suffix:
        output_dir = output_dir.parent

    # ✅ Ensure directory exists
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"BD_1_1_{timestamp}.txt"

    path.write_text(doc, encoding="utf-8")
    return path


def _is_md_table_line(s: str) -> bool:
    s = s.strip()
    return s.startswith("|") and s.count("|") >= 2


def _parse_md_table_block(lines):
    """Parse a list of |-delimited lines into list-of-lists, skipping separators."""
    import re as _re
    rows = []
    for raw in lines:
        line = raw.strip()
        if not line or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if cells and all(_re.fullmatch(r":?-{1,}:?", c or "") for c in cells):
            continue
        rows.append(cells)
    return rows


def _split_md_table_groups(lines):
    """Split a run of |-delimited lines into separate tables.

    Returns a list of tables, each a list-of-rows (cells). A row that is
    immediately followed by a separator row (|---|) is treated as a new
    table's header, so two adjacent tables sharing column names are NOT
    merged into one (which would swallow the second header as a data row).
    """
    import re as _re
    is_sep = lambda s: bool(_re.fullmatch(r"\|[\s\-:|]+\|", s.strip()))
    pipe = [l.strip() for l in lines if l.strip().startswith("|") and l.strip().endswith("|")]
    groups = []
    n = len(pipe)
    t = 0
    while t < n:
        if t + 1 < n and is_sep(pipe[t + 1]):
            header = [c.strip() for c in pipe[t].strip("|").split("|")]
            rows = [header]
            k = t + 2
            while k < n:
                if is_sep(pipe[k]):
                    break
                if k + 1 < n and is_sep(pipe[k + 1]):
                    break  # next line is a new header
                rows.append([c.strip() for c in pipe[k].strip("|").split("|")])
                k += 1
            groups.append(rows)
            t = k
        else:
            t += 1
    if not groups:
        # No separator found — fall back to treating everything as one table.
        flat = _parse_md_table_block(lines)
        if flat:
            groups = [flat]
    return groups


def _add_inline_runs(paragraph, text, base_size=11, color=None, font_name="Arial",
                     base_italic=False):
    """Append runs to `paragraph`, rendering inline **bold** and *italic*
    Markdown as real bold/italic runs (NOT printing the asterisks literally).
    Falls back to a single plain run if `text` has no markers.
    """
    from docx.shared import Pt
    import re as _re

    def _emit(seg, bold=False, italic=False):
        if not seg:
            return
        r = paragraph.add_run(seg)
        r.font.size = Pt(base_size)
        r.font.name = font_name
        if color is not None:
            r.font.color.rgb = color
        if bold:
            r.font.bold = True
        if italic or base_italic:
            r.font.italic = True

    if not text:
        _emit("")
        return

    # Split on **bold** first; then split remaining segments on *italic*.
    parts = _re.split(r'(\*\*[^*\n]+?\*\*)', text)
    for part in parts:
        if part.startswith("**") and part.endswith("**") and len(part) >= 5:
            _emit(part[2:-2], bold=True)
            continue
        # Split this segment on *italic* (single asterisk pair, not adjacent
        # to another asterisk).
        sub = _re.split(r'(?<!\*)(\*[^*\n]+?\*)(?!\*)', part)
        for s in sub:
            if s.startswith("*") and s.endswith("*") and len(s) >= 3 and not s.startswith("**"):
                _emit(s[1:-1], italic=True)
            else:
                _emit(s)


# Canonicalise every <IMAGE> variant (case, spacing, missing slash/quotes,
# trailing </IMAGE>) to <IMAGE id="x"/>. Uses [ \t] (never \s) so it can't
# swallow newlines or merge tags that sit on separate lines.
_IMG_TAG_NORM = re.compile(
    r'<[ \t]*image[ \t]+id[ \t]*=[ \t]*["\']?([A-Za-z0-9_\-]+)["\']?[ \t]*/?[ \t]*>'
    r'([ \t]*<[ \t]*/[ \t]*image[ \t]*>)?',
    re.IGNORECASE,
)
_IMG_LINE_RE = re.compile(r'<IMAGE id="[^"]+"/>')


def _normalize_image_tags(text):
    """Collapse <IMAGE> variants to the canonical <IMAGE id="x"/> form."""
    if not text:
        return text
    return _IMG_TAG_NORM.sub(lambda m: '<IMAGE id="%s"/>' % m.group(1), text)


def _apply_table_structure(word_doc, tbl) -> None:
    """Apply the structural formatting python-docx does not set by default.

    A bare `add_table()` produces a table that looks acceptable on page one
    and then falls apart: the header row does not repeat when the table
    breaks across a page, so continuation pages show unlabelled columns; a
    single row can be split down the middle by a page break; text sits flush
    against the cell borders; and because no explicit widths are set, Word
    recomputes the layout from cell content and produces uneven columns that
    can run past the right margin.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    n_cols = len(tbl.columns)
    if not n_cols:
        return
    try:
        sec = word_doc.sections[0]
        total_w = int(sec.page_width - sec.left_margin - sec.right_margin) // 635
    except Exception:
        total_w = 9360   # Letter with 1in margins, in dxa (twentieths of a point)
    total_w = max(1440, min(total_w, 14400))
    col_w = max(1, total_w // n_cols)

    tblPr = tbl._tbl.tblPr
    tblW = tblPr.find(qn("w:tblW"))
    if tblW is None:
        tblW = OxmlElement("w:tblW")
        tblPr.append(tblW)
    tblW.set(qn("w:w"), str(col_w * n_cols))
    tblW.set(qn("w:type"), "dxa")

    if tblPr.find(qn("w:tblLayout")) is None:
        layout = OxmlElement("w:tblLayout")
        layout.set(qn("w:type"), "fixed")
        tblPr.append(layout)

    if tblPr.find(qn("w:tblCellMar")) is None:
        cell_mar = OxmlElement("w:tblCellMar")
        for side, val in (("top", 60), ("left", 108), ("bottom", 60), ("right", 108)):
            node = OxmlElement(f"w:{side}")
            node.set(qn("w:w"), str(val))
            node.set(qn("w:type"), "dxa")
            cell_mar.append(node)
        tblPr.append(cell_mar)

    for gridCol in tbl._tbl.findall(qn("w:tblGrid") + "/" + qn("w:gridCol")):
        gridCol.set(qn("w:w"), str(col_w))

    for r_idx, row in enumerate(tbl.rows):
        trPr = row._tr.get_or_add_trPr()
        if r_idx == 0 and trPr.find(qn("w:tblHeader")) is None:
            header = OxmlElement("w:tblHeader")
            header.set(qn("w:val"), "true")
            trPr.append(header)
        if trPr.find(qn("w:cantSplit")) is None:
            trPr.append(OxmlElement("w:cantSplit"))
        for cell in row.cells:
            tcPr = cell._tc.get_or_add_tcPr()
            tcW = tcPr.find(qn("w:tcW"))
            if tcW is None:
                tcW = OxmlElement("w:tcW")
                tcPr.append(tcW)
            tcW.set(qn("w:w"), str(col_w))
            tcW.set(qn("w:type"), "dxa")
            if tcPr.find(qn("w:vAlign")) is None:
                vAlign = OxmlElement("w:vAlign")
                vAlign.set(qn("w:val"), "center")
                tcPr.append(vAlign)


def _emit_body_lines(word_doc, content_text, left_indent, BLACK, NAVY, GRAY):
    """
    Emit body content into a word_doc, handling:
      - Markdown tables (| col | col |) → centered Word table
      - Bullets (-, *, •) → ListParagraph, justified
      - Figure captions ("Figure ...:" lines) → centered, italic
      - Plain paragraphs → justified
    """
    from docx.shared import Pt, Inches
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT

    lines = content_text.split("\n")
    i = 0
    while i < len(lines):
        raw = lines[i]
        line = raw.strip()
        if not line:
            i += 1
            continue

        # Markdown heading (# / ## / ###) → bold sub-heading, strip the hashes.
        # This handles content that was generated with markdown section headers
        # that the sanitizer didn't catch (e.g. content already stored in JSON).
        _hdr_m = re.match(r'^(#{1,6})\s+(.+)$', line)
        if _hdr_m:
            inner = _hdr_m.group(2).strip()
            if inner:
                p = word_doc.add_paragraph()
                run = p.add_run(inner)
                run.font.size = Pt(11)
                run.font.bold = True
                run.font.color.rgb = NAVY
                run.font.name = "Arial"
                if left_indent:
                    p.paragraph_format.left_indent = Inches(left_indent)
                p.paragraph_format.space_before = Pt(8)
                p.paragraph_format.space_after = Pt(2)
            i += 1
            continue

        # Preserve <IMAGE id="..."/> tags so _resolve_images_in_docx can embed
        # the picture afterwards. Normalise variants first so they all survive.
        line = _normalize_image_tags(line)
        if _IMG_LINE_RE.search(line):
            if not _IMG_LINE_RE.sub("", line).strip():
                # Line is only image tag(s) — emit each on its own paragraph
                # so the post-processor finds and replaces it with the picture.
                for _m in _IMG_LINE_RE.finditer(line):
                    word_doc.add_paragraph(_m.group(0))
                i += 1
                continue
            # Mixed text + inline tag: strip other HTML but KEEP the IMAGE tag.
            line = re.sub(r'<(?![Ii][Mm][Aa][Gg][Ee]\b)[^>]+>', '', line).strip()
        else:
            # Strip stray HTML tags that may survive in already-stored content.
            if re.match(r'^\s*<[a-zA-Z/][^>]*>\s*$', line):
                i += 1
                continue
            if '<' in line and '>' in line:
                line = re.sub(r'<[^>]+>', '', line).strip()
                if not line:
                    i += 1
                    continue

        # Bold-only line (e.g. a table title like **Supplier Attributes**) →
        # render as a bold sub-heading paragraph with the ** markers stripped,
        # so table names actually print in the exported document.
        if len(line) >= 5 and line.startswith("**") and line.endswith("**") and "|" not in line:
            inner = line[2:-2].strip()
            if inner:
                p = word_doc.add_paragraph()
                run = p.add_run(inner)
                run.font.size = Pt(11)
                run.font.bold = True
                run.font.color.rgb = BLACK
                run.font.name = "Arial"
                if left_indent:
                    p.paragraph_format.left_indent = Inches(left_indent)
                p.paragraph_format.space_before = Pt(6)
                p.paragraph_format.space_after = Pt(2)
                i += 1
                continue

        # Markdown table → consume consecutive table rows
        if _is_md_table_line(line):
            tbl_block = []
            while i < len(lines) and _is_md_table_line(lines[i]):
                tbl_block.append(lines[i])
                i += 1
            # Split the block into one-or-more tables. A row immediately
            # followed by a separator (|---|) is a header that starts a new
            # table — this prevents two adjacent tables with the same columns
            # (Demand/Supply vs Supplier attributes) from merging into one.
            for grp in _split_md_table_groups(tbl_block):
                rows = grp
                if not rows:
                    continue
                n_cols = max(len(r) for r in rows)
                rows = [r + [""] * (n_cols - len(r)) for r in rows]
                tbl = word_doc.add_table(rows=len(rows), cols=n_cols)
                tbl.style = "Table Grid"
                tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
                for ri, row in enumerate(rows):
                    for ci, cell_text in enumerate(row):
                        cell = tbl.cell(ri, ci)
                        cp = cell.paragraphs[0]
                        # Cell text is markdown like everything else in the
                        # body. It used to go in via a single raw add_run(),
                        # so "**Bristlecone**" printed its asterisks into the
                        # table while the same markup rendered correctly in
                        # the surrounding paragraphs and bullets.
                        _add_inline_runs(cp, cell_text or "", base_size=10,
                                         color=BLACK, font_name="Arial")
                        if ri == 0:
                            for cr in cp.runs:
                                cr.font.bold = True
                _apply_table_structure(word_doc, tbl)
            continue

        # Figure caption → centered, italic.
        # Accept both bare ("Figure 2.3.2-1: …") and italic-wrapped
        # ("*Figure 2.3.2-1: …*") forms; strip the asterisks before printing
        # so the caption renders as proper italic text, not raw markdown.
        _cap_stripped = line.strip("*").strip("_").strip()
        if _cap_stripped.lower().startswith(("figure ", "fig.", "image ", "diagram ", "chart ", "table ")) and ":" in _cap_stripped[:25]:
            p = word_doc.add_paragraph()
            run = p.add_run(_cap_stripped)
            run.font.size = Pt(10)
            run.font.italic = True
            run.font.color.rgb = GRAY
            run.font.name = "Arial"
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(6)
            i += 1
            continue

        # Bullet — detect indent level from the RAW line (tab = 1 level,
        # 2 or 4 leading spaces = 1 level) so hierarchical bullets keep
        # their nesting. Inline **bold** and *italic* get real bold/italic
        # runs instead of being printed as raw markdown.
        _bullet_match = re.match(r'^(\s*)([-*•])\s+(.*)$', raw)
        if _bullet_match:
            indent_ws, _marker, btext = _bullet_match.groups()
            # Count level: every tab or every 2 spaces = +1
            tabs = indent_ws.count('\t')
            spaces = len(indent_ws) - tabs * (1 if '\t' in indent_ws else 0)
            level = tabs + (spaces // 2)
            level = min(max(level, 0), 5)
            try:
                style_name = "List Bullet" if level == 0 else f"List Bullet {min(level + 1, 5)}"
                p = word_doc.add_paragraph(style=style_name)
            except KeyError:
                # Some templates don't have List Bullet 2/3/4 — fall back.
                p = word_doc.add_paragraph(style="List Bullet")
            _add_inline_runs(p, btext.strip(), base_size=11, color=BLACK, font_name="Arial")
            # Stack indent: base + level offset
            p.paragraph_format.left_indent = Inches(left_indent + 0.25 + level * 0.3)
            p.paragraph_format.space_after = Pt(2)
            p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            i += 1
            continue

        # Plain paragraph — render inline **bold** / *italic* as real runs.
        p = word_doc.add_paragraph()
        _add_inline_runs(p, line, base_size=11, color=BLACK, font_name="Arial")
        if left_indent:
            p.paragraph_format.left_indent = Inches(left_indent)
        p.paragraph_format.space_after = Pt(4)
        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        i += 1


def save_docx(doc: str, output_dir: Path, state=None,
              heading_only_ids: Optional[set] = None,
              no_heading_ids: Optional[set] = None,
              strict_selection: bool = False,
              title_map: Optional[dict] = None) -> Path:
    """
    Generate a formatted BD .docx with Bristlecone/Arclin branding.
    Pure python-docx implementation — no Node.js required.
    """
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor, Cm
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    import copy

    if output_dir.suffix:
        output_dir = output_dir.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"BD_{timestamp}.docx"

    # ── Colors ──────────────────────────────────────────────────
    NAVY        = RGBColor(0x1B, 0x3A, 0x6B)
    BLUE        = RGBColor(0x2E, 0x75, 0xB6)
    BLACK       = RGBColor(0x1A, 0x1A, 0x1A)
    GRAY        = RGBColor(0x73, 0x73, 0x73)
    WHITE       = RGBColor(0xFF, 0xFF, 0xFF)

    # ── Logo paths ───────────────────────────────────────────────
    logo_dir          = Path(__file__).parent / "logos"
    arclin_logo       = logo_dir / "arclin_logo.jpeg"
    bristlecone_logo  = logo_dir / "bristlecone_logo.jpeg"

    word_doc = Document()

    # ── Page margins ─────────────────────────────────────────────
    for sec in word_doc.sections:
        sec.left_margin   = Inches(1.0)
        sec.right_margin  = Inches(1.0)
        sec.top_margin    = Inches(1.0)
        sec.bottom_margin = Inches(1.0)

    # ── Helper: set cell background color ────────────────────────
    def set_cell_bg(cell, hex_color):
        tc = cell._tc
        tcPr = tc.get_or_add_tcPr()
        shd = OxmlElement("w:shd")
        shd.set(qn("w:val"), "clear")
        shd.set(qn("w:color"), "auto")
        shd.set(qn("w:fill"), hex_color)
        tcPr.append(shd)

    # ── Helper: add horizontal rule ──────────────────────────────
    def add_hr(paragraph, color="2E75B6", size=6):
        pPr = paragraph._p.get_or_add_pPr()
        pb = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        bottom.set(qn("w:val"), "single")
        bottom.set(qn("w:sz"), str(size))
        bottom.set(qn("w:space"), "2")
        bottom.set(qn("w:color"), color)
        pb.append(bottom)
        pPr.append(pb)

    # ── Helper: add top rule ─────────────────────────────────────
    def add_top_rule(paragraph, color="2E75B6", size=4):
        pPr = paragraph._p.get_or_add_pPr()
        pb = OxmlElement("w:pBdr")
        top = OxmlElement("w:top")
        top.set(qn("w:val"), "single")
        top.set(qn("w:sz"), str(size))
        top.set(qn("w:space"), "2")
        top.set(qn("w:color"), color)
        pb.append(top)
        pPr.append(pb)

    # ── Header (every page) ──────────────────────────────────────
    def make_header(section):
        header = section.header
        header.is_linked_to_previous = False
        for para in header.paragraphs:
            p = para._p
            p.getparent().remove(p)

        hdr_para = header.add_paragraph()
        add_hr(hdr_para)
        hdr_para.paragraph_format.space_after = Pt(4)

        run = hdr_para.add_run()
        if bristlecone_logo.exists():
            run.add_picture(str(bristlecone_logo), width=Inches(1.3))

        # NOTE: previous-client (Arclin) logo intentionally removed from the
        # page header. Do not embed any client-specific logo here.

    # ── Footer (every page) ──────────────────────────────────────
    def make_footer(section):
        footer = section.footer
        footer.is_linked_to_previous = False
        for para in footer.paragraphs:
            p = para._p
            p.getparent().remove(p)

        ftr_para = footer.add_paragraph()
        add_top_rule(ftr_para)
        ftr_para.paragraph_format.space_before = Pt(4)
        ftr_para.alignment = WD_ALIGN_PARAGRAPH.LEFT

        # Tab stops for center and right alignment
        from docx.oxml import OxmlElement as OE
        pPr = ftr_para._p.get_or_add_pPr()
        tabs = OE("w:tabs")
        tab_center = OE("w:tab")
        tab_center.set(qn("w:val"), "center")
        tab_center.set(qn("w:pos"), "4680")
        tab_right = OE("w:tab")
        tab_right.set(qn("w:val"), "right")
        tab_right.set(qn("w:pos"), "9360")
        tabs.append(tab_center)
        tabs.append(tab_right)
        pPr.append(tabs)

        # Left — Confidential
        run_conf = ftr_para.add_run("Confidential")
        run_conf.font.size = Pt(9)
        run_conf.font.color.rgb = GRAY
        run_conf.font.name = "Arial"

        # Center — Document ID
        ftr_para.add_run("\t")
        run_doc = ftr_para.add_run("Document ID: BD-001")
        run_doc.font.size = Pt(9)
        run_doc.font.color.rgb = GRAY
        run_doc.font.name = "Arial"

        # Right — Page X / Y
        ftr_para.add_run("\t")

        run_page = ftr_para.add_run()
        fldChar1 = OxmlElement("w:fldChar")
        fldChar1.set(qn("w:fldCharType"), "begin")
        instrText = OxmlElement("w:instrText")
        instrText.text = "PAGE"
        fldChar2 = OxmlElement("w:fldChar")
        fldChar2.set(qn("w:fldCharType"), "end")
        run_page._r.append(fldChar1)
        run_page._r.append(instrText)
        run_page._r.append(fldChar2)
        run_page.font.size = Pt(9)
        run_page.font.color.rgb = GRAY
        run_page.font.name = "Arial"

        run_sep = ftr_para.add_run(" / ")
        run_sep.font.size = Pt(9)
        run_sep.font.color.rgb = GRAY
        run_sep.font.name = "Arial"

        run_total = ftr_para.add_run()
        fldChar3 = OxmlElement("w:fldChar")
        fldChar3.set(qn("w:fldCharType"), "begin")
        instrText2 = OxmlElement("w:instrText")
        instrText2.text = "NUMPAGES"
        fldChar4 = OxmlElement("w:fldChar")
        fldChar4.set(qn("w:fldCharType"), "end")
        run_total._r.append(fldChar3)
        run_total._r.append(instrText2)
        run_total._r.append(fldChar4)
        run_total.font.size = Pt(9)
        run_total.font.color.rgb = GRAY
        run_total.font.name = "Arial"

    make_header(word_doc.sections[0])
    make_footer(word_doc.sections[0])

    # ── Build sections dict ──────────────────────────────────────
    # Never default to a real previous-client name. Use the project's client
    # name when present, otherwise a neutral placeholder.
    company = "[Client]"
    all_sections = {}
    if state is not None:
        company = (getattr(state, "company", "") or "").strip() or "[Client]"
        for attr in ["section1_sections", "br_sections", "ssc_sections",
                     "dp_sections", "ip_sections", "di_sections"]:
            sec_data = getattr(state, attr, None)
            if sec_data:
                all_sections.update(sec_data)

    # ── All section titles — caller-supplied title_map wins (per-project
    #    customs like "7.1.3"); fall back to the global live titles for any
    #    section the caller didn't list.
    ALL_TITLES = dict(_live_titles())
    if title_map:
        ALL_TITLES.update({k: v for k, v in title_map.items() if v})

    def sort_key(sid):
        return [int(p) if p.isdigit() else p for p in sid.split(".")]

    def depth_of(sid):
        return len(sid.split("."))

    # ================================================================
    # 1. COVER PAGE
    # ================================================================
    date_str = datetime.now().strftime("%B %d, %Y")

    # Blue bar + content table
    cover_table = word_doc.add_table(rows=1, cols=2)
    cover_table.alignment = WD_TABLE_ALIGNMENT.LEFT
    cover_table.columns[0].width = Inches(0.4)
    cover_table.columns[1].width = Inches(8.6)

    # Left navy bar
    left_cell = cover_table.cell(0, 0)
    set_cell_bg(left_cell, "1B3A6B")
    left_cell.paragraphs[0].add_run(" ")

    # Right content
    right_cell = cover_table.cell(0, 1)
    right_cell.paragraphs[0].clear()

    p1 = right_cell.add_paragraph()
    p1.paragraph_format.space_before = Pt(120)

    p_title = right_cell.add_paragraph()
    r = p_title.add_run("Blueprint Document")
    r.font.size = Pt(26)
    r.font.color.rgb = BLACK
    r.font.name = "Arial"
    p_title.paragraph_format.space_after = Pt(10)

    p_sub = right_cell.add_paragraph()
    r2 = p_sub.add_run(f"for {company}")
    r2.font.size = Pt(20)
    r2.font.color.rgb = NAVY
    r2.font.name = "Arial"
    p_sub.paragraph_format.space_after = Pt(10)

    p_date = right_cell.add_paragraph()
    r3 = p_date.add_run(date_str)
    r3.font.size = Pt(11)
    r3.font.color.rgb = GRAY
    r3.font.name = "Arial"

    p_ver = right_cell.add_paragraph()
    r4 = p_ver.add_run("Version 1.0")
    r4.font.size = Pt(11)
    r4.font.color.rgb = GRAY
    r4.font.name = "Arial"

    word_doc.add_page_break()

    # ================================================================
    # 2. REVISION HISTORY
    # ================================================================
    rh_heading = word_doc.add_paragraph()
    rh_run = rh_heading.add_run("Revision History")
    rh_run.font.size = Pt(16)
    rh_run.font.bold = True
    rh_run.font.color.rgb = NAVY
    rh_run.font.name = "Arial"
    rh_heading.paragraph_format.space_after = Pt(8)

    rh_table = word_doc.add_table(rows=2, cols=4)
    rh_table.style = "Table Grid"
    rh_table.alignment = WD_TABLE_ALIGNMENT.CENTER

    headers = ["Revision", "Author Name", "Date", "Description"]
    for i, h in enumerate(headers):
        cell = rh_table.cell(0, i)
        set_cell_bg(cell, "1B3A6B")
        p = cell.paragraphs[0]
        run = p.add_run(h)
        run.font.bold = True
        run.font.color.rgb = WHITE
        run.font.size = Pt(10)
        run.font.name = "Arial"

    data = ["0.1", "", date_str, "Initial Release"]
    for i, d in enumerate(data):
        cell = rh_table.cell(1, i)
        p = cell.paragraphs[0]
        run = p.add_run(d)
        run.font.size = Pt(10)
        run.font.name = "Arial"

    word_doc.add_page_break()

    # ================================================================
    # 3. TABLE OF CONTENTS
    # ================================================================
    toc_heading = word_doc.add_paragraph()
    toc_run = toc_heading.add_run("Table of Contents")
    toc_run.font.size = Pt(16)
    toc_run.font.bold = True
    toc_run.font.color.rgb = NAVY
    toc_run.font.name = "Arial"
    toc_heading.paragraph_format.space_after = Pt(12)

    # Tab stop position for right-aligned page numbers
    TAB_POS = 8640  # twips — roughly 6 inches from left margin

    sorted_ids = sorted(all_sections.keys(), key=sort_key)
    for sid in sorted_ids:
        # TOC entries: blank-out the title when it's missing or equal to the
        # ID itself so we never render "7.1.3   7.1.3" in the table of contents.
        _t = ALL_TITLES.get(sid, "")
        title = "" if (not _t or _t.strip() == sid) else _t
        d = depth_of(sid)

        p = word_doc.add_paragraph()
        p.paragraph_format.space_before = Pt(6 if d == 1 else 2)
        p.paragraph_format.space_after = Pt(0)

        # Set left indent based on depth
        # d=1: no indent, d=2: 0.3", d=3: 0.6"
        p.paragraph_format.left_indent  = Inches((d - 1) * 0.3)
        p.paragraph_format.first_line_indent = Inches(0)

        # Right-aligned tab stop with a dot leader, so each entry runs
        # "1.2  Title .................. 7" the way a Word TOC does. The tab
        # stop was previously declared with leader="none" and then never
        # used — no tab character was ever emitted and no page number
        # followed it, so entries just trailed off after the title.
        pPr = p._p.get_or_add_pPr()
        tabs = OxmlElement("w:tabs")
        tab_right = OxmlElement("w:tab")
        tab_right.set(qn("w:val"), "right")
        # Pull the stop in by the entry's own indent so every page number
        # still lines up on the same right edge regardless of depth.
        tab_right.set(qn("w:pos"), str(TAB_POS - int((d - 1) * 0.3 * 1440)))
        tab_right.set(qn("w:leader"), "dot")
        tabs.append(tab_right)
        pPr.append(tabs)

        # Section number — bold for level 1
        num_run = p.add_run(f"{sid}")
        num_run.font.size = Pt(12 if d == 1 else 11 if d == 2 else 10)
        num_run.font.bold = (d == 1)
        num_run.font.color.rgb = NAVY if d == 1 else BLACK
        num_run.font.name = "Arial"

        # Gap between number and title
        p.add_run("  ")

        # Title
        title_run = p.add_run(title)
        title_run.font.size = Pt(12 if d == 1 else 11 if d == 2 else 10)
        title_run.font.bold = (d == 1)
        title_run.font.color.rgb = NAVY if d == 1 else BLACK
        title_run.font.name = "Arial"

        # Tab across to the right margin, then the page number as a PAGEREF
        # field pointing at the bookmark on the matching body heading.
        tab_run = p.add_run("\t")
        tab_run.font.size = Pt(12 if d == 1 else 11 if d == 2 else 10)
        tab_run.font.name = "Arial"
        _add_pageref_run(p, _toc_bookmark_name(sid))
        for r in p.runs[-1:]:
            r.font.size = Pt(12 if d == 1 else 11 if d == 2 else 10)
            r.font.bold = (d == 1)
            r.font.color.rgb = NAVY if d == 1 else BLACK
            r.font.name = "Arial"

    word_doc.add_page_break()

    # ================================================================
    # 4. BODY SECTIONS
    # ================================================================
    _ho_set = set(heading_only_ids or [])
    _nh_set = set(no_heading_ids or [])
    # Bookmark ids must be unique across the document; the TOC references
    # these by name via PAGEREF.
    _bookmark_id = 1000

    top_ids = sorted(
        set(sid.split(".")[0] for sid in all_sections.keys()),
        key=lambda x: int(x)
    )

    for top_id in top_ids:
        ids = sorted(
            [sid for sid in all_sections if sid == top_id or sid.startswith(top_id + ".")],
            key=sort_key
        )

        top_title = ALL_TITLES.get(top_id, f"Section {top_id}")

        # When strict_selection is on, only emit the top-level heading when
        # that exact ID was selected by the user (i.e. it's a key in
        # all_sections, not just inferred from a subsection). And honor the
        # No-heading suppression.
        emit_top_heading = (
            (not strict_selection or top_id in all_sections)
            and top_id not in _nh_set
        )

        if emit_top_heading:
            # Divider
            div = word_doc.add_paragraph()
            add_hr(div, color="2E75B6", size=4)
            div.paragraph_format.space_before = Pt(12)
            div.paragraph_format.space_after = Pt(12)

            # Heading 1
            h1 = word_doc.add_paragraph()
            h1_run = h1.add_run(f"Section {top_id}: {top_title}")
            h1_run.font.size = Pt(16)
            h1_run.font.bold = True
            h1_run.font.color.rgb = NAVY
            h1_run.font.name = "Arial"
            h1.paragraph_format.space_after = Pt(8)
            # Structure for the navigation pane and a target for the TOC's
            # PAGEREF — the visual formatting above is left exactly as-is.
            _set_outline_level(h1, 1)
            _bookmark_id += 1
            _bookmark_paragraph(h1, _toc_bookmark_name(top_id), _bookmark_id)

        # Top-level body content — suppressed for heading-only and no-heading
        if (top_id in all_sections and all_sections.get(top_id)
                and top_id not in _ho_set and top_id not in _nh_set):
            _emit_body_lines(word_doc, all_sections[top_id], left_indent=0.0,
                             BLACK=BLACK, NAVY=NAVY, GRAY=GRAY)

        # Subsections
        for sid in ids:
            if sid == top_id:
                continue
            if sid in _nh_set:
                # No-heading suppresses both heading and body entirely.
                continue

            d = depth_of(sid)
            sub_title = ALL_TITLES.get(sid, "")
            # Don't echo the ID as its own title — drop the title fragment if
            # it's missing or identical to the section ID.
            heading_text = f"{sid}  {sub_title}".rstrip() if (sub_title and sub_title.strip() != sid) else sid

            if d == 2:
                h = word_doc.add_paragraph()
                hr = h.add_run(heading_text)
                hr.font.size = Pt(13)
                hr.font.bold = True
                hr.font.color.rgb = BLUE
                hr.font.name = "Arial"
                h.paragraph_format.space_before = Pt(10)
                h.paragraph_format.space_after = Pt(6)
                _set_outline_level(h, 2)
                _bookmark_id += 1
                _bookmark_paragraph(h, _toc_bookmark_name(sid), _bookmark_id)

            elif d == 3:
                h = word_doc.add_paragraph()
                hr = h.add_run(heading_text)
                hr.font.size = Pt(12)
                hr.font.bold = True
                hr.font.color.rgb = BLUE
                hr.font.name = "Arial"
                h.paragraph_format.left_indent = Inches(0.3)
                h.paragraph_format.space_before = Pt(8)
                h.paragraph_format.space_after = Pt(4)
                _set_outline_level(h, 3)
                _bookmark_id += 1
                _bookmark_paragraph(h, _toc_bookmark_name(sid), _bookmark_id)

            if all_sections.get(sid) and sid not in _ho_set:
                _emit_body_lines(word_doc, all_sections[sid], left_indent=(d - 1) * 0.2,
                                 BLACK=BLACK, NAVY=NAVY, GRAY=GRAY)

    # Make Word evaluate the TOC's PAGEREF fields on open so the page numbers
    # are real rather than the cached placeholder.
    _enable_update_fields(word_doc)
    word_doc.save(str(path))
    return path

# ================================================================
# BRISTLECONE FORMAT - USES TEMPLATE STYLES
# ================================================================

def save_docx_bristlecone(doc: str, output_dir: Path) -> Path:
    """
    Generate DOCX with Bristlecone branding and styles.
    
    Uses the Kinaxis Supply Chain Planning Tool template as style reference:
    - Blue title color (0070C0)
    - Frutiger/Arial fonts
    - Professional margins (0.5" L/R, 1" T/B)
    - Structured section hierarchy
    - BC Corporate styling
    
    Args:
        doc: Plain text document with Section headers
        output_dir: Output directory for generated DOCX
        
    Returns:
        Path to generated DOCX file
    """
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    
    # ✅ Ensure output directory
    if output_dir.suffix:
        output_dir = output_dir.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    
    # ✅ Create new document (don't modify template)
    word_doc = Document()
    
    # ✅ Set page margins to match Bristlecone template
    section = word_doc.sections[0]
    section.left_margin = Inches(0.5)
    section.right_margin = Inches(0.5)
    section.top_margin = Inches(1.0)
    section.bottom_margin = Inches(1.0)
    
    # ✅ Bristlecone color scheme
    BLUE_BRISTLE = RGBColor(0, 112, 192)  # 0070C0
    
    # ✅ Track sections to avoid duplicates
    section_seen = set()
    
    # ✅ Add title (if not present)
    title = word_doc.add_heading("Blueprint Document", level=0)
    title_run_list = title.runs
    for run in title_run_list:
        run.font.size = Pt(28)
        run.font.bold = True
        run.font.color.rgb = BLUE_BRISTLE
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    
    # ✅ Process document content
    for line in doc.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        
        # Skip separator lines
        if stripped.startswith("=") or stripped.startswith("─"):
            continue
        
        # ✅ Section headers with Bristlecone blue
        if stripped.startswith("Section"):
            if stripped in section_seen:
                continue  # skip duplicate
            section_seen.add(stripped)
            
            # Parse section number to determine level
            parts = stripped.split(":")
            section_id = parts[0].replace("Section", "").strip()
            dots = section_id.count(".")
            
            # Determine heading level
            if dots == 0:
                # Main section: use Heading 1
                heading = word_doc.add_heading(stripped, level=1)
                heading_level = 1
            else:
                # Subsection: use Heading 2
                heading = word_doc.add_heading(stripped, level=2)
                heading_level = 2
            
            # Apply Bristlecone blue to heading
            for run in heading.runs:
                run.font.color.rgb = BLUE_BRISTLE
                if heading_level == 1:
                    run.font.size = Pt(18)
                    run.font.bold = True
                else:
                    run.font.size = Pt(14)
                    run.font.bold = True
            
            heading.alignment = WD_ALIGN_PARAGRAPH.LEFT
            continue
        
        # ✅ Body content: structured bullets with proper spacing
        # Split into logical points
        if any(sep in stripped for sep in [".", ",", ";"]):
            # Split by sentence delimiters but keep content
            points = [p.strip() for p in re.split(r'[.;]+', stripped) if p.strip()]
            for point in points:
                para = word_doc.add_paragraph(style="List Bullet")
                run = para.add_run(point)
                run.font.name = "Arial"
                run.font.size = Pt(11)
                para.alignment = WD_ALIGN_PARAGRAPH.LEFT
                para.space_after = Pt(6)
        else:
            # Single point
            para = word_doc.add_paragraph(style="List Bullet")
            run = para.add_run(stripped)
            run.font.name = "Arial"
            run.font.size = Pt(11)
            para.alignment = WD_ALIGN_PARAGRAPH.LEFT
            para.space_after = Pt(6)
    
    # ✅ Generate filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"BD_Bristlecone_{timestamp}.docx"
    
    word_doc.save(str(path))
    return path

# ================================================================
# DYNAMIC ASSEMBLY — respects live section titles from Screen 3
# These functions replace assemble_document() and save_docx() for
# the Screen 3 editor flow where sections may be renamed or added.
# ================================================================

def assemble_document_dynamic(
    sections: dict,
    title_map: dict,
) -> str:
    """
    Assemble approved sections into a flat text document.

    Args:
        sections  : { section_id: content_text } — all approved content
        title_map : { section_id: title }        — live titles from screen3_sections.json

    Returns:
        Plain text document string, sections in numeric order.
    """
    if not sections:
        return ""

    def _sort_key(sid):
        return [int(p) if p.isdigit() else p for p in sid.split(".")]

    # Group sections by top-level number
    top_ids = sorted(
        set(sid.split(".")[0] for sid in sections),
        key=lambda x: (int(x) if x.isdigit() else x)
    )

    lines = []

    for top_id in top_ids:
        # Collect all IDs belonging to this top section
        group_ids = sorted(
            [sid for sid in sections if sid == top_id or sid.startswith(top_id + ".")],
            key=_sort_key,
        )

        # Section group header — use live title, fall back to section number
        top_title = title_map.get(top_id, f"Section {top_id}")
        lines.append("=" * 60)
        lines.append(f"Section {top_id}: {top_title}")
        lines.append("=" * 60)
        lines.append("")

        for sid in group_ids:
            content = sections.get(sid, "").strip()
            if not content:
                continue

            if sid == top_id:
                # Top-level overview content — no extra heading
                lines.append(content)
                lines.append("")
            else:
                sub_title = title_map.get(sid, sid)
                lines.append(f"Section {sid}: {sub_title}")
                lines.append(content)
                lines.append("")

    return "\n".join(lines)


def save_docx_dynamic(
    sections: dict,
    title_map: dict,
    output_dir: Path,
    company: str = "Client",
) -> Path:
    """
    Generate a formatted Blueprint Document .docx using live section titles.

    Args:
        sections  : { section_id: content_text }
        title_map : { section_id: title } — from screen3_sections.json
        output_dir: output directory
        company   : client company name for cover page

    Returns:
        Path to generated .docx file
    """
    from docx import Document
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from datetime import datetime

    if output_dir.suffix:
        output_dir = output_dir.parent
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = output_dir / f"BD_{timestamp}.docx"

    NAVY  = RGBColor(0x1B, 0x3A, 0x6B)
    BLUE  = RGBColor(0x2E, 0x75, 0xB6)
    BLACK = RGBColor(0x1A, 0x1A, 0x1A)
    WHITE = RGBColor(0xFF, 0xFF, 0xFF)

    word_doc = Document()
    for sec in word_doc.sections:
        sec.left_margin   = Inches(1.0)
        sec.right_margin  = Inches(1.0)
        sec.top_margin    = Inches(1.0)
        sec.bottom_margin = Inches(1.0)

    def add_hr(paragraph, color="2E75B6", size=4):
        pPr = paragraph._p.get_or_add_pPr()
        pb  = OxmlElement("w:pBdr")
        bot = OxmlElement("w:bottom")
        bot.set(qn("w:val"),   "single")
        bot.set(qn("w:sz"),    str(size))
        bot.set(qn("w:space"), "2")
        bot.set(qn("w:color"), color)
        pb.append(bot)
        pPr.append(pb)

    def _sort_key(sid):
        return [int(p) if p.isdigit() else p for p in sid.split(".")]

    def depth_of(sid):
        return len(sid.split("."))

    date_str = datetime.now().strftime("%B %d, %Y")

    # ── Cover page ───────────────────────────────────────────────────────────
    cover_title = word_doc.add_paragraph()
    ct_run = cover_title.add_run("Blueprint Document")
    ct_run.font.size  = Pt(28)
    ct_run.font.bold  = True
    ct_run.font.color.rgb = NAVY
    ct_run.font.name  = "Arial"
    cover_title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cover_title.paragraph_format.space_before = Pt(80)

    sub = word_doc.add_paragraph()
    sub_run = sub.add_run(company)
    sub_run.font.size  = Pt(16)
    sub_run.font.color.rgb = BLUE
    sub_run.font.name  = "Arial"
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER

    date_p = word_doc.add_paragraph()
    date_run = date_p.add_run(date_str)
    date_run.font.size  = Pt(12)
    date_run.font.color.rgb = BLACK
    date_run.font.name  = "Arial"
    date_p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    word_doc.add_page_break()

    # ── Table of contents ────────────────────────────────────────────────────
    toc_h = word_doc.add_paragraph()
    toc_run = toc_h.add_run("Table of Contents")
    toc_run.font.size  = Pt(16)
    toc_run.font.bold  = True
    toc_run.font.color.rgb = NAVY
    toc_run.font.name  = "Arial"
    toc_h.paragraph_format.space_after = Pt(12)

    sorted_ids = sorted(sections.keys(), key=_sort_key)
    for sid in sorted_ids:
        title = title_map.get(sid, sid)
        d = depth_of(sid)
        p = word_doc.add_paragraph()
        p.paragraph_format.left_indent  = Inches((d - 1) * 0.3)
        p.paragraph_format.space_before = Pt(5 if d == 1 else 2)
        p.paragraph_format.space_after  = Pt(0)
        run = p.add_run(f"{sid}  {title}")
        run.font.size  = Pt(12 if d == 1 else 11 if d == 2 else 10)
        run.font.bold  = (d == 1)
        run.font.color.rgb = NAVY if d == 1 else BLACK
        run.font.name  = "Arial"

    word_doc.add_page_break()

    # ── Body sections ────────────────────────────────────────────────────────
    top_ids = sorted(
        set(sid.split(".")[0] for sid in sections),
        key=lambda x: (int(x) if x.isdigit() else x)
    )

    for top_id in top_ids:
        group_ids = sorted(
            [sid for sid in sections if sid == top_id or sid.startswith(top_id + ".")],
            key=_sort_key,
        )

        top_title = title_map.get(top_id, f"Section {top_id}")

        # Divider rule
        div = word_doc.add_paragraph()
        add_hr(div, color="2E75B6", size=4)
        div.paragraph_format.space_before = Pt(12)
        div.paragraph_format.space_after  = Pt(12)

        # Section heading
        h1 = word_doc.add_paragraph()
        h1_run = h1.add_run(f"Section {top_id}: {top_title}")
        h1_run.font.size  = Pt(16)
        h1_run.font.bold  = True
        h1_run.font.color.rgb = NAVY
        h1_run.font.name  = "Arial"
        h1.paragraph_format.space_after = Pt(8)

        for sid in group_ids:
            content = sections.get(sid, "").strip()
            if not content:
                continue

            d = depth_of(sid)

            if sid == top_id:
                # Top-level overview paragraph
                for line in content.split("\n"):
                    line = line.strip()
                    if line:
                        p = word_doc.add_paragraph()
                        run = p.add_run(line)
                        run.font.size  = Pt(11)
                        run.font.color.rgb = BLACK
                        run.font.name  = "Arial"
                        p.paragraph_format.space_after = Pt(4)
                        p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                continue

            sub_title = title_map.get(sid, sid)

            # Subsection heading
            h = word_doc.add_paragraph()
            hr = h.add_run(f"{sid}  {sub_title}")
            hr.font.size  = Pt(13 if d == 2 else 12)
            hr.font.bold  = True
            hr.font.color.rgb = BLUE
            hr.font.name  = "Arial"
            if d >= 3:
                h.paragraph_format.left_indent = Inches(0.3)
            h.paragraph_format.space_before = Pt(10 if d == 2 else 8)
            h.paragraph_format.space_after  = Pt(6 if d == 2 else 4)

            # Subsection content
            for line in content.split("\n"):
                line = line.strip()
                if line:
                    p = word_doc.add_paragraph()
                    run = p.add_run(line)
                    run.font.size  = Pt(11)
                    run.font.color.rgb = BLACK
                    run.font.name  = "Arial"
                    p.paragraph_format.left_indent = Inches((d - 1) * 0.2)
                    p.paragraph_format.space_after = Pt(4)
                    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY

    word_doc.save(str(path))
    return path

# ─────────────────────────────────────────────────────────────────────────────
# TEMPLATE-BASED EXPORT
# Fills the uploaded Solution Blueprint template with generated content.
# ─────────────────────────────────────────────────────────────────────────────

# Maps our section IDs to the heading text in the template
_TEMPLATE_SECTION_MAP = {
    # Introduction
    "1":     "Introduction",
    "1.1":   "Company Information",       # not in template — inserted after Purpose
    "1.2":   "Current State of Business", # not in template — inserted after Purpose
    "1.3":   "Purpose",
    "1.4":   "Scope",
    "1.5":   "Definitions & Acronyms",
    # Benefit Realization
    "2":     "Benefit Realization",
    "2.1":   "Quantitative Benefits",     # we use for Business Issues
    "2.2":   "Qualitative Benefits",      # we use for Value Drivers
    # Supply Chain Scope
    "3":     "Supply Chain Scope",
    "3.1":   "Supply Chain Maps",
    "3.2":   "Sites",
    "3.3":   None,   # Demand Foundation — not in template, add after Sites
    "3.4":   None,   # Supply Foundation — not in template
    "3.5":   None,   # Inventory Management Foundation — not in template
    "3.6":   None,   # Constraints — not in template
    "3.7":   None,   # Scenario Structure — not in template
    # Demand Planning
    "4":     "Demand Planning",
    "4.1":   "Demand Planning Process Overview (Level 2)",
    # Supply & Inventory Planning
    "5":     "Supply & Inventory Planning",
    "5.1":   "Review and Adjust Planning Parameters",
    "5.2":   "Manage Capacity Constraints",
    "5.3":   "Resolve Supply Plan Exceptions",
    "5.4":   "Resolve Inventory Exceptions",
    # Data Integration
    "6":     "Data Integration",
    "6.1":   "Architecture",
    "6.2":   "Data Sources",
    "6.3":   "Data Files",
    "6.4":   None,  # Data Frequency — insert after Data Files
}


def save_docx_from_template(
    sections: dict,
    template_path: Path,
    output_dir: Path,
    client_name: str = "",
    revision_user: str = "",
    revision_email: str = "",
    revision_description: str = "",
    strip_empty_sections: bool = False,
    heading_only_ids: Optional[set] = None,
    strict_selection: bool = False,
    title_map: Optional[dict] = None,
) -> Path:
    """
    Fill the Solution Blueprint template with generated section content.

    Strategy:
    1. Copy the template to a new file
    2. Replace [customer] placeholder with client name
    3. For each section with content, find its heading in the template
       and insert the generated content immediately after the heading,
       BEFORE the next heading of the same or higher level.
       Any existing placeholder text in that range is cleared first.
    4. For sections not in the template (3.3-3.7, 6.4 etc.),
       append a new heading + content after the nearest sibling section.

    Returns the path to the filled document.
    """
    from docx import Document
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement
    from copy import deepcopy
    import lxml.etree as etree
    import re as _re

    # ── 1. Copy template ──────────────────────────────────────────────────────
    import shutil
    ts = __import__("datetime").datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    fname = f"BD_{ts}.docx"
    out_path = output_dir / fname
    shutil.copy2(str(template_path), str(out_path))

    doc = Document(str(out_path))
    body = doc.element.body

    # ── 2. Replace [customer] placeholder + dates everywhere ────────────────
    # Walk the WHOLE document — main body, every table cell (incl. nested),
    # and every header/footer — and run the placeholder/date substitutions
    # there. The original implementation only iterated `doc.paragraphs`,
    # which missed tables (where the [customer] placeholder lives on the
    # title page of the Solution Blueprint template) and missed runs that
    # Word split across formatting boundaries.
    display_name = client_name or "Client"
    today_str    = __import__("datetime").datetime.utcnow().strftime("%Y-%m-%d")
    _stamp_template_text(doc, display_name, today_str)
    # Stamp "Issue Date: …" style labels specifically — the cover-page line
    # often lives in a table cell where the bare regex pass can miss it.
    try:
        _stamp_issue_date(doc, today_str)
    except Exception as _dt_err:
        logger.warning("Could not stamp Issue Date labels: %s", _dt_err)

    # ── Helper: find paragraph index by heading text (case-insensitive) ──────
    all_paras = list(doc.paragraphs)

    def _find_heading_idx(text: str) -> int:
        if not text:
            return -1
        text_lower = text.lower().strip()
        for i, p in enumerate(all_paras):
            if p.style.name.startswith("Heading") or p.style.name == "Title":
                if p.text.strip().lower() == text_lower:
                    return i
        # Partial match fallback
        for i, p in enumerate(all_paras):
            if p.style.name.startswith("Heading"):
                if text_lower in p.text.strip().lower():
                    return i
        return -1

    def _heading_level(para) -> int:
        name = para.style.name
        if name == "Title": return 0
        if name.startswith("Heading"):
            try: return int(name.split()[-1])
            except: return 1
        return 99

    # ── Helper: detect/parse Markdown tables ─────────────────────────────────
    def _is_md_table_row(line: str) -> bool:
        """
        True if a line looks like a Markdown table row, e.g.
            | Col A | Col B |
            |-------|-------|
        Requires at least two pipe characters and content between them.
        """
        if not line:
            return False
        s = line.strip()
        if not s.startswith("|"):
            return False
        # Need at least one interior pipe to form a cell boundary.
        return s.count("|") >= 2

    def _parse_md_table(tbl_lines: list) -> list:
        """
        Parse a block of Markdown table lines into a list-of-lists of cells.
        Skips the separator row (e.g. |---|---|).
        Returns [] if no usable rows are found.
        """
        rows = []
        for raw in tbl_lines:
            line = raw.strip()
            if not line or not line.startswith("|"):
                continue
            # Drop leading/trailing pipes, then split on '|'.
            inner = line.strip("|")
            cells = [c.strip() for c in inner.split("|")]
            # Skip separator rows (cells like '---', ':---:', etc.)
            if cells and all(_re.fullmatch(r":?-{1,}:?", c or "") for c in cells):
                continue
            rows.append(cells)
        return rows

    # ── Helper: insert content paragraphs after a given paragraph ────────────
    def _insert_content_after(anchor_para, content_text: str, level: int = 2):
        """
        Insert content_text after anchor_para in the document body.
        Handles Markdown tables, bullets, sub-headings, plain paragraphs.
        """
        anchor_elem = anchor_para._element
        insert_after = anchor_elem

        # Clear existing placeholder paragraphs between anchor and next heading
        # Find all paragraphs that are plain text (not headings) after anchor
        # up to the next heading of same/higher level — remove them
        paras = list(body)
        anchor_idx = list(body).index(anchor_elem)
        remove_until = anchor_idx + 1
        for j in range(anchor_idx + 1, len(paras)):
            elem = paras[j]
            tag = elem.tag.split("}")[-1]
            if tag == "p":
                import docx
                p_obj = docx.text.paragraph.Paragraph(elem, doc)
                if p_obj.style.name.startswith("Heading"):
                    plvl = _heading_level(p_obj)
                    if plvl <= level:
                        break
                # Remove placeholder text (contains [ ], is Normal/Text 2)
                if ("[" in p_obj.text and "]" in p_obj.text) or                    p_obj.style.name in ("Normal", "Text 2", "List Paragraph"):
                    body.remove(elem)
                    continue
            elif tag == "tbl":
                pass  # leave tables in place
            remove_until = j + 1

        # Now insert new content
        lines = content_text.split("\n")
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            new_elem = None

            # Markdown table
            if _is_md_table_row(line):
                tbl_lines = []
                while i < len(lines) and _is_md_table_row(lines[i].strip()):
                    tbl_lines.append(lines[i])
                    i += 1
                rows = _parse_md_table(tbl_lines)
                if rows:
                    # Create a Word table using template's table style if available
                    n_cols = max(len(r) for r in rows)
                    rows_p = [r + [""] * (n_cols - len(r)) for r in rows]
                    tbl_elem, trail_p = _make_inline_table(doc, rows_p)
                    insert_after.addnext(tbl_elem)
                    tbl_elem.addnext(trail_p)
                    insert_after = trail_p
                continue

            # Bullet
            elif line.startswith(("• ", "- ", "* ")):
                p = OxmlElement("w:p")
                pPr = OxmlElement("w:pPr")
                pStyle = OxmlElement("w:pStyle")
                pStyle.set(qn("w:val"), "ListParagraph")
                pPr.append(pStyle)
                jc = OxmlElement("w:jc")
                jc.set(qn("w:val"), "both")
                pPr.append(jc)
                p.append(pPr)
                r = OxmlElement("w:r")
                t = OxmlElement("w:t")
                t.text = line[2:].strip()
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                r.append(t)
                p.append(r)
                new_elem = p

            # Sub-heading label (Solution Overview:, Key Solution Components: etc)
            elif any(line.startswith(lbl) for lbl in (
                "Solution Overview:", "Key Solution Components:",
                "Strategic Assumptions:", "Source:", "Notes:"
            )):
                colon = line.index(":")
                label = line[:colon + 1]
                content = line[colon + 1:].strip()
                p = OxmlElement("w:p")
                r_label = OxmlElement("w:r")
                rPr = OxmlElement("w:rPr")
                bold = OxmlElement("w:b"); rPr.append(bold)
                r_label.append(rPr)
                t_label = OxmlElement("w:t")
                t_label.text = label + (" " if content else "")
                t_label.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                r_label.append(t_label)
                p.append(r_label)
                if content:
                    r_cont = OxmlElement("w:r")
                    t_cont = OxmlElement("w:t")
                    t_cont.text = content
                    t_cont.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    r_cont.append(t_cont)
                    p.append(r_cont)
                new_elem = p

            # Normal paragraph
            else:
                p = OxmlElement("w:p")
                # Use Text 2 style if it exists in template
                pPr = OxmlElement("w:pPr")
                pStyle = OxmlElement("w:pStyle")
                pStyle.set(qn("w:val"), "Text2")
                pPr.append(pStyle)
                p.append(pPr)
                r = OxmlElement("w:r")
                t = OxmlElement("w:t")
                t.text = line
                t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                r.append(t)
                p.append(r)
                new_elem = p

            if new_elem is not None:
                insert_after.addnext(new_elem)
                insert_after = new_elem
            i += 1

    # ── Helper: make inline Word table ───────────────────────────────────────
    def _make_inline_table(doc, rows: list):
        from docx.oxml import OxmlElement
        from docx.oxml.ns import qn
        from app.sow_markdown import split_bold_segments

        n_cols = max(1, max((len(r) for r in rows), default=1))
        # Width the table to the printable area of the page it lands on rather
        # than a fixed 8500 dxa, so it neither overhangs a narrow margin nor
        # leaves a stripe of white space beside a wide one.
        try:
            sec = doc.sections[0]
            total_w = int(sec.page_width - sec.left_margin - sec.right_margin) // 635
        except Exception:
            total_w = 9360   # Letter with 1in margins, in dxa
        total_w = max(1440, min(total_w, 14400))
        col_w = max(1, total_w // n_cols)
        total_w = col_w * n_cols   # keep grid and table width consistent

        tbl = OxmlElement("w:tbl")
        tblPr = OxmlElement("w:tblPr")
        tblStyle = OxmlElement("w:tblStyle")
        tblStyle.set(qn("w:val"), "TableGrid")
        tblPr.append(tblStyle)
        tblW = OxmlElement("w:tblW")
        tblW.set(qn("w:w"), str(total_w)); tblW.set(qn("w:type"), "dxa")
        tblPr.append(tblW)
        # Fixed layout: without it Word recomputes column widths from content
        # and the explicit grid below is ignored.
        tblLayout = OxmlElement("w:tblLayout")
        tblLayout.set(qn("w:type"), "fixed")
        tblPr.append(tblLayout)
        # Breathing room inside every cell — text otherwise sits flush against
        # the cell borders.
        tblCellMar = OxmlElement("w:tblCellMar")
        for side, val in (("top", 60), ("left", 108), ("bottom", 60), ("right", 108)):
            node = OxmlElement(f"w:{side}")
            node.set(qn("w:w"), str(val)); node.set(qn("w:type"), "dxa")
            tblCellMar.append(node)
        tblPr.append(tblCellMar)
        # Center the table on the page.
        jc = OxmlElement("w:jc")
        jc.set(qn("w:val"), "center")
        tblPr.append(jc)
        tbl.append(tblPr)

        # OOXML requires <w:tblGrid> with one <w:gridCol> per column.
        tblGrid = OxmlElement("w:tblGrid")
        for _ in range(n_cols):
            gridCol = OxmlElement("w:gridCol")
            gridCol.set(qn("w:w"), str(col_w))
            tblGrid.append(gridCol)
        tbl.append(tblGrid)

        for r_idx, row_data in enumerate(rows):
            tr = OxmlElement("w:tr")
            is_header = (r_idx == 0)
            trPr = OxmlElement("w:trPr")
            if is_header:
                # Repeat the header on every page the table spans. Without
                # this a table breaking across a page boundary continues with
                # unlabelled columns.
                tblHeader = OxmlElement("w:tblHeader")
                tblHeader.set(qn("w:val"), "true")
                trPr.append(tblHeader)
            # Keep a row's wrapped lines together instead of splitting one row
            # across two pages.
            cantSplit = OxmlElement("w:cantSplit")
            trPr.append(cantSplit)
            tr.append(trPr)

            # Pad short rows so a ragged markdown table still yields a
            # rectangular Word table (Word renders a row with missing cells as
            # a visibly broken grid).
            cells = list(row_data) + [""] * (n_cols - len(row_data))
            for c_idx in range(n_cols):
                cell_text = cells[c_idx]
                tc = OxmlElement("w:tc")
                tcPr = OxmlElement("w:tcPr")
                tcW = OxmlElement("w:tcW")
                tcW.set(qn("w:w"), str(col_w)); tcW.set(qn("w:type"), "dxa")
                tcPr.append(tcW)
                vAlign = OxmlElement("w:vAlign")
                vAlign.set(qn("w:val"), "center")
                tcPr.append(vAlign)
                if is_header:
                    shd = OxmlElement("w:shd")
                    shd.set(qn("w:val"), "clear"); shd.set(qn("w:color"), "auto")
                    shd.set(qn("w:fill"), "1B3A6B")
                    tcPr.append(shd)
                tc.append(tcPr)
                p = OxmlElement("w:p")
                # Cell text is markdown: emit **bold** as real bold runs rather
                # than printing the asterisks into the document.
                for seg_text, seg_bold in (split_bold_segments(cell_text or "")
                                           or [("", False)]):
                    r = OxmlElement("w:r")
                    if is_header or seg_bold:
                        rPr = OxmlElement("w:rPr")
                        rPr.append(OxmlElement("w:b"))
                        if is_header:
                            clr = OxmlElement("w:color")
                            clr.set(qn("w:val"), "FFFFFF")
                            rPr.append(clr)
                        r.append(rPr)
                    t = OxmlElement("w:t")
                    t.text = seg_text
                    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    r.append(t)
                    p.append(r)
                tc.append(p); tr.append(tc)
            tbl.append(tr)

        # Word requires a paragraph after a table; without it the docx will
        # appear to "swallow" subsequent content. Return both elements so the
        # caller can insert the table then its trailing paragraph.
        trail = OxmlElement("w:p")
        return tbl, trail

    # ── 3. Insert content for each section ───────────────────────────────────
    # Process sections in order so insertions don't break indices
    ordered_ids = sorted(
        sections.keys(),
        key=lambda sid: [int(x) if x.isdigit() else x
                         for x in sid.split(".")]
    )

    heading_only_set = set(heading_only_ids or [])
    for sid in ordered_ids:
        content = (sections.get(sid) or "").strip()
        is_heading_only = sid in heading_only_set
        # Heading-only sections legitimately have empty content — keep going so
        # the heading is still emitted (and survives _enforce_generated_only).
        if not content and not is_heading_only:
            continue

        template_heading = _TEMPLATE_SECTION_MAP.get(sid)
        level = len(sid.split(".")) + 1  # "2" → H1, "2.1" → H2 etc

        if template_heading:
            h_idx = _find_heading_idx(template_heading)
            if h_idx >= 0:
                if not is_heading_only:
                    _insert_content_after(all_paras[h_idx], content, level)
                    logger.info("Template: inserted content for section %s under '%s'",
                                sid, template_heading)
                else:
                    logger.info("Template: heading-only section %s — heading kept, body skipped", sid)
                continue

        # Section not found in template — append at end of parent section
        logger.info("Template: section %s not found in template — appended after parent", sid)
        parent_id = ".".join(sid.split(".")[:-1]) if "." in sid else sid
        parent_heading = _TEMPLATE_SECTION_MAP.get(parent_id)
        if parent_heading:
            h_idx = _find_heading_idx(parent_heading)
            if h_idx >= 0:
                # Find end of parent section (next H1/H2 at same level)
                anchor = all_paras[h_idx]
                # Add a sub-heading then content
                import docx
                p = OxmlElement("w:p")
                pPr = OxmlElement("w:pPr")
                pStyle = OxmlElement("w:pStyle")
                pStyle.set(qn("w:val"), f"Heading{level}")
                pPr.append(pStyle)
                p.append(pPr)
                r = OxmlElement("w:r")
                t = OxmlElement("w:t")
                try:
                    from app.section_titles import get_titles as _gt
                except ImportError:
                    from section_titles import get_titles as _gt
                t.text = _gt().get(sid, sid)
                r.append(t); p.append(r)

                # Insert heading after parent anchor
                anchor._element.addnext(p)
                # Heading-only sections stop here; the heading is enough.
                if is_heading_only:
                    continue
                # Create a fake paragraph wrapper for _insert_content_after
                import docx
                fake_para = docx.text.paragraph.Paragraph(p, doc)
                _insert_content_after(fake_para, content, level + 1)
                continue

    # ── 3a-pre. Rebuild Table of Contents (must run BEFORE stripping so we
    #            can re-issue it cleanly afterwards) ────────────────────────
    # The strip step physically removes headings + their bodies, so the only
    # way the TOC can mirror what remains is to be regenerated from the
    # post-strip heading list. We do this in two passes:
    #   • clear the existing TOC body now
    #   • re-emit fresh TOC entries after the strip step
    # The actual page numbers are left to Word — we mark fields dirty so it
    # refreshes them on open.

    # ── 3a. STRICT body cleanup: keep ONLY user-generated sections + front
    #         matter, then rewrite each surviving section heading to
    #         "{section_id} {title}" so the numbering matches what the user
    #         actually generated (no Word auto-numbering surprises).
    if strip_empty_sections:
        try:
            _enforce_generated_sections_only(
                doc=doc,
                sections=sections,
                section_map=_TEMPLATE_SECTION_MAP,
                heading_only_ids=heading_only_set,
                strict=strict_selection,
                title_map=title_map,
            )
        except Exception as _strip_err:
            logger.warning(
                "Could not enforce generated-only body (continuing): %s",
                _strip_err,
            )

    # ── 3b. Populate Revision History with the logged-in user ────────────────
    if revision_user or revision_description:
        try:
            _populate_revision_history(
                doc,
                user_name=revision_user or "Author",
                user_email=revision_email,
                description=revision_description or "Initial draft",
            )
        except Exception as _rev_err:
            logger.warning(
                "Could not update revision history (continuing): %s", _rev_err,
            )

    # ── 3c. Rebuild the Table of Contents to list ONLY surviving headings ──
    try:
        _rebuild_table_of_contents(doc)
    except Exception as _toc_err:
        logger.warning("Could not rebuild Table of Contents: %s", _toc_err)

    # Mark all fields dirty so Word refreshes page numbers / cross-references
    # the next time the document is opened.
    try:
        _mark_fields_dirty(doc)
    except Exception as _fdirty_err:
        logger.warning("Could not flag fields dirty: %s", _fdirty_err)

    # ── 4. Save ───────────────────────────────────────────────────────────────
    doc.save(str(out_path))
    logger.info("Template-filled document saved: %s", out_path)
    return out_path


def _normalize_heading(text: str) -> str:
    """
    Reduce a heading to its comparable core so "1. Introduction",
    "1 Introduction", "1.1 Introduction" and "Introduction" all match.
    Strips leading numbering / punctuation / whitespace and lowercases.
    """
    import re as _re
    s = (text or "").strip()
    # Drop leading section numbers like "1.", "1.1", "1)", "1 -"
    s = _re.sub(r"^[\dIVXLCM]+(\.\d+)*[\.\):\-\s]+", "", s)
    # Collapse whitespace and lowercase
    s = _re.sub(r"\s+", " ", s).strip().lower()
    return s


def _heading_rank_for_elem(elem):
    """
    Return Word heading level (1, 2, 3 …) or 0 if not a heading paragraph.
    Tolerates both the XML style id ("Heading1") and the human style name
    ("Heading 1") that Word emits.
    """
    from docx.oxml.ns import qn
    if elem.tag != qn("w:p"):
        return 0
    pStyle = elem.find(qn("w:pPr") + "/" + qn("w:pStyle"))
    if pStyle is None:
        return 0
    val = (pStyle.get(qn("w:val")) or "").strip()
    if val == "Title":
        return 1
    # Accept "Heading1", "Heading 1", "heading1", etc.
    import re as _re
    m = _re.match(r"(?i)^heading\s*(\d+)$", val)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 1
    return 0


def _para_text_for_elem(elem):
    from docx.oxml.ns import qn
    if elem.tag != qn("w:p"):
        return ""
    return "".join(t.text or "" for t in elem.iter(qn("w:t"))).strip()


def _strip_empty_template_sections(doc, sections: dict, section_map: dict):
    """
    Remove headings (and the paragraphs/tables underneath them) for every
    template section that is "completely empty" — meaning neither the
    section itself NOR any of its descendants were generated by the user.

    Example: if the user generated only "3.1" and "3.2", we MUST keep the
    parent heading "3 Supply Chain Scope" — otherwise stripping it would
    take all of section 3's body with it, including the 3.1/3.2 content we
    just inserted. Section 4 ("Demand Planning") on the other hand has no
    descendants in `sections`, so its whole subtree gets removed.

    Heading matching is fuzzy: "1. Introduction", "1.1 Company Information",
    "1.1 Company Information " and "Company Information" all match the same
    map entry. Front matter that is not in section_map (Revision History,
    Document Approval, Table of Contents, etc.) is preserved.
    """
    generated_ids = {sid for sid, content in sections.items() if (content or "").strip()}

    def _has_generated_descendant(sid: str) -> bool:
        """True if sid itself or any descendant is in generated_ids."""
        if sid in generated_ids:
            return True
        prefix = sid + "."
        return any(g == sid or g.startswith(prefix) for g in generated_ids)

    # Build lookup keyed on the normalised heading text — but only include
    # template entries that have NO generated descendant whatsoever.
    drop_headings = {}
    for sid, heading in section_map.items():
        if not heading:
            continue
        if _has_generated_descendant(sid):
            continue  # keep this heading (it or one of its children is in use)
        drop_headings.setdefault(_normalize_heading(heading), sid)

    if not drop_headings:
        return

    body = doc.element.body
    children = list(body)

    to_remove = []
    i = 0
    n = len(children)
    while i < n:
        elem = children[i]
        rank = _heading_rank_for_elem(elem)
        if rank > 0 and _normalize_heading(_para_text_for_elem(elem)) in drop_headings:
            to_remove.append(elem)
            j = i + 1
            while j < n:
                nxt = children[j]
                nxt_rank = _heading_rank_for_elem(nxt)
                if nxt_rank and nxt_rank <= rank:
                    break
                to_remove.append(nxt)
                j += 1
            i = j
            continue
        i += 1

    for elem in to_remove:
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)

    if to_remove:
        logger.info(
            "Stripped %d element(s) for %d un-generated template section(s).",
            len(to_remove), len(drop_headings),
        )


# Headings that belong to the template's front matter — they have no
# corresponding section_id but must NEVER be removed by the strict cleanup.
_FRONT_MATTER_HEADINGS = frozenset({
    "table of contents",
    "revision history",
    "amendment history",
    "version history",
    "document approval",
    "document approvals",
    "approvers",
    "approval",
    "document control",
    "executive summary",
    "preface",
    "foreword",
    "abbreviations",
    "acronyms",
    "glossary",
})


def _enforce_generated_sections_only(doc, sections: dict, section_map: dict,
                                     heading_only_ids: Optional[set] = None,
                                     strict: bool = False,
                                     title_map: Optional[dict] = None):
    """
    The strict version of `_strip_empty_template_sections`.

    Walks every Heading-1/2/3 paragraph in the body. For each heading we
    decide its fate by matching it to a section_id from `section_map` or the
    canonical title table — then:

      • Heading belongs to a section the user generated (or to a parent of
        one)        → KEEP, and rewrite its visible text to
                      "{section_id} {title}" so the displayed numbering is
                      exactly what the user produced.
      • Heading is front matter (TOC, Revision History, Approvals, …)
                    → KEEP, text untouched.
      • Heading matches a known section the user did NOT generate
                    → REMOVE (heading + body until next heading of equal or
                      higher rank).
      • Heading does not match anything we recognise
                    → REMOVE (treated as a leftover template section).

    This collapses the "strip un-generated" and "rewrite numbering" steps
    into one pass so we can't drift between them.
    """
    try:
        from app.section_titles import get_titles
    except ImportError:
        from section_titles import get_titles

    # Heading-only sections have empty content by design — their heading must
    # survive the strip pass even though the body is blank.
    _ho = set(heading_only_ids or [])
    generated_ids = {sid for sid, content in sections.items() if (content or "").strip()} | _ho

    def _has_generated_descendant(sid: str) -> bool:
        if sid in generated_ids:
            return True
        # Strict mode: do NOT keep a parent heading just because one of its
        # descendants is selected. The user must explicitly pick the parent
        # for it to appear in the output.
        if strict:
            return False
        prefix = sid + "."
        return any(g == sid or g.startswith(prefix) for g in generated_ids)

    # Build heading_text → section_id lookup. Prefer entries in section_map
    # (the template's own wording) but also accept the canonical section
    # titles, so a user-renamed section still matches.
    headtxt_to_sid: dict[str, str] = {}
    for sid, heading in section_map.items():
        if heading:
            headtxt_to_sid.setdefault(_normalize_heading(heading), sid)
    try:
        for sid, title in (get_titles() or {}).items():
            if title:
                headtxt_to_sid.setdefault(_normalize_heading(title), sid)
    except Exception:
        pass

    # Per-id display title that will replace the heading text on the kept
    # headings, so the user sees "3.1 Supply Chain Maps" instead of just
    # "Supply Chain Maps".
    try:
        titles_map = dict(get_titles() or {})
    except Exception:
        titles_map = {}
    # Caller-supplied title_map wins for per-project customs (e.g. "7.1.3").
    if title_map:
        titles_map.update({k: v for k, v in title_map.items() if v})

    def _display_title_for(sid: str) -> str:
        title = titles_map.get(sid) or section_map.get(sid)
        # Never echo the section ID as its own title — print just the number
        # if no human title is available.
        if not title or title.strip() == sid:
            return sid
        return f"{sid} {title}"

    body = doc.element.body
    children = list(body)
    n = len(children)

    # ── Pre-scan: locate the end of the front matter zone so we don't
    #    accidentally eat the cover page / revision history / approvals /
    #    table of contents. Strict cleanup begins AFTER the body of the
    #    last front-matter heading.
    last_fm_idx = -1
    for idx, elem in enumerate(children):
        rank = _heading_rank_for_elem(elem)
        if rank > 0 and _normalize_heading(_para_text_for_elem(elem)) in _FRONT_MATTER_HEADINGS:
            last_fm_idx = idx
    fm_end_idx = 0
    if last_fm_idx >= 0:
        fm_rank = _heading_rank_for_elem(children[last_fm_idx])
        fm_end_idx = last_fm_idx + 1
        while fm_end_idx < n:
            nxt = children[fm_end_idx]
            nxt_rank = _heading_rank_for_elem(nxt)
            if nxt_rank and nxt_rank <= fm_rank:
                break
            fm_end_idx += 1

    to_remove: list = []
    rewrites: list[tuple] = []  # (heading_elem, "id title")

    from docx.oxml.ns import qn as _qn

    def _is_title_style(p_elem):
        """True if the paragraph uses the Title style (cover-page heading)."""
        if p_elem.tag != _qn("w:p"):
            return False
        pStyle = p_elem.find(_qn("w:pPr") + "/" + _qn("w:pStyle"))
        return pStyle is not None and (pStyle.get(_qn("w:val")) or "") == "Title"

    i = fm_end_idx
    while i < n:
        elem = children[i]
        rank = _heading_rank_for_elem(elem)
        if rank == 0:
            i += 1
            continue

        text = _para_text_for_elem(elem)
        norm = _normalize_heading(text)

        # 0) Title-styled paragraphs are cover-page / part-title elements.
        #    Never remove or renumber them — they belong to the template's
        #    visual identity, not to the section hierarchy.
        if _is_title_style(elem):
            i += 1
            continue

        # 1) front matter — always keep, never rewrite (in case more
        #    front-matter headings appear in the body, e.g. "Glossary")
        if norm in _FRONT_MATTER_HEADINGS:
            i += 1
            continue

        # 2) does this heading map to a section the user generated?
        sid = headtxt_to_sid.get(norm)
        if sid and _has_generated_descendant(sid):
            rewrites.append((elem, _display_title_for(sid)))
            i += 1
            continue

        # 3) we recognise it but nothing under it is generated → DROP
        #    OR we don't recognise it at all → DROP (leftover template content)
        to_remove.append(elem)
        j = i + 1
        while j < n:
            nxt = children[j]
            nxt_rank = _heading_rank_for_elem(nxt)
            if nxt_rank and nxt_rank <= rank:
                break
            to_remove.append(nxt)
            j += 1
        i = j

    # Apply removals — but PRESERVE any paragraph that carries an embedded
    # <w:sectPr>. The sectPr defines page borders, margins, headers and
    # footers for the section ending at that paragraph; deleting it
    # destroys the page layout (the "borders are missing" symptom). We
    # leave these structural paragraphs in place — they take no visible
    # space because their runs are empty after we clear them.
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    preserved_for_layout = 0
    for elem in to_remove:
        # Detect a section-break paragraph and skip its deletion.
        if elem.tag == qn("w:p"):
            pPr = elem.find(qn("w:pPr"))
            if pPr is not None and pPr.find(qn("w:sectPr")) is not None:
                # Clear any visible text inside the paragraph but keep the
                # paragraph itself + its pPr/sectPr in place. This way the
                # section layout (page borders, margins, headers) is
                # preserved even though the un-generated section's heading
                # & body content are gone.
                for r in list(elem.findall(qn("w:r"))):
                    elem.remove(r)
                # Also strip the heading style from this paragraph so it
                # doesn't appear in the Table of Contents.
                pStyle = pPr.find(qn("w:pStyle"))
                if pStyle is not None:
                    pPr.remove(pStyle)
                preserved_for_layout += 1
                continue
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)

    if preserved_for_layout:
        logger.info(
            "Preserved %d structural paragraph(s) carrying section "
            "properties (page borders / margins / headers / footers).",
            preserved_for_layout,
        )

    # Apply heading rewrites.
    for elem, new_text in rewrites:
        _rewrite_heading_text(elem, new_text)

    if to_remove or rewrites:
        logger.info(
            "Strict body cleanup: removed %d element(s), rewrote %d heading(s).",
            len(to_remove), len(rewrites),
        )


def _rewrite_heading_text(p_elem, new_text: str):
    """
    Replace the visible text of a heading paragraph with `new_text` while
    preserving its style. Also clears any `w:numPr` so Word does NOT layer
    an automatic numbering on top of the explicit number we just wrote.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    # Clear automatic numbering markup
    pPr = p_elem.find(qn("w:pPr"))
    if pPr is not None:
        numPr = pPr.find(qn("w:numPr"))
        if numPr is not None:
            pPr.remove(numPr)

    # Remove every run, then add a single fresh run with the new text.
    for r in list(p_elem.findall(qn("w:r"))):
        p_elem.remove(r)
    r = OxmlElement("w:r")
    t = OxmlElement("w:t")
    t.text = new_text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    r.append(t)
    p_elem.append(r)


def _stamp_issue_date(doc, today_str: str):
    """
    Find every paragraph that begins with an "Issue Date" / "Date Issued" /
    "Date" label (followed by ":" or "-" or a tab) and rewrite whatever
    follows that label with today's date. This catches the cover-page
    "Issue Date: 29 April 2025" line that lives in a table cell, which the
    generic regex pass sometimes misses because the date is on a different
    visual line / different run.
    """
    import re as _re

    LABEL_RE = _re.compile(
        r"^\s*(issue\s*date|date\s*issued|effective\s*date|export\s*date|"
        r"document\s*date|prepared\s*on|created\s*on|date\s*prepared|"
        r"release\s*date|date)\s*[:\-–—\t]\s*.*$",
        _re.IGNORECASE,
    )

    hits = 0
    for para in _iter_all_paragraphs(doc):
        txt = para.text
        if not txt:
            continue
        m = LABEL_RE.match(txt)
        if not m:
            continue
        label = m.group(1)
        new_text = f"{label.title()}: {today_str}"
        _replace_in_paragraph(para, {txt: new_text})
        hits += 1
    if hits:
        logger.info("Stamped %d Issue Date label(s) with today's date.", hits)


def _is_empty_placeholder_paragraph(elem) -> bool:
    """
    True if a paragraph element looks like leftover template placeholder
    body copy — i.e. it's Normal / Text 2 / List Paragraph styled AND its
    text either is empty, or contains a [bracketed] placeholder token, or
    is the literal "<insert ...>" boilerplate.
    """
    from docx.oxml.ns import qn
    if elem.tag != qn("w:p"):
        return False
    pStyle = elem.find(qn("w:pPr") + "/" + qn("w:pStyle"))
    style_val = (pStyle.get(qn("w:val")) if pStyle is not None else "") or ""
    placeholder_styles = {"Normal", "Text2", "Text 2", "ListParagraph", "List Paragraph"}
    if style_val and style_val not in placeholder_styles:
        return False
    text = _para_text_for_elem(elem)
    if not text:
        return True
    if "[" in text and "]" in text:
        return True
    low = text.lower()
    if low.startswith("<insert") or low.startswith("insert ") or "tbd" == low.strip():
        return True
    return False


def _clear_placeholder_under_generated_headings(doc, sections: dict, section_map: dict):
    """
    For every generated section's heading, remove any leftover placeholder
    paragraphs that follow it BEFORE we insert real content. This is a
    safety net for the case where `_insert_content_after` didn't strip a
    placeholder (e.g. unrecognised style) and ensures the generated text
    doesn't appear *after* a leftover "Insert customer overview here..."
    line.
    """
    generated_ids = {sid for sid, content in sections.items() if (content or "").strip()}
    keep_headings = {}
    for sid in generated_ids:
        heading = section_map.get(sid)
        if not heading:
            continue
        keep_headings.setdefault(_normalize_heading(heading), sid)
    if not keep_headings:
        return

    body = doc.element.body
    children = list(body)

    i = 0
    n = len(children)
    while i < n:
        elem = children[i]
        rank = _heading_rank_for_elem(elem)
        if rank > 0 and _normalize_heading(_para_text_for_elem(elem)) in keep_headings:
            j = i + 1
            while j < n:
                nxt = children[j]
                nxt_rank = _heading_rank_for_elem(nxt)
                if nxt_rank and nxt_rank <= rank:
                    break
                if _is_empty_placeholder_paragraph(nxt):
                    parent = nxt.getparent()
                    if parent is not None:
                        parent.remove(nxt)
                j += 1
        i += 1


def _iter_all_paragraphs(doc):
    """
    Yield every Paragraph object in the document, including those inside
    tables (recursively), headers and footers. Used for full-document
    placeholder replacement.
    """
    # Body paragraphs
    for p in doc.paragraphs:
        yield p

    def _walk_table(tbl):
        for row in tbl.rows:
            for cell in row.cells:
                for p in cell.paragraphs:
                    yield p
                for nested in cell.tables:
                    yield from _walk_table(nested)

    for tbl in doc.tables:
        yield from _walk_table(tbl)

    # Headers / footers in every section
    for section in doc.sections:
        for hf in (section.header, section.first_page_header, section.even_page_header,
                   section.footer, section.first_page_footer, section.even_page_footer):
            if hf is None:
                continue
            for p in hf.paragraphs:
                yield p
            for tbl in hf.tables:
                yield from _walk_table(tbl)


def _replace_in_paragraph(para, replacements: dict):
    """
    Find/replace inside a paragraph while preserving formatting as much as
    possible. Word commonly splits a placeholder like "[customer]" across
    multiple runs because of cursor movements / spell-check / hidden marks,
    so we join all runs, run the replacements on the joined text, and (only
    if anything actually changed) collapse the runs into a single run
    holding the replaced text. Formatting of the FIRST run is preserved.
    """
    if not para.runs:
        return False
    joined = "".join(r.text or "" for r in para.runs)
    new_text = joined
    for k, v in replacements.items():
        if k in new_text:
            new_text = new_text.replace(k, v)
    if new_text == joined:
        return False
    # Write the replaced text into the first run, blank the rest.
    para.runs[0].text = new_text
    for r in para.runs[1:]:
        r.text = ""
    return True


def neutralize_legacy_client_name(docx_path, client_name: str = "") -> int:
    """SAFETY NET — guarantee no previous-client ("Arclin") name survives in a
    finished document, no matter where it came from (hardcoded default, a
    re-imported old export, or copied source text).

    Walks the entire document (body, tables, headers, footers) and replaces any
    occurrence of the legacy client token with the resolved client name (or a
    neutral "[Client]" placeholder when none is known). Returns the number of
    paragraphs changed. Best-effort: never raises.
    """
    try:
        from pathlib import Path as _P
        from docx import Document as _D
        docx_path = _P(str(docx_path))
        if not docx_path.exists():
            return 0
        replacement = (client_name or "").strip() or "[Client]"
        variants = [
            "Arclin Manufacturing Inc.", "Arclin Manufacturing Inc",
            "Arclin Manufacturing", "ARCLIN", "Arclin", "arclin",
        ]
        replacements = {v: replacement for v in variants if v.lower() != replacement.lower()}
        if not replacements:
            return 0
        doc = _D(str(docx_path))
        changed = 0
        for para in _iter_all_paragraphs(doc):
            if _replace_in_paragraph(para, replacements):
                changed += 1
        if changed:
            doc.save(str(docx_path))
        return changed
    except Exception:
        return 0


def _stamp_template_text(doc, customer_name: str, today_str: str):
    """
    Walk the whole document and:
      • replace every [customer] / [Customer] / [CUSTOMER] / [client] /
        [Client] / [CLIENT] occurrence with the resolved client name;
      • replace [date] / [Date] / [DATE] / <date> / {date} / Insert Date /
        any literal date string (YYYY-MM-DD, DD/MM/YYYY, MM/DD/YYYY,
        DD-MM-YYYY, "Month DD, YYYY", "DD Month YYYY", "Month YYYY", etc.)
        with today's date.

    Operates on the entire document — main body, every table cell (incl.
    nested), and every header/footer — so customer/date stamps on the
    cover page (which lives in a table) are reached too.
    """
    import re as _re

    customer = customer_name or "Client"
    # Common date patterns we recognise. Anchored loosely so we never match a
    # bare digit run that happens to live inside another word.
    DATE_PATTERNS = [
        _re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),                  # 2025-12-31
        _re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),                # 31/12/2025
        _re.compile(r"\b\d{1,2}-\d{1,2}-\d{2,4}\b"),                # 31-12-2025
        _re.compile(r"\b\d{1,2}\.\d{1,2}\.\d{2,4}\b"),              # 31.12.2025
        _re.compile(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
            r"\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}\b",
            _re.IGNORECASE,
        ),                                                          # Jan 31, 2025 / Jan 31st 2025
        _re.compile(
            r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{2,4}\b",
            _re.IGNORECASE,
        ),                                                          # 31 January 2025
        _re.compile(
            r"\b(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{4}\b",
            _re.IGNORECASE,
        ),                                                          # January 2025
    ]

    placeholder_replacements = {
        "[customer]": customer,  "[Customer]": customer,  "[CUSTOMER]": customer,
        "[client]":   customer,  "[Client]":   customer,  "[CLIENT]":   customer,
        "<customer>": customer,  "<Customer>": customer,
        "{customer}": customer,  "{Customer}": customer,
        "[date]":     today_str, "[Date]":     today_str, "[DATE]":     today_str,
        "<date>":     today_str, "<Date>":     today_str,
        "{date}":     today_str, "{Date}":     today_str,
        "Insert Date": today_str, "insert date": today_str,
        "DD/MM/YYYY":  today_str, "MM/DD/YYYY":  today_str, "YYYY-MM-DD":  today_str,
    }

    placeholder_hits = 0
    date_hits = 0

    for para in _iter_all_paragraphs(doc):
        # Pass 1 — exact placeholder substitution (re-uses joined-runs trick)
        if _replace_in_paragraph(para, placeholder_replacements):
            placeholder_hits += 1

        # Pass 2 — date pattern substitution. Skip paragraphs whose text doesn't
        # look like it could contain a year, just to keep the regex cost down.
        text = para.text
        if not text or not any(ch.isdigit() for ch in text):
            continue
        new_text = text
        for pat in DATE_PATTERNS:
            new_text = pat.sub(today_str, new_text)
        if new_text != text:
            # We do an exact full-text replace inside the paragraph runs.
            _replace_in_paragraph(para, {text: new_text})
            date_hits += 1

    # Pass 3 — replace SDT content controls (Word's "click here to enter a
    # date" / "click here to enter text" controls). These live outside the
    # normal paragraph run model so the paragraph walker above doesn't
    # touch them.
    sdt_hits = _stamp_sdt_content_controls(doc, customer, today_str)

    if placeholder_hits or date_hits or sdt_hits:
        logger.info(
            "Template text stamped: %d placeholder para(s), %d date para(s), %d SDT control(s).",
            placeholder_hits, date_hits, sdt_hits,
        )


def _stamp_sdt_content_controls(doc, customer: str, today_str: str) -> int:
    """
    Replace the text inside every Word Structured Document Tag (SDT).
    SDTs are the "click here to enter a date" placeholders on cover pages
    and inside tables. Each SDT has a `<w:sdtContent>` block that holds the
    visible text; we rewrite that block based on the SDT's alias / tag
    so a date-styled SDT gets today's date and a name-styled one gets the
    customer name.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    hits = 0
    root = doc.element

    for sdt in list(root.iter(qn("w:sdt"))):
        # Inspect the SDT's metadata to decide what to put in it
        pr = sdt.find(qn("w:sdtPr"))
        alias_node = pr.find(qn("w:alias")) if pr is not None else None
        tag_node   = pr.find(qn("w:tag"))   if pr is not None else None
        alias = (alias_node.get(qn("w:val")) if alias_node is not None else "") or ""
        tag   = (tag_node.get(qn("w:val"))   if tag_node   is not None else "") or ""
        is_date = (pr.find(qn("w:date")) is not None) if pr is not None else False
        meta = f"{alias} {tag}".lower()

        if is_date or "date" in meta:
            value = today_str
        elif any(k in meta for k in ("customer", "client", "company", "author", "name")):
            value = customer
        else:
            continue  # don't touch SDTs we don't understand

        sdt_content = sdt.find(qn("w:sdtContent"))
        if sdt_content is None:
            continue

        # Find the first run inside sdtContent and rewrite its text; drop
        # the rest so the value isn't duplicated.
        runs = sdt_content.findall(".//" + qn("w:r"))
        if not runs:
            continue
        first = runs[0]
        # Remove all <w:t> children from the first run, then add a single new one.
        for t in list(first.findall(qn("w:t"))):
            first.remove(t)
        new_t = OxmlElement("w:t")
        new_t.text = value
        new_t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        first.append(new_t)
        # Strip remaining runs in this sdtContent to avoid leftover text.
        for r in runs[1:]:
            for t in list(r.findall(qn("w:t"))):
                r.remove(t)
        hits += 1

    return hits


def _rebuild_table_of_contents(doc):
    """
    Replace the body of the "Table of Contents" section with a real Word
    TOC field so that the moment Word opens the document it populates the
    TOC from the actual headings present (and only those) — i.e. the TOC
    automatically matches whatever survived `_strip_empty_template_sections`.

    Why a field and not pre-rendered paragraphs?
      • Page numbers can only be computed by a Word layout engine, not by
        python-docx.
      • A static list would drift the moment anyone edits the document.
      • The XML field `TOC \\o "1-3" \\h \\z \\u` is the same construct
        Word inserts when the user clicks References → Table of Contents.

    Combined with `_mark_fields_dirty`, Word refreshes this on first open.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    body = doc.element.body
    children = list(body)

    # Step 1 — find the TOC heading
    toc_idx = -1
    toc_rank = 0
    for idx, elem in enumerate(children):
        rank = _heading_rank_for_elem(elem)
        if rank and "table of contents" in _para_text_for_elem(elem).lower():
            toc_idx = idx
            toc_rank = rank
            break
    if toc_idx < 0:
        return  # template has no TOC heading — nothing to rebuild

    # Step 2 — remove old TOC body (and any pre-existing TOC fields/SDTs)
    end_idx = toc_idx + 1
    while end_idx < len(children):
        nxt = children[end_idx]
        nxt_rank = _heading_rank_for_elem(nxt)
        if nxt_rank and nxt_rank <= toc_rank:
            break
        end_idx += 1
    for elem in children[toc_idx + 1: end_idx]:
        parent = elem.getparent()
        if parent is not None:
            parent.remove(elem)

    # Step 3 — inject a fresh TOC field paragraph right after the heading
    XMLNS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

    def _w(tag):
        return f"{{{XMLNS_W}}}{tag}"

    p = OxmlElement("w:p")

    def _add_field_run(parent, instr_text=None, fld_char=None, body_text=None):
        r = OxmlElement("w:r")
        if fld_char is not None:
            fc = OxmlElement("w:fldChar")
            fc.set(qn("w:fldCharType"), fld_char)
            r.append(fc)
        if instr_text is not None:
            it = OxmlElement("w:instrText")
            it.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            it.text = instr_text
            r.append(it)
        if body_text is not None:
            t = OxmlElement("w:t")
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t.text = body_text
            r.append(t)
        parent.append(r)

    # \\o "1-3" — include heading levels 1..3
    # \\h       — hyperlink entries to headings
    # \\z       — hide tab leaders & page numbers in Web layout
    # \\u       — use applied paragraph outline level
    _add_field_run(p, fld_char="begin")
    _add_field_run(p, instr_text=' TOC \\o "1-3" \\h \\z \\u ')
    _add_field_run(p, fld_char="separate")
    _add_field_run(p, body_text="Right-click here and choose 'Update Field' to refresh the Table of Contents.")
    _add_field_run(p, fld_char="end")

    children[toc_idx].addnext(p)

    logger.info("Rebuilt Table of Contents (inserted a live TOC field).")


def _populate_revision_history(doc, user_name: str, user_email: str, description: str):
    """
    Find a table in the template whose first row contains "Revision" / "Version"
    / "Date" / "Author" / "Description"-style headers and fill its FIRST data
    row with the logged-in user's name, today's date and the description. Any
    leftover placeholder cells in that row are cleared.
    """
    import datetime as _dt
    today = _dt.date.today().strftime("%Y-%m-%d")
    author = user_name or user_email or "Author"

    HEADER_HINTS = {"version", "rev", "revision", "date", "author", "owner",
                    "description", "change", "notes"}

    def _cell_text(cell):
        return "".join(p.text for p in cell.paragraphs).strip().lower()

    def _set_cell(cell, value: str):
        # Clear all paragraphs except the first; rewrite the first.
        for p in cell.paragraphs[1:]:
            p._element.getparent().remove(p._element)
        first = cell.paragraphs[0]
        for r in list(first.runs):
            r._element.getparent().remove(r._element)
        first.add_run(value)

    for table in doc.tables:
        if not table.rows or len(table.rows[0].cells) < 2:
            continue
        headers = [_cell_text(c) for c in table.rows[0].cells]
        if not any(any(hint in h for hint in HEADER_HINTS) for h in headers):
            continue
        if len(table.rows) < 2:
            # Add a fresh row if the template only has the header
            new_row = table.add_row()
            data_cells = new_row.cells
        else:
            data_cells = table.rows[1].cells

        for hdr, cell in zip(headers, data_cells):
            value = ""
            if "version" in hdr or hdr.startswith("rev"):
                value = "1.0"
            elif "date" in hdr:
                value = today
            elif "author" in hdr or "owner" in hdr or "by" in hdr or "name" in hdr:
                value = author
            elif "description" in hdr or "change" in hdr or "notes" in hdr or "summary" in hdr:
                value = description
            else:
                continue  # leave unknown columns alone
            _set_cell(cell, value)

        logger.info("Revision History updated: %s | %s | %s", today, author, description)
        return  # only the first matching table — don't touch others

    logger.info("Revision History table not found in template — nothing to update.")


def _mark_fields_dirty(doc):
    """
    Set <w:updateFields w:val="true"/> in settings.xml so Word refreshes the
    Table of Contents (and any other fields) the first time the document is
    opened. Without this the TOC would still reflect the un-stripped template.
    """
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    settings = doc.settings.element
    existing = settings.find(qn("w:updateFields"))
    if existing is None:
        node = OxmlElement("w:updateFields")
        node.set(qn("w:val"), "true")
        settings.append(node)
    else:
        existing.set(qn("w:val"), "true")
