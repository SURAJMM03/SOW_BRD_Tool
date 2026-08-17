"""
chat_export.py — render Optimization Assistant Q&A transcripts to DOCX / PDF / TXT.

Used by POST /api/chat/export (chat_routes.py). Supports a single Q&A or a
full conversation. The DOCX renderer understands the same Markdown subset the
chat UI renders: headings, bullet/numbered lists, tables, code fences,
**bold** and `inline code`. PDF is produced by converting the DOCX with
LibreOffice (soffice — already used elsewhere in this app); if soffice is not
available, a plain-text PDF is generated with PyMuPDF as a fallback.
"""

from __future__ import annotations

import re
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List

logger = logging.getLogger("BRDConvoAPI.chat_export")

EXPORT_DIR = Path(__file__).parent / "static" / "exports"


def _new_path(fmt: str) -> Path:
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return EXPORT_DIR / f"chat_export_{stamp}.{fmt}"


# ── Markdown helpers ──────────────────────────────────────────────────────────
_TBL_SEP = re.compile(r"^\|[\s\-:|]+\|$")
_HEAD = re.compile(r"^(#{1,4})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.*)$")
_INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def _md_plain(text: str) -> str:
    """Strip Markdown decorations for TXT / fallback-PDF output."""
    s = text or ""
    s = re.sub(r"```[a-zA-Z]*\n?", "", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"^#{1,4}\s+", "", s, flags=re.M)
    return s.strip()


def _add_runs(paragraph, text: str) -> None:
    """Add runs to a python-docx paragraph, honouring **bold** and `code`."""
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Consolas"
        else:
            paragraph.add_run(part)


def _row_cells(line: str) -> List[str]:
    t = line.strip()
    if t.startswith("|"):
        t = t[1:]
    if t.endswith("|"):
        t = t[:-1]
    return [c.strip() for c in t.split("|")]


def _render_answer_docx(doc, answer: str) -> None:
    """Render one Markdown answer into the docx document."""
    fences = (answer or "").split("```")
    for fi, seg in enumerate(fences):
        if fi % 2 == 1:  # code fence body
            body = re.sub(r"^[a-zA-Z]*\n", "", seg)
            p = doc.add_paragraph()
            run = p.add_run(body.rstrip("\n"))
            run.font.name = "Consolas"
            continue
        lines = seg.replace("\r\n", "\n").split("\n")
        i = 0
        while i < len(lines):
            line = lines[i]
            # table
            if "|" in line and i + 1 < len(lines) and _TBL_SEP.match(lines[i + 1].strip()):
                header = _row_cells(line)
                i += 2
                rows = []
                while i < len(lines) and "|" in lines[i] and lines[i].strip():
                    rows.append(_row_cells(lines[i]))
                    i += 1
                table = doc.add_table(rows=1, cols=len(header))
                try:
                    table.style = "Table Grid"
                except Exception:
                    pass
                for ci, cell_text in enumerate(header):
                    cell_p = table.rows[0].cells[ci].paragraphs[0]
                    _add_runs(cell_p, cell_text)
                    for run in cell_p.runs:
                        run.bold = True
                for row in rows:
                    cells = table.add_row().cells
                    for ci in range(len(header)):
                        _add_runs(cells[ci].paragraphs[0], row[ci] if ci < len(row) else "")
                continue
            m = _HEAD.match(line)
            if m:
                level = min(len(m.group(1)) + 1, 4)  # answer headings sit under Q headings
                doc.add_heading(re.sub(r"\*\*", "", m.group(2)), level=level)
                i += 1
                continue
            m = _BULLET.match(line)
            if m:
                p = doc.add_paragraph(style="List Bullet")
                _add_runs(p, m.group(1))
                i += 1
                continue
            m = _NUMBERED.match(line)
            if m:
                p = doc.add_paragraph(style="List Number")
                _add_runs(p, m.group(1))
                i += 1
                continue
            if line.strip():
                p = doc.add_paragraph()
                _add_runs(p, line)
            i += 1


# ── Renderers ────────────────────────────────────────────────────────────────
def render_txt(items: List[Dict], title: str) -> Path:
    out = _new_path("txt")
    parts = [title, "=" * len(title),
             f"Exported: {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    for n, it in enumerate(items, 1):
        parts.append(f"[{n}] Q: {(it.get('question') or '').strip()}")
        parts.append("")
        parts.append(_md_plain(it.get("answer") or ""))
        parts.append("")
        parts.append("-" * 60)
        parts.append("")
    out.write_text("\n".join(parts), encoding="utf-8")
    return out


def render_docx(items: List[Dict], title: str) -> Path:
    from docx import Document

    out = _new_path("docx")
    doc = Document()
    doc.add_heading(title, level=0)
    meta = doc.add_paragraph()
    meta.add_run(
        f"Exported from the Optimization Assistant on "
        f"{datetime.now().strftime('%Y-%m-%d %H:%M')} — {len(items)} Q&A item(s)."
    ).italic = True
    for it in items:
        q = (it.get("question") or "").strip()
        doc.add_heading(f"Q: {q}" if q else "Answer", level=1)
        _render_answer_docx(doc, it.get("answer") or "")
    doc.save(str(out))
    return out


def _docx_to_pdf(docx_path: Path) -> Path:
    """Convert DOCX → PDF with LibreOffice; the resulting file shares the stem."""
    subprocess.run(
        ["soffice", "--headless", "--convert-to", "pdf",
         "--outdir", str(EXPORT_DIR), str(docx_path)],
        check=True, timeout=120,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    pdf = docx_path.with_suffix(".pdf")
    if not pdf.exists():
        raise RuntimeError("LibreOffice reported success but no PDF was produced")
    return pdf


def _fallback_pdf(items: List[Dict], title: str) -> Path:
    """Plain-text PDF via PyMuPDF when LibreOffice is unavailable."""
    import fitz

    out = _new_path("pdf")
    text = f"{title}\nExported {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
    for n, it in enumerate(items, 1):
        text += f"[{n}] Q: {(it.get('question') or '').strip()}\n\n"
        text += _md_plain(it.get("answer") or "") + "\n\n" + ("-" * 60) + "\n\n"
    doc = fitz.open()
    rect = fitz.Rect(50, 50, 545, 792)
    remaining = text
    while remaining:
        page = doc.new_page()
        spill = page.insert_textbox(rect, remaining, fontsize=10, fontname="helv")
        # insert_textbox returns unused height ≥0 when everything fit
        if spill >= 0:
            break
        # crude split: keep roughly what fits per page and continue
        cut = int(len(remaining) * 0.75) or len(remaining)
        remaining = remaining[cut:]
    doc.save(str(out))
    doc.close()
    return out


def render_pdf(items: List[Dict], title: str) -> Path:
    docx_path = render_docx(items, title)
    try:
        return _docx_to_pdf(docx_path)
    except Exception as exc:
        logger.warning("soffice PDF conversion failed (%s) — using PyMuPDF fallback.", exc)
        return _fallback_pdf(items, title)
    finally:
        try:
            docx_path.unlink()  # intermediate file; the caller asked for PDF
        except OSError:
            pass


def render_export(items: List[Dict], fmt: str, title: str = None) -> Path:
    title = (title or "Optimization Assistant — Chat Export").strip()
    fmt = (fmt or "docx").lower()
    if fmt == "txt":
        return render_txt(items, title)
    if fmt == "pdf":
        return render_pdf(items, title)
    return render_docx(items, title)
