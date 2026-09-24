"""
pitch_scorecard_llm.py — score an uploaded deck against PITCH_SKILL.md.

THE RULE THIS MODULE IS BUILT AROUND: every criterion is judged against the
WHOLE deck. Nothing is sampled, retrieved, or truncated away.

That rule exists because of a specific, silent failure mode. An earlier design
retrieved the ten slides most relevant to each category and scored against
those. It reads as a sensible optimisation and it is not: most of this rubric's
criteria are answered by the ABSENCE of something ("no RACI anywhere", "no
pricing slide", "no client testimonial"). If the model only ever sees ten
slides, "I did not find it" means "it was not in the ten slides I was given" —
which is indistinguishable, in the output, from "it is not in the deck". The
scorecard then reports a confident, slide-cited gap that does not exist. On a
115-slide deck that approach hid about 93% of the text from every decision.

So: the whole deck goes into every category call. An 89-slide deck is ~150k
characters, roughly 40k tokens, which fits comfortably. Only when a deck genuinely
exceeds the context budget does the module fall back to splitting it — and then it
scores every category against EVERY part and merges, so no slide is ever unseen
for any criterion. The merge takes the best-evidenced verdict, because finding
evidence is positive proof while not finding it is only provisional.

Cost: 14 calls for a normal deck (one per category), plus one extra call per
category per additional part on an oversized deck. The digest pass that used to
run exists now only to give cross-part context in that fallback.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from app import pitch_scorecard as ps
from app.claude_provider import completion_from_prompt

logger = logging.getLogger("PitchScorecardLLM")

# ── Budget ───────────────────────────────────────────────────────────────────
# How much deck text may go into a single category prompt. Deliberately large:
# the whole point is that the model sees everything. ~3 chars per token is a
# conservative estimate for slide text with markup.
_CHARS_PER_TOKEN = 3
DECK_TOKEN_BUDGET = int(os.getenv("PITCH_DECK_TOKEN_BUDGET", "90000"))
_DECK_CHAR_BUDGET = DECK_TOKEN_BUDGET * _CHARS_PER_TOKEN

# Output budget scales with how many criteria a category has, so a six-criterion
# category is never cut off mid-JSON (which would lose the whole category).
_TOKENS_PER_CRITERION = 340
_OUTPUT_FLOOR = 1200
_OUTPUT_CEILING = 6000

_CATEGORY_ATTEMPTS = 3   # transient failure, then a stricter re-ask
_DIGEST_ATTEMPTS = 2

# Fallback-path digest only (see module docstring).
_DIGEST_WINDOW = 14
_DIGEST_SLIDE_CHARS = 2000
_DIGEST_MAX_TOKENS = 1800
_SIGNALS = (
    "exec-summary, client-context, pov, solution, architecture, methodology, "
    "timeline, team-roles, bios, raci, fte, governance, org-chart, pricing, "
    "rate-card, assumptions, exclusions, risk, mitigation, case-study, "
    "client-logos, testimonial-quote, partner-named, ai-tool, accelerator, "
    "quantified-metric, placeholder-text, dense-table, image-only, appendix"
)

# Ratings ordered weakest→strongest, for merging verdicts across deck parts.
_RATING_RANK = {
    ps.RATING_NA: -1,
    ps.RATING_FAIL: 0,
    ps.RATING_MIN: 1,
    ps.RATING_BEST: 2,
}

SCORING_FAILED = "scoring_failed"


# ── Deck assembly ────────────────────────────────────────────────────────────
def load_deck(project_id: str):
    """Return (slides_by_doc, slide_counts, deck_docs, notes, unpaged).

    Deliberately NOT sow_section_routes._load_all_project_chunks: that appends
    chunks from linked repositories, which would let shared boilerplate score as
    if it were in the client's deck.

    Unpaged material (a DOCX RFP, say) is kept rather than discarded — it cannot
    be cited by slide, but excluding it would let the model report content as
    missing when it is sitting in an uploaded document.
    """
    from app.project_routes import PROJECTS, UPLOAD_ROOT
    from app.chunking_pipeline import load_project_chunks
    from app.sow_section_routes import _chunk_text

    notes: List[str] = []
    if project_id not in PROJECTS:
        return {}, {}, [], ["Project not found."], {}

    chunks = load_project_chunks(UPLOAD_ROOT / project_id)
    if not chunks:
        return {}, {}, [], ["No indexed source documents — upload a deck first."], {}

    by_doc: Dict[str, Dict[int, List[Tuple[int, str]]]] = {}
    unpaged_parts: Dict[str, List[Tuple[int, str]]] = {}
    for chunk in chunks:
        text = _chunk_text(chunk)
        if not text.strip():
            continue
        doc = chunk.get("doc_name") or "(unnamed)"
        page = chunk.get("page_number")
        order = chunk.get("chunk_id") or 0
        if page is None:
            unpaged_parts.setdefault(doc, []).append((order, text))
            continue
        # A single slide yields several chunks — body, plus one per table, chart
        # and SmartArt block. Rejoin them in chunk order or a table gets cited as
        # if it were its own slide.
        by_doc.setdefault(doc, {}).setdefault(int(page), []).append((order, text))

    slides_by_doc: Dict[str, Dict[int, str]] = {}
    slide_counts: Dict[str, int] = {}
    for doc, pages in by_doc.items():
        slides_by_doc[doc] = {
            page: "\n".join(t for _, t in sorted(parts, key=lambda x: x[0]))
            for page, parts in pages.items()
        }
        # max(), not len(): a deck with a blank slide 50 still has 102 slides,
        # and undercounting turns a valid citation into a "hallucinated" one.
        slide_counts[doc] = max(pages)

    unpaged = {doc: "\n".join(t for _, t in sorted(parts, key=lambda x: x[0]))
               for doc, parts in unpaged_parts.items()}

    if not slides_by_doc and not unpaged:
        return {}, {}, [], ["No readable text found in the uploaded documents."], {}

    deck_docs = sorted(slides_by_doc, key=lambda d: slide_counts[d], reverse=True)
    if len(deck_docs) > 1:
        notes.append(
            "Scored " + ", ".join(f"{d} ({slide_counts[d]} slides)" for d in deck_docs)
            + ". Citations name their document.")
    for doc in sorted(unpaged):
        notes.append(
            f"{doc} carries no slide or page numbers. Its text is included in the "
            "review, but findings from it cannot cite a slide.")
    return slides_by_doc, slide_counts, deck_docs, notes, unpaged


def deck_fingerprint(slides_by_doc: Dict[str, Dict[int, str]],
                     unpaged: Optional[Dict[str, str]] = None) -> str:
    """Changes when the deck changes, so cached work auto-invalidates."""
    h = hashlib.sha256()
    for doc in sorted(slides_by_doc):
        h.update(doc.encode("utf-8", "ignore"))
        for slide in sorted(slides_by_doc[doc]):
            h.update(str(slide).encode())
            h.update(slides_by_doc[doc][slide][:200].encode("utf-8", "ignore"))
    for doc in sorted(unpaged or {}):
        h.update(doc.encode("utf-8", "ignore"))
        h.update((unpaged[doc][:400]).encode("utf-8", "ignore"))
    return h.hexdigest()[:16]


def _has_vision_text(slides_by_doc: Dict[str, Dict[int, str]]) -> bool:
    """Did the extractor describe the pictures? BRANDING depends on it."""
    return any("[Slide Images]" in text
               for slides in slides_by_doc.values() for text in slides.values())


def build_deck_blocks(slides_by_doc: Dict[str, Dict[int, str]],
                      unpaged: Dict[str, str]) -> List[Tuple[str, str]]:
    """The whole deck as (label, text) blocks, in document order.

    One block per slide. Nothing is truncated here — truncation is what made the
    old design unable to tell "absent" from "not shown to me".
    """
    blocks: List[Tuple[str, str]] = []
    for doc in sorted(slides_by_doc, key=lambda d: -len(slides_by_doc[d])):
        for slide in sorted(slides_by_doc[doc]):
            blocks.append((f"=== {doc} — Slide {slide} ===",
                           slides_by_doc[doc][slide]))
    for doc in sorted(unpaged):
        blocks.append((f"=== {doc} — (no slide numbers) ===", unpaged[doc]))
    return blocks


def split_into_parts(blocks: List[Tuple[str, str]],
                     char_budget: int = _DECK_CHAR_BUDGET) -> List[List[Tuple[str, str]]]:
    """Whole deck as one part when it fits; otherwise contiguous parts.

    Splitting is the last resort, and even then every category is scored against
    every part — the deck is never sampled, only paged.
    """
    total = sum(len(label) + len(text) + 2 for label, text in blocks)
    if total <= char_budget:
        return [blocks]

    n_parts = math.ceil(total / char_budget)
    target = math.ceil(total / n_parts)
    parts, current, size = [], [], 0
    for label, text in blocks:
        block_size = len(label) + len(text) + 2
        if current and size + block_size > target:
            parts.append(current)
            current, size = [], 0
        current.append((label, text))
        size += block_size
    if current:
        parts.append(current)
    return parts


def _render(blocks: List[Tuple[str, str]]) -> str:
    return "\n\n".join(f"{label}\n{text}" for label, text in blocks)


# ── Digest (fallback path only) ──────────────────────────────────────────────
def _digest_prompt(doc: str, window: List[Tuple[int, str]]) -> str:
    body = "\n\n".join(
        f"=== Slide {slide} ===\n{text[:_DIGEST_SLIDE_CHARS]}" for slide, text in window)
    numbers = ", ".join(str(s) for s, _ in window)
    return f"""You are indexing a sales proposal deck so it can be reviewed.

For EACH slide listed below, emit exactly one line:

    <slide number> | <at most 15 words on what the slide is> | <comma-separated signals>

Draw signals ONLY from this fixed list. Use as many as apply, or `none`:
{_SIGNALS}

Rules:
- Emit one line per slide, for these slides only: {numbers}
- Never invent a slide number and never omit one.
- Add `placeholder-text` whenever a value is left as xxx, TBD, <…> or similar.
- Add `image-only` when a slide has a picture but almost no text.
- No preamble, no commentary, no markdown. Just the lines.

Document: {doc}

{body}"""


def _parse_digest(raw: str, valid_slides: set) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for line in (raw or "").splitlines():
        line = line.strip().lstrip("-*").strip()
        if "|" not in line:
            continue
        head, _, rest = line.partition("|")
        m = re.match(r"^\s*(?:slide\s*)?(\d+)\s*$", head, re.I)
        if not m:
            continue
        slide = int(m.group(1))
        if slide in valid_slides:
            out[slide] = rest.strip()
    return out


def build_digest(slides_by_doc: Dict[str, Dict[int, str]],
                 progress=None, problems: Optional[List[str]] = None
                 ) -> Dict[str, Dict[int, str]]:
    """One line per slide. Only used when a deck is too big to send whole."""
    digest: Dict[str, Dict[int, str]] = {}
    for doc, slides in slides_by_doc.items():
        ordered = sorted(slides.items())
        digest[doc] = {}
        for i in range(0, len(ordered), _DIGEST_WINDOW):
            window = ordered[i:i + _DIGEST_WINDOW]
            label = f"Indexing {doc} slides {window[0][0]}-{window[-1][0]}"
            if progress:
                progress(label)
            wanted = {s for s, _ in window}
            got = {}
            for attempt in range(1, _DIGEST_ATTEMPTS + 1):
                try:
                    raw = completion_from_prompt(
                        _digest_prompt(doc, window), max_tokens=_DIGEST_MAX_TOKENS,
                        temperature=0.0, max_output_cap=_DIGEST_MAX_TOKENS)
                    got = _parse_digest(raw, wanted)
                    if got:
                        break
                except Exception:
                    logger.exception("Digest window failed (attempt %d): %s", attempt, label)
            digest[doc].update(got)
            missing = wanted - set(got)
            if missing and problems is not None:
                # Never silent: an unindexed slide is a slide the fallback path
                # cannot describe, and the reviewer should know which.
                problems.append(
                    f"Deck index incomplete for {doc}: slides "
                    f"{', '.join(str(s) for s in sorted(missing))} could not be summarised.")
    return digest


def _format_digest(digest: Dict[str, Dict[int, str]], slide_counts: Dict[str, int]) -> str:
    lines = []
    for doc in sorted(digest, key=lambda d: slide_counts.get(d, 0), reverse=True):
        lines.append(f"--- {doc} ({slide_counts.get(doc, '?')} slides) ---")
        for slide in sorted(digest[doc]):
            lines.append(f"{slide} | {digest[doc][slide]}")
    return "\n".join(lines) if lines else ""


# ── Category prompt ──────────────────────────────────────────────────────────
def _category_prompt(rubric: Dict, cat: Dict, deck_text: str,
                     slide_counts: Dict[str, int], facts_text: str,
                     carried: Optional[str], vision_available: bool,
                     part_note: str = "", digest_text: str = "",
                     stricter: bool = False) -> str:
    table = ["| # | id | Criterion | Does Not Meet | Minimally Meets | Best Practice |",
             "|---|---|---|---|---|---|"]
    for crit in cat["criteria"]:
        a = crit["anchors"]
        table.append(
            f"| {crit['number']} | {crit['id']} | {crit['name']} | "
            f"{a[ps.RATING_FAIL]} | {a[ps.RATING_MIN]} | {a[ps.RATING_BEST]} |")

    extents = "; ".join(f"{doc} has slides 1-{n}" for doc, n in slide_counts.items())

    blocks = [
        rubric["operating_rules"],
        "",
        "---",
        "",
        f"## Score this category: {cat['no']}. {cat['name']} (weight {cat['weight']:.2f})",
        "",
        f"**What this category covers:**\n{cat['description']}",
        "",
        "**Criteria and their rating anchors.** The anchors are the scale — rate "
        "against them as written.",
        "",
        "\n".join(table),
    ]

    if cat["may_be_na"]:
        blocks += ["", "This category may be rated `Not Applicable` in full if it "
                       "genuinely does not apply to this deal (see Rule 3)."]
    if cat["no"] == 6 and not vision_available:
        blocks += ["", "**No `[Slide Images]` descriptions are available for this deck.** "
                       "Criteria 1, 3, 5 and 6 are visual judgements you therefore cannot "
                       "make from the text alone: rate them on what evidence you do have, "
                       "set `confidence` to `low`, and say in the comment that no visual "
                       "signal was available."]
    if carried:
        blocks += ["", f"**Already decided:** {carried}"]

    blocks += ["", "---", ""]
    if part_note:
        blocks.append(part_note)
        blocks.append("")
    if digest_text:
        blocks += [f"## Index of every slide in the deck\n\n```\n{digest_text}\n```", ""]

    blocks += [
        "## The deck",
        "",
        "This is the material to score. Read all of it before rating anything.",
        "",
        deck_text,
    ]
    if facts_text:
        blocks += ["", "## Established project facts", "", facts_text]

    blocks += [
        "",
        "---",
        "",
        f"Slide numbers you may cite: {extents}. Citing anything outside that is an error.",
        "",
        rubric["response_contract"],
        "",
        f"Return JSON for exactly these {len(cat['criteria'])} criteria: "
        + ", ".join(c["id"] for c in cat["criteria"]) + ".",
    ]
    if stricter:
        blocks += [
            "",
            "IMPORTANT: your previous answer could not be parsed. Return ONLY a JSON "
            "object, starting with { and ending with }. No prose before or after it, "
            "no markdown fence, no explanation.",
        ]
    else:
        blocks.append("No prose outside the JSON.")
    return "\n".join(blocks)


def _output_tokens_for(cat: Dict) -> int:
    return max(_OUTPUT_FLOOR,
               min(_OUTPUT_CEILING, len(cat["criteria"]) * _TOKENS_PER_CRITERION))


# ── Response validation ──────────────────────────────────────────────────────
def _parse_response(raw: str, cat: Dict, slide_counts: Dict[str, int],
                    default_doc: str) -> Tuple[Dict[str, Dict], List[str]]:
    """Parse and sanity-check one category's JSON. Returns (answers, problems)."""
    problems: List[str] = []
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}, [f"{cat['name']}: no JSON found in the response."]
    try:
        payload = json.loads(m.group(0))
    except json.JSONDecodeError as exc:
        return {}, [f"{cat['name']}: unparseable JSON ({exc})."]

    valid_ids = {c["id"] for c in cat["criteria"]}
    answers: Dict[str, Dict] = {}

    for entry in payload.get("criteria") or []:
        if not isinstance(entry, dict):
            continue
        cid = str(entry.get("id") or "").strip()
        if cid not in valid_ids:
            problems.append(f"{cat['name']}: ignored unknown criterion id '{cid}'.")
            continue
        rating = ps.normalize_rating(entry.get("rating"))
        if rating is None:
            problems.append(
                f"{cat['name']} {cid}: unrecognised rating "
                f"{entry.get('rating')!r} — left unrated for review.")
            continue

        slides, dropped = [], []
        for cite in entry.get("slides") or []:
            if isinstance(cite, dict):
                doc, slide = cite.get("doc") or default_doc, cite.get("slide")
            else:
                doc, slide = default_doc, cite
            try:
                slide = int(slide)
            except (TypeError, ValueError):
                continue
            if doc not in slide_counts:
                match = next((d for d in slide_counts if d.lower() == str(doc).lower()), None)
                doc = match or default_doc
            if 1 <= slide <= slide_counts.get(doc, 0):
                slides.append({"doc": doc, "slide": slide})
            else:
                dropped.append(f"{doc} slide {slide}")
        if dropped:
            problems.append(
                f"{cat['name']} {cid}: dropped citation(s) outside the deck — "
                + ", ".join(dropped) + ".")

        confidence = str(entry.get("confidence") or "").lower()
        evidence = str(entry.get("evidence_status") or "").lower()
        answers[cid] = {
            "rating": rating,
            "score": ps.RATING_SCORES[rating],
            "comment": str(entry.get("comment") or "").strip(),
            "to_improve": str(entry.get("to_improve") or "").strip(),
            "quote": str(entry.get("quote") or "").strip(),
            "slides": slides,
            "evidence_status": evidence if evidence in ps.EVIDENCE_STATES else "",
            "confidence": confidence if confidence in ps.CONFIDENCE_LEVELS else "medium",
            "source": ps.SOURCE_AI,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    missing = valid_ids - set(answers)
    if missing:
        problems.append(
            f"{cat['name']}: no rating returned for {', '.join(sorted(missing))}.")
    return answers, problems


def merge_verdicts(existing: Optional[Dict], incoming: Dict) -> Dict:
    """Combine one criterion's verdicts from two parts of a split deck.

    The stronger rating wins. Evidence found anywhere is proof; not finding it in
    one part of the deck proves nothing, so a "Does Not Meet" from part 1 must
    never override a "Best Practice" from part 3. Not Applicable yields to any
    real rating for the same reason.
    """
    if existing is None:
        return incoming
    if _RATING_RANK.get(incoming["rating"], -1) > _RATING_RANK.get(existing["rating"], -1):
        winner, loser = incoming, existing
    else:
        winner, loser = existing, incoming
    merged = dict(winner)
    seen = {(s["doc"], s["slide"]) for s in merged.get("slides", [])}
    for cite in loser.get("slides", []):
        if (cite["doc"], cite["slide"]) not in seen:
            merged.setdefault("slides", []).append(cite)
            seen.add((cite["doc"], cite["slide"]))
    return merged


# ── Run orchestration ────────────────────────────────────────────────────────
_RUNS: Dict[str, Dict] = {}
_RUN_LOCK = threading.Lock()


def _log(run: Dict, text: str, level: str = "info") -> None:
    run["log"].append({
        "ts": datetime.now().strftime("%H:%M:%S"), "text": text, "type": level})
    if len(run["log"]) > 400:
        del run["log"][:-400]


def run_status(project_id: str, since: int = 0) -> Dict:
    run = _RUNS.get(project_id)
    if not run:
        return {"active": False, "running": False, "log": [], "log_total": 0}
    return {
        "active": True,
        "running": run["running"],
        "step": run["step"],
        "total_steps": run["total_steps"],
        "current": run["current"],
        "log": run["log"][since:],
        "log_total": len(run["log"]),
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "error": run["error"],
        "problems": run["problems"],
    }


def stop_run(project_id: str) -> Dict:
    run = _RUNS.get(project_id)
    if run and run["running"]:
        run["stop_requested"] = True
        _log(run, "Stop requested — finishing the current category.", "warn")
    return run_status(project_id)


def is_running(project_id: str) -> bool:
    run = _RUNS.get(project_id)
    return bool(run and run["running"])


def start_run(project_id: str, force: bool = False,
              overwrite_human: bool = False, actor: str = "") -> Dict:
    with _RUN_LOCK:
        if is_running(project_id):
            raise RuntimeError("A scoring run is already in progress for this project.")
        _RUNS[project_id] = {
            "running": True, "stop_requested": False, "step": 0, "total_steps": 0,
            "current": "Starting…", "log": [], "problems": [], "error": None,
            "started_at": datetime.now(timezone.utc).isoformat(), "finished_at": None,
        }
    threading.Thread(
        target=_worker, args=(project_id, force, overwrite_human, actor),
        daemon=True, name=f"pitch-score-{project_id}").start()
    return run_status(project_id)


def _worker(project_id: str, force: bool, overwrite_human: bool, actor: str) -> None:
    run = _RUNS[project_id]
    try:
        _score_project(project_id, run, force, overwrite_human, actor)
    except Exception as exc:
        logger.exception("Pitch scoring failed for %s", project_id)
        run["error"] = str(exc)
        _log(run, f"Scoring failed: {exc}", "error")
    finally:
        run["running"] = False
        run["current"] = None
        run["finished_at"] = datetime.now(timezone.utc).isoformat()


def _score_category(cat: Dict, rubric: Dict, parts: List[List[Tuple[str, str]]],
                    slide_counts: Dict[str, int], default_doc: str,
                    facts_text: str, carried: Optional[str], vision: bool,
                    digest_text: str, run: Dict) -> Tuple[Dict[str, Dict], List[str]]:
    """Score one category against every part of the deck, then merge."""
    merged: Dict[str, Dict] = {}
    problems: List[str] = []
    max_tokens = _output_tokens_for(cat)

    for index, part in enumerate(parts, start=1):
        part_note = ""
        if len(parts) > 1:
            part_note = (
                f"**This is part {index} of {len(parts)} of the deck.** Rate on what you "
                "can see here. If the evidence for a criterion is not in this part, say so "
                "plainly rather than concluding it is absent from the whole deck — the "
                "other parts are scored separately and the strongest verdict is kept.")

        answers: Dict[str, Dict] = {}
        part_problems: List[str] = []
        for attempt in range(1, _CATEGORY_ATTEMPTS + 1):
            prompt = _category_prompt(
                rubric, cat, _render(part), slide_counts, facts_text, carried,
                vision, part_note=part_note, digest_text=digest_text,
                stricter=attempt > 1)
            try:
                raw = completion_from_prompt(
                    prompt, max_tokens=max_tokens, temperature=0.0,
                    max_output_cap=max_tokens)
            except Exception as exc:
                logger.exception("%s part %d attempt %d failed", cat["name"], index, attempt)
                if attempt == _CATEGORY_ATTEMPTS:
                    part_problems.append(
                        f"{cat['name']}: the model call failed after "
                        f"{_CATEGORY_ATTEMPTS} attempts ({exc}).")
                    _log(run, f"{cat['name']}: call failed, retrying…"
                         if attempt < _CATEGORY_ATTEMPTS else
                         f"{cat['name']}: giving up after {attempt} attempts.", "error")
                continue

            answers, part_problems = _parse_response(raw, cat, slide_counts, default_doc)
            if len(answers) == len(cat["criteria"]):
                break
            if attempt < _CATEGORY_ATTEMPTS:
                _log(run, f"{cat['name']}: {len(answers)}/{len(cat['criteria'])} "
                          f"criteria returned — re-asking (attempt {attempt + 1}).", "warn")

        problems.extend(part_problems)
        for cid, answer in answers.items():
            merged[cid] = merge_verdicts(merged.get(cid), answer)

    return merged, problems


def _score_project(project_id: str, run: Dict, force: bool,
                   overwrite_human: bool, actor: str) -> None:
    from app.chunking_pipeline import _get_project_lock, load_project_chunks
    from app.project_routes import UPLOAD_ROOT

    rubric = ps.load_rubric()

    _log(run, "Reading the uploaded deck…")
    slides_by_doc, slide_counts, deck_docs, notes, unpaged = load_deck(project_id)
    for note in notes:
        _log(run, note, "warn")
    if not slides_by_doc and not unpaged:
        raise RuntimeError(notes[0] if notes else "No deck to score.")

    default_doc = deck_docs[0] if deck_docs else next(iter(unpaged), "")
    total_slides = sum(len(s) for s in slides_by_doc.values())
    vision = _has_vision_text(slides_by_doc)

    blocks = build_deck_blocks(slides_by_doc, unpaged)
    deck_chars = sum(len(a) + len(b) for a, b in blocks)
    parts = split_into_parts(blocks)

    _log(run, f"{total_slides} slides across {len(deck_docs)} document(s), "
              f"{deck_chars:,} characters.")
    if len(parts) == 1:
        _log(run, "The whole deck fits in one pass — every criterion is scored "
                  "against all of it.")
    else:
        _log(run, f"Deck exceeds the {DECK_TOKEN_BUDGET:,}-token budget; splitting into "
                  f"{len(parts)} parts. Every category is scored against every part and "
                  "the strongest verdict kept, so no slide goes unseen.", "warn")
    if not vision:
        _log(run, "No slide-image descriptions in this deck — the four visual "
                  "BRANDING criteria will come back low-confidence.", "warn")

    problems: List[str] = []

    # The digest is only worth its calls when the deck had to be split: it gives
    # each part a view of the slides it cannot see.
    store = ps.load_store(project_id)
    fingerprint = deck_fingerprint(slides_by_doc, unpaged)
    digest_text = ""
    digest: Dict[str, Dict[int, str]] = {}
    digest_steps = 0
    if len(parts) > 1:
        digest_steps = sum(-(-len(s) // _DIGEST_WINDOW) for s in slides_by_doc.values())

    run["total_steps"] = digest_steps + len(rubric["categories"])

    def step(label: str) -> None:
        run["step"] += 1
        run["current"] = label
        _log(run, label)

    if len(parts) > 1:
        cached = store.get("digest") if store.get("deck_fingerprint") == fingerprint else None
        if cached and not force:
            digest = {doc: {int(k): v for k, v in slides.items()}
                      for doc, slides in cached.items()}
            run["step"] += digest_steps
            _log(run, "Deck unchanged — reusing the existing deck index.")
        else:
            digest = build_digest(slides_by_doc, progress=step, problems=problems)
        digest_text = _format_digest(digest, slide_counts)

    facts_text = ""
    try:
        from app.sow_key_facts import extract_key_facts, format_facts_for_context
        chunks = load_project_chunks(UPLOAD_ROOT / project_id)
        facts_text = format_facts_for_context(extract_key_facts(project_id, chunks))
    except Exception:
        logger.exception("Key facts unavailable for %s — scoring without them", project_id)

    carried: Optional[str] = None
    kept_human = 0
    failed_criteria: List[str] = []

    for cat in rubric["categories"]:
        if run["stop_requested"]:
            _log(run, "Stopped before completing the remaining categories.", "warn")
            break

        step(f"Scoring {cat['no']}. {cat['name']}")
        answers, issues = _score_category(
            cat, rubric, parts, slide_counts, default_doc, facts_text, carried,
            vision, digest_text, run)
        problems.extend(issues)
        for issue in issues:
            _log(run, issue, "warn")

        # Carry the duplicated criterion's verdict forward so 9.4 and 10.1 agree.
        if cat["no"] == 9 and "9.4" in answers:
            verdict = answers["9.4"]
            carried = (f"Criterion 9.4 METHODOLOGY DEFINITION — identical anchors to 10.1 — "
                       f"was rated '{verdict['rating']}': {verdict['comment']} "
                       "Rate 10.1 consistently unless the deck gives you reason not to.")

        # A criterion the model never returned is a SCORING FAILURE, not an
        # unrated one. Record it so the reviewer sees it needs their judgement
        # rather than assuming the run simply had not reached it.
        for crit in cat["criteria"]:
            if crit["id"] not in answers:
                failed_criteria.append(crit["id"])

        with _get_project_lock(project_id):
            store = ps.load_store(project_id)
            for cid, answer in answers.items():
                existing = store["answers"].get(cid) or {}
                if existing.get("source") == ps.SOURCE_HUMAN and not overwrite_human:
                    kept_human += 1
                    continue
                store["answers"][cid] = answer
            store["deck_fingerprint"] = fingerprint
            store["deck_docs"] = deck_docs
            store["slide_counts"] = slide_counts
            store["digest"] = digest
            store["scored_at"] = datetime.now(timezone.utc).isoformat()
            store["actor"] = actor or store.get("actor", "")
            store["run_notes"] = notes
            store["deck_parts"] = len(parts)
            store["failed_criteria"] = sorted(set(
                (store.get("failed_criteria") or []) + failed_criteria)
                - set(store["answers"]))
            ps.save_store(project_id, store)

        _log(run, f"{cat['name']}: {len(answers)} of {len(cat['criteria'])} criteria rated.")

    run["problems"] = problems
    if kept_human:
        _log(run, f"Kept {kept_human} rating(s) you had edited by hand.", "warn")
    if failed_criteria:
        _log(run, f"{len(failed_criteria)} criteria could not be scored and need your "
                  f"judgement: {', '.join(sorted(set(failed_criteria)))}.", "error")
    summary = ps.summary(project_id)
    _log(run, f"Done — {summary['total_pct']}% ({summary['band']}). "
              f"{summary['unrated_count']} criteria unrated.", "success")
