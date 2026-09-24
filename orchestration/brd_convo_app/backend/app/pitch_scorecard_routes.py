"""
pitch_scorecard_routes.py — API for the pre-SOW pitch deck scorecard.

The gate is ADVISORY. Nothing here can stop a SOW being drafted; the page's
"Continue to Template" is always live. These endpoints only produce, edit and
export an opinion about the deck.

Every write returns the whole recomputed scorecard rather than an acknowledgement,
so one round trip both saves the edit and gives the page its new totals. The
client never recomputes the weighted total itself — two implementations of the
same arithmetic drift, and the server's is the one the workbook is built from.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import pitch_scorecard as ps
from app import pitch_scorecard_llm as llm
from app import pitch_scorecard_xlsx as px

logger = logging.getLogger("PitchScorecardRoutes")

router = APIRouter(prefix="/api/sow/pitch-score", tags=["sow-pitch-score"])

XLSX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


def _actor(request: Request) -> str:
    """The signed-in user's email, for the audit fields. The identity middleware
    puts the host-provided user on request.state; falling back to "" keeps these
    endpoints usable in a dev environment with no identity header set."""
    user = getattr(request.state, "user", None) or {}
    return user.get("email", "") if isinstance(user, dict) else ""


def _project_name(project_id: str) -> str:
    try:
        from app.project_routes import PROJECTS
        return (PROJECTS.get(project_id) or {}).get("name") or project_id
    except Exception:
        return project_id


# ── Models ───────────────────────────────────────────────────────────────────
class RunRequest(BaseModel):
    # Re-index the deck even if it hasn't changed.
    force: bool = False
    # Off by default: a re-score must not silently discard a reviewer's own
    # judgement. The page warns before turning this on.
    overwrite_human: bool = False


class CriterionUpdate(BaseModel):
    criterion_id: str
    rating: Optional[str] = None
    comment: Optional[str] = None
    to_improve: Optional[str] = None
    slides: Optional[List[Dict]] = None


class WeightUpdate(BaseModel):
    weights: Dict[str, float]


# ── Read ─────────────────────────────────────────────────────────────────────
@router.get("/{project_id}")
def get_scorecard(project_id: str):
    """The rubric merged with whatever has been rated so far."""
    try:
        return ps.summary(project_id)
    except RuntimeError as exc:  # rubric failed to load — a deployment problem
        logger.exception("Pitch rubric unavailable")
        raise HTTPException(status_code=500, detail=str(exc))


# ── Scoring run ──────────────────────────────────────────────────────────────
@router.post("/{project_id}/run")
def start_scoring(project_id: str, req: RunRequest, request: Request):
    try:
        return llm.start_run(project_id, force=req.force,
                             overwrite_human=req.overwrite_human,
                             actor=_actor(request))
    except RuntimeError as exc:
        # Already running — the page should poll rather than start a second one.
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/{project_id}/status")
def scoring_status(project_id: str, since: int = Query(0, ge=0)):
    return llm.run_status(project_id, since=since)


@router.post("/{project_id}/stop")
def stop_scoring(project_id: str):
    return llm.stop_run(project_id)


# ── Reviewer edits ───────────────────────────────────────────────────────────
@router.put("/{project_id}/criterion")
def update_criterion(project_id: str, req: CriterionUpdate, request: Request):
    try:
        return ps.set_criterion(
            project_id, req.criterion_id, rating=req.rating, comment=req.comment,
            to_improve=req.to_improve, slides=req.slides, actor=_actor(request))
    except ps.ScorecardError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/{project_id}/weights")
def update_weights(project_id: str, req: WeightUpdate, request: Request):
    try:
        return ps.set_weights(project_id, req.weights, actor=_actor(request))
    except ps.ScorecardError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Export ───────────────────────────────────────────────────────────────────
@router.get("/{project_id}/export.xlsx")
def export_xlsx(project_id: str):
    """Build the workbook in memory and stream it.

    Deliberately not written to app/static/exports/ like the DOCX exports are:
    that directory is served by the /static mount, so anything in it is
    retrievable by anyone who can guess the filename, with no project scoping. A
    pitch scorecard carries a named reviewer's candid assessment of a live deal.

    Building on GET also means the bytes always match the screen — the score
    changes with every edit, so a file on disk would be a snapshot that quietly
    goes stale.
    """
    summary = ps.summary(project_id)
    buf = px.build_workbook(summary)
    filename = px.suggested_filename(_project_name(project_id))
    return StreamingResponse(
        buf,
        media_type=XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
