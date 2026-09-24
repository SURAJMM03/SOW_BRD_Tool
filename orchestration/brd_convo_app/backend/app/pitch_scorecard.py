"""
pitch_scorecard.py — the Pitch Deck Scorecard rubric, its maths, and its store.

A deck uploaded for a SOW project is scored against PITCH_SKILL.md before the SOW
is drafted from it. This module owns three things and nothing else:

  1. Parsing PITCH_SKILL.md into a rubric (14 categories, 57 criteria).
  2. The scoring maths, which must agree exactly with the team's spreadsheet.
  3. The per-project answer store, and the validated writer a reviewer's edit
     goes through.

The LLM pass lives in pitch_scorecard_llm.py and the workbook writer in
pitch_scorecard_xlsx.py, so both can be tested without touching either.

WHY THE ROW RANGES MATTER. The exported workbook's summary sheet averages fixed
cell ranges ('Pitch Checklist'!I4:I7 for category 1, and so on). Those ranges are
positional: they only mean "category 1's criteria" because category 1 happens to
occupy rows 4-7. Adding a criterion to the skill file shifts every later category
by a row, and a summary that averages the wrong rows is wrong *silently* — the
numbers still look plausible. So the ranges are derived from the parsed rubric and
then checked against the geometry the workbook was built with, and a mismatch is
fatal rather than logged. See _EXPECTED_ROW_SPANS.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("PitchScorecard")

_THIS_DIR = Path(__file__).parent
# backend/app -> backend -> brd_convo_app -> orchestration -> <repo root>
_SKILL_PATH = _THIS_DIR.parents[3] / "PITCH_SKILL.md"

# ── Ratings ──────────────────────────────────────────────────────────────────
RATING_BEST = "Best Practice"
RATING_MIN = "Minimally Meets"
RATING_FAIL = "Does Not Meet"
RATING_NA = "Not Applicable"

VALID_RATINGS: Tuple[str, ...] = (RATING_BEST, RATING_MIN, RATING_FAIL, RATING_NA)

RATING_SCORES: Dict[str, Optional[int]] = {
    RATING_BEST: 2,
    RATING_MIN: 1,
    RATING_FAIL: 0,
    RATING_NA: None,  # excluded from the average, exactly as Excel skips "—"
}

# The workbook's column H carries emoji-prefixed forms, and an LLM will produce
# casing and spacing variants of its own. Normalise once, here, and nowhere else.
EXCEL_RATINGS: Dict[str, str] = {
    RATING_BEST: "✅ Best Practice",
    RATING_MIN: "⚠️ Minimally Meets",
    RATING_FAIL: "❌ Does Not Meet",
    RATING_NA: "Not Applicable",
}

EVIDENCE_STATES = ("found", "absent", "unassessable")
CONFIDENCE_LEVELS = ("high", "medium", "low")

SOURCE_AI = "ai"
SOURCE_HUMAN = "human"

# Band thresholds, mirroring the workbook's B20 formula.
BAND_READY = "✅ SUBMISSION-READY (≥80%)"
BAND_WORK = "⚠️ NEEDS WORK (60-79%) — address High/Critical gaps"
BAND_NOT_READY = "❌ NOT READY (<60%) — major rework required"
BAND_AWAITING = "— awaiting ratings —"

# ── Parsing ──────────────────────────────────────────────────────────────────
# "### 4. PARTNERSHIPS (4 criteria, weight 0.10) — category may be N/A"
_CATEGORY_RE = re.compile(
    r"^### (\d+)\. (.+?) \((\d+) criteria, weight ([\d.]+)\)(.*)$", re.M)
# A criteria-table row. The leading \d+ is what makes the |---|---| separator
# unmatchable; [^|\n] (not [^|]) stops a malformed row swallowing the next line.
_ROW_RE = re.compile(
    r"^\|\s*(\d+)\s*\|([^|\n]*)\|([^|\n]*)\|([^|\n]*)\|([^|\n]*)\|\s*$", re.M)

# The geometry the exported workbook is built around: (first_row, last_row) per
# category on the "Pitch Checklist" sheet. Read off the source workbook's own
# B-column vertical merges, which are its record of which rows belong to which
# category. Criteria occupy rows 4-60; 57 of them.
_EXPECTED_ROW_SPANS: Tuple[Tuple[int, int], ...] = (
    (4, 7), (8, 10), (11, 14), (15, 18), (19, 23), (24, 29), (30, 34),
    (35, 38), (39, 42), (43, 46), (47, 50), (51, 54), (55, 57), (58, 60),
)
_FIRST_CRITERION_ROW = 4

_RUBRIC_CACHE: Optional[Dict] = None


class ScorecardError(ValueError):
    """A rejected write — surfaced to the caller as HTTP 400."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_rating(value: Optional[str]) -> Optional[str]:
    """Accept the emoji, plain and loosely-cased forms; return the canonical one.

    Returns None for anything unrecognised so callers can decide whether that is
    a validation error (a human edit) or a dropped row (a model's answer).
    """
    if not value:
        return None
    text = str(value).strip()
    for canonical in VALID_RATINGS:
        if text == canonical or text == EXCEL_RATINGS[canonical]:
            return canonical
    # Strip any leading emoji/punctuation and compare case-insensitively.
    bare = re.sub(r"^[^\w]+", "", text).strip().lower()
    for canonical in VALID_RATINGS:
        if bare == canonical.lower():
            return canonical
    if bare in ("n/a", "na", "not applicable"):
        return RATING_NA
    return None


def load_rubric(force: bool = False) -> Dict:
    """Parse PITCH_SKILL.md into {categories: [...], criteria_index: {...}}.

    Raises RuntimeError if the file is missing or its geometry has drifted from
    the workbook's — see the module docstring for why that is fatal.
    """
    global _RUBRIC_CACHE
    if _RUBRIC_CACHE is not None and not force:
        return _RUBRIC_CACHE

    if not _SKILL_PATH.exists():
        raise RuntimeError(
            f"PITCH_SKILL.md not found at {_SKILL_PATH} — the pitch scorecard "
            "has no rubric and cannot score anything.")

    text = _SKILL_PATH.read_text(encoding="utf-8")
    categories: List[Dict] = []

    for m in _CATEGORY_RE.finditer(text):
        cat_no, name, declared, weight, tail = (
            int(m.group(1)), m.group(2).strip(), int(m.group(3)),
            float(m.group(4)), m.group(5))
        nxt = text.find("\n### ", m.end())
        body = text[m.end(): nxt if nxt != -1 else len(text)]
        rows = _ROW_RE.findall(body)

        if len(rows) != declared:
            raise RuntimeError(
                f"PITCH_SKILL.md category {cat_no} ({name}) declares {declared} "
                f"criteria but {len(rows)} table rows parsed. Fix the heading or "
                "the table — a miscounted rubric silently mis-scores every deck.")

        criteria = []
        for num, crit, fail, minimal, best in rows:
            criteria.append({
                "id": f"{cat_no}.{int(num)}",
                "number": int(num),
                "category_no": cat_no,
                "category": name,
                "name": crit.strip(),
                "anchors": {
                    RATING_FAIL: fail.strip(),
                    RATING_MIN: minimal.strip(),
                    RATING_BEST: best.strip(),
                },
            })

        categories.append({
            "no": cat_no,
            "name": name,
            "weight": weight,
            "may_be_na": "may be n/a" in tail.lower(),
            "description": _extract_description(body),
            "criteria": criteria,
        })

    if not categories:
        raise RuntimeError(
            f"PITCH_SKILL.md at {_SKILL_PATH} parsed to zero categories — the "
            "category heading format has changed and _CATEGORY_RE no longer matches.")

    _assign_row_spans(categories)
    _verify_geometry(categories)

    rubric = {
        "categories": categories,
        "criteria_index": {c["id"]: c for cat in categories for c in cat["criteria"]},
        "operating_rules": _extract_section(text, "## 0. Operating rules"),
        "response_contract": _extract_section(text, "## 3. Response contract"),
    }
    logger.info("Pitch rubric loaded: %d categories, %d criteria",
                len(categories), len(rubric["criteria_index"]))
    _RUBRIC_CACHE = rubric
    return rubric


def _extract_description(body: str) -> str:
    """The '**What this category covers:**' block, up to the criteria table."""
    m = re.search(r"\*\*What this category covers:\*\*\s*(.*?)(?=\n\|)", body, re.S)
    return m.group(1).strip() if m else ""


def _extract_section(text: str, heading: str) -> str:
    """A '## ' section verbatim, for pasting into a prompt."""
    start = text.find(heading)
    if start == -1:
        return ""
    nxt = text.find("\n## ", start + len(heading))
    return text[start: nxt if nxt != -1 else len(text)].strip()


def _assign_row_spans(categories: List[Dict]) -> None:
    """Derive each category's workbook row range by walking the rubric in order."""
    row = _FIRST_CRITERION_ROW
    for cat in categories:
        cat["row_start"] = row
        cat["row_end"] = row + len(cat["criteria"]) - 1
        row = cat["row_end"] + 1


def _verify_geometry(categories: List[Dict]) -> None:
    """Fail loudly if the rubric no longer matches the exported workbook's layout."""
    spans = tuple((c["row_start"], c["row_end"]) for c in categories)
    if spans != _EXPECTED_ROW_SPANS:
        raise RuntimeError(
            "PITCH_SKILL.md no longer matches the workbook layout.\n"
            f"  expected row spans: {_EXPECTED_ROW_SPANS}\n"
            f"  derived row spans:  {spans}\n"
            "Criteria were added, removed or reordered. The exported summary "
            "sheet averages fixed cell ranges, so it would silently average the "
            "wrong rows. Update _EXPECTED_ROW_SPANS together with the workbook "
            "template, deliberately.")

    total = sum(len(c["criteria"]) for c in categories)
    if total != 57:
        raise RuntimeError(f"Pitch rubric has {total} criteria, expected 57.")

    weight_sum = round(sum(c["weight"] for c in categories), 6)
    if weight_sum != 1.0:
        raise RuntimeError(
            f"Pitch rubric weights total {weight_sum}, expected 1.00. Fix the "
            "weights on the category headings in PITCH_SKILL.md.")


# ── Store ────────────────────────────────────────────────────────────────────
def _store_path(project_id: str) -> Path:
    return _THIS_DIR / f"pitch_score_{project_id}.json"


def _empty_store() -> Dict:
    return {
        "deck_fingerprint": None,
        "deck_docs": [],
        "slide_counts": {},
        "digest": None,
        "answers": {},
        "weights": {},
        "scored_at": None,
        "actor": "",
        "run_notes": [],
        "deck_parts": 1,
        # Criteria a scoring run reached but could not rate — a model failure,
        # not a criterion nobody has got to yet. The two look identical in the
        # score (both excluded from the average) and must not look identical to
        # the reviewer, who has to supply a judgement for these.
        "failed_criteria": [],
    }


def load_store(project_id: str) -> Dict:
    path = _store_path(project_id)
    if not path.exists():
        return _empty_store()
    try:
        store = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("pitch_score_%s.json unreadable — starting empty", project_id)
        return _empty_store()
    base = _empty_store()
    base.update(store)
    return base


def save_store(project_id: str, store: Dict) -> None:
    _store_path(project_id).write_text(
        json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8")


def effective_weights(rubric: Dict, store: Dict) -> Dict[int, float]:
    """Category weights, with any reviewer override applied."""
    overrides = store.get("weights") or {}
    out = {}
    for cat in rubric["categories"]:
        raw = overrides.get(str(cat["no"]), overrides.get(cat["no"]))
        out[cat["no"]] = float(raw) if raw is not None else float(cat["weight"])
    return out


# ── Scoring maths ────────────────────────────────────────────────────────────
def compute_scores(rubric: Dict, answers: Dict, weights: Dict[int, float]) -> Dict:
    """Roll criterion ratings up to a weighted total, the workbook's way.

    Mirrors the spreadsheet exactly: a category's average is taken over its rated,
    non-N/A criteria — Excel's AVERAGE skips both the "—" that N/A produces and
    the blanks that unrated criteria leave.

    The two kinds of exclusion are NOT equivalent, though, and this is the one
    place the app is deliberately more careful than the spreadsheet:

      * A category every criterion of which is N/A does not apply to this deal.
        Its weight drops to 0 and the remaining weights are renormalised to 1.00,
        so the percentage stays comparable across deals. That is the workbook's
        own instruction, done mechanically instead of by hand.

      * A category nobody has rated yet is simply unfinished. It keeps its weight
        and contributes 0, so a half-scored deck reads as low rather than
        flatteringly high, and `complete` stays False to say why.
    """
    rows, applicable, unrated_total = [], [], 0

    for cat in rubric["categories"]:
        scores, na, unrated = [], 0, 0
        for crit in cat["criteria"]:
            rating = (answers.get(crit["id"]) or {}).get("rating")
            rating = normalize_rating(rating)
            if rating is None:
                unrated += 1
            elif rating == RATING_NA:
                na += 1
            else:
                scores.append(RATING_SCORES[rating])

        total_criteria = len(cat["criteria"])
        all_na = na == total_criteria and total_criteria > 0
        none_rated = unrated == total_criteria and total_criteria > 0
        unrated_total += unrated

        avg = (sum(scores) / len(scores)) if scores else None
        rows.append({
            "no": cat["no"],
            "name": cat["name"],
            "declared_weight": float(cat["weight"]),
            "weight": weights[cat["no"]],
            "avg": avg,
            "score_pct": (avg / 2) if avg is not None else 0.0,
            "criteria_count": total_criteria,
            "rated_count": len(scores),
            "na_count": na,
            "unrated_count": unrated,
            "all_na": all_na,
            "none_rated": none_rated,
        })
        if not all_na and weights[cat["no"]] > 0:
            applicable.append(cat["no"])

    # Renormalise across the categories that still apply, so N/A doesn't shrink
    # the total. Guard the degenerate case where a reviewer N/As everything.
    applicable_weight = sum(r["weight"] for r in rows if r["no"] in applicable)
    for r in rows:
        if r["no"] in applicable and applicable_weight > 0:
            r["effective_weight"] = r["weight"] / applicable_weight
        else:
            r["effective_weight"] = 0.0
        r["weighted"] = r["score_pct"] * r["effective_weight"]

    total = sum(r["weighted"] for r in rows)
    return {
        "categories": rows,
        "total": total,
        "band": band_for(total),
        "unrated_count": unrated_total,
        "complete": unrated_total == 0,
        "applicable_weight": applicable_weight,
    }


def band_for(total: float) -> str:
    """The workbook's B20 ladder."""
    if total >= 0.8:
        return BAND_READY
    if total >= 0.6:
        return BAND_WORK
    if total > 0:
        return BAND_NOT_READY
    return BAND_AWAITING


# ── Validated writes ─────────────────────────────────────────────────────────
def set_criterion(project_id: str, criterion_id: str, rating: Optional[str] = None,
                  comment: Optional[str] = None, to_improve: Optional[str] = None,
                  slides: Optional[List[Dict]] = None, actor: str = "") -> Dict:
    """Apply a reviewer's edit to one criterion and persist it.

    Only the fields actually supplied are touched, so editing a comment does not
    silently clear the AI's citations. Any edit stamps the row as human-authored,
    which is what protects it from being overwritten by a later re-score.
    """
    rubric = load_rubric()
    if criterion_id not in rubric["criteria_index"]:
        raise ScorecardError(f"Unknown criterion '{criterion_id}'.")

    store = load_store(project_id)
    answer = dict(store["answers"].get(criterion_id) or {})

    if rating is not None:
        canonical = normalize_rating(rating)
        if canonical is None:
            raise ScorecardError(
                f"'{rating}' is not a valid rating. Expected one of: "
                + ", ".join(VALID_RATINGS))
        answer["rating"] = canonical
        answer["score"] = RATING_SCORES[canonical]

    if comment is not None:
        answer["comment"] = str(comment)
    if to_improve is not None:
        answer["to_improve"] = str(to_improve)
    if slides is not None:
        answer["slides"] = _clean_slides(slides)

    answer["source"] = SOURCE_HUMAN
    answer["updated_at"] = _now()
    answer["updated_by"] = actor or ""

    store["answers"][criterion_id] = answer
    save_store(project_id, store)
    return summary(project_id)


def set_weights(project_id: str, weights: Dict, actor: str = "") -> Dict:
    """Override category weights (e.g. zeroing an N/A category)."""
    rubric = load_rubric()
    valid = {c["no"] for c in rubric["categories"]}
    cleaned = {}
    for key, value in (weights or {}).items():
        try:
            cat_no = int(key)
            weight = float(value)
        except (TypeError, ValueError):
            raise ScorecardError(f"Weight for '{key}' must be a number.")
        if cat_no not in valid:
            raise ScorecardError(f"Unknown category number {cat_no}.")
        if weight < 0:
            raise ScorecardError("Weights cannot be negative.")
        cleaned[str(cat_no)] = weight

    store = load_store(project_id)
    store["weights"] = cleaned
    store["actor"] = actor or store.get("actor", "")
    save_store(project_id, store)
    return summary(project_id)


def _clean_slides(slides) -> List[Dict]:
    """Coerce citations to [{doc, slide}], tolerating bare ints from older callers."""
    out = []
    for entry in slides or []:
        if isinstance(entry, dict):
            slide = entry.get("slide")
            doc = str(entry.get("doc") or "")
        else:
            slide, doc = entry, ""
        try:
            slide = int(slide)
        except (TypeError, ValueError):
            continue
        out.append({"doc": doc, "slide": slide})
    return out


# ── Read model ───────────────────────────────────────────────────────────────
def summary(project_id: str) -> Dict:
    """The whole scorecard: rubric definition merged with saved answers.

    Every write returns this, so one round trip both persists an edit and gives
    the page the recomputed totals — the client never does the maths itself.
    """
    rubric = load_rubric()
    store = load_store(project_id)
    weights = effective_weights(rubric, store)
    scores = compute_scores(rubric, store["answers"], weights)
    by_no = {r["no"]: r for r in scores["categories"]}
    failed = set(store.get("failed_criteria") or [])

    categories = []
    for cat in rubric["categories"]:
        row = by_no[cat["no"]]
        criteria = []
        for crit in cat["criteria"]:
            answer = store["answers"].get(crit["id"]) or {}
            rating = normalize_rating(answer.get("rating"))
            criteria.append({
                "id": crit["id"],
                "number": crit["number"],
                "name": crit["name"],
                "anchors": crit["anchors"],
                "rating": rating,
                "score": RATING_SCORES.get(rating) if rating else None,
                "comment": answer.get("comment", ""),
                "to_improve": answer.get("to_improve", ""),
                "quote": answer.get("quote", ""),
                "slides": answer.get("slides", []),
                "evidence_status": answer.get("evidence_status", ""),
                "confidence": answer.get("confidence", ""),
                "source": answer.get("source", ""),
                "updated_at": answer.get("updated_at", ""),
                "updated_by": answer.get("updated_by", ""),
                # True only when a run tried and failed — needs a human verdict.
                "scoring_failed": crit["id"] in failed and not rating,
            })
        categories.append({
            **row,
            "description": cat["description"],
            "may_be_na": cat["may_be_na"],
            # The workbook writer needs these: its summary sheet averages fixed
            # cell ranges, and they are derived from the rubric, not hard-coded.
            "row_start": cat["row_start"],
            "row_end": cat["row_end"],
            "criteria": criteria,
        })

    return {
        "project_id": project_id,
        "total": scores["total"],
        "total_pct": round(scores["total"] * 100, 1),
        "band": scores["band"],
        "complete": scores["complete"],
        "unrated_count": scores["unrated_count"],
        "categories": categories,
        "deck_docs": store.get("deck_docs", []),
        "slide_counts": store.get("slide_counts", {}),
        "scored_at": store.get("scored_at"),
        "run_notes": store.get("run_notes", []),
        "criteria_total": len(rubric["criteria_index"]),
        "deck_parts": store.get("deck_parts", 1),
        "failed_count": len([c for c in failed
                             if not normalize_rating(
                                 (store["answers"].get(c) or {}).get("rating"))]),
    }
