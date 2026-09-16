"""
sow_review_gate.py — Mandatory pre-signature review gate (US-03)

A SOW cannot reach "final" while any compliance checkpoint is still open.
Every checkpoint has to be explicitly resolved as validated, waived or not
applicable; a waiver additionally needs a named approver, so nothing is
silently written off.

WHERE THE CHECKLIST COMES FROM
──────────────────────────────
SOW_SKILL.md is already the single source of truth for the review checklist —
sections "### 2.N <name> (N checkpoints)", each followed by a Markdown table
of `| # | Checkpoint | Precedent SOW | Risk |`. Parsing that file rather than
re-typing the checkpoints here means Legal can edit one document and the gate
follows; there is no second copy to drift. 101 checkpoints across 11
categories at the time of writing, and the parser cross-checks each table's
row count against the count declared in its own heading so a malformed edit
fails loudly instead of silently dropping checkpoints.

Category 2.11 is marked "conditional" in the skill file (it only applies to
some SOWs). It is still surfaced and still has to be resolved — "not
applicable, because …" is the correct resolution for a conditional category
that does not apply, and that is a decision worth recording rather than
assuming.

REASONS
───────
`REASON_REQUIRED_STATES` decides which resolutions must carry a written
reason. It defaults to waived + not-applicable: those are the two where a
human is overriding or excusing a control and the justification is the whole
audit trail. "Validated" is evidenced by the SOW text itself, so a reason is
optional there — requiring free text on all 101 to finalize one document
would push reviewers toward copy-pasting "ok", which is worse than nothing.
If your process wants a reason on every checkpoint, add "validated" to the
set — the API and UI pick it up with no other change.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger("SOWReviewGate")

_THIS_DIR = Path(__file__).parent
# SOW_SKILL.md lives at the repo root, four levels up from app/.
_SKILL_PATH = _THIS_DIR.parents[3] / "SOW_SKILL.md"

STATE_OPEN = "open"
STATE_VALIDATED = "validated"
STATE_WAIVED = "waived"
STATE_NA = "na"

RESOLVED_STATES: Set[str] = {STATE_VALIDATED, STATE_WAIVED, STATE_NA}
ALL_STATES: Set[str] = RESOLVED_STATES | {STATE_OPEN}

# See the REASONS note in the module docstring.
REASON_REQUIRED_STATES: Set[str] = {STATE_WAIVED, STATE_NA}
# Only a waiver needs a named human to stand behind it.
APPROVER_REQUIRED_STATES: Set[str] = {STATE_WAIVED}

STATUS_DRAFT = "draft"
STATUS_FINAL = "final"

_CATEGORY_RE = re.compile(
    r'^### (2\.\d+) ([^(\n]+?)\s*\((\d+) checkpoints\)(.*)$', re.M)
_ROW_RE = re.compile(
    r'^\|\s*(\d+)\s*\|([^|]*)\|([^|]*)\|([^|]*)\|\s*$', re.M)

_CHECKLIST_CACHE: Optional[List[Dict]] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_checklist(force: bool = False) -> List[Dict]:
    """Parse SOW_SKILL.md into a flat, ordered checkpoint list.

    Each checkpoint id is "<category>.<row>" (e.g. "2.1.3") — stable as long
    as a row keeps its number within its category, which is what makes a
    project's saved resolutions survive an unrelated edit elsewhere in the
    file. Renumbering rows inside a category WILL re-point saved answers, so
    treat the numbering as an interface.
    """
    global _CHECKLIST_CACHE
    if _CHECKLIST_CACHE is not None and not force:
        return _CHECKLIST_CACHE

    if not _SKILL_PATH.exists():
        logger.error("SOW_SKILL.md not found at %s — review gate has no checklist",
                     _SKILL_PATH)
        _CHECKLIST_CACHE = []
        return _CHECKLIST_CACHE

    text = _SKILL_PATH.read_text(encoding="utf-8")
    checkpoints: List[Dict] = []

    for m in _CATEGORY_RE.finditer(text):
        cat_id, cat_name, declared, tail = (
            m.group(1), m.group(2).strip(), int(m.group(3)), m.group(4))
        nxt = text.find("\n### ", m.end())
        body = text[m.end(): nxt if nxt != -1 else len(text)]
        rows = _ROW_RE.findall(body)

        if len(rows) != declared:
            # Loud, not fatal: a gate built from a miscounted checklist would
            # pass a SOW that still has unreviewed controls.
            logger.error(
                "SOW_SKILL.md category %s declares %d checkpoints but %d table "
                "rows parsed — the review gate will use the %d it found",
                cat_id, declared, len(rows), len(rows))

        conditional = "conditional" in tail.lower()
        for num, cp_text, precedent, risk in rows:
            checkpoints.append({
                "id": f"{cat_id}.{num.strip()}",
                "category_id": cat_id,
                "category": cat_name,
                "number": int(num),
                "text": cp_text.strip(),
                "precedent": precedent.strip(),
                "risk": risk.strip(),
                "conditional": conditional,
            })

    logger.info("Review checklist loaded: %d checkpoints across %d categories",
                len(checkpoints), len({c["category_id"] for c in checkpoints}))
    _CHECKLIST_CACHE = checkpoints
    return checkpoints


# ─────────────────────────────────────────────────────────────────────────────
# Per-project review state
# ─────────────────────────────────────────────────────────────────────────────

def _review_path(project_id: str) -> Path:
    return _THIS_DIR / f"sow_review_{project_id}.json"


def _empty_store() -> Dict:
    return {"status": STATUS_DRAFT, "checkpoints": {},
            "finalized_at": None, "finalized_by": None,
            "legal_review_flags": [], "scope_check_flags": []}


def load_review(project_id: str) -> Dict:
    path = _review_path(project_id)
    if not path.exists():
        return _empty_store()
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Unreadable review store for %s — starting fresh", project_id)
        return _empty_store()
    base = _empty_store()
    base.update(store)
    return base


def save_review(project_id: str, store: Dict) -> None:
    _review_path(project_id).write_text(
        json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")


class ReviewError(ValueError):
    """A checkpoint resolution that the gate's own rules reject."""


def set_checkpoint(project_id: str, checkpoint_id: str, state: str,
                   reason: str = "", approver: str = "",
                   actor: str = "") -> Dict:
    """Record one checkpoint's resolution. Raises ReviewError on any rule
    breach so the caller can turn it into a 400 with the real reason."""
    if state not in ALL_STATES:
        raise ReviewError(
            f"Unknown state '{state}' — expected one of: {', '.join(sorted(ALL_STATES))}")

    valid_ids = {c["id"] for c in load_checklist()}
    if checkpoint_id not in valid_ids:
        raise ReviewError(f"Unknown checkpoint '{checkpoint_id}'")

    reason = (reason or "").strip()
    approver = (approver or "").strip()

    if state in REASON_REQUIRED_STATES and not reason:
        raise ReviewError(f"A reason is required when marking a checkpoint '{state}'")
    if state in APPROVER_REQUIRED_STATES and not approver:
        raise ReviewError("A waiver requires a named approver")

    store = load_review(project_id)
    if store["status"] == STATUS_FINAL:
        raise ReviewError(
            "This SOW is final — reopen it before changing checkpoint answers")

    if state == STATE_OPEN:
        store["checkpoints"].pop(checkpoint_id, None)
    else:
        store["checkpoints"][checkpoint_id] = {
            "state": state,
            "reason": reason,
            # Only a waiver carries an approver; keeping the field off the
            # other states stops a stale name implying sign-off that never
            # happened.
            "approver": approver if state in APPROVER_REQUIRED_STATES else "",
            "updated_at": _now(),
            "updated_by": actor,
        }
    save_review(project_id, store)
    return store


def unresolved_checkpoints(project_id: str) -> List[Dict]:
    """Every checkpoint with no recorded resolution, in checklist order."""
    store = load_review(project_id)
    answers = store.get("checkpoints", {})
    return [c for c in load_checklist()
            if answers.get(c["id"], {}).get("state") not in RESOLVED_STATES]


def review_summary(project_id: str) -> Dict:
    """Checklist + this project's answers + whether the gate would pass."""
    checklist = load_checklist()
    store = load_review(project_id)
    answers = store.get("checkpoints", {})

    counts = {STATE_VALIDATED: 0, STATE_WAIVED: 0, STATE_NA: 0, STATE_OPEN: 0}
    items = []
    for c in checklist:
        a = answers.get(c["id"], {})
        state = a.get("state", STATE_OPEN)
        if state not in RESOLVED_STATES:
            state = STATE_OPEN
        counts[state] += 1
        items.append({**c, "state": state,
                      "reason": a.get("reason", ""),
                      "approver": a.get("approver", ""),
                      "updated_at": a.get("updated_at"),
                      "updated_by": a.get("updated_by", "")})

    open_count = counts[STATE_OPEN]
    flags = store.get("legal_review_flags", [])
    scope_flags = store.get("scope_check_flags", [])
    return {
        "project_id": project_id,
        "status": store.get("status", STATUS_DRAFT),
        "finalized_at": store.get("finalized_at"),
        "finalized_by": store.get("finalized_by"),
        "total": len(checklist),
        "counts": counts,
        "open_count": open_count,
        "can_finalize": open_count == 0 and len(checklist) > 0,
        "legal_review_flag_count": len(flags),
        "scope_check_flag_count": len(scope_flags),
        "reason_required_states": sorted(REASON_REQUIRED_STATES),
        "approver_required_states": sorted(APPROVER_REQUIRED_STATES),
        "checkpoints": items,
    }


def finalize(project_id: str, actor: str = "") -> Dict:
    """Move the SOW to final. Refuses while any checkpoint is unresolved."""
    checklist = load_checklist()
    if not checklist:
        raise ReviewError(
            "The review checklist could not be loaded — refusing to finalize a "
            "SOW that has not actually been checked")

    store = load_review(project_id)
    if store["status"] == STATUS_FINAL:
        raise ReviewError("This SOW is already final")

    outstanding = unresolved_checkpoints(project_id)
    if outstanding:
        raise ReviewError(
            f"{len(outstanding)} of {len(checklist)} checkpoints are still open — "
            "every one must be validated, waived or marked not applicable first")

    store["status"] = STATUS_FINAL
    store["finalized_at"] = _now()
    store["finalized_by"] = actor
    save_review(project_id, store)
    logger.info("SOW %s finalized by %s", project_id, actor or "unknown")
    return store


def reopen(project_id: str, actor: str = "") -> Dict:
    """Take a final SOW back to draft so it can be corrected. Deliberately
    separate from finalize so returning to draft is its own audited act."""
    store = load_review(project_id)
    if store["status"] != STATUS_FINAL:
        raise ReviewError("This SOW is not final")
    store["status"] = STATUS_DRAFT
    store["reopened_at"] = _now()
    store["reopened_by"] = actor
    save_review(project_id, store)
    logger.info("SOW %s reopened by %s", project_id, actor or "unknown")
    return store


def is_final(project_id: str) -> bool:
    return load_review(project_id).get("status") == STATUS_FINAL


def set_legal_review_flags(project_id: str, flags: List[Dict]) -> None:
    """Store the clause deviations found against the legal baseline (US-02).
    Kept in the review store so the gate screen shows the checklist and the
    Legal flags together — they are the same review conversation."""
    store = load_review(project_id)
    store["legal_review_flags"] = flags
    save_review(project_id, store)


def set_scope_check_flags(project_id: str, flags: List[Dict]) -> None:
    """Store the US-04..US-10 content findings (sow_content_checks) so the gate
    screen shows scope, acceptance and change-control gaps in the same place
    as the checklist and the legal deviations."""
    store = load_review(project_id)
    store["scope_check_flags"] = flags
    save_review(project_id, store)
