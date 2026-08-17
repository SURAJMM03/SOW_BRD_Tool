"""
sow_routes.py — Statement of Work (SOW) Review, powered by SOW_SKILL.md

A standalone tool, reachable from the SOW Tool's own hub (/sow-hub) — it has
no dependency on a BRD project existing. Isolation is keyed by a client-generated
`session_id` (any non-empty string; not pre-registered anywhere server-side),
not a project_id.

Endpoints:
  POST /api/sow/upload    Upload an SOW document to review.
                          Stored under its own sow_uploads/ tree — never touches
                          the BRD upload tree or chunking pipeline.
  POST /api/sow/review    Review an existing SOW against the Bristlecone checklist.
  GET  /api/sow/history   List past review results for a session (survives refresh).
  GET  /api/sow/result/{id}  Fetch one past result's full text.
  POST /api/sow/export    Render the last review output to a real .docx.

review loads SOW_SKILL.md as the system prompt for the model call (see
claude_provider.completion_from_prompt's system_prompt param) and accepts either
an uploaded file_id (extracted via chunking_pipeline.extract_text_from_file,
read-only — no chunking/indexing side effects) or raw pasted text.

This module is intentionally independent of the BRD generation pipeline
(project_routes.py, screen3_routes.py, brd_workflow.py) — the only things
reused from that side are plain constants (VALID_SOURCE_EXTS, MAX_FILE_SIZE)
and, for .docx export, the already-generic chat_export.render helper.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app import chunking_pipeline
from app.claude_provider import completion_from_prompt
from app.project_routes import VALID_SOURCE_EXTS, MAX_FILE_SIZE
from app.sow_export import render_sow_docx

logger = logging.getLogger("SOWRoutes")

router = APIRouter(prefix="/api/sow", tags=["sow"])

# Own storage root — independent of project_routes.UPLOAD_ROOT (the BRD upload tree).
SOW_UPLOAD_ROOT = Path(__file__).parent / "sow_uploads"
SOW_UPLOAD_ROOT.mkdir(exist_ok=True)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# SKILL FILE — read once at import time
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# sow_routes.py lives at: <repo_root>/orchestration/brd_convo_app/backend/app/sow_routes.py
_REPO_ROOT = Path(__file__).resolve().parents[4]
_SKILL_PATH = _REPO_ROOT / "SOW_SKILL.md"
try:
    SOW_SKILL_TEXT = _SKILL_PATH.read_text(encoding="utf-8")
    logger.info("Loaded SOW_SKILL.md (%d chars) from %s", len(SOW_SKILL_TEXT), _SKILL_PATH)
except Exception as exc:
    SOW_SKILL_TEXT = ""
    logger.error("Could not load SOW_SKILL.md from %s: %s", _SKILL_PATH, exc)

PURPOSES = ("review_document",)

# The 101-checkpoint REVIEW report is a long, table-heavy document — 8000 was
# too tight a starting budget (real risk of a silent cut-off; see
# claude_provider.py's truncation safety net, which will auto-escalate once
# more, up to 2x, if this still isn't enough).
SOW_MAX_OUTPUT_TOKENS = 12000

# Uploaded files for this feature live under SOW_UPLOAD_ROOT, entirely separate
# from the BRD upload tree (project_routes.UPLOAD_ROOT), so they can never be
# mistaken for BRD source material and never trigger the chunking thread.
_SOW_FILES: dict[str, dict] = {}


def _sanitize_session_id(session_id: str) -> str:
    safe = "".join(c for c in (session_id or "").strip() if c.isalnum() or c in "-_")
    if not safe:
        raise HTTPException(400, "Invalid session_id")
    return safe


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RESULT PERSISTENCE — every review output is saved server-side, keyed by
# the browser-minted session_id, and survives a page refresh or a backend
# restart. Before this, a completed review lived only in a JS variable in
# the browser tab — reload the page and it was gone for good.
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
_RESULTS_FILE = SOW_UPLOAD_ROOT / "sow_results.json"
SOW_RESULTS: dict[str, dict] = {}
_MAX_RESULTS_PER_SESSION = 20  # oldest are pruned beyond this — this is a scratch
                                # history, not an audit trail; no pagination needed yet.


def _save_results_state():
    try:
        _RESULTS_FILE.write_text(json.dumps(SOW_RESULTS, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("Could not persist SOW results state: %s", exc)


def _load_results_state():
    if _RESULTS_FILE.exists():
        try:
            data = json.loads(_RESULTS_FILE.read_text(encoding="utf-8"))
            SOW_RESULTS.update(data)
            logger.info("Loaded %d saved SOW results from state", len(SOW_RESULTS))
        except Exception as exc:
            logger.warning("Could not load SOW results state: %s", exc)


_load_results_state()


def _save_result(session_id: str, mode: str, output: str, source_label: str) -> str:
    result_id = str(uuid4())[:8]
    now = datetime.utcnow().isoformat()
    SOW_RESULTS[result_id] = {
        "id": result_id,
        "session_id": session_id,
        "mode": mode,  # always "review" now; older entries may still say "generate"
        "output": output,
        "source_label": source_label,
        "created_at": now,
    }
    # Prune this session's older entries beyond the cap so the store doesn't
    # grow unbounded over a long-lived browser tab.
    same_session = sorted(
        (r for r in SOW_RESULTS.values() if r["session_id"] == session_id),
        key=lambda r: r["created_at"], reverse=True,
    )
    for stale in same_session[_MAX_RESULTS_PER_SESSION:]:
        SOW_RESULTS.pop(stale["id"], None)
    _save_results_state()
    return result_id


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MODELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class SOWFileOut(BaseModel):
    file_id: str
    session_id: str
    purpose: str
    name: str
    size: int


class SOWReviewRequest(BaseModel):
    session_id: str
    file_id: str | None = None
    text: str | None = None


class SOWResult(BaseModel):
    ok: bool
    mode: str
    output: str
    result_id: str | None = None


class SOWHistoryItem(BaseModel):
    id: str
    mode: str
    source_label: str
    created_at: str
    preview: str


class SOWExportRequest(BaseModel):
    text: str


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# UPLOAD — mirrors project_routes.upload_file's validation/storage pattern
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.post("/upload", response_model=SOWFileOut)
async def upload_sow_file(
    file: UploadFile = File(...),
    session_id: str = Form(...),
    purpose: str = Form(...),
):
    """
    Upload an SOW document to review. Stored under
    sow_uploads/{session_id}/{purpose}/ — its own tree, independent of any
    BRD project and never touching the BRD chunking pipeline.
    """
    if not session_id or not session_id.strip():
        raise HTTPException(400, "session_id is required")
    if purpose not in PURPOSES:
        raise HTTPException(400, f"purpose must be one of: {', '.join(PURPOSES)}")

    safe_name = Path(file.filename or "").name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(400, "Invalid filename")

    ext = Path(safe_name).suffix.lower()
    if ext not in VALID_SOURCE_EXTS:
        raise HTTPException(400, f"Invalid file type '{ext}'. Accepted: {', '.join(sorted(VALID_SOURCE_EXTS))}")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit")

    safe_session = _sanitize_session_id(session_id)

    sow_dir = SOW_UPLOAD_ROOT / safe_session / purpose
    sow_dir.mkdir(parents=True, exist_ok=True)
    file_path = (sow_dir / safe_name).resolve()
    if not str(file_path).startswith(str(sow_dir.resolve())):
        raise HTTPException(400, "Invalid filename")
    with open(file_path, "wb") as f:
        f.write(content)

    file_id = str(uuid4())[:8]
    _SOW_FILES[file_id] = {
        "id": file_id,
        "session_id": safe_session,
        "purpose": purpose,
        "name": safe_name,
        "size": len(content),
        "disk_path": str(file_path),
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    logger.info("SOW upload: %s → session=%s purpose=%s (%d bytes)",
                safe_name, safe_session, purpose, len(content))

    return SOWFileOut(file_id=file_id, session_id=safe_session, purpose=purpose,
                       name=safe_name, size=len(content))


def _resolve_input_text(
    session_id: str,
    file_id: str | None,
    text: str | None,
) -> str:
    """Return the raw text to feed the model, from an uploaded file or pasted text."""
    if file_id:
        record = _SOW_FILES.get(file_id)
        if not record or record["session_id"] != session_id:
            raise HTTPException(404, "SOW file not found for this session")
        path = Path(record["disk_path"])
        if not path.exists():
            raise HTTPException(404, "Uploaded file is missing on disk")
        blocks = chunking_pipeline.extract_text_from_file(path)
        joined = "\n\n".join(b.get("text", "") for b in blocks if b.get("text"))
        if not joined.strip():
            raise HTTPException(400, "Could not extract any text from the uploaded file")
        return joined
    if text and text.strip():
        return text.strip()
    raise HTTPException(400, "Provide file_id or text")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REVIEW — SOW_SKILL.md Mode 1
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.post("/review", response_model=SOWResult)
async def review_sow(req: SOWReviewRequest):
    if not SOW_SKILL_TEXT:
        raise HTTPException(500, "SOW_SKILL.md could not be loaded on the server")

    session_id = _sanitize_session_id(req.session_id)
    sow_text = _resolve_input_text(session_id, req.file_id, req.text)

    user_message = f"""MODE: REVIEW

Follow this skill's Mode 1: REVIEW instructions exactly:
- Apply the four operating principles in Section 0 to every check (cite verbatim quotes,
  one verdict per check, use "Not Addressed" instead of guessing when the document is silent,
  tag every finding High/Medium/Low confidence).
- Run the full 11-category, 101-checkpoint audit checklist in Section 2, using only the
  checkpoints that actually apply (skip 2.11 unless this is one of the five named precedent
  engagements), and marking any checkpoint "Cannot Verify" if a needed companion document
  (e.g. the MSA, a CR log, a Risk Register, sign-off evidence) wasn't provided.
- Produce the report in the exact structure given in Section 3 (Summary, Category-by-Category
  Findings, Audit Summary Scorecard with overall Green/Amber/Red rating, Top Risks,
  Contradictions Found, Open Questions for the Team, Cannot Verify — Evidence Needed).
- Perform the Section 0 Rule 5 self-verification pass before finalizing your answer.

SOW document to review:

---
{sow_text}
---

Produce the full SOW Review Report now.
"""

    output = completion_from_prompt(
        user_message,
        system_prompt=SOW_SKILL_TEXT,
        max_tokens=SOW_MAX_OUTPUT_TOKENS,
        temperature=0.2,
        max_output_cap=SOW_MAX_OUTPUT_TOKENS,
    )
    result_id = _save_result(session_id, "review", output, "SOW document review")
    return SOWResult(ok=True, mode="review", output=output, result_id=result_id)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HISTORY — recover a past review result for this session (survives
# a page refresh; see the RESULT PERSISTENCE block above).
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.get("/history", response_model=list[SOWHistoryItem])
async def list_sow_history(session_id: str):
    safe_session = _sanitize_session_id(session_id)
    items = [r for r in SOW_RESULTS.values() if r["session_id"] == safe_session]
    items.sort(key=lambda r: r["created_at"], reverse=True)
    out = []
    for r in items:
        preview = " ".join((r.get("output") or "").split())[:140]
        out.append(SOWHistoryItem(
            id=r["id"], mode=r["mode"], source_label=r.get("source_label", ""),
            created_at=r["created_at"], preview=preview,
        ))
    return out


@router.get("/result/{result_id}", response_model=SOWResult)
async def get_sow_result(result_id: str, session_id: str):
    safe_session = _sanitize_session_id(session_id)
    r = SOW_RESULTS.get(result_id)
    if not r or r["session_id"] != safe_session:
        raise HTTPException(404, "Result not found for this session")
    return SOWResult(ok=True, mode=r["mode"], output=r["output"], result_id=r["id"])


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# EXPORT — render the last review output to .docx
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
@router.post("/export")
async def export_sow(req: SOWExportRequest):
    if not req.text or not req.text.strip():
        raise HTTPException(400, "Nothing to export — text is empty")
    path = render_sow_docx(req.text, "SOW Review Report")
    # Reuses the existing generic download route in screen3_routes.py, which
    # already serves from this same static/exports/ directory.
    return {"ok": True, "docx_url": f"/api/document/download/docx/{path.name}"}
