"""
sow_template_routes.py — SOW Template Library

Mirrors app/template_routes.py's design (a template = the Table of Contents
of an imported Word document, turned into a section list), kept as its own
store/router so SOW templates never mix with BRD templates. One template in
the library may be marked `is_default`: the org-wide Bristlecone-branded SOW
template applied automatically to any SOW project that hasn't uploaded its
own. Until a real branded file is imported, `FALLBACK_SECTIONS` (transcribed
from SOW_SKILL.md's Step 3 structure) seeds new projects so the tool is
usable end to end from day one — swapping in the real file later is a pure
data change (import it, mark it default), no code change.

Endpoints:
  GET    /api/sow-templates
  POST   /api/sow-templates/import
  GET    /api/sow-templates/{template_id}
  PUT    /api/sow-templates/{template_id}
  DELETE /api/sow-templates/{template_id}
  PUT    /api/sow-templates/{template_id}/sections/{section_id}
  POST   /api/sow-templates/{template_id}/set-default
  POST   /api/sow-templates/{template_id}/apply/{project_id}
  GET    /api/sow-templates/resolve/{project_id}   — which template (if any) applies
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from pydantic import BaseModel

from app.template_routes import _extract_toc

logger = logging.getLogger("SOWTemplateRoutes")
router = APIRouter()

_THIS_DIR = Path(__file__).parent
SOW_TEMPLATES_STORE_PATH = _THIS_DIR / "sow_templates_store.json"

# Where an uploaded/default template's own .docx file lives, so the export
# step (app/sow_workflow.py) can copy it and fill it in — mirrors
# project_routes.py's per-project _template.docx convention but keyed by
# this library's own template_id instead of a project_id.
SOW_TEMPLATE_FILES_DIR = _THIS_DIR / "sow_template_files"
SOW_TEMPLATE_FILES_DIR.mkdir(exist_ok=True)


def _detect_sections_with_own_content(docx_bytes: bytes, sections: List[Dict]) -> set:
    """Return the ids of sections that have real body content (a paragraph or
    table) directly under their own heading, before the next heading at any
    level. A section with numbered sub-sections is NOT automatically a pure
    "container" — e.g. the real template's "14. Performance Reporting & KPIs"
    has its own intro sentence + table before "14.1 SLAs & Targets" begins,
    while "3. Project Overview" has nothing at all before "3.1 Background".
    Only sections with NEITHER their own content NOR being a leaf should ever
    be excluded from generation (see sow_section_routes._blocked_ids).

    Matches headings to `sections` by POSITION rather than by re-parsing
    title text: `_extract_toc` builds `sections` by walking the same
    heading-styled paragraphs in the same document order, so the Nth heading
    found here corresponds to the Nth entry in `sections` — this sidesteps
    having to re-strip numeric prefixes ("14." vs "Performance Reporting…")
    to match a heading's raw text back to its cleaned title.
    """
    import io
    from docx import Document
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(docx_bytes))
    heading_styles = {"Heading 1", "Heading 2", "Heading 3", "Heading 4", "Heading 5"}

    result = set()
    current_id = None
    section_iter = iter(sections)
    next_section = next(section_iter, None)
    body = doc.element.body

    for child in body:
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            style = p.style.name if p.style else ""
            if style in heading_styles:
                current_id = next_section["id"] if next_section else None
                next_section = next(section_iter, None)
                continue
            if style == "Title":
                current_id = None  # the doc Title itself is never a SOW section
                continue
            if current_id and p.text.strip():
                result.add(current_id)
        elif tag == "tbl" and current_id:
            result.add(current_id)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Fallback section list — transcribed from SOW_SKILL.md Section 4, Step 3.
# Used to seed a project's sections when neither a custom template has been
# applied to it nor a default template exists in the library yet.
# ─────────────────────────────────────────────────────────────────────────────
FALLBACK_SECTIONS: List[Dict] = [
    {"id": "cover", "title": "Cover Page",                                   "level": 1},
    {"id": "1",     "title": "Agreement Details",                           "level": 1},
    {"id": "2",     "title": "Executive Summary",                           "level": 1},
    {"id": "3",     "title": "Project Overview",                            "level": 1},
    {"id": "4",     "title": "Scope of Work",                               "level": 1},
    {"id": "5",     "title": "Proposed Architecture",                       "level": 1},
    {"id": "6",     "title": "Solution Implementation Approach & Project Plan", "level": 1},
    {"id": "7",     "title": "Project Management",                          "level": 1},
    {"id": "8",     "title": "Personnel Requirements",                      "level": 1},
    {"id": "9",     "title": "Key Deliverables",                            "level": 1},
    {"id": "10",    "title": "Key Assumptions",                             "level": 1},
    {"id": "11",    "title": "Acceptance Criteria",                         "level": 1},
    {"id": "12",    "title": "Obligations",                                 "level": 1},
    {"id": "13",    "title": "Commercials",                                 "level": 1},
    {"id": "14",    "title": "Performance Reporting & KPIs",                "level": 1},
    {"id": "15",    "title": "Risks",                                       "level": 1},
    {"id": "16",    "title": "Governance & Risk Mitigation",                "level": 1},
    {"id": "17",    "title": "Change Management Process",                   "level": 1},
    {"id": "18",    "title": "Security & Data Protection",                  "level": 1},
    {"id": "19",    "title": "Termination",                                 "level": 1},
    {"id": "20",    "title": "Acceptance and Sign-Off",                     "level": 1},
    {"id": "21",    "title": "Key Observations",                            "level": 1},
    {"id": "22",    "title": "Reviewer Recommendations",                    "level": 1},
    {"id": "appA",  "title": "Appendix A — Technical Prerequisites",        "level": 1},
    {"id": "appB",  "title": "Appendix B — Resource Profiles & Certifications", "level": 1},
]
# Sections that use a table in their generated content — mirrors SOW_SKILL.md's
# Step 3 "Table discipline" rule. Every other section is prose/bullets only.
# Includes both the top-level ids (FALLBACK_SECTIONS' granularity) and the
# granular sub-section ids the real branded template decomposes into (e.g.
# "4.6 Integration Scope" rather than just "4 Scope of Work").
TABLE_SECTION_IDS = {
    "cover", "0", "1", "4", "6", "7", "9", "11", "13", "14", "15", "17", "20",
    "4.6", "6.2", "7.2", "13.2",
}
# Sections where a relevant diagram (process flow / architecture / rollout)
# is most valuable, per SOW_SKILL.md's Step 3 notes — used to prioritize
# image retrieval, not to gate it (any section may cite a relevant image).
DIAGRAM_PRIORITY_SECTION_IDS = {"3", "5", "6", "3.3", "6.1"}


# ─────────────────────────────────────────────────────────────────────────────
# Storage helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_templates() -> List[Dict]:
    if SOW_TEMPLATES_STORE_PATH.exists():
        try:
            return json.loads(SOW_TEMPLATES_STORE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_templates(templates: List[Dict]) -> None:
    SOW_TEMPLATES_STORE_PATH.write_text(
        json.dumps(templates, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _find_template(templates: List[Dict], template_id: str) -> Dict:
    for t in templates:
        if t["id"] == template_id:
            return t
    raise HTTPException(404, "SOW template not found")


def get_default_template() -> Optional[Dict]:
    """Return the org-wide default SOW template, if one has been marked."""
    for t in _load_templates():
        if t.get("is_default"):
            return t
    return None


def get_template_file_path(template_id: str) -> Optional[Path]:
    """Return the .docx path for a template, if it has one on disk."""
    p = SOW_TEMPLATE_FILES_DIR / f"{template_id}.docx"
    return p if p.exists() else None


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models
# ─────────────────────────────────────────────────────────────────────────────

class SectionUpdate(BaseModel):
    web_keywords: List[str] = []
    doc_keywords: List[str] = []
    prompt:       str       = ""
    structure:    str       = ""
    heading_only: bool      = False
    no_heading:   bool      = False


class TemplateRename(BaseModel):
    name: str


# ─────────────────────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/sow-templates")
def list_sow_templates():
    return [
        {
            "id":            t["id"],
            "name":          t["name"],
            "source_doc":    t.get("source_doc", ""),
            "created":       t["created"],
            "updated":       t["updated"],
            "section_count": len(t.get("sections", [])),
            "is_default":    bool(t.get("is_default", False)),
        }
        for t in _load_templates()
    ]


# Structural headings that describe the document itself rather than a
# fillable SOW section — never turn these into a "generate content for this
# section" item. "Table of Contents" in particular exists in the branded
# template purely so app/sow_workflow.py can inject a real Word TOC field
# under it on export; it must never appear in the reviewable section list.
_META_SECTION_TITLES = {"table of contents", "toc"}


def _filter_meta_sections(sections: List[Dict]) -> List[Dict]:
    return [s for s in sections if (s.get("title") or "").strip().lower() not in _META_SECTION_TITLES]


@router.post("/api/sow-templates/import")
async def import_sow_template(
    file: UploadFile = File(...),
    name: str = Form(""),
    is_default: bool = Form(False),
):
    """Upload a .docx SOW template, extract its TOC/headings as the section
    list. Pass is_default=true for the org-wide Bristlecone-branded template
    (only one may be default at a time — importing a new one as default
    unmarks the previous one)."""
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(400, "Only .docx files are supported")

    content = await file.read()
    try:
        # Detect own-content BEFORE filtering out meta headings (e.g. "Table
        # of Contents") — that detection matches headings to sections by
        # position in document order, so dropping an entry first would shift
        # every section after it out of alignment.
        raw_sections = _extract_toc(content)
        own_content_ids = _detect_sections_with_own_content(content, raw_sections)
        for s in raw_sections:
            s["has_own_content"] = s["id"] in own_content_ids
        sections = _filter_meta_sections(raw_sections)
    except Exception as exc:
        raise HTTPException(400, f"Could not parse document: {exc}")

    if not sections:
        raise HTTPException(
            400,
            "No table of contents or headings found in this document. "
            "Make sure it uses Word heading styles (Heading 1, Heading 2 …) "
            "or has an inserted Table of Contents field.",
        )

    now = datetime.utcnow().isoformat()
    template_id = str(uuid.uuid4())[:8]
    template = {
        "id":         template_id,
        "name":       (name.strip() or file.filename),
        "source_doc": file.filename,
        "created":    now,
        "updated":    now,
        "sections":   sections,
        "is_default": bool(is_default),
    }

    templates = _load_templates()
    if is_default:
        for t in templates:
            t["is_default"] = False
    templates.append(template)
    _save_templates(templates)

    # Keep the original .docx on disk — needed at export time for the
    # heading-anchored fill (app/sow_workflow.py).
    (SOW_TEMPLATE_FILES_DIR / f"{template_id}.docx").write_bytes(content)

    logger.info(
        "SOW template %s (%s) created from %s — %d sections, default=%s",
        template_id, template["name"], file.filename, len(sections), is_default,
    )
    return template


@router.get("/api/sow-templates/{template_id}")
def get_sow_template(template_id: str):
    return _find_template(_load_templates(), template_id)


@router.put("/api/sow-templates/{template_id}")
def rename_sow_template(template_id: str, req: TemplateRename):
    name = req.name.strip()
    if not name:
        raise HTTPException(400, "Name cannot be empty")
    templates = _load_templates()
    t = _find_template(templates, template_id)
    t["name"] = name
    t["updated"] = datetime.utcnow().isoformat()
    _save_templates(templates)
    return t


@router.delete("/api/sow-templates/{template_id}")
def delete_sow_template(template_id: str):
    templates = _load_templates()
    remaining = [t for t in templates if t["id"] != template_id]
    if len(remaining) == len(templates):
        raise HTTPException(404, "SOW template not found")
    _save_templates(remaining)
    f = SOW_TEMPLATE_FILES_DIR / f"{template_id}.docx"
    if f.exists():
        f.unlink()
    return {"deleted": True, "template_id": template_id}


@router.put("/api/sow-templates/{template_id}/sections/{section_id}")
def update_sow_template_section(template_id: str, section_id: str, req: SectionUpdate):
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


@router.post("/api/sow-templates/{template_id}/set-default")
def set_default_sow_template(template_id: str):
    templates = _load_templates()
    t = _find_template(templates, template_id)
    for other in templates:
        other["is_default"] = (other["id"] == template_id)
    _save_templates(templates)
    logger.info("SOW template %s (%s) set as org default", template_id, t["name"])
    return {"ok": True, "template_id": template_id}


# ─────────────────────────────────────────────────────────────────────────────
# Apply template (or the fallback structure) to a SOW project
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/sow-templates/{template_id}/apply/{project_id}")
def apply_sow_template(template_id: str, project_id: str):
    """Seed a SOW project's section structure from a template's TOC."""
    templates = _load_templates()
    t = _find_template(templates, template_id)
    sections = t.get("sections", [])
    if not sections:
        raise HTTPException(400, "Template has no sections to apply")

    from app.sow_section_routes import seed_project_sections

    seed_project_sections(project_id, sections, template_id=template_id)
    logger.info("Applied SOW template %s (%s) to project %s — %d sections",
                template_id, t["name"], project_id, len(sections))
    return {
        "ok": True, "template_id": template_id, "template_name": t["name"],
        "project_id": project_id, "sections_set": len(sections),
    }


@router.post("/api/sow-templates/apply-fallback/{project_id}")
def apply_fallback_sections(project_id: str):
    """Seed a SOW project with the built-in fallback structure (used when no
    default/custom template exists yet)."""
    from app.sow_section_routes import seed_project_sections

    seed_project_sections(project_id, FALLBACK_SECTIONS, template_id=None)
    return {"ok": True, "template_id": None, "project_id": project_id,
            "sections_set": len(FALLBACK_SECTIONS)}


@router.get("/api/sow-templates/resolve/{project_id}")
def resolve_template_for_project(project_id: str):
    """Report which template (if any) is already applied to a project, and
    what would apply by default if none is. Lets the SOW template-selection
    UI show sensible pre-selection."""
    from app.sow_section_routes import get_project_template_id

    applied_id = get_project_template_id(project_id)
    default = get_default_template()
    return {
        "applied_template_id": applied_id,
        "default_template_id": default["id"] if default else None,
        "default_template_name": default["name"] if default else None,
        "has_any_template": bool(_load_templates()),
    }
