"""
template_routes.py — Global template library management

Templates are global (reusable across any project). Each template is built
from the Table of Contents of an imported Word document, then sections can
be configured with keywords, prompts, and formatting flags.

Endpoints:
  GET    /api/templates
  POST   /api/templates/import
  GET    /api/templates/{template_id}
  PUT    /api/templates/{template_id}
  DELETE /api/templates/{template_id}
  PUT    /api/templates/{template_id}/sections/{section_id}
  POST   /api/templates/{template_id}/apply/{project_id}
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

logger = logging.getLogger("TemplateRoutes")
router = APIRouter()

_THIS_DIR = Path(__file__).parent
TEMPLATES_STORE_PATH = _THIS_DIR / "templates_store.json"

# ─────────────────────────────────────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_templates() -> List[Dict]:
    if TEMPLATES_STORE_PATH.exists():
        try:
            return json.loads(TEMPLATES_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_templates(templates: List[Dict]) -> None:
    TEMPLATES_STORE_PATH.write_text(
        json.dumps(templates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _find_template(templates: List[Dict], template_id: str) -> Dict:
    for t in templates:
        if t["id"] == template_id:
            return t
    raise HTTPException(404, "Template not found")


# ─────────────────────────────────────────────────────────────────────────────
# TOC extraction from .docx
# ─────────────────────────────────────────────────────────────────────────────

_TOC_STYLES = {"TOC 1": 1, "TOC 2": 2, "TOC 3": 3, "TOC 4": 4, "TOC 5": 5}
_HEADING_STYLES = {
    "Heading 1": 1, "Heading 2": 2, "Heading 3": 3,
    "Heading 4": 4, "Heading 5": 5,
}


# Matches a leading section number prefix in a TOC/heading line, e.g.
#   "1 Introduction"          → ("1",     "Introduction")
#   "1.1 Scope"               → ("1.1",   "Scope")
#   "7.1.1 Process Overview"  → ("7.1.1", "Process Overview")
#   "1.1. Scope"              → ("1.1",   "Scope")   (trailing dot allowed)
# The separator between the number and the title may be any whitespace
# (space, non-breaking space, tab) or one of " -–:".
_SECTION_NUM_RE = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?[\s \-–:]+(.+?)\s*$")


def _clean_toc_text(raw_text: str) -> str:
    """Strip the trailing page-number cell and TOC dot-leaders from a paragraph.

    Word TOC paragraphs typically look like:
        "1.1<TAB>Scope<TAB>5"
        "Scope ........... 5"   (no tab, dot-leader run)
        "1.1 Scope\t\t\t5"
    We want just the title cell, with the page number removed.
    """
    if not raw_text:
        return ""
    parts = raw_text.split("\t")
    # Drop trailing parts that are pure page numbers
    while parts and parts[-1].strip().isdigit():
        parts.pop()
    text = "\t".join(parts).strip()
    # Collapse dot-leader runs ("....") and any whitespace + trailing page number
    text = re.sub(r"[ \t]*\.{2,}[ \t]*\d+\s*$", "", text)
    # Replace any remaining tabs with a single space so the title reads cleanly
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_toc(docx_bytes: bytes) -> List[Dict]:
    import io
    from docx import Document

    doc = Document(io.BytesIO(docx_bytes))
    raw: List[Dict] = []

    for para in doc.paragraphs:
        style_name = (para.style.name or "") if para.style else ""
        level = _TOC_STYLES.get(style_name)
        if level is None:
            continue
        text = _clean_toc_text(para.text)
        if text:
            raw.append({"title": text, "level": level})

    # Fall back to heading styles when the doc has no built-in TOC
    if not raw:
        for para in doc.paragraphs:
            style_name = (para.style.name or "") if para.style else ""
            level = _HEADING_STYLES.get(style_name)
            if level is None:
                continue
            text = _clean_toc_text(para.text)
            if text:
                raw.append({"title": text, "level": level})

    return _assign_section_ids(raw)


def _assign_section_ids(raw: List[Dict]) -> List[Dict]:
    """Assign hierarchical IDs to extracted entries.

    If a title already starts with a numeric prefix like "7.1.1 ...", the
    embedded prefix is used as the section ID and stripped from the title.
    The level is recomputed from the depth of the prefix so a "7.1.1" entry
    is always level 3 even if Word labelled the paragraph as TOC 1.

    Entries without an embedded prefix fall back to synthesised IDs from
    a per-level counter, e.g. 1, 1.1, 1.2, 2, 2.1 ...
    """
    counters = [0] * 12
    result: List[Dict] = []

    for entry in raw:
        title = (entry.get("title") or "").strip()
        m = _SECTION_NUM_RE.match(title)

        if m:
            section_id = m.group(1)
            clean_title = m.group(2).strip()
            parts = section_id.split(".")
            level = max(1, min(len(parts), 12))
            # Sync the counters so any *following* entries without an embedded
            # prefix continue numbering from where the document left off.
            for i, part in enumerate(parts):
                try:
                    counters[i] = int(part)
                except ValueError:
                    counters[i] = 0
            for i in range(level, len(counters)):
                counters[i] = 0
        else:
            level = max(1, min(entry.get("level", 1), 12))
            counters[level - 1] += 1
            for i in range(level, len(counters)):
                counters[i] = 0
            section_id = ".".join(str(counters[i]) for i in range(level))
            clean_title = title

        result.append({
            "id":           section_id,
            "title":        clean_title,
            "level":        level,
            "web_keywords": [],
            "doc_keywords": [],
            "prompt":       "",
            "structure":    "",
            "heading_only": False,
            "no_heading":   False,
        })

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class SectionUpdate(BaseModel):
    web_keywords:  List[str] = []
    doc_keywords:  List[str] = []
    prompt:        str       = ""
    structure:     str       = ""
    heading_only:  bool      = False
    no_heading:    bool      = False


class TemplateRename(BaseModel):
    name: str


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/templates")
def list_templates():
    """Return lightweight summary list (no section detail)."""
    return [
        {
            "id":            t["id"],
            "name":          t["name"],
            "source_doc":    t.get("source_doc", ""),
            "created":       t["created"],
            "updated":       t["updated"],
            "section_count": len(t.get("sections", [])),
        }
        for t in _load_templates()
    ]


@router.post("/api/templates/import")
async def import_template(
    file: UploadFile = File(...),
    name: str = Form(""),
):
    """Upload a .docx, extract its TOC, and save as a new template."""
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(400, "Only .docx files are supported")

    content = await file.read()
    try:
        sections = _extract_toc(content)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse document: {exc}")

    if not sections:
        raise HTTPException(
            400,
            "No table of contents or headings found in this document. "
            "Make sure the document uses Word heading styles (Heading 1, Heading 2 …) "
            "or has an inserted Table of Contents field.",
        )

    now = datetime.utcnow().isoformat()
    template = {
        "id":         str(uuid.uuid4())[:8],
        "name":       (name.strip() or file.filename),
        "source_doc": file.filename,
        "created":    now,
        "updated":    now,
        "sections":   sections,
    }

    templates = _load_templates()
    templates.append(template)
    _save_templates(templates)
    logger.info(
        "Template %s (%s) created from %s — %d sections",
        template["id"], template["name"], file.filename, len(sections),
    )
    return template


@router.get("/api/templates/{template_id}")
def get_template(template_id: str):
    return _find_template(_load_templates(), template_id)


@router.put("/api/templates/{template_id}")
def rename_template(template_id: str, req: TemplateRename):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name cannot be empty")
    templates = _load_templates()
    t = _find_template(templates, template_id)
    t["name"] = name
    t["updated"] = datetime.utcnow().isoformat()
    _save_templates(templates)
    return t


@router.delete("/api/templates/{template_id}")
def delete_template(template_id: str):
    templates = _load_templates()
    remaining = [t for t in templates if t["id"] != template_id]
    if len(remaining) == len(templates):
        raise HTTPException(404, "Template not found")
    _save_templates(remaining)
    return {"deleted": True, "template_id": template_id}


@router.put("/api/templates/{template_id}/sections/{section_id}")
def update_section(template_id: str, section_id: str, req: SectionUpdate):
    templates = _load_templates()
    t = _find_template(templates, template_id)
    for sec in t.get("sections", []):
        if sec["id"] == section_id:
            sec["web_keywords"] = req.web_keywords
            sec["doc_keywords"] = req.doc_keywords
            sec["prompt"]       = req.prompt
            sec["structure"]    = req.structure
            sec["heading_only"] = req.heading_only
            sec["no_heading"]   = req.no_heading
            t["updated"] = datetime.utcnow().isoformat()
            _save_templates(templates)
            return sec
    raise HTTPException(404, f"Section '{section_id}' not found in template")


# ─────────────────────────────────────────────────────────────────────────────
# Apply template to a project
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/templates/{template_id}/apply/{project_id}")
def apply_template(template_id: str, project_id: str, overwrite: bool = True):
    """
    Seed a project with a template's section structure and keyword defaults.

    - Writes the template sections to the project's sections store
      (screen3_sections_{project_id}.json → "active" list).
    - Writes each section's keyword/prompt/structure/flag config to the
      project's keyword store (screen3_keyword_{project_id}.json).

    overwrite=true  (default): replace both stores entirely.
    overwrite=false           : keep existing section config; only fill
                                keyword entries that are currently empty.
    """
    templates = _load_templates()
    t = _find_template(templates, template_id)
    sections = t.get("sections", [])

    if not sections:
        raise HTTPException(400, "Template has no sections to apply")

    # ── Resolve screen3 store helpers ────────────────────────────────────────
    # Import lazily so template_routes has no hard circular dependency on
    # screen3_routes (they share the same app dir but are separate modules).
    try:
        from app.screen3_routes import (
            _load_sections_store, _save_sections_store,
            _load_keyword_store,  _save_keyword_store,
        )
    except ImportError:
        from screen3_routes import (
            _load_sections_store, _save_sections_store,
            _load_keyword_store,  _save_keyword_store,
        )

    # ── Build the active sections list ───────────────────────────────────────
    active_sections = [
        {"id": s["id"], "title": s["title"], "level": s.get("level", 1)}
        for s in sections
    ]

    if overwrite:
        _save_sections_store({"active": active_sections, "archived": []}, project_id)
    else:
        existing_store = _load_sections_store(project_id)
        if not existing_store.get("active"):
            _save_sections_store({"active": active_sections, "archived": []}, project_id)
        # If already has sections, leave them untouched when overwrite=False

    # ── Seed the keyword store ────────────────────────────────────────────────
    kw_store = {} if overwrite else _load_keyword_store(project_id)

    seeded = 0
    for sec in sections:
        sid = sec["id"]
        has_config = bool(
            sec.get("web_keywords") or sec.get("doc_keywords") or
            sec.get("prompt") or sec.get("structure") or
            sec.get("heading_only") or sec.get("no_heading")
        )
        if not has_config:
            continue

        if not overwrite and kw_store.get(sid, {}).get("web_keywords"):
            continue  # already configured — don't overwrite

        existing = kw_store.get(sid, {})
        kw_store[sid] = {
            **existing,
            "web_keywords": sec.get("web_keywords", []),
            "doc_keywords": sec.get("doc_keywords", []),
            "prompt":       sec.get("prompt", ""),
            "structure":    sec.get("structure", ""),
            "heading_only": sec.get("heading_only", False),
            "no_heading":   sec.get("no_heading", False),
        }
        seeded += 1

    _save_keyword_store(kw_store, project_id)

    logger.info(
        "Applied template %s (%s) to project %s — %d sections, %d keyword configs seeded, overwrite=%s",
        template_id, t["name"], project_id, len(sections), seeded, overwrite,
    )
    return {
        "ok":             True,
        "template_id":    template_id,
        "template_name":  t["name"],
        "project_id":     project_id,
        "sections_set":   len(sections),
        "keywords_seeded": seeded,
        "overwrite":      overwrite,
    }
