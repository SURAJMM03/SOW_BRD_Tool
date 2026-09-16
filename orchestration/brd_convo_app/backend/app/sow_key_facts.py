"""
sow_key_facts.py — One-time, project-wide extraction of the concrete facts a
SOW needs (dates, client, duration, locations, commercials …) from the uploaded
source deck(s).

WHY THIS EXISTS
───────────────
Per-section retrieval (sow_section_routes._search_chunks) picks the top-k
chunks whose *topic* matches a section's guidance text. That works well for
prose sections ("describe the implementation approach") but fails reliably for
short, factual, administrative details, because those facts are usually stated
once, in passing, on a slide whose overall topic is something else entirely.

Measured on a real 145-chunk deck: the slide reading "June 2026 kickoff to Feb
2028 Go-live" — i.e. literally the project's start and end dates — ranked 50th
of 60 for the "Agreement Details" section query, far below that section's
top_k=6 window. The dates were extracted and chunked correctly; retrieval
simply never showed them to the model, so the model emitted a <CONFIRM: …>
placeholder and the reviewer saw "the tool can't find the dates". Typing the
dates into a section's "optional instructions" box worked only because it
smuggled the answer into the prompt by hand.

The fix is to stop making these facts compete in a topical similarity race.
Once per project we run a dedicated extraction pass that:
  1. gathers candidates with SEVERAL fact-shaped queries (not one section
     query), so date/duration/location vocabulary actually surfaces its own
     chunks, and
  2. asks the model once for a strict-JSON fact sheet,
then injects that fact sheet into EVERY section's prompt. A section no longer
has to win a retrieval lottery to know when the project starts.

Results are cached per project and keyed by a fingerprint of the underlying
chunks, so this costs one LLM call per project (not per section), and
automatically refreshes when new source material is uploaded.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("SOWKeyFacts")

_THIS_DIR = Path(__file__).parent


def _facts_path(pid: str) -> Path:
    return _THIS_DIR / f"sow_facts_{pid}.json"


# ─────────────────────────────────────────────────────────────────────────────
# Retrieval probes
#
# One query per fact family, each written in the vocabulary the SOURCE deck
# would use rather than the vocabulary a contract would use. This is the whole
# trick: a deck says "June 2026 kickoff to Feb 2028 Go-live", it does not say
# "SOW Effective Date". A query built from contract language ("Agreement
# Details, Contract/SOW ID, Governing Agreement, Amendment/Supersedes") is
# semantically far from that sentence, which is exactly why the section-level
# query missed it. These probes deliberately speak deck-language.
# ─────────────────────────────────────────────────────────────────────────────
_PROBES: List[str] = [
    "project timeline start date kickoff go-live date end date duration "
    "months schedule phases milestones hypercare cutover wave release",
    "client name customer company legal entity account engagement project name "
    "proposal title response to RFP",
    "delivery locations onsite offshore geography regions countries sites "
    "plants offices time zone delivery centers",
    "scope in-scope out of scope modules systems applications integrations "
    "landscape environments instances",
    "commercials pricing cost effort estimate rate card resources team size "
    "FTE headcount pyramid engagement model fixed price time and materials",
    "governance steering committee reporting cadence escalation RACI roles "
    "responsibilities project manager stakeholders sponsor",
    "assumptions dependencies prerequisites exclusions constraints risks",
    "deliverables acceptance criteria sign-off milestones payment terms invoicing",
    "methodology approach phases activate explore realize deploy run "
    "implementation strategy conversion migration testing",
    "SLA support KPI performance metrics availability response resolution",
]

# Additional non-semantic sweep: chunks containing explicit date-ish or
# money-ish tokens are always worth showing the extractor, whatever their
# topical similarity score. Cheap insurance against an embedding miss on the
# single most-requested field.
_HARD_SIGNALS = re.compile(
    r"\b(20\d{2})\b"
    r"|\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*'?\d{2,4}\b"
    r"|\b(go[- ]?live|kick[- ]?off|hypercare|cutover|effective date|end date|"
    r"start date|commencement|duration|milestone)\b",
    re.I,
)

_FACT_SCHEMA = {
    "client_name": "Client / customer company name as written in the source.",
    "client_legal_entity": "Full legal entity name if stated (e.g. 'Alcon Inc.').",
    "project_name": "Name/title of the project or engagement.",
    "effective_start_date": (
        "When the engagement starts. Use the most specific form stated "
        "(e.g. 'June 2026'). Derive from kickoff/start/commencement wording."
    ),
    "end_date": (
        "When the engagement ends. Derive from go-live plus any hypercare/"
        "support tail if that is how the source expresses it (e.g. 'March 2028')."
    ),
    "duration": "Total duration if stated or directly derivable (e.g. '21 months').",
    "go_live_date": "Go-live / cutover date if distinct from the end date.",
    "key_milestones": "List of {name, date} for named milestones/phases.",
    "delivery_locations": "Onsite/offshore locations, regions, or sites.",
    "engagement_model": "Fixed price / T&M / managed service / hybrid, if stated.",
    "team_size": "Headcount / FTE / resource counts if stated.",
    "scope_summary": "One or two sentences: what is being delivered.",
    "in_scope": "List of explicitly in-scope items/systems/modules.",
    "out_of_scope": "List of explicitly excluded items.",
    "governing_agreement": "MSA / ISA / consulting agreement referenced, if any.",
    "contract_or_sow_id": "Contract or SOW identifier, if stated.",
    "commercials": "Pricing/cost/effort figures stated in the source.",
    "assumptions": "Key assumptions stated in the source.",
    "notable_dates": (
        "Any other concrete dates found, as {date, what_it_refers_to} — a "
        "catch-all so no date in the deck is silently dropped."
    ),
}


def _chunk_text(chunk: Dict) -> str:
    return chunk.get("chunk_text") or chunk.get("text") or ""


def _fingerprint(chunks: List[Dict]) -> str:
    """Cheap content fingerprint — changes when source material changes, so the
    cached fact sheet auto-invalidates after a new upload."""
    h = hashlib.sha256()
    h.update(str(len(chunks)).encode())
    for c in chunks[:400]:
        # chunk_id is an int in some pipelines and a str in others — coerce.
        h.update(str(c.get("chunk_id") or "").encode("utf-8", "ignore"))
        h.update(_chunk_text(c)[:200].encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def _gather_candidates(chunks: List[Dict], max_chunks: int = 60) -> List[Dict]:
    """Union of several fact-shaped probe searches plus a hard-signal sweep.

    Deliberately NOT a single top-k call — the whole failure mode this module
    exists to fix is that one topical query cannot surface every kind of fact
    at once.
    """
    from app.sow_section_routes import _search_chunks

    def _key(c: Dict) -> str:
        cid = c.get("chunk_id")
        return f"id:{cid}" if cid is not None else _chunk_text(c)[:120]

    picked: Dict[str, Dict] = {}
    order: List[str] = []

    # Pass 1 — chunks carrying explicit date/money signals always get a seat.
    for c in chunks:
        if _HARD_SIGNALS.search(_chunk_text(c)):
            k = _key(c)
            if k not in picked:
                picked[k] = c
                order.append(k)

    # Pass 2 — topical probes, round-robin so no single probe monopolises the
    # budget when the deck happens to be dense in one area.
    probe_hits: List[List[Dict]] = []
    for probe in _PROBES:
        try:
            probe_hits.append(_search_chunks(chunks, probe, top_k=6))
        except Exception as exc:
            logger.warning("Key-fact probe failed (%s): %s", probe[:40], exc)
            probe_hits.append([])

    for rank in range(6):
        for hits in probe_hits:
            if rank < len(hits):
                c = hits[rank]
                k = _key(c)
                if k not in picked:
                    picked[k] = c
                    order.append(k)

    # Pass 3 — the opening slides almost always carry title/client/date framing.
    for c in chunks[:5]:
        k = _key(c)
        if k not in picked:
            picked[k] = c
            order.append(k)

    return [picked[k] for k in order[:max_chunks]]


def _build_prompt(candidates: List[Dict], client_hint: str = "") -> str:
    schema_lines = "\n".join(f'  "{k}": {v}' for k, v in _FACT_SCHEMA.items())
    blocks = []
    for c in candidates:
        head = c.get("section_heading", "") or ""
        doc = c.get("doc_name", "source")
        blocks.append(f"[{doc} — {head}]\n{_chunk_text(c)[:1400]}")
    source = "\n\n".join(blocks)

    hint = f"\nThe project record lists the client as: {client_hint}\n" if client_hint else ""

    return f"""You are reading the source material for a professional Statement of Work
and extracting the concrete facts it contains, so that later drafting steps
never have to guess or ask the reviewer for them.
{hint}
Return ONLY a JSON object — no prose, no markdown fence — with exactly these keys:

{{
{schema_lines}
}}

RULES — read carefully, these determine whether the output is usable:
1. Use ONLY what the source below states or directly implies. Never invent.
2. DO derive a fact when the source expresses it in different words. The
   source will rarely use contract vocabulary. Examples of correct derivation:
     "June 2026 kickoff to Feb 2028 Go-live + 1-month Hypercare"
        -> effective_start_date: "June 2026"
        -> go_live_date: "February 2028"
        -> end_date: "March 2028 (Feb 2028 go-live + 1 month hypercare)"
     "Project Timeline includes 20 months of Technical Conversion and 1 month
      of Hypercare"  ->  duration: "21 months (20 conversion + 1 hypercare)"
   When you derive rather than quote, say so briefly in parentheses as shown.
3. If a fact genuinely is not present, use null (for a single value) or []
   (for a list). Do NOT write "not specified" / "TBD" / "N/A" as a string, and
   do not omit the key.
4. Prefer the most specific form stated. "Q1 2028" is fine if that is all the
   source says; do not sharpen it into a false exact date.
5. For every date you report, make sure it actually appears in, or follows
   arithmetically from, the source text.

SOURCE MATERIAL:
{source}

Return the JSON object now."""


_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def _parse_facts(raw: str) -> Optional[Dict]:
    txt = _FENCE_RE.sub("", (raw or "").strip())
    start, end = txt.find("{"), txt.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(txt[start:end + 1])
    except Exception as exc:
        logger.warning("Key-fact JSON parse failed: %s", exc)
        return None


def _is_empty(value) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        v = value.strip().lower()
        return v in ("", "null", "none", "n/a", "na", "tbd", "not specified",
                     "not stated", "unknown", "not provided")
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def extract_key_facts(project_id: str, chunks: List[Dict],
                      client_hint: str = "", force: bool = False) -> Dict:
    """Return the cached fact sheet for this project, extracting it if needed.

    Cheap and safe to call on every section generation: it only hits the LLM
    when there is no cache entry for the current chunk fingerprint.
    """
    if not chunks:
        return {}

    fp = _fingerprint(chunks)
    path = _facts_path(project_id)

    if not force and path.exists():
        try:
            cached = json.loads(path.read_text(encoding="utf-8"))
            if cached.get("fingerprint") == fp:
                return cached.get("facts") or {}
        except Exception:
            pass

    candidates = _gather_candidates(chunks)
    if not candidates:
        return {}

    from app.claude_provider import completion_from_prompt

    try:
        raw = completion_from_prompt(
            _build_prompt(candidates, client_hint),
            max_tokens=2000,
            temperature=0.0,
            max_output_cap=2000,
        )
    except Exception as exc:
        logger.warning("Key-fact extraction LLM call failed for %s: %s", project_id, exc)
        return {}

    facts = _parse_facts(raw)
    if facts is None:
        logger.warning("Key-fact extraction returned unparseable output for %s", project_id)
        return {}

    facts = {k: v for k, v in facts.items() if not _is_empty(v)}

    try:
        path.write_text(
            json.dumps({"fingerprint": fp, "facts": facts}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not cache key facts for %s: %s", project_id, exc)

    logger.info("Key facts extracted for %s: %d field(s) populated (%s)",
                project_id, len(facts), ", ".join(sorted(facts)) or "none")
    return facts


_LABELS = {
    "client_name": "Client",
    "client_legal_entity": "Client legal entity",
    "project_name": "Project name",
    "effective_start_date": "Effective / start date",
    "end_date": "End date",
    "duration": "Duration",
    "go_live_date": "Go-live date",
    "key_milestones": "Key milestones",
    "delivery_locations": "Delivery locations",
    "engagement_model": "Engagement model",
    "team_size": "Team size",
    "scope_summary": "Scope summary",
    "in_scope": "In scope",
    "out_of_scope": "Out of scope",
    "governing_agreement": "Governing agreement",
    "contract_or_sow_id": "Contract / SOW ID",
    "commercials": "Commercials",
    "assumptions": "Assumptions",
    "notable_dates": "Other dates found in the source",
}


# Per-field ceiling on the rendered fact sheet. This block goes into EVERY
# section's prompt, so an exhaustive in-scope list from a large deck would
# otherwise cost more context than the section's own retrieved source material.
# Generous enough to keep whole date/milestone lists intact (the fields this
# module exists for), tight enough that one sprawling list cannot dominate.
_MAX_FIELD_CHARS = 700


def _render_value(value) -> str:
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                parts.append(" — ".join(str(v) for v in item.values() if v))
            else:
                parts.append(str(item))
        rendered = "; ".join(p for p in parts if p)
    elif isinstance(value, dict):
        rendered = "; ".join(f"{k}: {v}" for k, v in value.items() if v)
    else:
        rendered = str(value)

    if len(rendered) > _MAX_FIELD_CHARS:
        # Trim on a separator so the last entry shown is never a half fact.
        cut = rendered.rfind("; ", 0, _MAX_FIELD_CHARS)
        rendered = rendered[: cut if cut > 0 else _MAX_FIELD_CHARS] + " … (truncated)"
    return rendered


def format_facts_for_context(facts: Dict) -> str:
    """Render the fact sheet as a high-priority prompt block.

    Presented as established fact rather than as retrieved "source material",
    because these were extracted from the source deliberately and should
    outrank whatever the per-section similarity search happened to return.
    """
    if not facts:
        return ""
    lines = [
        "ESTABLISHED PROJECT FACTS — extracted from this project's own source "
        "material. These are authoritative: use them directly wherever this "
        "section calls for them, and do NOT emit a <CONFIRM: …> placeholder "
        "for anything answered here. Only include the ones this section "
        "actually needs; do not dump the whole list into your answer.",
    ]
    for key, label in _LABELS.items():
        if key in facts and not _is_empty(facts[key]):
            rendered = _render_value(facts[key])
            if rendered.strip():
                lines.append(f"  - {label}: {rendered}")
    for key, value in facts.items():
        if key not in _LABELS and not _is_empty(value):
            rendered = _render_value(value)
            if rendered.strip():
                lines.append(f"  - {key.replace('_', ' ').title()}: {rendered}")
    return "\n".join(lines) if len(lines) > 1 else ""
