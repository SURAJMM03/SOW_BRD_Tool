"""
sow_review_routes.py — API for the legal baseline (US-02) and the mandatory
pre-signature review gate (US-03).

  Legal baseline
    GET    /api/sow/reference                     current baseline + history
    POST   /api/sow/reference                     install a new baseline
    GET    /api/sow/reference/download            the baseline .docx itself
    GET    /api/sow/reference/clauses             extracted clause list
    POST   /api/sow/reference/check/{project_id}  compare a draft, store flags

  SOW content checks (US-04 … US-10)
    POST   /api/sow/scope-check/{project_id}      run the content checks

  Review gate
    GET    /api/sow/review/{project_id}           checklist + answers + gate
    POST   /api/sow/review/{project_id}/checkpoint
    POST   /api/sow/review/{project_id}/finalize
    POST   /api/sow/review/{project_id}/reopen
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app import sow_reference as ref
from app import sow_review_gate as gate
from app import sow_content_checks as content_checks

logger = logging.getLogger("SOWReviewRoutes")
router = APIRouter(prefix="/api/sow", tags=["sow-review"])


def _actor(request: Request) -> str:
    """The signed-in user's email, for the audit fields. The identity
    middleware puts the host-provided user on request.state; falling back to ""
    (not an error) keeps these endpoints usable in a dev environment where no
    host identity header is set."""
    user = getattr(request.state, "user", None) or {}
    return user.get("email", "") if isinstance(user, dict) else ""


# ─────────────────────────────────────────────────────────────────────────────
# US-02 — legally reviewed baseline
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/reference")
def get_reference():
    return ref.reference_status()


@router.post("/reference")
async def upload_reference(
    request: Request,
    file: UploadFile = File(...),
    version: str = Form(...),
    effective_date: str = Form(...),
    notes: str = Form(""),
):
    """Install a new legally reviewed baseline. Any current one is superseded
    and its file removed (see sow_reference.SUPERSEDED_RETENTION)."""
    if not (file.filename or "").lower().endswith(".docx"):
        raise HTTPException(400, "The baseline must be a .docx file")
    data = await file.read()
    try:
        current = ref.store_reference(
            data, file.filename, version, effective_date,
            notes=notes, uploaded_by=_actor(request))
    except ref.ReferenceError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        logger.exception("Failed to store legal baseline")
        raise HTTPException(500, f"Could not read that .docx: {e}")
    return {"ok": True, "current": current}


@router.get("/reference/download")
def download_reference():
    status = ref.reference_status()
    if not status["has_reference"]:
        raise HTTPException(404, "No legal baseline has been filed yet")
    name = f"SOW_legal_baseline_{status['current']['version']}.docx"
    return FileResponse(
        ref.REFERENCE_DOCX, filename=name,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")


@router.get("/reference/clauses")
def get_reference_clauses():
    return {"clauses": ref.load_clauses()}


@router.post("/reference/check/{project_id}")
def check_against_reference(project_id: str):
    """Compare this project's drafted sections against the baseline and store
    the resulting flags on the review record, so the gate screen shows the
    checklist and the Legal flags side by side."""
    from app.sow_section_routes import get_sections_for_review

    sections = get_sections_for_review(project_id)
    result = ref.compare_sections(sections)
    if result["available"]:
        gate.set_legal_review_flags(project_id, result["flags"])
    return result


@router.post("/scope-check/{project_id}")
def run_scope_checks(project_id: str):
    """Verify the drafted SOW against US-04 (scope boundaries), US-05
    (acceptance and deemed acceptance), US-06 (change requests), US-07
    (commercial terms), US-08 (governance and escalation), US-09 (risk
    ownership) and US-10 (KPIs and SLAs), storing the findings on the review
    record. The route keeps its original path so existing links still work."""
    from app.sow_section_routes import get_sections_for_review

    from app.project_routes import PROJECTS

    sections = get_sections_for_review(project_id)
    # The client's real name lets the checks recognise "Acme" as the customer
    # party in a contacts table or a risk owner cell.
    client_name = (PROJECTS.get(project_id, {}) or {}).get("client", "") or ""
    result = content_checks.run_checks(sections, client_name=client_name)
    gate.set_scope_check_flags(project_id, result["flags"])
    return result


# ─────────────────────────────────────────────────────────────────────────────
# US-03 — mandatory review gate
# ─────────────────────────────────────────────────────────────────────────────

class CheckpointUpdate(BaseModel):
    checkpoint_id: str
    state: str                # validated | waived | na | open
    reason: str = ""
    approver: str = ""        # required for a waiver


@router.get("/review/{project_id}")
def get_review(project_id: str):
    summary = gate.review_summary(project_id)
    store = gate.load_review(project_id)
    summary["legal_review_flags"] = store.get("legal_review_flags", [])
    summary["scope_check_flags"] = store.get("scope_check_flags", [])
    return summary


@router.post("/review/{project_id}/checkpoint")
def set_checkpoint(project_id: str, req: CheckpointUpdate, request: Request):
    try:
        gate.set_checkpoint(project_id, req.checkpoint_id, req.state,
                            reason=req.reason, approver=req.approver,
                            actor=_actor(request))
    except gate.ReviewError as e:
        raise HTTPException(400, str(e))
    return gate.review_summary(project_id)


@router.post("/review/{project_id}/finalize")
def finalize_sow(project_id: str, request: Request):
    try:
        gate.finalize(project_id, actor=_actor(request))
    except gate.ReviewError as e:
        # 409: the request is well-formed, the SOW's state just forbids it.
        raise HTTPException(409, str(e))
    return gate.review_summary(project_id)


@router.post("/review/{project_id}/reopen")
def reopen_sow(project_id: str, request: Request):
    try:
        gate.reopen(project_id, actor=_actor(request))
    except gate.ReviewError as e:
        raise HTTPException(409, str(e))
    return gate.review_summary(project_id)
