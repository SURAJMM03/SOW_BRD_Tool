"""
chunking_pipeline.py — Document chunking and indexing for Blueprint Document projects

Merges the LLM-based keyword extraction approach from chunk_extracter.py
with rich metadata, project scoping, and concurrency safety.

Pipeline:
  File on disk → text extraction (local) → chunking (local) →
  keyword extraction (LLM via GPT-4o-mini, or local fallback) →
  save to project-scoped chunks.json

Each chunk carries full metadata for Blueprint Document reference tracing:
  - project_id, file_id, doc_name
  - page_number (PDF), section_heading (DOCX)
  - char_start, char_end (offsets into extracted text)
  - chunk_text (PRESERVED — the original extractor discarded this)
  - keywords (LLM-extracted, high quality)

Fixes over original chunk_extracter.py:
  1. Chunk text is preserved (was discarded — critical flaw)
  2. Rich metadata for traceability (page, heading, offsets)
  3. Project-scoped storage, not hardcoded paths
  4. Concurrent-safe with per-project locking
  5. Larger chunk size (2000 chars / ~300 words) with overlap
  6. Local keyword fallback when OPENAI_API_KEY is not set
  7. XLSX support carried over from original
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("ChunkingPipeline")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONCURRENCY — per-project locks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_project_locks: dict[str, threading.Lock] = {}
_locks_lock = threading.Lock()

def _get_project_lock(project_id: str) -> threading.Lock:
    with _locks_lock:
        if project_id not in _project_locks:
            _project_locks[project_id] = threading.Lock()
        return _project_locks[project_id]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CONFIG
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CHUNK_SIZE_CHARS = 2000      # ~300 words per chunk (original used 500 — too small)
CHUNK_OVERLAP_CHARS = 200    # overlap to preserve context at boundaries
KEYWORDS_PER_CHUNK = 12       # same as original chunk_extracter.py


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BRD SECTION CLASSIFICATION
# Maps document headings → BRD top-level section IDs ("1"–"6")
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_SECTION_KEYWORDS: dict[str, list[str]] = {
    "1": [
        "introduction", "purpose", "scope", "company information",
        "definitions", "acronyms", "background", "overview",
    ],
    "2": [
        "benefit realization", "benefit realisation", "business issues",
        "value drivers", "quantitative benefits", "qualitative benefits",
        "kpi", "key performance", "roi", "return on investment",
    ],
    "3": [
        "supply chain scope", "supply chain map", "sites",
        "demand foundation", "supply foundation",
        "inventory management foundation", "scenario structure",
        "planning constraints", "supply chain design",
    ],
    "4": [
        "demand planning", "demand process", "demand management",
        "statistical forecast", "forecast consumption", "net off",
        "demand review", "unconstrained forecast", "consensus demand",
        "demand test", "demand test cases",
    ],
    "5": [
        "supply planning", "inventory planning", "supply & inventory",
        "supply and inventory", "capacity constraints",
        "supply exceptions", "inventory exceptions",
        "supply review", "replenishment", "supply test", "supply test cases",
    ],
    "6": [
        "data integration", "data integrations", "architecture",
        "data sources", "data files", "data frequency",
        "interface", "inbound", "outbound", "etl", "middleware",
    ],
}


def classify_chunk_section(section_heading: str, chunk_text: str) -> str:
    """
    Return the BRD section ID ("1"–"6") that best matches a chunk,
    based on keyword matching in the heading and opening text.
    Returns "" when no section can be determined.
    """
    haystack = f"{section_heading} {chunk_text[:400]}".lower()
    best_sid = ""
    best_count = 0
    for sid, keywords in _SECTION_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in haystack)
        if count > best_count:
            best_count = count
            best_sid = sid
    return best_sid if best_count >= 1 else ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHUNK SCHEMA
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
def make_chunk(
    chunk_id: int,
    project_id: str,
    file_id: str,
    doc_name: str,
    chunk_text: str,
    page_number: Optional[int] = None,
    section_heading: Optional[str] = None,
    char_start: int = 0,
    char_end: int = 0,
    keywords: list[str] = None,
    brd_section_id: str = "",
) -> dict:
    return {
        "chunk_id": chunk_id,
        "project_id": project_id,
        "file_id": file_id,
        "doc_name": doc_name,
        "page_number": page_number,
        "section_heading": section_heading or "",
        "char_start": char_start,
        "char_end": char_end,
        "chunk_text": chunk_text,        # PRESERVED — original discarded this
        "keywords": keywords or [],
        "brd_section_id": brd_section_id,
        "created_at": datetime.utcnow().isoformat(),
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# TEXT EXTRACTION (local, no API)
# Same file types as chunk_extracter.py + TXT support
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def extract_text_from_file(file_path: Path) -> list[dict]:
    """
    Extract text from a file, returning a list of page/section blocks.
    Each block: { "text": str, "page": int|None, "heading": str|None }
    """
    ext = file_path.suffix.lower()
    if ext == ".txt":
        return _extract_txt(file_path)
    elif ext in (".docx", ".doc"):
        return _extract_docx(file_path)
    elif ext == ".pdf":
        return _extract_pdf(file_path)
    elif ext in (".xlsx", ".xls", ".xlsm"):
        return _extract_xlsx(file_path)
    elif ext in (".pptx", ".ppt"):
        return _extract_pptx(file_path)
    elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        return _extract_image_file(file_path)
    else:
        logger.warning("Unsupported file type: %s", ext)
        return []


def _extract_image_file(path: Path) -> list[dict]:
    """Treat a directly-uploaded image as a single searchable text block.

    We hand the image to GPT-4o vision and store its description as the
    block text so doc-search hits the picture by content. When vision is
    unavailable we still emit a minimal block (just the filename) so the
    file ends up as a chunk and the upload doesn't appear to silently fail.
    """
    try:
        raw = path.read_bytes()
    except Exception as e:
        logger.error("Could not read image %s: %s", path.name, e)
        return []
    desc = ""
    try:
        desc = _vision_describe(raw, context=f"Uploaded image: {path.name}") or ""
    except Exception as e:
        logger.warning("Vision describe failed for %s: %s", path.name, e)
    body = f"[Image: {path.name}]"
    if desc:
        body += "\n" + desc
    return [{
        "text":    body,
        "page":    None,
        "heading": path.stem,
    }]


# ── GPT-4o Vision helper ─────────────────────────────────────────────────────

# ── Cached OpenAI client + per-file vision budget ────────────────────────────
# Building a new OpenAI client (with its httpx.Client SSL handshake) for every
# vision call was the dominant cost in large-PDF chunking. Cache one client
# per process and reuse it across calls.
_VISION_CLIENT = None
_VISION_STRIKES = 0
_VISION_DISABLED = False
_VISION_STRIKE_LIMIT = 5

def _get_vision_client():
    global _VISION_CLIENT
    if _VISION_CLIENT is not None:
        return _VISION_CLIENT
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None
    try:
        import httpx as _httpx
        from openai import OpenAI as _OAI
        ssl_verify = os.getenv("OPENAI_SSL_VERIFY", "false").lower() not in ("false", "0", "no")
        _VISION_CLIENT = _OAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            http_client=_httpx.Client(verify=ssl_verify, timeout=30.0),
            max_retries=1,
            timeout=30.0,
        )
        return _VISION_CLIENT
    except Exception as e:
        logger.warning("Could not init vision client: %s", e)
        return None


def _vision_describe(image_bytes: bytes, context: str = "") -> str:
    """
    Describe an image using GPT-4o Vision.
    Returns "" if vision unavailable, API key missing, or image is blank/tiny.
    context: hint string (e.g. "PDF: Kraton deck, Page 4, Section: Sites")
    """
    global _VISION_STRIKES, _VISION_DISABLED
    if _VISION_DISABLED:
        return ""
    # Byte size alone can't tell a small-but-real diagram from a tiny logo —
    # a simple line-art flowchart can compress to a few KB. Only skip on byte
    # size if we can't also check pixel dimensions; a decent pixel footprint
    # overrides a low byte count.
    if len(image_bytes) < 3000:
        return ""
    if len(image_bytes) < 8000:
        try:
            from PIL import Image
            import io as _io
            with Image.open(_io.BytesIO(image_bytes)) as im:
                w, h = im.size
            if w < 150 and h < 150:
                return ""
        except Exception:
            return ""   # can't probe dimensions — fall back to the old byte-only skip
    client = _get_vision_client()
    if client is None:
        return ""
    try:
        import base64 as _b64
        b64 = _b64.b64encode(image_bytes).decode()
        resp = client.chat.completions.create(
            model=os.getenv("VISION_MODEL", "gpt-4.1"),
            max_tokens=600,
            temperature=0.1,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": (
                        "You are extracting content from a supply chain / ERP Blueprint Document.\n"
                        f"Context: {context}\n\n"
                        "Describe this image in detail. Focus on:\n"
                        "- All visible text, labels, titles, numbers\n"
                        "- Tables, charts, diagrams and their data\n"
                        "- Supply chain maps, site names, system names\n"
                        "- Process flows, architecture diagrams, KPIs, metrics\n"
                        "- Anything useful for a supply chain consultant\n\n"
                        "If this is a flowchart, process flow, or SmartArt-style diagram, "
                        "transcribe each step/box's label in order and describe the "
                        "connections between them explicitly (e.g. 'Step A → Step B').\n\n"
                        "Return a structured plain-text description.\n"
                        "If the image is blank, decorative, or a logo with no useful info, "
                        "reply with exactly: SKIP"
                    )},
                    {"type": "image_url", "image_url": {
                        "url": f"data:image/png;base64,{b64}",
                        "detail": "high"
                    }},
                ]
            }],
        )
        result = resp.choices[0].message.content.strip()
        _VISION_STRIKES = 0
        return "" if result.upper() == "SKIP" else result
    except Exception as e:
        logger.warning("Vision describe failed for '%s': %s", context, e)
        _VISION_STRIKES += 1
        if _VISION_STRIKES >= _VISION_STRIKE_LIMIT:
            _VISION_DISABLED = True
            logger.error(
                "Vision circuit breaker tripped after %d failures — disabling "
                "vision for the rest of this run", _VISION_STRIKES,
            )
        return ""


def _extract_txt(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return [{"text": text, "page": None, "heading": None}]


def _extract_docx(path: Path) -> list[dict]:
    """
    Extract text AND embedded images from DOCX.
    Images are described via GPT-4o Vision (when OPENAI_API_KEY is set).
    Each embedded image becomes its own chunk with heading "Image: <filename>".
    """
    try:
        from docx import Document
    except ImportError:
        logger.warning("python-docx not installed — reading as plain text")
        text = path.read_text(encoding="utf-8", errors="replace")
        return [{"text": text, "page": None, "heading": None}]

    doc = Document(str(path))

    # Walk the document body in READING ORDER so paragraphs and tables stay
    # interleaved correctly. python-docx keeps tables out of `doc.paragraphs`,
    # so iterating paragraphs alone silently drops ALL table data (e.g. the
    # Planning Hierarchy Attributes / Supplier Attributes tables). We iterate
    # the underlying body XML and dispatch <w:p> → Paragraph, <w:tbl> → Table.
    from docx.text.paragraph import Paragraph
    from docx.table import Table

    def _table_to_markdown(tbl) -> str:
        rows = []
        for r in tbl.rows:
            cells = [(" ".join(c.text.split())).strip() for c in r.cells]
            rows.append(cells)
        if not rows:
            return ""
        # Collapse runs of identical cells produced by merged cells.
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        out = []
        header = rows[0]
        out.append("| " + " | ".join(header) + " |")
        out.append("| " + " | ".join(["---"] * width) + " |")
        for r in rows[1:]:
            out.append("| " + " | ".join(r) + " |")
        return "\n".join(out)

    blocks = []
    current_heading = None
    current_text_parts = []

    def _flush():
        if current_text_parts:
            blocks.append({
                "text": "\n".join(current_text_parts),
                "page": None,
                "heading": current_heading,
            })

    body = doc.element.body
    for child in body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            para = Paragraph(child, doc)
            style = para.style.name if para.style else ""
            if style and style.startswith("Heading"):
                _flush()
                current_text_parts = []
                current_heading = para.text.strip()
            else:
                t = para.text.strip()
                if t:
                    current_text_parts.append(t)
        elif tag == "tbl":
            tbl = Table(child, doc)
            md = _table_to_markdown(tbl)
            if md.strip():
                # Emit the table as its own block under the current heading so
                # the chunker keeps the whole table together and the agent can
                # reproduce every row.
                blocks.append({
                    "text": (("\n".join(current_text_parts) + "\n\n") if current_text_parts else "")
                             + md,
                    "page": None,
                    "heading": current_heading,
                })
                current_text_parts = []

    _flush()

    if not blocks:
        full_text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
        blocks = [{"text": full_text, "page": None, "heading": None}]

    # ── Text boxes / shape-drawn flowcharts ─────────────────────────────────
    # Word draws flowcharts as floating shapes (rectangles/arrows), each an
    # anchored <w:drawing> whose text lives in a nested <w:txbxContent> —
    # invisible to the body walk above, which only dispatches direct <w:p>/
    # <w:tbl> children of the body. <w:txbxContent> still holds ordinary
    # <w:p> paragraphs underneath, so python-docx's own Paragraph wrapper
    # can read them once located; only the connector/arrow relationships
    # between boxes are lost, not the text itself.
    try:
        textbox_count = 0
        for el in doc.element.body.iter():
            if el.tag.split("}")[-1] != "txbxContent":
                continue
            box_lines = []
            for p_el in el:
                if p_el.tag.split("}")[-1] != "p":
                    continue
                t = Paragraph(p_el, doc).text.strip()
                if t:
                    box_lines.append(t)
            box_text = "\n".join(box_lines).strip()
            if box_text:
                blocks.append({
                    "text":    box_text,
                    "page":    None,
                    "heading": "Text Box",
                })
                textbox_count += 1
        if textbox_count:
            logger.info("DOCX %s: extracted %d text box(es)", path.name, textbox_count)
    except Exception as e:
        logger.debug("DOCX text box extraction failed for %s: %s", path.name, e)

    # ── Extract and describe embedded images ──────────────────────────────────
    try:
        import zipfile
        with zipfile.ZipFile(str(path), "r") as zf:
            img_files = [
                n for n in zf.namelist()
                if n.startswith("word/media/") and
                any(n.lower().endswith(ext) for ext in
                    (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tiff", ".emf", ".wmf"))
            ]
            img_count = 0
            for img_name in img_files:
                img_bytes = zf.read(img_name)
                fname = img_name.split("/")[-1]
                ctx = f"DOCX: {path.name} — embedded image {fname}"
                description = _vision_describe(img_bytes, ctx)
                if description:
                    blocks.append({
                        "text":    f"[Embedded Image: {fname}]\n{description}",
                        "page":    None,
                        "heading": f"Image: {fname}",
                    })
                    img_count += 1
        if img_count:
            logger.info("DOCX %s: described %d image(s) via Vision", path.name, img_count)
    except Exception as e:
        logger.debug("DOCX image extraction failed for %s: %s", path.name, e)

    return blocks


def _extract_pdf(path: Path) -> list[dict]:
    """
    Extract text AND images from PDF.

    For every page:
    - If page has text: extract it. Also describe any large embedded images.
    - If page has little/no text (image-heavy scan/diagram): render the whole
      page as a PNG and describe it with GPT-4o Vision.

    This captures supply chain maps, architecture diagrams, screenshots,
    and scanned pages that contain zero extractable text.
    """
    try:
        import fitz
    except ImportError:
        logger.warning("PyMuPDF not installed — run: pip install pymupdf")
        try:
            from pdfminer.high_level import extract_text
            return [{"text": extract_text(str(path)), "page": None, "heading": None}]
        except ImportError:
            logger.warning("No PDF library available")
            return []

    try:
        doc = fitz.open(str(path))
    except Exception as e:
        logger.error("fitz could not open %s: %s", path.name, e)
        return []

    blocks = []
    use_vision = bool(os.getenv("OPENAI_API_KEY", "").strip())

    # Per-file vision budget — large PDFs would otherwise serialise dozens of
    # vision calls and stall chunking for minutes. Configurable via env:
    #   CHUNK_VISION_MAX             total vision calls per PDF (default 40 —
    #                                 raised from 20; a diagram-heavy deck
    #                                 converted to PDF was silently losing
    #                                 every page past the old cap)
    #   CHUNK_VISION_INLINE_IMAGES   describe embedded images on text-heavy
    #                                pages too (default true — a diagram next
    #                                to body text was skipped entirely before;
    #                                CHUNK_VISION_INLINE_MAX_PER_PAGE bounds
    #                                the cost of turning this on)
    try:
        vision_max = int(os.getenv("CHUNK_VISION_MAX", "40"))
    except ValueError:
        vision_max = 40
    inline_imgs = os.getenv("CHUNK_VISION_INLINE_IMAGES", "true").lower() in ("1", "true", "yes")
    try:
        inline_imgs_per_page = int(os.getenv("CHUNK_VISION_INLINE_MAX_PER_PAGE", "3"))
    except ValueError:
        inline_imgs_per_page = 3

    # First pass: collect every page's text + heading + a request for any
    # vision work needed. Vision calls are then issued in parallel below.
    page_records = []        # one entry per page: (page_num, text, heading, ctx, kind, vision_payload)
    for page_num in range(len(doc)):
        page = doc.load_page(page_num)
        text = page.get_text("text").strip()

        heading = None
        try:
            for blk in page.get_text("dict").get("blocks", []):
                for line in blk.get("lines", []):
                    for span in line.get("spans", []):
                        if (span.get("flags", 0) & 16 or span.get("size", 0) > 14):
                            t = span.get("text", "").strip()
                            if len(t) > 3:
                                heading = t[:120]
                                break
                    if heading:
                        break
                if heading:
                    break
        except Exception:
            pass

        ctx = f"PDF: {path.name}, Page {page_num + 1}" + (f" — {heading}" if heading else "")

        rec = {"page_num": page_num + 1, "text": text, "heading": heading, "ctx": ctx,
               "page_img": None, "embed_imgs": []}

        # Image-only page → render page → 1 vision call
        if len(text) < 80 and use_vision:
            try:
                mat = fitz.Matrix(2.0, 2.0)
                pix = page.get_pixmap(matrix=mat, alpha=False)
                rec["page_img"] = pix.tobytes("png")
            except Exception as e:
                logger.debug("Page render failed p%d: %s", page_num + 1, e)
        elif text and use_vision and inline_imgs:
            # Text-heavy pages: only do per-image vision when opt-in. This is
            # the path that historically multiplied vision-call count by 5-10×.
            try:
                for img_info in page.get_images(full=True):
                    if len(rec["embed_imgs"]) >= inline_imgs_per_page:
                        break
                    xref = img_info[0]
                    base_img = doc.extract_image(xref)
                    img_bytes = base_img.get("image", b"")
                    w, h = base_img.get("width", 0), base_img.get("height", 0)
                    if w > 120 and h > 120 and len(img_bytes) > 10000:
                        rec["embed_imgs"].append(img_bytes)
            except Exception as e:
                logger.debug("Image extraction failed p%d: %s", page_num + 1, e)

        page_records.append(rec)

    # Build the flat list of vision jobs, bounded by vision_max.
    vision_jobs = []   # list of (job_key, image_bytes, ctx)
    for rec in page_records:
        if rec["page_img"] is not None:
            vision_jobs.append((("page", rec["page_num"]), rec["page_img"], rec["ctx"]))
        for idx, ib in enumerate(rec["embed_imgs"]):
            vision_jobs.append((("img", rec["page_num"], idx), ib, f"{rec['ctx']}, embedded image"))

    if len(vision_jobs) > vision_max:
        logger.info(
            "PDF %s: capping vision calls %d → %d (CHUNK_VISION_MAX). "
            "Increase the env var if you want full coverage.",
            path.name, len(vision_jobs), vision_max,
        )
        vision_jobs = vision_jobs[:vision_max]

    # Issue vision calls in parallel — each call is ~5-10s on a slow link, so
    # 8-way parallelism turns a 2-minute sequential pass into ~20s.
    vision_results: dict = {}
    if vision_jobs:
        from concurrent.futures import ThreadPoolExecutor
        max_workers = min(8, len(vision_jobs))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for key, desc in zip(
                [j[0] for j in vision_jobs],
                pool.map(lambda j: _vision_describe(j[1], j[2]), vision_jobs),
            ):
                if desc:
                    vision_results[key] = desc

    # Stitch vision results back into blocks, in original page order.
    try:
        for rec in page_records:
            page_num = rec["page_num"]
            text = rec["text"]
            heading = rec["heading"]

            if rec["page_img"] is not None:
                desc = vision_results.get(("page", page_num))
                if desc:
                    blocks.append({
                        "text":    desc,
                        "page":    page_num,
                        "heading": heading or f"Page {page_num} (image)",
                    })
                    continue

            if text:
                image_descs = [
                    vision_results[("img", page_num, i)]
                    for i in range(len(rec["embed_imgs"]))
                    if ("img", page_num, i) in vision_results
                ]
                combined = text
                if image_descs:
                    combined += "\n\n[Page Images]:\n" + "\n\n---\n\n".join(image_descs)

                blocks.append({
                    "text":    combined,
                    "page":    page_num,
                    "heading": heading,
                })

    finally:
        # Release the file handle even if iteration raised — otherwise Windows
        # keeps the PDF locked and subsequent DELETEs hit WinError 32.
        try:
            doc.close()
        except Exception:
            pass
    img_blocks = sum(1 for b in blocks if "Page Images" in b.get("text","") or "(image)" in (b.get("heading") or ""))
    logger.info("PDF %s: %d blocks (%d with image content)", path.name, len(blocks), img_blocks)
    return blocks


def _cell_to_str(val) -> str:
    """Convert any cell value to a clean string."""
    if val is None:
        return ""
    from datetime import datetime, date
    if isinstance(val, (datetime, date)):
        return val.strftime("%Y-%m-%d")
    s = str(val).strip()
    # Remove trailing ".0" from integers stored as floats (e.g. openpyxl returns 42.0)
    if s.endswith(".0") and s[:-2].lstrip("-").isdigit():
        s = s[:-2]
    return s


def _sheet_to_block(sheet_name: str, rows: list[list],
                    doc_name: str = "") -> dict | None:
    """
    Convert a list of rows (each a list of cell strings) into a text block.
    Uses the first non-empty row as column headers for richer context.
    Includes doc_name in the heading so chunk scorers can match by filename.
    Returns None if the sheet has no meaningful content.
    """
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return None

    # Build a rich heading: doc_name + sheet_name for easier retrieval
    # e.g. "Overview_sites.xlsx — Sheet: Site"
    if doc_name:
        heading = f"{doc_name} — Sheet: {sheet_name}"
    else:
        heading = f"Sheet: {sheet_name}"

    lines = [heading]
    headers = rows[0]
    has_headers = any(h for h in headers)

    if has_headers:
        lines.append("Columns: " + " | ".join(h for h in headers if h))

    data_rows = rows[1:] if has_headers else rows
    for row in data_rows:
        if has_headers:
            pairs = [f"{h}: {v}" for h, v in zip(headers, row) if h and v]
            row_text = ", ".join(pairs) if pairs else " | ".join(c for c in row if c)
        else:
            row_text = " | ".join(c for c in row if c)
        if row_text:
            lines.append(row_text)

    text = "\n".join(lines)
    if text.strip() == heading:
        return None
    return {"text": text, "page": None, "heading": heading}


def _xlsx_resolve_ref(wb, ref: str) -> list:
    """Resolve a chart data-source formula like "'Sheet1'!$B$2:$B$4" to the
    actual cell values (workbook is opened data_only=True, so these are
    resolved values, not formulas). Returns [] on any parse failure."""
    from openpyxl.utils.cell import range_boundaries
    if not ref or "!" not in ref:
        return []
    sheet_part, range_part = ref.rsplit("!", 1)
    sheet_name = sheet_part.strip("'")
    ws = wb[sheet_name]
    min_col, min_row, max_col, max_row = range_boundaries(range_part)
    values = []
    for row in ws.iter_rows(min_row=min_row, max_row=max_row,
                             min_col=min_col, max_col=max_col, values_only=True):
        values.extend(row)
    return values


def _xlsx_chart_to_markdown(wb, chart) -> str:
    """Native chart series data is exact — read straight from the chart's
    data-source references rather than describing the rendered chart as an
    image, mirroring the PPTX native-chart handling above."""
    title = ""
    try:
        if chart.title and chart.title.tx and chart.title.tx.rich:
            title = "".join(
                r.t or "" for p in chart.title.tx.rich.p for r in (p.r or [])
            ).strip()
    except Exception:
        pass

    categories = []
    series_cols = []  # list of (name, values)
    for s in chart.series:
        try:
            name = ""
            if s.tx:
                if s.tx.strRef and s.tx.strRef.f:
                    vals = _xlsx_resolve_ref(wb, s.tx.strRef.f)
                    name = str(vals[0]) if vals and vals[0] is not None else ""
                elif s.tx.v:
                    name = str(s.tx.v)
            if not categories and s.cat:
                cat_ref = (s.cat.strRef.f if s.cat.strRef else
                           s.cat.numRef.f if s.cat.numRef else None)
                if cat_ref:
                    categories = [_cell_to_str(v) for v in _xlsx_resolve_ref(wb, cat_ref)]
            values = []
            if s.val and s.val.numRef and s.val.numRef.f:
                values = [_cell_to_str(v) for v in _xlsx_resolve_ref(wb, s.val.numRef.f)]
            series_cols.append((name or f"Series {len(series_cols)+1}", values))
        except Exception as e:
            logger.debug("XLSX chart series skipped: %s", e)

    if not categories and not any(v for _, v in series_cols):
        return ""

    lines = []
    if title:
        lines.append(f"**{title}**")
    header = ["Category"] + [n for n, _ in series_cols]
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * len(header)) + " |")
    n_rows = max([len(categories)] + [len(v) for _, v in series_cols] or [0])
    for i in range(n_rows):
        row = [categories[i] if i < len(categories) else ""]
        for _, vals in series_cols:
            row.append(vals[i] if i < len(vals) else "")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _extract_xlsx(path: Path) -> list[dict]:
    """
    Extract text from Excel files — supports both .xlsx (openpyxl) and .xls (xlrd).
    Each sheet becomes a separate block with column-header context; embedded
    charts and images each become their own block too.
    """
    ext = path.suffix.lower()

    if ext == ".xls":
        return _extract_xls(path)

    # .xlsx — use openpyxl
    try:
        import openpyxl
    except ImportError:
        logger.warning("openpyxl not installed — run: pip install openpyxl")
        return []

    try:
        wb = openpyxl.load_workbook(str(path), data_only=True)
    except Exception as e:
        logger.error("openpyxl could not open %s: %s", path.name, e)
        return []

    blocks = []
    for sheet_name in wb.sheetnames:
        try:
            ws = wb[sheet_name]
            rows = [[_cell_to_str(c) for c in row] for row in ws.iter_rows(values_only=True)]
            block = _sheet_to_block(sheet_name, rows, doc_name=path.name)
            if block:
                blocks.append(block)
        except Exception as e:
            logger.warning("Error reading sheet '%s' in %s: %s", sheet_name, path.name, e)
            continue

        # Embedded charts — same reasoning as the PPTX native-chart handling:
        # exact series data beats a vision paraphrase of the rendered chart.
        for chart in getattr(ws, "_charts", []):
            try:
                md = _xlsx_chart_to_markdown(wb, chart)
            except Exception as e:
                logger.debug("XLSX chart failed sheet '%s' in %s: %s", sheet_name, path.name, e)
                md = ""
            if md.strip():
                blocks.append({
                    "text":    md,
                    "page":    None,
                    "heading": f"{path.name} — Sheet: {sheet_name} — Chart",
                })

        # Embedded images — previously entirely unhandled for XLSX.
        for img in getattr(ws, "_images", []):
            try:
                img_bytes = img._data()
                ctx = f"XLSX: {path.name} — Sheet: {sheet_name}, embedded image"
                desc = _vision_describe(img_bytes, ctx)
                if desc:
                    blocks.append({
                        "text":    f"[Embedded Image]\n{desc}",
                        "page":    None,
                        "heading": f"{path.name} — Sheet: {sheet_name} — Image",
                    })
            except Exception as e:
                logger.debug("XLSX image failed sheet '%s' in %s: %s", sheet_name, path.name, e)

    return blocks


def _extract_pptx(path: Path) -> list[dict]:
    """
    Extract text AND images from PowerPoint files (.pptx / .ppt).

    For each slide:
    - Extracts all text frames, tables, and speaker notes
    - Detects PICTURE shapes and describes them with GPT-4o Vision
    - Image descriptions are appended as "[Slide Images]" within the slide block

    This captures supply chain maps, architecture screenshots, process flows
    and any diagram that appears on slides with no accompanying text.
    """
    try:
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        from pptx.enum.shapes import PP_PLACEHOLDER
    except ImportError:
        logger.error("python-pptx not installed — run: pip install python-pptx")
        return []

    # Legacy .ppt — attempt LibreOffice conversion
    if path.suffix.lower() == ".ppt":
        try:
            import subprocess, tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                subprocess.run(
                    ["soffice", "--headless", "--convert-to", "pptx",
                     "--outdir", tmpdir, str(path)],
                    capture_output=True, timeout=60
                )
                converted = list(Path(tmpdir).glob("*.pptx"))
                if converted:
                    logger.info("Converted .ppt to .pptx via LibreOffice")
                    return _extract_pptx(converted[0])
        except Exception as e:
            logger.warning("Could not convert .ppt (LibreOffice required): %s", e)
        return []

    try:
        prs = Presentation(str(path))
    except Exception as e:
        logger.error("python-pptx could not open %s: %s", path.name, e)
        return []

    use_vision = bool(os.getenv("OPENAI_API_KEY", "").strip())

    def _pptx_chart_to_markdown(chart) -> str:
        """Native chart data (categories + series values) is exact — read
        straight from the chart's XML data model rather than describing the
        rendered chart image, which would only ever be a paraphrase."""
        lines = []
        try:
            title = chart.chart_title.text_frame.text.strip() if chart.has_title else ""
        except Exception:
            title = ""
        if title:
            lines.append(f"**{title}**")
        for plot in chart.plots:
            categories = [str(c) if c is not None else "" for c in plot.categories]
            series_list = list(plot.series)
            if not categories and not series_list:
                continue
            header = ["Category"] + [s.name or f"Series {i+1}" for i, s in enumerate(series_list)]
            rows = []
            for i, cat in enumerate(categories):
                row = [cat]
                for s in series_list:
                    vals = list(s.values)
                    row.append(str(vals[i]) if i < len(vals) and vals[i] is not None else "")
                rows.append(row)
            lines.append("| " + " | ".join(header) + " |")
            lines.append("| " + " | ".join(["---"] * len(header)) + " |")
            for row in rows:
                lines.append("| " + " | ".join(row) + " |")
        return "\n".join(lines)

    _DGM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
    _A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"

    def _pptx_smartart_text(shape) -> str:
        """SmartArt (a GraphicFrame with a diagram graphicData) has no
        python-pptx support at all — its text lives in a separate diagram-data
        part reached via the shape's r:dm relationship, not on the shape
        itself. Returns "" if this isn't a SmartArt shape or parsing fails."""
        try:
            graphic_data = shape._element.find(
                f".//{{{'http://schemas.openxmlformats.org/drawingml/2006/main'}}}graphicData")
            if graphic_data is None or graphic_data.get("uri") != _DGM_NS:
                return ""
            rel_ids = graphic_data.find(f"{{{_DGM_NS}}}relIds")
            if rel_ids is None:
                return ""
            r_ns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            dm_rid = rel_ids.get(f"{{{r_ns}}}dm")
            if not dm_rid:
                return ""
            diagram_part = shape.part.related_part(dm_rid)
            from lxml import etree
            root = etree.fromstring(diagram_part.blob)
            lines = []
            for pt in root.findall(f".//{{{_DGM_NS}}}pt"):
                pt_type = pt.get("type", "node")
                if pt_type in ("parTrans", "sibTrans", "pres", "doc"):
                    continue
                texts = [t.text for t in pt.findall(f".//{{{_A_NS}}}t") if t.text and t.text.strip()]
                if texts:
                    lines.append(" ".join(texts).strip())
            return "\n".join(lines)
        except Exception as e:
            logger.debug("SmartArt text extraction failed for shape %s: %s", shape.name, e)
            return ""

    def _pptx_table_to_markdown(tbl) -> str:
        """Grid + forward-fill + markdown formatting, mirroring _extract_docx's
        _table_to_markdown so a slide's table survives as one coherent block
        instead of losing its header-to-value pairing.

        A phase/timeline table commonly vertically-merges a "Phase" cell
        across several date rows (the label renders only on the first row,
        blank on the rest) — naively dropping blank cells (the prior
        behavior) silently detached each date from its phase. Forward-filling
        each column (never row 0, the header) restores that association."""
        grid = [[c.text.strip() for c in row.cells] for row in tbl.rows]
        if not grid:
            return ""
        ncols = max(len(r) for r in grid)
        grid = [r + [""] * (ncols - len(r)) for r in grid]
        for col in range(ncols):
            last = grid[0][col]
            for r in range(1, len(grid)):
                if grid[r][col]:
                    last = grid[r][col]
                elif last:
                    grid[r][col] = last
        header = grid[0]
        out = ["| " + " | ".join(header) + " |",
               "| " + " | ".join(["---"] * ncols) + " |"]
        for row in grid[1:]:
            if any(c for c in row):
                out.append("| " + " | ".join(row) + " |")
        return "\n".join(out)

    def _shape_text(shape) -> list:
        """Recursively extract text from any shape type. Top-level tables are
        pulled out into their own isolated chunk block by the slide loop
        below (see table_shapes) so this branch only ever runs for a table
        nested inside a GROUP shape."""
        texts = []
        try:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                for s in shape.shapes:
                    texts.extend(_shape_text(s))
            elif shape.has_table:
                md = _pptx_table_to_markdown(shape.table)
                if md:
                    texts.append(md)
            elif shape.has_text_frame:
                for para in shape.text_frame.paragraphs:
                    t = " ".join(r.text for r in para.runs if r.text.strip())
                    if t.strip():
                        texts.append(t.strip())
        except Exception:
            pass
        return texts

    blocks = []
    total_images = 0

    for slide_num, slide in enumerate(prs.slides, 1):
        slide_texts = []
        title = None
        picture_shapes = []
        table_shapes = []
        chart_shapes = []
        smartart_blocks = []

        for shape in slide.shapes:
            # Identify title placeholder. CENTER_TITLE (a section-divider
            # layout, e.g. "Phase 2 – Go Live: March 2026") was previously
            # missed here — its text fell through to the generic body-text
            # path and the slide's heading fell back to "Slide N", weakening
            # retrieval ranking for exactly the kind of milestone-date slide
            # this section's guidance asks for.
            try:
                if shape.is_placeholder:
                    ph_type = shape.placeholder_format.type
                    if ph_type in (PP_PLACEHOLDER.TITLE, PP_PLACEHOLDER.CENTER_TITLE,
                                   PP_PLACEHOLDER.SLIDE_NUMBER) and shape.has_text_frame:
                        t = shape.text_frame.text.strip()
                        if t:
                            title = t
                            continue  # don't double-count title in body
            except Exception:
                pass
            # Collect picture shapes for vision processing
            try:
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    picture_shapes.append(shape)
                    continue
            except Exception:
                pass
            # Collect top-level tables separately so each becomes its own
            # chunk block (below) instead of being concatenated into the
            # slide's prose body — a large phase/date table concatenated with
            # other text can have its header row and data rows split across
            # different chunks by chunk_text()'s size-based splitting,
            # decoupling phase names from their dates.
            try:
                if shape.has_table:
                    table_shapes.append(shape)
                    continue
            except Exception:
                pass
            # Native charts (bar/line/pie) carry exact category/series data in
            # their XML — reading it directly is strictly more accurate than
            # describing the rendered chart image, which was the only option
            # before (and wasn't even attempted — chart shapes matched none of
            # GROUP/has_table/has_text_frame/PICTURE and fell through silently).
            try:
                if shape.has_chart:
                    chart_shapes.append(shape)
                    continue
            except Exception:
                pass
            # SmartArt diagrams (PowerPoint's flowchart/process-diagram tool)
            # are GraphicFrames with no text_frame/table/picture of their own —
            # same silent-fallthrough gap as charts. Their text lives in a
            # separate diagram-data part reached via relationship, not on the
            # shape itself.
            try:
                smartart_text = _pptx_smartart_text(shape)
                if smartart_text:
                    smartart_blocks.append(smartart_text)
                    continue
            except Exception:
                pass
            slide_texts.extend(_shape_text(shape))

        # Speaker notes
        try:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes and "click to edit" not in notes.lower():
                slide_texts.append(f"Notes: {notes}")
        except Exception:
            pass

        body = "\n".join(t for t in slide_texts if t)
        ctx = f"PPTX: {path.name}, Slide {slide_num}" + (f" — {title}" if title else "")

        # ── Describe images on this slide ─────────────────────────────────────
        image_descs = []
        if use_vision and picture_shapes:
            for shape in picture_shapes:
                try:
                    img_bytes = shape.image.blob
                    desc = _vision_describe(img_bytes, f"{ctx} image: {shape.name}")
                    if desc:
                        image_descs.append(desc)
                        total_images += 1
                except Exception as e:
                    logger.debug("PPTX image failed slide %d shape %s: %s",
                                 slide_num, shape.name, e)

        if image_descs:
            body = (body + "\n\n[Slide Images]:\n" +
                    "\n\n---\n\n".join(image_descs)).strip()

        if body.strip() or title:
            blocks.append({
                "text":    (f"{title}\n{body}" if title else body).strip(),
                "page":    slide_num,
                "heading": title or f"Slide {slide_num}",
            })

        # Each table gets its own isolated block (mirroring _extract_docx's
        # per-table blocks) so chunk_text()'s size-based splitting never
        # separates a table's header row from its data rows.
        for shape in table_shapes:
            try:
                md = _pptx_table_to_markdown(shape.table)
            except Exception:
                md = ""
            if md.strip():
                blocks.append({
                    "text":    md,
                    "page":    slide_num,
                    "heading": f"{ctx} — Table",
                })

        # Charts and SmartArt each get their own isolated block for the same
        # reason as tables above.
        for shape in chart_shapes:
            try:
                md = _pptx_chart_to_markdown(shape.chart)
            except Exception as e:
                logger.debug("PPTX chart failed slide %d shape %s: %s",
                             slide_num, shape.name, e)
                md = ""
            if md.strip():
                blocks.append({
                    "text":    md,
                    "page":    slide_num,
                    "heading": f"{ctx} — Chart",
                })

        for smartart_text in smartart_blocks:
            if smartart_text.strip():
                blocks.append({
                    "text":    smartart_text,
                    "page":    slide_num,
                    "heading": f"{ctx} — SmartArt",
                })

    logger.info("PPTX %s: %d slides, %d image(s) described",
                path.name, len(blocks), total_images)
    return blocks


def _extract_xls(path: Path) -> list[dict]:
    """.xls (legacy binary) extraction via xlrd."""
    try:
        import xlrd
    except ImportError:
        logger.warning("xlrd not installed — run: pip install xlrd>=2.0")
        return []

    try:
        wb = xlrd.open_workbook(str(path))
    except Exception as e:
        logger.error("xlrd could not open %s: %s", path.name, e)
        return []

    blocks = []
    for sheet_name in wb.sheet_names():
        try:
            ws = wb.sheet_by_name(sheet_name)
            rows = []
            for r in range(ws.nrows):
                row = []
                for c in range(ws.ncols):
                    ctype = ws.cell_type(r, c)
                    val   = ws.cell_value(r, c)
                    if ctype == xlrd.XL_CELL_DATE:
                        try:
                            import xlrd.xldate as _xld
                            from datetime import datetime as _dt
                            dt = _xld.xldate_as_datetime(val, wb.datemode)
                            row.append(dt.strftime("%Y-%m-%d"))
                        except Exception:
                            row.append(str(val))
                    elif ctype == xlrd.XL_CELL_EMPTY:
                        row.append("")
                    else:
                        row.append(_cell_to_str(val))
                rows.append(row)
            block = _sheet_to_block(sheet_name, rows)
            if block:
                blocks.append(block)
        except Exception as e:
            logger.warning("Error reading sheet '%s' in %s: %s", sheet_name, path.name, e)
    return blocks


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHUNKING — paragraph-aware with overlap
# (improved from chunk_extracter.py: larger chunks + overlap)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[dict]:
    """
    Split text into chunks at paragraph boundaries with overlap.
    Returns list of { "text": str, "char_start": int, "char_end": int }
    """
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return []

    chunks = []
    current_parts = []
    current_len = 0
    current_start = 0  # track position in original text

    for para in paragraphs:
        if current_len + len(para) > chunk_size and current_parts:
            # Flush current chunk
            chunk_text_str = "\n".join(current_parts)
            char_start = text.find(current_parts[0], current_start)
            if char_start == -1:
                char_start = current_start
            char_end = char_start + len(chunk_text_str)

            chunks.append({
                "text": chunk_text_str,
                "char_start": char_start,
                "char_end": min(char_end, len(text)),
            })

            # Overlap: keep the last portion of text for context
            overlap_parts = []
            overlap_len = 0
            for p in reversed(current_parts):
                if overlap_len + len(p) > overlap:
                    break
                overlap_parts.insert(0, p)
                overlap_len += len(p)

            current_parts = overlap_parts + [para]
            current_len = overlap_len + len(para)
            current_start = char_end - overlap_len
        else:
            current_parts.append(para)
            current_len += len(para)

    # Flush remaining
    if current_parts:
        chunk_text_str = "\n".join(current_parts)
        char_start = text.find(current_parts[0], current_start)
        if char_start == -1:
            char_start = current_start
        char_end = char_start + len(chunk_text_str)
        chunks.append({
            "text": chunk_text_str,
            "char_start": max(0, char_start),
            "char_end": min(char_end, len(text)),
        })

    return chunks


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# KEYWORD EXTRACTION — LLM (primary) + local fallback
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _has_openai_key() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def extract_keywords_llm(text: str, n: int = KEYWORDS_PER_CHUNK) -> list[str]:
    """Single-chunk LLM keyword extraction — kept for compatibility. Prefer extract_keywords_llm_batch."""
    results = extract_keywords_llm_batch([text], n)
    return results[0] if results else extract_keywords_local(text, n)


_BATCH_SIZE = 20  # chunks per LLM call


_KW_CLIENT = None
def _get_kw_client():
    global _KW_CLIENT
    if _KW_CLIENT is not None:
        return _KW_CLIENT
    if not os.getenv("OPENAI_API_KEY", "").strip():
        return None
    try:
        from openai import OpenAI
        import httpx as _httpx
        ssl_verify = os.getenv("OPENAI_SSL_VERIFY", "false").lower() not in ("false", "0", "no")
        _KW_CLIENT = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            http_client=_httpx.Client(verify=ssl_verify, timeout=60.0),
            max_retries=1,
            timeout=60.0,
        )
        return _KW_CLIENT
    except Exception as e:
        logger.warning("Could not init keyword client: %s", e)
        return None


def extract_keywords_llm_batch(texts: list[str], n: int = KEYWORDS_PER_CHUNK) -> list[list[str]]:
    """
    Extract keywords for multiple chunks in a single LLM call.
    Turns N serial API calls into ceil(N/20) calls — much faster for multi-chunk docs.
    Falls back to local extraction per-chunk on any LLM error.
    """
    if not texts:
        return []

    try:
        client = _get_kw_client()
        if client is None:
            raise RuntimeError("no openai client")

        chunk_lines = "\n\n".join(
            f'[{i}] "{text[:800].strip()}"'
            for i, text in enumerate(texts)
        )

        prompt = f"""You are a supply chain domain expert.
For each numbered text chunk below, extract exactly {n} concise, specific keyword phrases.
Rules:
- For table/spreadsheet data (rows with Value:, Description:, site names, codes): extract EVERY unique entity name, site name, code, or identifier visible in the chunk. These are more important than generic terms.
- For prose text: extract the most specific and distinctive concepts.
- Keywords should be 1-5 words, title-cased where appropriate.
- Prefer proper nouns, product names, site names, system names, and specific identifiers over generic words.
Return ONLY a JSON object — keys are chunk indices as strings, values are arrays of exactly {n} keyword strings.
No explanation, no markdown.

{chunk_lines}"""

        response = client.chat.completions.create(
            model=os.getenv("OPENAI_MINI_MODEL", "gpt-4.1-mini"),
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=max(200, 80 * len(texts)),
        )

        raw = response.choices[0].message.content.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result_map = json.loads(raw)

        out = []
        for i in range(len(texts)):
            kws = result_map.get(str(i), [])
            if isinstance(kws, list) and kws:
                out.append([str(k) for k in kws[:n]])
            else:
                out.append(extract_keywords_local(texts[i], n))
        return out

    except Exception as e:
        logger.warning("Batch LLM keyword extraction failed: %s — falling back to local", e)
        return [extract_keywords_local(t, n) for t in texts]


# Stop words for local fallback
_STOP_WORDS = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "as", "be", "was", "are",
    "been", "being", "have", "has", "had", "do", "does", "did", "will",
    "would", "could", "should", "may", "might", "shall", "can", "this",
    "that", "these", "those", "i", "we", "you", "he", "she", "they",
    "me", "him", "her", "us", "them", "my", "our", "your", "his", "its",
    "their", "what", "which", "who", "whom", "when", "where", "why", "how",
    "not", "no", "nor", "if", "then", "than", "so", "very", "just",
    "about", "up", "out", "into", "over", "after", "before", "between",
    "under", "also", "each", "all", "any", "both", "few", "more", "most",
    "other", "some", "such", "only", "own", "same", "too", "s", "t", "re",
    "ll", "ve", "d", "m", "don", "doesn", "didn", "won", "isn", "aren",
    "was", "were", "hasn", "haven", "hadn", "wouldn", "couldn", "shouldn",
}


def extract_keywords_local(text: str, max_keywords: int = KEYWORDS_PER_CHUNK) -> list[str]:
    """Local TF-based keyword extraction — used when no API key is available."""
    words = re.findall(r"[a-zA-Z]{3,}", text.lower())
    words = [w for w in words if w not in _STOP_WORDS and len(w) > 2]
    if not words:
        return []

    tf = {}
    for w in words:
        tf[w] = tf.get(w, 0) + 1

    # Boost capitalized multi-word phrases from original text
    phrases = re.findall(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+", text)
    for phrase in phrases:
        key = phrase.strip()
        if len(key) > 5:
            tf[key] = tf.get(key, 0) + 2

    sorted_terms = sorted(tf.items(), key=lambda x: -x[1])
    keywords = []
    seen = set()
    for term, _ in sorted_terms:
        titled = term.title() if " " not in term else term
        if titled.lower() not in seen:
            keywords.append(titled)
            seen.add(titled.lower())
        if len(keywords) >= max_keywords:
            break
    return keywords


def extract_keywords(text: str, n: int = KEYWORDS_PER_CHUNK) -> list[str]:
    """
    Main keyword extraction entry point.
    Uses LLM if OPENAI_API_KEY is set, otherwise falls back to local.
    """
    if _has_openai_key():
        return extract_keywords_llm(text, n)
    else:
        logger.info("No OPENAI_API_KEY — using local keyword extraction")
        return extract_keywords_local(text, n)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MAIN PIPELINE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def process_file(
    file_path: Path,
    project_id: str,
    file_id: str,
    existing_chunk_count: int = 0,
) -> list[dict]:
    """
    Process a single file: extract → chunk → batch keyword extraction → return chunks.
    Keywords are extracted in batched LLM calls (1 call per 20 chunks) instead of
    one call per chunk, making the pipeline significantly faster.
    """
    doc_name = file_path.name
    logger.info("Processing %s (project=%s, file=%s)", doc_name, project_id, file_id)

    blocks = extract_text_from_file(file_path)
    if not blocks:
        logger.warning("No text extracted from %s", doc_name)
        return []

    # Pass 1 — build all chunk records without keywords
    chunks = []
    chunk_id = existing_chunk_count

    for block in blocks:
        text = block["text"]
        if not text.strip():
            continue
        page    = block.get("page")
        heading = block.get("heading")
        for tc in chunk_text(text):
            brd_sid = classify_chunk_section(heading or "", tc["text"])
            chunks.append(make_chunk(
                chunk_id=chunk_id,
                project_id=project_id,
                file_id=file_id,
                doc_name=doc_name,
                chunk_text=tc["text"],
                page_number=page,
                section_heading=heading,
                char_start=tc["char_start"],
                char_end=tc["char_end"],
                keywords=[],
                brd_section_id=brd_sid,
            ))
            chunk_id += 1

    if not chunks:
        return []

    # Pass 2 — extract keywords for all chunks in batched LLM calls
    all_texts = [c["chunk_text"] for c in chunks]
    all_keywords: list[list[str]] = []

    if _has_openai_key():
        # Run batch LLM calls in parallel — each batch is independent and the
        # bottleneck is round-trip latency, so a small thread pool roughly
        # halves keyword extraction time on multi-batch docs.
        batches = [all_texts[i:i + _BATCH_SIZE] for i in range(0, len(all_texts), _BATCH_SIZE)]
        if len(batches) > 1:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(4, len(batches))) as pool:
                for batch_kws in pool.map(extract_keywords_llm_batch, batches):
                    all_keywords.extend(batch_kws)
        else:
            for b in batches:
                all_keywords.extend(extract_keywords_llm_batch(b))
        method = f"LLM batch ({_BATCH_SIZE}/call)"
    else:
        all_keywords = [extract_keywords_local(t) for t in all_texts]
        method = "local"

    for chunk, kws in zip(chunks, all_keywords):
        chunk["keywords"] = kws

    # Pass 3 — generate embeddings (always, when OpenAI key is available)
    try:
        try:
            from app.semantic_search import embed_texts
        except ImportError:
            from semantic_search import embed_texts
        all_texts = [c["chunk_text"] for c in chunks]
        logger.info("Generating embeddings for %d chunks from %s …", len(chunks), doc_name)
        embeddings = embed_texts(all_texts)
        for chunk, emb in zip(chunks, embeddings):
            chunk["embedding"] = emb
        logger.info("Embeddings ready for %s", doc_name)
    except Exception as e:
        logger.warning(
            "Embedding generation skipped for %s: %s "
            "— chunks saved without embeddings, call /embed-chunks to backfill",
            doc_name, e,
        )

    logger.info("Produced %d chunks from %s (keywords via %s)", len(chunks), doc_name, method)
    return chunks


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STORAGE — project-scoped chunks.json
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_project_chunks(project_dir: Path) -> list[dict]:
    chunks_path = project_dir / "chunks.json"
    if chunks_path.exists():
        try:
            with open(chunks_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            return []
    return []


def save_project_chunks(project_dir: Path, chunks: list[dict]) -> Path:
    chunks_path = project_dir / "chunks.json"
    with open(chunks_path, "w", encoding="utf-8") as f:
        json.dump(chunks, f, indent=2, ensure_ascii=False)
    logger.info("Saved %d chunks to %s", len(chunks), chunks_path)
    return chunks_path


def remove_file_chunks(project_dir: Path, file_id: str, project_id: str = None) -> int:
    """Remove all chunks belonging to a specific file."""
    def _do_remove():
        chunks = load_project_chunks(project_dir)
        before = len(chunks)
        chunks = [c for c in chunks if c.get("file_id") != file_id]
        after = len(chunks)
        save_project_chunks(project_dir, chunks)
        removed = before - after
        if removed:
            logger.info("Removed %d chunks for file %s", removed, file_id)
        return removed

    if project_id:
        lock = _get_project_lock(project_id)
        with lock:
            return _do_remove()
    else:
        return _do_remove()


def run_pipeline_for_file(
    file_path: Path,
    project_id: str,
    file_id: str,
    project_dir: Path,
) -> int:
    """
    Full pipeline for a single file. Called as a background task.
    Processing (extract + chunk + keywords) runs outside the lock.
    Only the final chunks.json write is serialized per project.
    """
    # Process the file OUTSIDE the lock (this is the slow part — especially LLM calls)
    new_chunks = process_file(
        file_path=file_path,
        project_id=project_id,
        file_id=file_id,
        existing_chunk_count=0,
    )

    # Acquire lock only for the read-modify-write of chunks.json
    lock = _get_project_lock(project_id)
    with lock:
        remove_file_chunks(project_dir, file_id)
        existing_chunks = load_project_chunks(project_dir)

        # Assign globally-unique ids based on the MAX existing id, not the
        # list length. Using len() collided after a file was deleted and a
        # different file re-chunked (e.g. delete A's 0-9, B keeps 10-19, then
        # re-add A → len()=10 → A gets 10-19, colliding with B). Collisions
        # made the chunk browser resolve one document's chunk_id to another
        # document's chunk (view / select / pin all keyed on chunk_id).
        existing_ids = [
            c.get("chunk_id") for c in existing_chunks
            if isinstance(c.get("chunk_id"), int)
        ]
        next_id = (max(existing_ids) + 1) if existing_ids else 0
        for i, chunk in enumerate(new_chunks):
            chunk["chunk_id"] = next_id + i

        all_chunks = existing_chunks + new_chunks
        save_project_chunks(project_dir, all_chunks)

        # Return (this_file_chunk_count, total_project_chunk_count)
        # Avoids a second disk read to count per-file chunks
        return len(new_chunks), len(all_chunks)