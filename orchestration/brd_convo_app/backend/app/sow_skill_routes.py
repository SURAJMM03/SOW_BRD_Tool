"""
sow_skill_routes.py — Assisted skill-learning: analyze real, signed sample
SOWs (kept in a "sow_samples" repository, see repository_routes.py's `kind`
field) and propose an edit to SOW_SKILL.md — the live system prompt behind
every generated/reviewed SOW — for a human to review and approve before it
is applied. Follows SOW_SKILL.md's own documented Section 5 ("Improving
this skill's accuracy over time"): minimal, specific edits plus a version
bump and changelog entry, never a wholesale rewrite, and never applied
without a human clicking Apply.

Endpoints:
  POST /api/sow/skill/analyze   {repo_id} -> {summary, proposed_text}
  POST /api/sow/skill/apply     {new_text} -> writes SOW_SKILL.md
  GET  /api/sow/skill/current   -> current file text
"""
from __future__ import annotations

import importlib
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.claude_provider import completion_from_prompt

logger = logging.getLogger("SOWSkillRoutes")
router = APIRouter(prefix="/api/sow/skill", tags=["sow-skill"])

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SKILL_PATH = _REPO_ROOT / "SOW_SKILL.md"

# Cap how much sample-SOW text goes into one analysis call — several real
# SOWs' worth of chunks is plenty of signal without risking a truncated call.
MAX_SAMPLE_CHARS = 30000


class AnalyzeRequest(BaseModel):
    repo_id: str


class AnalyzeResult(BaseModel):
    summary: str
    proposed_text: str


class ApplyRequest(BaseModel):
    new_text: str


@router.post("/analyze", response_model=AnalyzeResult)
def analyze_sow_samples(req: AnalyzeRequest):
    from app.repository_routes import REPOSITORIES, load_chunks_for_repos

    if req.repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")

    chunks = load_chunks_for_repos([req.repo_id])
    if not chunks:
        raise HTTPException(
            400,
            "This repository has no indexed content yet — upload sample SOWs and wait for "
            "chunking to finish before analyzing.",
        )

    sample_text = ""
    for c in chunks:
        piece = f"\n\n---\n[{c.get('doc_name', '')} — {c.get('section_heading', '')}]\n{c.get('text', '')}"
        if len(sample_text) + len(piece) > MAX_SAMPLE_CHARS:
            break
        sample_text += piece

    if not _SKILL_PATH.exists():
        raise HTTPException(500, "SOW_SKILL.md not found on the server")
    current_skill = _SKILL_PATH.read_text(encoding="utf-8")

    prompt = f"""You are helping Bristlecone improve the SOW_SKILL.md file below, which is the
live system prompt that drives every generated/reviewed Statement of Work. Follow this file's
own Section 5 ("Improving this skill's accuracy over time"): compare the real, signed sample
SOWs below against the current skill file, and propose the MINIMAL set of specific edits — a
new phrasing pattern, a missing checklist item, a structural tweak the samples reveal — never
a wholesale rewrite. Do not change the Section 0 operating principles or the RFP/locator/
table-discipline rules added in v0.3.0 unless a sample directly and clearly contradicts them.

Bump the `version` field in the frontmatter (a patch bump, e.g. 0.3.0 -> 0.3.1) and add exactly
ONE new line at the very top of the `## Changelog` section describing what changed and why, in
the same style as the existing changelog entries.

Return your answer in exactly two parts, in this order, using these exact markers on their own line:

## PROPOSED CHANGES SUMMARY
(a short bullet list a human reviewer can read in 30 seconds — what you are changing and why,
citing which sample(s) justified each change)

## FULL UPDATED SKILL FILE
(the ENTIRE new SOW_SKILL.md file content, ready to save as-is — the complete file, not a diff)

--- CURRENT SOW_SKILL.md ---
{current_skill}

--- SAMPLE SOWs (real, signed engagements) ---
{sample_text}
"""

    output = completion_from_prompt(
        prompt,
        max_tokens=16000,
        temperature=0.1,
        max_output_cap=16000,
    )

    marker = "## FULL UPDATED SKILL FILE"
    if marker not in output:
        raise HTTPException(500, "Could not parse a proposed skill file from the model's response")
    summary, _, proposed_text = output.partition(marker)
    summary = summary.replace("## PROPOSED CHANGES SUMMARY", "").strip()
    proposed_text = proposed_text.strip()
    if not proposed_text:
        raise HTTPException(500, "Model returned an empty proposed skill file")

    return AnalyzeResult(summary=summary, proposed_text=proposed_text)


@router.post("/apply")
def apply_sow_skill_update(req: ApplyRequest):
    if not req.new_text or not req.new_text.strip():
        raise HTTPException(400, "New skill text is empty")

    _SKILL_PATH.write_text(req.new_text, encoding="utf-8")

    # The section-generation and one-shot SOW routes each cache SOW_SKILL_TEXT
    # once at import time — refresh both so the change takes effect without a
    # server restart.
    for modname in ("app.sow_routes", "app.sow_section_routes"):
        try:
            mod = importlib.import_module(modname)
            mod.SOW_SKILL_TEXT = req.new_text
        except Exception as exc:
            logger.warning("Could not reload SOW_SKILL_TEXT in %s: %s", modname, exc)

    logger.info("SOW_SKILL.md updated via assisted skill-learning workflow (%d chars)", len(req.new_text))
    return {"ok": True}


@router.get("/current")
def get_current_skill():
    if not _SKILL_PATH.exists():
        raise HTTPException(500, "SOW_SKILL.md not found on the server")
    return {"text": _SKILL_PATH.read_text(encoding="utf-8")}
