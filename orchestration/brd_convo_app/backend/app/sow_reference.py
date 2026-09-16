"""
sow_reference.py — Legally reviewed baseline SOW (US-02)

The reference SOW is the copy Legal has already corrected. Its purpose is
negative: it stops an author reintroducing a compliance gap that Legal closed
once already. So this module does three things — store exactly one current
baseline in a known place, date it, and compare drafted clauses against it.

KNOWN LOCATION
──────────────
    app/sow_reference/
        manifest.json        the current version, its date, who filed it,
                             and the history of what it superseded
        reference.docx       the current baseline document itself
        clauses.json         its clauses, extracted by heading, for comparison

One directory, one current file, fixed names. `GET /api/sow/reference`
reports the path and version, so "where is the approved wording?" has a
single answer that does not depend on someone's inbox.

EXACTLY ONE CURRENT COPY
────────────────────────
Uploading a new baseline overwrites `reference.docx` and deletes any previous
file, because a superseded baseline that is still readable is exactly how a
retired clause gets copied forward. What survives supersession is the
manifest's `history`: version, date, filename and when it was replaced — an
audit trail of what the baseline used to be, without leaving the old text
sitting there to be reused. `SUPERSEDED_RETENTION` is the one knob: set it
above 0 to keep that many prior files under `superseded/` if your retention
policy requires the documents themselves.

DEVIATION FLAGGING
──────────────────
A drafted section is matched to a reference clause by normalised heading
title, then compared with difflib. Below `DEVIATION_THRESHOLD` similarity the
section is flagged for Legal review. Two extra rules catch the cases raw
similarity misses:
  * a clause in the baseline with no counterpart section at all is flagged as
    `missing` — the highest-value signal here, since deleting a clause Legal
    added is the exact failure this story exists to prevent;
  * `PROTECTED_PHRASES` are fragments Legal's corrections turned on (caps,
    indemnity, liability, IP …). If the baseline clause contains one and the
    draft does not, the section is flagged regardless of overall similarity —
    a 400-word clause can score 0.95 while having dropped the one sentence
    that mattered.

Flagging is advisory: it tells the reviewer where to look, and the review
gate (US-03) records what they decided. It never edits the draft.
"""
from __future__ import annotations

import difflib
import io
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("SOWReference")

_THIS_DIR = Path(__file__).parent
REFERENCE_DIR = _THIS_DIR / "sow_reference"
MANIFEST_PATH = REFERENCE_DIR / "manifest.json"
REFERENCE_DOCX = REFERENCE_DIR / "reference.docx"
CLAUSES_PATH = REFERENCE_DIR / "clauses.json"
SUPERSEDED_DIR = REFERENCE_DIR / "superseded"

# Prior baseline FILES to retain. 0 = none (the default): superseded copies
# are removed, per US-02. The manifest history is kept regardless.
SUPERSEDED_RETENTION = 0

# Below this difflib ratio a drafted clause is treated as a deviation.
DEVIATION_THRESHOLD = 0.72

# Fragments that carry legal weight. Present in the baseline clause but
# absent from the draft => flag, whatever the similarity score says.
PROTECTED_PHRASES: List[str] = [
    "indemnif", "limitation of liability", "liability cap", "not to exceed",
    "intellectual property", "work made for hire", "confidential",
    "data protection", "personal data", "gdpr", "termination for convenience",
    "governing law", "dispute resolution", "warranty", "sole remedy",
    "force majeure", "insurance", "audit right", "subcontract",
    "non-solicit", "background ip", "acceptance is deemed",
]

_HEADING_STYLES = {"Heading 1", "Heading 2", "Heading 3", "Heading 4", "Heading 5"}
# Leading clause numbering: "13.", "13.2", "Appendix A —", "Section 4:"
_NUM_PREFIX_RE = re.compile(
    r'^\s*(?:section\s+|clause\s+)?(?:\d+(?:\.\d+)*|[ivxlc]+|appendix\s+[a-z])'
    r'[\.\)\:\-—–\s]+', re.I)
_WS_RE = re.compile(r'\s+')


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalise_title(title: str) -> str:
    """Heading text reduced to a comparison key: numbering stripped, cased
    down, punctuation-insensitive. '13.2 Rate Card & Pyramid Structure' and
    'Rate Card and Pyramid Structure' both become 'rate card and pyramid
    structure', so a baseline and a draft that number their sections
    differently still match on subject."""
    t = _NUM_PREFIX_RE.sub("", title or "")
    t = t.replace("&", " and ")
    t = re.sub(r'[^\w\s]', " ", t)
    return _WS_RE.sub(" ", t).strip().lower()


def _normalise_body(text: str) -> str:
    """Whitespace- and case-insensitive body text for similarity scoring, so
    reformatting alone never reads as a legal deviation."""
    return _WS_RE.sub(" ", (text or "")).strip().lower()


# ─────────────────────────────────────────────────────────────────────────────
# Manifest / storage
# ─────────────────────────────────────────────────────────────────────────────

def _empty_manifest() -> Dict:
    return {"current": None, "history": []}


def load_manifest() -> Dict:
    if not MANIFEST_PATH.exists():
        return _empty_manifest()
    try:
        m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("Unreadable reference manifest — treating as empty")
        return _empty_manifest()
    base = _empty_manifest()
    base.update(m)
    return base


def _save_manifest(m: Dict) -> None:
    REFERENCE_DIR.mkdir(exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(m, indent=2, ensure_ascii=False),
                             encoding="utf-8")


def load_clauses() -> List[Dict]:
    if not CLAUSES_PATH.exists():
        return []
    try:
        return json.loads(CLAUSES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


class ReferenceError(ValueError):
    """A baseline operation the store refuses."""


def extract_clauses(docx_bytes: bytes) -> List[Dict]:
    """Split the baseline .docx into clauses at heading boundaries.

    Tables are flattened to pipe-delimited rows so a clause whose substance
    lives in a table (a liability cap, a payment schedule) still has body
    text to compare — otherwise those clauses would look empty and every
    draft would match them trivially.
    """
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(docx_bytes))
    clauses: List[Dict] = []
    current: Optional[Dict] = None
    order = 0

    def flush():
        if current and (current["text"].strip() or current["title"].strip()):
            clauses.append(current)

    body = doc.element.body
    for child in body:
        tag = child.tag.split("}")[-1]
        if tag == "p":
            p = Paragraph(child, doc)
            style = p.style.name if p.style else ""
            text = p.text.strip()
            if style in _HEADING_STYLES:
                flush()
                order += 1
                current = {"order": order, "title": text,
                           "key": _normalise_title(text), "text": ""}
                continue
            if current is None or not text:
                continue
            current["text"] += (("\n" if current["text"] else "") + text)
        elif tag == "tbl" and current is not None:
            tbl = Table(child, doc)
            for row in tbl.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    current["text"] += (("\n" if current["text"] else "")
                                        + " | ".join(cells))
    flush()
    return clauses


def store_reference(docx_bytes: bytes, source_filename: str, version: str,
                    effective_date: str, notes: str = "",
                    uploaded_by: str = "") -> Dict:
    """Install a new legally reviewed baseline, superseding any current one.

    `effective_date` is the date Legal signed the document off — supplied
    rather than inferred, because the upload date and the review date are
    routinely weeks apart and the review date is the one that matters.
    """
    version = (version or "").strip()
    effective_date = (effective_date or "").strip()
    if not version:
        raise ReferenceError("A version label is required (e.g. 'v2.1')")
    if not effective_date:
        raise ReferenceError("A Legal review / effective date is required")
    try:
        datetime.strptime(effective_date, "%Y-%m-%d")
    except ValueError:
        raise ReferenceError("Effective date must be ISO format, YYYY-MM-DD")

    clauses = extract_clauses(docx_bytes)
    if not clauses:
        raise ReferenceError(
            "No headings found in that .docx — the baseline must use Word "
            "heading styles so its clauses can be identified")

    REFERENCE_DIR.mkdir(exist_ok=True)
    manifest = load_manifest()
    previous = manifest.get("current")

    # Retire the outgoing baseline before writing the new one, so there is
    # never a moment with two current-looking files in the directory.
    if previous and REFERENCE_DOCX.exists():
        if SUPERSEDED_RETENTION > 0:
            SUPERSEDED_DIR.mkdir(exist_ok=True)
            keep = SUPERSEDED_DIR / f"{previous['version']}_{previous['stored_at'][:10]}.docx"
            shutil.move(str(REFERENCE_DOCX), str(keep))
            existing = sorted(SUPERSEDED_DIR.glob("*.docx"),
                              key=lambda p: p.stat().st_mtime, reverse=True)
            for stale in existing[SUPERSEDED_RETENTION:]:
                stale.unlink()
                logger.info("Removed superseded baseline beyond retention: %s",
                            stale.name)
        else:
            REFERENCE_DOCX.unlink()
            logger.info("Removed superseded baseline %s (%s)",
                        previous.get("version"), previous.get("source_filename"))

    if previous:
        manifest["history"].append({**previous, "superseded_at": _now(),
                                    "superseded_by_version": version,
                                    "file_retained": SUPERSEDED_RETENTION > 0})

    REFERENCE_DOCX.write_bytes(docx_bytes)
    CLAUSES_PATH.write_text(json.dumps(clauses, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    manifest["current"] = {
        "version": version,
        "effective_date": effective_date,
        "source_filename": source_filename,
        "notes": (notes or "").strip(),
        "uploaded_by": uploaded_by,
        "stored_at": _now(),
        "clause_count": len(clauses),
        "path": str(REFERENCE_DOCX),
    }
    _save_manifest(manifest)
    logger.info("Legal baseline %s (effective %s) stored — %d clauses, superseded %s",
                version, effective_date, len(clauses),
                previous.get("version") if previous else "nothing")
    return manifest["current"]


def reference_status() -> Dict:
    """What baseline is in force, and where it lives."""
    manifest = load_manifest()
    current = manifest.get("current")
    return {
        "has_reference": bool(current) and REFERENCE_DOCX.exists(),
        "current": current,
        "location": str(REFERENCE_DIR),
        "document_path": str(REFERENCE_DOCX) if REFERENCE_DOCX.exists() else None,
        "superseded_count": len(manifest.get("history", [])),
        "superseded_files_retained": SUPERSEDED_RETENTION,
        "history": manifest.get("history", []),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Deviation detection
# ─────────────────────────────────────────────────────────────────────────────

def _missing_protected_phrases(ref_text: str, draft_text: str) -> List[str]:
    r, d = ref_text.lower(), draft_text.lower()
    return [p for p in PROTECTED_PHRASES if p in r and p not in d]


def compare_sections(sections: List[Dict]) -> Dict:
    """Compare drafted sections against the baseline's clauses.

    `sections` is [{id, title, content}, …] — the project's drafted/approved
    text. Returns the flags plus the counts behind them, so the caller can
    show "12 of 24 clauses matched" rather than only the failures.
    """
    clauses = load_clauses()
    manifest = load_manifest()
    current = manifest.get("current")
    if not clauses or not current:
        return {"available": False, "flags": [], "compared": 0,
                "matched": 0, "reference_version": None,
                "reference_effective_date": None}

    by_key: Dict[str, Dict] = {}
    for c in clauses:
        if c["key"]:
            by_key.setdefault(c["key"], c)

    flags: List[Dict] = []
    matched = 0
    compared = 0
    seen_keys = set()

    for sec in sections:
        # A pure container heading ("3. Project Overview", whose text lives in
        # 3.1/3.2) never carries body content by design — comparing it would
        # report an empty clause on every well-formed SOW.
        if not sec.get("has_own_content", True):
            continue
        content = (sec.get("content") or "").strip()
        key = _normalise_title(sec.get("title", ""))
        ref = by_key.get(key)
        if not ref:
            # A section the baseline says nothing about is not a deviation —
            # every SOW carries project-specific material Legal never saw.
            continue
        seen_keys.add(key)
        compared += 1

        if not content:
            flags.append({
                "kind": "empty",
                "severity": "high",
                "section_id": sec.get("id"),
                "section_title": sec.get("title"),
                "clause_title": ref["title"],
                "similarity": 0.0,
                "missing_phrases": [],
                "detail": "This clause exists in the legal baseline but the SOW "
                          "section has no content.",
            })
            continue

        ratio = difflib.SequenceMatcher(
            None, _normalise_body(ref["text"]), _normalise_body(content)).ratio()
        missing = _missing_protected_phrases(ref["text"], content)

        if missing:
            flags.append({
                "kind": "protected_language_missing",
                "severity": "high",
                "section_id": sec.get("id"),
                "section_title": sec.get("title"),
                "clause_title": ref["title"],
                "similarity": round(ratio, 3),
                "missing_phrases": missing,
                "detail": "Language Legal relies on is present in the baseline "
                          "clause but absent here: " + ", ".join(missing),
            })
        elif ratio < DEVIATION_THRESHOLD:
            flags.append({
                "kind": "wording_deviation",
                "severity": "medium",
                "section_id": sec.get("id"),
                "section_title": sec.get("title"),
                "clause_title": ref["title"],
                "similarity": round(ratio, 3),
                "missing_phrases": [],
                "detail": f"Wording differs substantially from the baseline "
                          f"clause ({ratio:.0%} similar, threshold "
                          f"{DEVIATION_THRESHOLD:.0%}).",
            })
        else:
            matched += 1

    # Baseline clauses with no counterpart at all — the dropped-clause case.
    for c in clauses:
        if c["key"] and c["key"] not in seen_keys and c["text"].strip():
            flags.append({
                "kind": "missing",
                "severity": "high",
                "section_id": None,
                "section_title": None,
                "clause_title": c["title"],
                "similarity": 0.0,
                "missing_phrases": [],
                "detail": "The legal baseline contains this clause and the SOW "
                          "has no matching section.",
            })

    order = {"high": 0, "medium": 1, "low": 2}
    flags.sort(key=lambda f: (order.get(f["severity"], 9), f["clause_title"]))

    return {
        "available": True,
        "reference_version": current["version"],
        "reference_effective_date": current["effective_date"],
        "clause_count": len(clauses),
        "compared": compared,
        "matched": matched,
        "flags": flags,
        "threshold": DEVIATION_THRESHOLD,
    }
