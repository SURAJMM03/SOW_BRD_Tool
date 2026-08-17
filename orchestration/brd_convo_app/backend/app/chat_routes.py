"""
chat_routes.py — AI Chatbot (Phase 1 MVP)
==========================================

A grounded, document-aware chatbot endpoint that answers questions over the
content the user has already ingested into Repositories.

Design notes
------------
This module is intentionally *additive*. It reuses the project's existing
building blocks and does not modify the generation flow, storage, or Azure
configuration:

  • Retrieval ........ semantic_search()  (app/semantic_search.py)
  • Repo chunks ...... load_chunks_for_repos()  (app/repository_routes.py)
  • LLM .............. completion_from_prompt()  (app/claude_provider.py)
                       → routes to Azure OpenAI (gpt-5.x) via DefaultAzureCredential

Scope (per the agreed Phase 1 plan):
  • "repo"    — chat over a single repository
  • "project" — chat over every repository attached to a project

The endpoint is grounded: the model is instructed to answer ONLY from the
retrieved passages and to say so plainly when the answer is not present,
rather than inventing content. Each answer is returned with structured
citations (source file + heading/page) so the UI can show provenance.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, List, Dict, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("BRDConvoAPI.chat")

router = APIRouter()

# Tunables (kept conservative for an MVP) ------------------------------------
TOP_K = 6                 # anchor passages from ranking
MIN_SCORE = 0.20          # semantic similarity floor
MAX_PASSAGE_CHARS = 1100  # truncate each passage in the prompt (keeps prompt lean)
ANSWER_MAX_TOKENS = 4000  # answer budget; provider escalates on empty (gpt-5
                          # reasoning shares this budget with the visible answer)
# Neighbor-window expansion: pull contiguous chunks around each anchor so a
# multi-chunk table/section (e.g. a parameter table spanning several pages)
# arrives intact. Gentle linear decay keeps a strong anchor's full block ahead
# of weaker, unrelated anchors.
NEIGHBOR_WINDOW = 6        # chunks to pull on each side of an anchor
NEIGHBOR_STEP = 0.07      # priority lost per step away from the anchor
NEIGHBOR_MIN_FACTOR = 0.55  # floor so far neighbors still beat unrelated anchors
MAX_PASSAGES = 12         # cap on total passages after expansion (token budget)
GRADE_COVERAGE_MIN = 0.55 # min fraction of query terms a result must cover, else re-retrieve wider

# Exact sentinels the model returns when no passage is relevant. Kept as
# constants so the prompt and the post-check stay in sync.
NOT_FOUND = "I couldn't find that in the selected documents."
NOT_FOUND_DOC = "I couldn't find information about this topic in the uploaded documentation."
# Polite refusal when a question is about a specific named project / client /
# product / entity that the selected documents don't actually cover (T11). Kept
# verbatim so the prompt and the post-check stay in sync.
OUT_OF_SCOPE = (
    "I'm sorry, but I don't have any information about that in the selected "
    "documents — it looks like it may belong to a different project or workspace, "
    "so I'm not able to help with it here. If you switch to the relevant source "
    "or project, I'll be happy to help."
)

# ── Answer cache (in-memory, TTL) ────────────────────────────────────────────
# Keyed by question + scope + answer mode. Identical questions inside the TTL
# window get an instant identical reply without a retrieval + LLM round trip.
import time as _time

_ANSWER_CACHE: Dict[str, Tuple[float, Dict]] = {}
_ANSWER_CACHE_TTL = float(os.getenv("CHAT_ANSWER_CACHE_TTL", "3600"))  # seconds
_ANSWER_CACHE_MAX = int(os.getenv("CHAT_ANSWER_CACHE_MAX", "200"))    # entries

def _answer_cache_key(question: str, scope_type: str, scope_id, repo_ids, hybrid: bool) -> str:
    rids = ",".join(sorted(repo_ids or []))
    mode = "hybrid" if hybrid else "strict"
    return f"{question.strip().lower()}|{scope_type}|{scope_id or ''}|{rids}|{mode}"

def _answer_cache_get(key: str) -> Optional[Dict]:
    hit = _ANSWER_CACHE.get(key)
    if not hit:
        return None
    ts, resp = hit
    if _time.time() - ts > _ANSWER_CACHE_TTL:
        _ANSWER_CACHE.pop(key, None)
        return None
    return resp

def _answer_cache_put(key: str, resp: Dict) -> None:
    if len(_ANSWER_CACHE) >= _ANSWER_CACHE_MAX:
        oldest = min(_ANSWER_CACHE, key=lambda k: _ANSWER_CACHE[k][0])
        _ANSWER_CACHE.pop(oldest, None)
    _ANSWER_CACHE[key] = (_time.time(), resp)


def _is_not_found(answer: str) -> bool:
    a = (answer or "").strip().lower().rstrip(".")
    return a.startswith("i couldn't find") or a.startswith("i could not find")


def _is_out_of_scope(answer: str) -> bool:
    """True when the model returned the polite out-of-scope refusal (T11)."""
    a = (answer or "").strip().lower()
    return a.startswith("i'm sorry, but i don't have any information about that")


# ── Imports of existing building blocks (with app./bare fallback to match
#    the import style used throughout this codebase) ─────────────────────────
def _load_chunks_for_repos(repo_ids: List[str]) -> List[Dict]:
    try:
        from app.repository_routes import load_chunks_for_repos
    except ImportError:
        from repository_routes import load_chunks_for_repos
    return load_chunks_for_repos(repo_ids)


def _repo_registry() -> Dict[str, dict]:
    try:
        from app.repository_routes import REPOSITORIES
    except ImportError:
        from repository_routes import REPOSITORIES
    return REPOSITORIES


def _project_repo_ids(project_id: str) -> List[str]:
    try:
        from app.repository_routes import _get_project_state
    except ImportError:
        from repository_routes import _get_project_state
    PROJECTS, _ = _get_project_state()
    if project_id not in PROJECTS:
        raise HTTPException(404, f"Project '{project_id}' not found")
    return list(PROJECTS[project_id].get("repositories", []))


def _completion(prompt: str) -> str:
    try:
        from app.claude_provider import completion_from_prompt
    except ImportError:
        from claude_provider import completion_from_prompt
    # reasoning_effort "none" keeps gpt-5.x from spending the whole token budget on
    # hidden reasoning (which yields empty content). Override via CHAT_REASONING_EFFORT;
    # empty falls through to the provider's AZURE_OPENAI_REASONING_EFFORT default.
    effort = os.getenv("CHAT_REASONING_EFFORT", "none")
    return completion_from_prompt(prompt, max_tokens=ANSWER_MAX_TOKENS,
                                  temperature=0.0, reasoning_effort=effort)


def _semantic_available() -> bool:
    try:
        from app.semantic_search import is_semantic_enabled
    except ImportError:
        from semantic_search import is_semantic_enabled
    return bool(is_semantic_enabled())


def _semantic_search(query: str, chunks: List[Dict], top_k: int = TOP_K) -> List[Tuple[float, Dict]]:
    try:
        from app.semantic_search import semantic_search
    except ImportError:
        from semantic_search import semantic_search
    return semantic_search(query, chunks, top_k=top_k, min_score=MIN_SCORE)


# ── Keyword fallback ────────────────────────────────────────────────────────
# Used when semantic search is disabled (no embeddings / no OpenAI key) or
# returns nothing. Keeps the chatbot usable in every deployment.
_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "are", "was", "will",
    "have", "been", "their", "what", "which", "how", "does", "did", "is", "of",
    "to", "in", "on", "a", "an", "do", "can", "about", "tell", "me", "please",
}


def _keyword_search(query: str, chunks: List[Dict], top_k: int = TOP_K) -> List[Tuple[float, Dict]]:
    """TF-IDF keyword ranking.

    Plain term-frequency lets common words ("parameter", "process") dominate, so
    a rare but decisive term (e.g. a function name like "ConSamePickAndDrop")
    never surfaces in a large corpus. Weighting each term by its inverse document
    frequency makes specific terms win. Matches in the heading/keywords are
    boosted over body text.
    """
    terms = {w for w in re.findall(r"[a-zA-Z0-9]+", query.lower())
             if len(w) > 2 and w not in _STOP}
    if not terms:
        return []

    n = len(chunks) or 1
    docs = []          # (heading, kws, text, chunk)
    df = dict.fromkeys(terms, 0)
    for c in chunks:
        text = (c.get("chunk_text", "") or "").lower()
        heading = (c.get("section_heading", "") or "").lower()
        kws = " ".join(c.get("keywords", []) or []).lower()
        docs.append((heading, kws, text, c))
        blob = heading + " " + kws + " " + text
        for t in terms:
            if t in blob:
                df[t] += 1
    idf = {t: math.log(1.0 + n / (1.0 + df[t])) for t in terms}

    scored: List[Tuple[float, Dict]] = []
    for heading, kws, text, c in docs:
        s = 0.0
        for t in terms:
            tf = text.count(t) + 2 * kws.count(t) + 3 * heading.count(t)
            if tf:
                s += (1.0 + math.log(tf)) * idf[t]
        if s > 0:
            s /= (1.0 + len(text) / 4000.0)  # length normalisation
            scored.append((float(s), c))
    scored.sort(key=lambda x: -x[0])
    return scored[:top_k]


def _expand_neighbors(anchors: List[Tuple[float, Dict]], chunks: List[Dict],
                      window: int = NEIGHBOR_WINDOW, cap: int = MAX_PASSAGES) -> List[Tuple[float, Dict]]:
    """Pull contiguous neighbor chunks (same file) around each anchor so a
    section/table split across chunks comes through whole. Neighbors inherit a
    decayed priority; the set is capped and returned in reading order."""
    if not anchors:
        return anchors
    index = {}
    for c in chunks:
        cid = c.get("chunk_id")
        if isinstance(cid, int):
            index[(c.get("repo_id"), cid)] = c

    cand = {}   # key -> (priority, chunk)
    def consider(key, pr, c):
        if key not in cand or pr > cand[key][0]:
            cand[key] = (pr, c)

    for score, c in anchors:
        rid = c.get("repo_id"); cid = c.get("chunk_id")
        consider((rid, cid), score, c)
        if isinstance(cid, int):
            for off in range(1, window + 1):
                pr = score * max(NEIGHBOR_MIN_FACTOR, 1.0 - NEIGHBOR_STEP * off)
                for nk in ((rid, cid - off), (rid, cid + off)):
                    nc = index.get(nk)
                    # only expand within the SAME source file (avoid bleeding
                    # across files whose chunk_ids happen to be adjacent)
                    if nc is not None and nc.get("file_id") == c.get("file_id"):
                        consider(nk, pr, nc)

    items = list(cand.values())
    items.sort(key=lambda x: -x[0])          # keep highest-priority within budget
    items = items[:cap]
    # reading order: group by document, then by chunk position
    items.sort(key=lambda x: ((x[1].get("doc_name", "") or ""), x[1].get("chunk_id", 0)))
    return [(pr, c) for pr, c in items]


def _rank(question: str, chunks: List[Dict], top_k: int) -> Tuple[List[Tuple[float, Dict]], str]:
    """Return (anchors, backend) from semantic (if enabled) else keyword."""
    if _semantic_available():
        anchors = _semantic_search(question, chunks, top_k=top_k)
        if anchors:
            return anchors, "semantic"
    return _keyword_search(question, chunks, top_k=top_k), "keyword"


def _context_sufficient(question: str, passages: List[Tuple[float, Dict]]) -> bool:
    """Grader: cheap, no-LLM check of whether the retrieved passages actually
    cover the question. Used to decide whether to re-retrieve with a wider net."""
    terms = {w for w in re.findall(r"[a-zA-Z0-9]+", question.lower())
             if len(w) > 2 and w not in _STOP}
    if not terms:
        return True
    if len(passages) < 2:
        return False
    blob = " ".join((c.get("chunk_text", "") or "") for _, c in passages).lower()
    covered = sum(1 for t in terms if t in blob)
    return (covered / len(terms)) >= GRADE_COVERAGE_MIN


def _retrieve(question: str, chunks: List[Dict]) -> Tuple[List[Tuple[float, Dict]], str]:
    """Retrieve with neighbor-window expansion, then GRADE the result; if the
    context looks thin, re-retrieve once with a wider net (more anchors, larger
    window/cap). This is the single-pass equivalent of a LangGraph grader loop."""
    anchors, backend = _rank(question, chunks, top_k=TOP_K)
    passages = _expand_neighbors(anchors, chunks)
    if not _context_sufficient(question, passages):
        wide_anchors, backend = _rank(question, chunks, top_k=TOP_K * 2)
        wider = _expand_neighbors(wide_anchors, chunks,
                                  window=NEIGHBOR_WINDOW + 2,
                                  cap=MAX_PASSAGES + 6)
        logger.info(
            "chat_ask: context thin (%d passages) — re-retrieved wider (%d passages)",
            len(passages), len(wider),
        )
        if len(wider) >= len(passages):
            passages = wider
    return passages, backend


# ── Prompt construction (Hybrid RAG Response Strategy) ───────────────────────
def _passage_blocks(passages: List[Tuple[float, Dict]]) -> str:
    blocks = []
    for i, (_score, c) in enumerate(passages, start=1):
        doc = c.get("doc_name", "") or "Unknown document"
        heading = c.get("section_heading") or ""
        page = c.get("page_number")
        loc = doc
        if heading:
            loc += f" › {heading}"
        if page not in (None, "", 0):
            loc += f" (p.{page})"
        text = (c.get("chunk_text", "") or "").strip()[:MAX_PASSAGE_CHARS]
        blocks.append(f"[{i}] Source: {loc}\n{text}")
    return "\n\n".join(blocks)


MISSING_PARAM_PHRASE = (
    "This parameter is listed in the uploaded documentation, but its behavior is "
    "not described."
)


def _build_prompt(question: str, passages: List[Tuple[float, Dict]], hybrid: bool = True) -> str:
    """Enterprise Document AI Assistant prompt: synthesise (not summarise) the
    retrieved passages into an intent-aware, professionally structured answer.
    Uploaded passages are authoritative; general knowledge is a clearly-labelled
    fallback in hybrid mode and disabled in strict mode."""
    context = _passage_blocks(passages)
    p = (
        "You are an expert assistant. Use the reference material below to answer the "
        "user's question. Give ONLY the answer itself — write as if this is your own "
        "subject-matter knowledge.\n\n"
        "HOW TO ANSWER:\n"
        "- Understand the user's intent, then merge information across all the reference "
        "material into one coherent, complete answer. Remove duplicates and explain in "
        "clear language rather than copying raw text.\n"
        "- Format for readability with Markdown headings, and use a Markdown TABLE when "
        "explaining multiple parameters/fields or comparing items.\n"
        "- Pick the structure that fits the intent: a function → Overview, Purpose, "
        "Parameters (table), Notes; parameters → a table (Parameter, Description, Type, "
        "Default, etc. where available); comparison → a table; configuration → steps; "
        "troubleshooting → Problem/Cause/Resolution; how-to → numbered steps; concept → "
        "Definition/Purpose/Example. Start with a brief overview.\n\n"
        "STRICTLY DO NOT INCLUDE (answer only):\n"
        "- No mention of sources, documents, or where the information came from — never "
        "write phrases like 'according to the documentation', 'the uploaded documents', "
        "'From Uploaded Documentation', or 'listed in the signature but not described'.\n"
        "- No citation markers ([1], [2], bracketed numbers), no 'Sources' list, no "
        "'Missing Information' section, and no 'Confidence' line.\n"
        "- Just present the complete answer directly.\n\n"
        "ACCURACY:\n"
        "- Base the answer on the reference material; do not contradict it and do not "
        "invent specifics that aren't supported.\n"
    )
    if hybrid:
        p += (
            "- If a GENERAL/CONCEPTUAL detail is not in the reference material, you may use "
            "your own general knowledge to give a complete, accurate answer — seamlessly, "
            "without noting that it came from outside the material.\n"
        )
    else:
        p += (
            "- Use ONLY the reference material. If a detail is not present, simply omit it "
            "rather than guessing or adding outside information.\n"
        )
    p += (
        "\nSCOPE / RELEVANCE (important):\n"
        "- If the question is about a SPECIFIC NAMED project, client, company, product, "
        "module, or workbook (a proper noun, e.g. a named plan or a named customer) that "
        "does NOT actually appear in the reference material, do NOT assemble an answer from "
        "loosely-related passages that merely share common words, and do NOT fall back to "
        "general knowledge to guess. The named thing likely belongs to a different project "
        "or workspace.\n"
        f"- In that case reply with EXACTLY this and nothing else: \"{OUT_OF_SCOPE}\"\n"
        "- This does not apply to ordinary general/conceptual questions — answer those "
        "normally.\n"
        "\nIf the reference material is unrelated to the question and you cannot answer it "
        f"{'from general knowledge either' if hybrid else 'from the material'}, reply only: "
        f"\"{NOT_FOUND_DOC}\"\n\n"
        "=== REFERENCE MATERIAL ===\n"
        f"{context}\n\n"
        "=== QUESTION ===\n"
        f"{question}\n\n"
        "=== ANSWER (Markdown, answer only) ===\n"
    )
    return p


def _general_knowledge_prompt(question: str) -> str:
    """Hybrid-mode fallback when retrieval finds nothing: answer directly from
    general knowledge — answer only, no source/citation/meta commentary."""
    return (
        "You are an expert assistant. Answer the user's question directly and completely "
        "from your own knowledge. Give ONLY the answer.\n"
        "- Format for readability with Markdown headings and tables where helpful.\n"
        "- Do NOT mention sources or documents, do NOT add citation markers, a 'Sources' "
        "list, a 'Missing Information' section, or a 'Confidence' line.\n\n"
        f"=== QUESTION ===\n{question}\n\n=== ANSWER (Markdown, answer only) ===\n"
    )


# Inline citation refs the model actually used, e.g. "[1]" / "[2][3]".
_CITE_RE = re.compile(r"\[(\d+)\]")


def _strip_cite_markers(answer: str) -> str:
    """Remove inline [n] citation markers (and any whitespace they leave behind)
    so answers read cleanly without bracketed source numbers."""
    s = _CITE_RE.sub("", answer or "")
    s = re.sub(r"[ \t]{2,}", " ", s)         # collapse double spaces left behind
    s = re.sub(r" +([.,;:)])", r"\1", s)     # tidy space before punctuation
    s = re.sub(r"[ \t]+(\n|$)", r"\1", s)    # trim trailing spaces on each line
    return s.strip()


def _citations(passages: List[Tuple[float, Dict]], answer: str = None) -> List[Dict]:
    """Build citation objects. When `answer` is given, return ONLY the passages
    the model actually cited (per the Hybrid RAG strategy: cite what's used)."""
    used = None
    if answer is not None:
        used = {int(m) for m in _CITE_RE.findall(answer)}
    out = []
    for i, (score, c) in enumerate(passages, start=1):
        if used is not None and i not in used:
            continue
        out.append({
            "ref": i,
            "doc_name": c.get("doc_name", "") or "Unknown document",
            "heading": c.get("section_heading") or "",
            "page": c.get("page_number"),
            "chunk_id": c.get("chunk_id"),
            "repo_id": c.get("repo_id"),
            "snippet": (c.get("chunk_text", "") or "").strip()[:240],
            "score": round(float(score), 4),
        })
    return out


def _sources_from_passages(passages: List[Tuple[float, Dict]], limit: int = 5) -> List[str]:
    """Distinct source document names from the passages actually retrieved,
    ordered by best (highest-scoring) appearance. Used to tell the user which
    document(s) the answer was grounded in (T7)."""
    best: Dict[str, float] = {}
    for score, c in passages:
        doc = (c.get("doc_name") or "").strip()
        if not doc:
            continue
        if doc not in best or float(score) > best[doc]:
            best[doc] = float(score)
    ordered = sorted(best.keys(), key=lambda d: -best[d])
    return ordered[:limit]


# ── Image results (T5) ───────────────────────────────────────────────────────
# When the user explicitly asks to *see* something (image / diagram / screenshot
# / figure / "show me"), surface the most relevant images from the image index
# as inline thumbnails alongside the answer.
_IMAGE_INTENT_RE = re.compile(
    r"\b(image|images|picture|pictures|photo|photos|diagram|diagrams|figure|figures|"
    r"screenshot|screenshots|screen ?shot|illustration|illustrations|visual|visuals|"
    r"chart|charts|graphic|graphics|fl/?chart|flow ?chart|swimlane|drawing|drawings|"
    r"snapshot|show me|depict|visualis|visualiz)\b", re.I)

# Captions/keywords that indicate non-informative images (logos, decorative
# backgrounds) we should not surface in answers.
_IMG_JUNK_RE = re.compile(r"\b(logo|branding|abstract|background|decorative|watermark)\b", re.I)


def _wants_images(question: str) -> bool:
    return bool(_IMAGE_INTENT_RE.search(question or ""))


def _search_images(query: str, top_k: int = 8) -> List[Dict]:
    try:
        from app.image_index import search_images
    except ImportError:
        from image_index import search_images
    try:
        return search_images(query=query, top_k=top_k) or []
    except Exception as exc:  # never let image lookup break the answer
        logger.warning("chat_ask: image search failed: %s", exc)
        return []


# Words that express the *request to see* an image rather than its subject.
# Stripped before image search so the query carries only the real topic
# (e.g. "explain recent activity including images" → "recent activity").
_IMAGE_QUERY_NOISE = {
    "image", "images", "picture", "pictures", "photo", "photos", "diagram",
    "diagrams", "figure", "figures", "screenshot", "screenshots", "screen",
    "shot", "illustration", "illustrations", "visual", "visuals", "graphic",
    "graphics", "flowchart", "swimlane", "drawing", "drawings", "snapshot",
    "show", "see", "display", "depict", "explain", "tell", "give", "including",
    "include", "includes", "relevant", "related", "any", "some", "view",
}


def _clean_image_query(question: str) -> str:
    """Drop image-request and generic words, keeping the substantive topic so the
    image index matches on subject rather than on words like 'show'/'images'."""
    toks = [w for w in re.findall(r"[a-zA-Z0-9]+", (question or "").lower())
            if len(w) > 2 and w not in _STOP and w not in _IMAGE_QUERY_NOISE]
    return " ".join(toks)


def _norm_doc(name: str) -> str:
    """Normalise a document name for comparison (basename, lowercased)."""
    base = os.path.basename((name or "").strip().replace("\\", "/"))
    return base.lower()


def _as_int_page(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _all_image_records() -> List[Dict]:
    """Every record currently in the in-memory image index (may be empty)."""
    try:
        from app import image_index as ii
    except ImportError:
        try:
            import image_index as ii  # type: ignore
        except ImportError:
            return []
    idx = getattr(ii, "image_index", None)
    return list(getattr(idx, "raw_records", []) or []) if idx else []


_HEXID_RE = re.compile(r"^[0-9a-f]{8,}$", re.I)


def _img_caption(rec: Dict) -> str:
    """A human-readable label. Many extracted images have no vision caption — the
    'caption' field is just the image id — so fall back to the section heading,
    then a page label, rather than showing a hex id to the user."""
    image_id = str(rec.get("image_id") or "")
    cap = (rec.get("caption") or "").strip()
    if cap and cap.lower() != image_id.lower() and not _HEXID_RE.match(cap):
        return cap
    heading = (rec.get("section_heading") or "").strip().rstrip(":")
    if heading and heading.lower() not in ("note", "tip", "important", "unknown"):
        return heading
    page = rec.get("page_number")
    return f"Figure (p.{page})" if page not in (None, "", 0) else "Figure"


def _img_obj(rec: Dict) -> Dict:
    image_id = rec.get("image_id")
    return {
        "image_id": image_id,
        "url": f"/api/images/{image_id}",
        "caption": _img_caption(rec),
        "source_doc": (rec.get("source_doc") or "").strip(),
        "page": rec.get("page_number"),
    }


def _is_junk_image(rec: Dict) -> bool:
    caption = (rec.get("caption") or "")
    kws = " ".join(rec.get("keywords", []) or [])
    return bool(_IMG_JUNK_RE.search(caption) or _IMG_JUNK_RE.search(kws))


def _relevant_images(question: str, passages: Optional[List[Tuple[float, Dict]]] = None,
                     limit: int = 4) -> List[Dict]:
    """Return display-ready images for the question, anchored to the answer.

    Policy (so we never show an image from a document the answer did not use):
      • When the answer is grounded in specific document(s), images are drawn
        ONLY from those same document(s). We scan the image index directly for
        every image in those docs and rank by topic match, then by proximity to
        the cited page. If those docs have no matching image, we show none —
        better than a misleading image from an unrelated document.
      • When there is no grounding (e.g. a general-knowledge answer), fall back
        to the best keyword matches across the whole image library.
    """
    topic = _clean_image_query(question)
    heads, docs, pages = [], [], []
    for _s, c in (passages or [])[:8]:
        h = (c.get("section_heading") or "").strip()
        if h and h not in heads:
            heads.append(h)
        d = (c.get("doc_name") or "").strip()
        if d and d not in docs:
            docs.append(d)
        p = _as_int_page(c.get("page_number"))
        if p is not None:
            pages.append(p)
    ground_docs = {_norm_doc(d) for d in docs if d}

    # Topic terms = what the QUESTION is about (its cleaned subject). Fall back to
    # the grounding section headings only when the question carries no topic
    # (e.g. "show me the images"). Generic heading noise is dropped.
    def _toks(s):
        return {w for w in re.findall(r"[a-zA-Z0-9]+", (s or "").lower())
                if len(w) > 2 and w not in _STOP}
    terms = _toks(topic) or _toks(" ".join(heads))

    # ── Grounded answer: pull images only from the grounding documents ────────
    if ground_docs:
        scored = []   # (overlap, page_dist, rec)
        for rec in _all_image_records():
            if _norm_doc(rec.get("source_doc", "")) not in ground_docs:
                continue
            if not rec.get("image_id") or _is_junk_image(rec):
                continue
            image_id = str(rec.get("image_id") or "")
            cap = (rec.get("caption") or "").strip()
            if cap.lower() == image_id.lower() or _HEXID_RE.match(cap):
                cap = ""   # placeholder id, not real caption — don't match on it
            blob = (cap + " "
                    + " ".join(rec.get("keywords", []) or []) + " "
                    + str(rec.get("description", "")) + " "
                    + str(rec.get("section_heading", ""))).lower()
            overlap = sum(1 for w in terms if w in blob) if terms else 0
            # Require a genuine TOPICAL match — never surface a page-neighbour that
            # merely happens to sit near the cited page with no subject overlap.
            if overlap <= 0:
                continue
            ipage = _as_int_page(rec.get("page_number"))
            page_dist = min((abs(ipage - p) for p in pages), default=10 ** 6) \
                if (ipage is not None and pages) else 10 ** 6
            scored.append((overlap, page_dist, rec))
        if not scored:
            return []
        # Strongest topic overlap first, then closest to the cited page.
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [_img_obj(s[2]) for s in scored[:limit]]

    # ── Ungrounded answer: best keyword matches across the whole library ──────
    query = topic or (question or "")
    hits = _search_images(query, top_k=max(limit * 4, 16))
    if not hits and (question or "").strip() and query != question:
        hits = _search_images(question, top_k=max(limit * 4, 16))
    out: List[Dict] = []
    for rec in hits:
        if float(rec.get("score", 0) or 0) <= 0 or not rec.get("image_id") or _is_junk_image(rec):
            continue
        out.append(_img_obj(rec))
        if len(out) >= limit:
            break
    return out


def _cite_obj(ref: int, score: float, c: Dict) -> Dict:
    return {
        "ref": ref,
        "doc_name": c.get("doc_name", "") or "Unknown document",
        "heading": c.get("section_heading") or "",
        "page": c.get("page_number"),
        "chunk_id": c.get("chunk_id"),
        "repo_id": c.get("repo_id"),
        "snippet": (c.get("chunk_text", "") or "").strip()[:240],
        "score": round(float(score), 4),
    }


def _renumber_and_cite(answer: str, passages: List[Tuple[float, Dict]]) -> Tuple[str, List[Dict]]:
    """Make the visible citations consistent: keep only [n] markers that point to
    a real passage, renumber them sequentially (1,2,3…) in order of first
    appearance, rewrite the answer text to match, and drop dangling markers.
    This guarantees every [n] the user sees has a matching source chip."""
    mapping: Dict[int, int] = {}
    order: List[int] = []
    for m in _CITE_RE.findall(answer or ""):
        n = int(m)
        if 1 <= n <= len(passages) and n not in mapping:
            mapping[n] = len(mapping) + 1
            order.append(n)

    def _repl(mt):
        n = int(mt.group(1))
        return f"[{mapping[n]}]" if n in mapping else ""   # drop out-of-range/dangling

    new_answer = _CITE_RE.sub(_repl, answer or "").strip()
    cites = [_cite_obj(mapping[old], passages[old - 1][0], passages[old - 1][1])
             for old in order]
    return new_answer, cites


# ── In-app "how to use the tool" guide ───────────────────────────────────────
# Curated, authoritative walkthrough of the app. Used for scope_type == "help"
# so customers who don't know how to use the tool get clear, step-wise answers.
# An optional static/tool_guide.md (or the existing help.html) overrides/enriches
# this at runtime so the guide can be maintained without code changes.
TOOL_GUIDE = """
PRODUCT: Blueprint Document Agent (Kinaxis RapidStart / RapidResponse), by BristleCone.
WHAT IT DOES: An AI-assisted authoring tool that generates Blueprint Document (BRD)
sections from the source materials you upload (workshop transcripts, RFP/SOW, existing
blueprints, user-story sheets) and process images (Kinaxis screenshots, swimlane
diagrams). It synthesises from what you upload — it does not fabricate content.
KEY PRINCIPLE: Output quality is proportional to the quality/completeness of uploads.

MAIN SCREENS (top navigation):
- Projects: create or open a project (one per Blueprint Document / client engagement).
- Upload (Document Upload): add source documents to the open project.
- Repositories: reusable libraries of documents that can be attached to many projects.
- Image Library / Image Repository: import process diagrams and Kinaxis screenshots.
- Templates: manage Blueprint Document section templates.
- Editor (Template Editor / Screen 3): author sections — generate, review, approve.
- Help: the full author's guide and cheat sheet.
- AI Assistant (chat button, bottom-right): ask questions grounded in your documents,
  or switch to "How to use the tool" mode for app help.

RECOMMENDED UPLOAD ORDER (most important first):
1) Workshop transcripts (.docx/.txt, one per workstream) — drives all content.
2) User-story sheet (.xlsx) — acceptance criteria and process detail per story.
3) RFP / SOW — scope boundaries, client name, deliverables.
4) Existing blueprint / design doc — prior-phase context.
5) Process diagrams (PNG/JPEG) → Image Library — Level 2/3 flows first.
6) Kinaxis screenshots → Image Library — one per user story / config screen.
7) IT / data-architecture docs — interface specs, ETL designs.

END-TO-END WORKFLOW:
PHASE 1 — GATHER (before opening the tool): collect and clean workshop transcripts,
user-story sheet, RFP/SOW, prior blueprints, and process images.
PHASE 2 — SET UP:
  Step 1: Go to Projects → "New project" → give it a name → open it.
  Step 2: Go to Upload → drag-and-drop your source documents. Each file is processed
          (chunked + indexed) automatically; wait for status "indexed".
  Step 3 (optional): Go to Repositories → create a repository, upload shared documents
          there, then attach the repository to your project so its content is available.
  Step 4: Upload process images via the Image Library.
  Step 5: Go to Templates (or the Editor) and apply a Blueprint Document template.
PHASE 3 — AUTHOR (Editor / Screen 3):
  - Pick a section from the left list. Use "Browse Chunks" to see the source passages
    and "Browse Images" to attach diagrams.
  - Click Generate to draft the section from your sources, then review and edit.
  - Approve the section when satisfied; repeat for each section.
PHASE 4 — EXPORT:
  - Do a final quality check, then Export to Word (.docx); PDF export is also available.

USING THE AI ASSISTANT (this chatbot):
  - Click the chat button (bottom-right). In "My documents" mode, tick one or more
    repositories in the picker, then ask a question — answers are grounded in those
    documents and show citations (source file, section, page).
  - In "How to use the tool" mode, ask things like "how do I create a project?" or
    "how do I export to Word?" and you'll get step-by-step instructions.

TROUBLESHOOTING:
  - Empty/weak answers in document mode → make sure the documents are uploaded and show
    status "indexed", and that the right repositories are selected in the picker.
  - A section won't generate → confirm a template is applied and source documents exist.
""".strip()

_TOOL_GUIDE_CACHE: Optional[str] = None


def _load_tool_guide() -> str:
    """Return the tool guide. Prefers static/tool_guide.md if present, else the
    embedded TOOL_GUIDE constant (cached after first read)."""
    global _TOOL_GUIDE_CACHE
    if _TOOL_GUIDE_CACHE is not None:
        return _TOOL_GUIDE_CACHE
    guide = TOOL_GUIDE
    try:
        from pathlib import Path
        md = Path(__file__).parent / "static" / "tool_guide.md"
        if md.exists():
            txt = md.read_text(encoding="utf-8").strip()
            if txt:
                guide = txt
    except Exception:
        pass
    _TOOL_GUIDE_CACHE = guide
    return guide


def _build_help_prompt(question: str, guide: str) -> str:
    return (
        "You are the in-app help assistant for the Blueprint Document Agent. "
        "Answer the user's question about HOW TO USE THIS TOOL using the guide below. "
        "Give clear, numbered, step-by-step instructions that a non-technical user can "
        "follow, and name the exact screen or button to click. Keep it friendly and "
        "concise. If the guide does not cover the question, give your best brief guidance "
        "and suggest opening the Help page. Do not discuss the user's uploaded documents "
        "here — this is about operating the tool itself.\n\n"
        "=== TOOL GUIDE ===\n"
        f"{guide}\n\n"
        "=== QUESTION ===\n"
        f"{question}\n\n"
        "=== ANSWER (numbered steps where appropriate) ===\n"
    )


def _help_response(question: str, auto: bool = False) -> Dict:
    """Answer a 'how to use the tool' question from the in-app guide."""
    prompt = _build_help_prompt(question, _load_tool_guide())
    try:
        answer = _completion(prompt)
    except Exception as exc:
        logger.exception("chat_ask(help): LLM completion failed")
        raise HTTPException(502, f"LLM request failed: {exc}")
    return {
        "answer": _strip_cite_markers((answer or "").strip()),
        "citations": [],
        "scope_type": "help-auto" if auto else "help",
        "scope_id": None, "backend": "guide", "passages_used": 1,
    }


def _general_knowledge_response(question: str, scope_type: str, scope_id) -> Dict:
    """Hybrid-mode fallback when retrieval finds nothing: clearly-labelled general
    knowledge, no document citations."""
    try:
        answer = _completion(_general_knowledge_prompt(question))
    except Exception as exc:
        logger.exception("chat_ask: general-knowledge completion failed")
        raise HTTPException(502, f"LLM request failed: {exc}")
    return {
        "answer": _strip_cite_markers((answer or "").strip()),
        "citations": [],          # general knowledge — never cite documents
        "sources": [],
        "images": _relevant_images(question) if _wants_images(question) else [],
        "scope_type": scope_type, "scope_id": scope_id,
        "backend": "general", "passages_used": 0,
    }


# Detect questions about operating the TOOL itself (vs. the user's documents),
# used to auto-route to the guide when a document search comes up empty.
_HOWTO_VERB = re.compile(
    r"\b(use|create|open|upload|export|download|generate|attach|apply|add|delete|"
    r"start|begin|set ?up|configure|login|log ?in|sign ?up|navigate|find|make)\b", re.I)
_HOWTO_Q = re.compile(r"\b(how|where|which|what|can i|do i|steps?)\b", re.I)
_APP_NOUNS = (
    "this tool", "the tool", "this app", "the app", "the website", "the system",
    "this website", "this system", "project", "upload", "template", "repository",
    "repositories", "editor", "section", "export", "login", "log in", "sign up",
    "button", "screen", "page", "blueprint document", "brd document",
)


def _looks_like_tool_howto(question: str) -> bool:
    q = (question or "").lower()
    if "how to use" in q or "how do i use" in q or "how to use the tool" in q:
        return True
    return bool(_HOWTO_Q.search(q) and _HOWTO_VERB.search(q)
                and any(n in q for n in _APP_NOUNS))


# ── Request / response models ────────────────────────────────────────────────
class ChatAskRequest(BaseModel):
    question: str
    scope_type: str = "repo"          # "repo" | "repos" | "project" | "all"
    scope_id: Optional[str] = None     # repo_id when repo, project_id when project
    repo_ids: Optional[List[str]] = None  # list of repo_ids when scope_type == "repos"
    answer_mode: str = "hybrid"        # "hybrid" (docs + labelled AI fallback) | "strict" (docs only)


# ── Endpoints ─────────────────────────────────────────────────────────────────
@router.get("/api/chat/scopes")
def chat_scopes(project_id: Optional[str] = None):
    """List repositories the chat can be pointed at.

    Returns every repository, and (when project_id is given) which ones are
    attached to that project so the UI can offer an 'All repos in this project'
    option alongside individual repositories.
    """
    repos = _repo_registry()
    attached = set(_project_repo_ids(project_id)) if project_id else set()

    repo_list = [
        {
            "repo_id": rid,
            "name": r.get("name", rid),
            "attached_to_project": rid in attached,
        }
        for rid, r in repos.items()
    ]
    repo_list.sort(key=lambda x: (not x["attached_to_project"], x["name"].lower()))

    return {
        "project_id": project_id,
        "project_repo_count": len(attached),
        "repositories": repo_list,
    }


@router.post("/api/chat")
def chat_ask(req: ChatAskRequest):
    """Answer a question grounded in the selected repository / project documents."""
    question = (req.question or "").strip()
    if not question:
        raise HTTPException(400, "question is required")

    scope_type = (req.scope_type or "repo").strip().lower()
    hybrid = (req.answer_mode or "hybrid").strip().lower() != "strict"

    # ── "How to use the tool" mode — answer from the in-app guide ────────────
    if scope_type == "help":
        return _help_response(question)

    # Resolve which repositories to search ------------------------------------
    if scope_type == "project":
        if not req.scope_id:
            raise HTTPException(400, "scope_id (project_id) is required for project scope")
        repo_ids = _project_repo_ids(req.scope_id)
        if not repo_ids:
            return {
                "answer": "This project has no repositories attached yet. "
                          "Attach a repository, then ask again.",
                "citations": [], "scope_type": scope_type, "scope_id": req.scope_id,
                "backend": None, "passages_used": 0,
            }
    elif scope_type == "repo":
        if not req.scope_id:
            raise HTTPException(400, "scope_id (repo_id) is required for repo scope")
        if req.scope_id not in _repo_registry():
            raise HTTPException(404, f"Repository '{req.scope_id}' not found")
        repo_ids = [req.scope_id]
    elif scope_type == "repos":
        # Multi-select: search across a chosen subset of repositories.
        requested = req.repo_ids or ([req.scope_id] if req.scope_id else [])
        if not requested:
            raise HTTPException(400, "repo_ids is required for 'repos' scope")
        registry = _repo_registry()
        repo_ids = [rid for rid in requested if rid in registry]
        if not repo_ids:
            raise HTTPException(404, "None of the requested repositories were found")
    elif scope_type == "all":
        # Search across every repository the user has, regardless of project.
        repo_ids = list(_repo_registry().keys())
        if not repo_ids:
            return {
                "answer": "There are no repositories yet. Create a repository and "
                          "upload documents, then ask again.",
                "citations": [], "scope_type": scope_type, "scope_id": None,
                "backend": None, "passages_used": 0,
            }
    else:
        raise HTTPException(400, "scope_type must be 'repo', 'repos', 'project', or 'all'")

    # Answer cache: identical question + scope + mode → instant identical reply
    _ck = _answer_cache_key(question, scope_type, req.scope_id, repo_ids, hybrid)
    _cached = _answer_cache_get(_ck)
    if _cached is not None:
        logger.info("chat_ask: answer cache hit (q=%r)", question[:60])
        return {**_cached, "cached": True}

    # Load + retrieve ---------------------------------------------------------
    chunks = _load_chunks_for_repos(repo_ids)
    logger.info(
        "chat_ask: scope=%s scope_id=%s repos=%d chunks=%d q=%r",
        scope_type, req.scope_id, len(repo_ids), len(chunks), question[:80],
    )
    if not chunks:
        return {
            "answer": "I couldn't find any indexed content in the selected scope. "
                      "Upload and process documents in the repository first.",
            "citations": [], "scope_type": scope_type, "scope_id": req.scope_id,
            "backend": None, "passages_used": 0,
        }

    passages, backend = _retrieve(question, chunks)
    if not passages:
        # Tool-usage questions → in-app guide regardless of mode.
        if _looks_like_tool_howto(question):
            return _help_response(question, auto=True)
        # Hybrid mode: fall back to clearly-labelled general knowledge.
        if hybrid:
            return _general_knowledge_response(question, scope_type, req.scope_id)
        # Strict mode: documents only.
        return {
            "answer": NOT_FOUND_DOC + " (Strict document mode — general knowledge is "
                      "disabled. Switch to Hybrid mode, select more repositories, or "
                      "rephrase using the exact terms in the documents.)",
            "citations": [], "scope_type": scope_type, "scope_id": req.scope_id,
            "backend": backend, "passages_used": 0,
        }

    # Generate (Hybrid RAG strategy) ------------------------------------------
    prompt = _build_prompt(question, passages, hybrid=hybrid)
    try:
        answer = _completion(prompt)
    except Exception as exc:  # surface LLM/auth/quota errors clearly to the UI
        logger.exception("chat_ask: LLM completion failed")
        # Content filter blocked the grounded prompt (usually triggered by
        # verbatim document text). In hybrid mode, fall back to a general-
        # knowledge answer (different prompt, no doc passages) instead of
        # surfacing a raw error to the user.
        _m = str(exc).lower()
        if hybrid and ("content filter" in _m or "content_filter" in _m
                       or "content management policy" in _m):
            logger.warning("chat_ask: content filter blocked — falling back to general knowledge.")
            try:
                return _general_knowledge_response(question, scope_type, req.scope_id)
            except Exception:
                logger.exception("chat_ask: general-knowledge fallback also failed")
        raise HTTPException(502, f"LLM request failed: {exc}")

    answer = (answer or "").strip()
    # T11: the question targets a named project/entity the documents don't cover —
    # return the polite refusal verbatim, with no sources or images attached.
    if _is_out_of_scope(answer):
        return {
            "answer": OUT_OF_SCOPE,
            "citations": [], "sources": [], "images": [],
            "scope_type": scope_type, "scope_id": req.scope_id,
            "backend": backend, "passages_used": 0,
        }
    # Nothing relevant in the documents.
    if _is_not_found(answer):
        if _looks_like_tool_howto(question):
            return _help_response(question, auto=True)
        # In hybrid mode the model itself may have appended a labelled
        # "General Knowledge" section; keep the answer but show no doc citations.
        return {
            "answer": _strip_cite_markers(answer) if hybrid else (
                NOT_FOUND_DOC + " (Strict document mode.) Try selecting more "
                "repositories or rephrasing with the exact terms in the documents."),
            "citations": [],
            "scope_type": scope_type, "scope_id": req.scope_id,
            "backend": backend, "passages_used": 0,
        }

    # Per user preference: deliver a clean answer with no citation markers or
    # source chips — strip any residual [n] the model may have added.
    answer = _strip_cite_markers(answer)
    # T7: tell the user which document(s) the answer was grounded in.
    sources = _sources_from_passages(passages)
    # T5: surface relevant images when the user asked to see one — tied to the
    # passages that grounded the answer so they match the discussed topic.
    images = _relevant_images(question, passages) if _wants_images(question) else []
    resp = {
        "answer": answer,
        "citations": [],
        "sources": sources,
        "images": images,
        "scope_type": scope_type,
        "scope_id": req.scope_id,
        "backend": backend,
        "passages_used": len(passages),
    }
    _answer_cache_put(_ck, resp)   # store successful answers for identical re-asks
    return resp


# ── Export chat answers to DOCX / PDF / TXT ──────────────────────────────────
class ChatExportRequest(BaseModel):
    format: str = "docx"                    # "docx" | "pdf" | "txt"
    title: Optional[str] = None
    items: List[Dict[str, str]] = []        # [{"question": ..., "answer": ...}]


_EXPORT_MEDIA = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pdf":  "application/pdf",
    "txt":  "text/plain; charset=utf-8",
}


@router.post("/api/chat/export")
def chat_export(req: ChatExportRequest):
    """Export one answer or a whole conversation as a downloadable file."""
    from fastapi.responses import FileResponse

    fmt = (req.format or "docx").strip().lower()
    if fmt not in _EXPORT_MEDIA:
        raise HTTPException(400, "format must be 'docx', 'pdf', or 'txt'")
    items = [i for i in (req.items or [])
             if (i.get("question") or "").strip() or (i.get("answer") or "").strip()]
    if not items:
        raise HTTPException(400, "items is required — nothing to export")

    try:
        from app.chat_export import render_export
    except ImportError:
        from chat_export import render_export
    try:
        path = render_export(items, fmt, req.title)
    except Exception as exc:
        logger.exception("chat_export: rendering failed")
        raise HTTPException(500, f"Export failed: {exc}")

    return FileResponse(
        path=str(path),
        media_type=_EXPORT_MEDIA[fmt],
        filename=path.name,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# ══════════════════════════════════════════════════════════════════════════════
# Chat history (T6) — server-side, per-user persistence (Phase 2)
# ──────────────────────────────────────────────────────────────────────────────
# Conversations are stored in a single JSON file keyed by user email. This mirrors
# the JSON-state pattern used elsewhere in the app (projects/repos state) and adds
# no new dependencies. The chat endpoints are unauthenticated in this app, so the
# user identity is supplied by the client (the email kept in sessionStorage after
# login) — consistent with how the rest of the API is called.
_CHAT_HISTORY_PATH = Path(__file__).parent / "chat_history.json"
_CHAT_HISTORY_LOCK = threading.Lock()


def _normalise_user(user: Optional[str]) -> str:
    u = (user or "").strip().lower()
    return u or "_anonymous"


def _load_history() -> Dict[str, Any]:
    try:
        if _CHAT_HISTORY_PATH.exists():
            with open(_CHAT_HISTORY_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.warning("chat_history: failed to read store: %s", exc)
    return {}


def _save_history(data: Dict[str, Any]) -> None:
    tmp = _CHAT_HISTORY_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, _CHAT_HISTORY_PATH)


def _derive_title(messages: List[Dict]) -> str:
    """Use the first user message as the chat title (trimmed)."""
    for m in messages or []:
        if (m.get("role") == "user") and (m.get("content") or "").strip():
            t = " ".join((m["content"]).split())
            return t[:60] + ("…" if len(t) > 60 else "")
    return "New chat"


class ChatHistorySaveRequest(BaseModel):
    user: Optional[str] = None
    chat_id: Optional[str] = None
    title: Optional[str] = None
    messages: List[Dict[str, Any]] = []


@router.get("/api/chat/history")
def list_chat_history(user: Optional[str] = None):
    """List a user's saved chats (most-recent first), without message bodies."""
    uid = _normalise_user(user)
    with _CHAT_HISTORY_LOCK:
        store = _load_history()
        chats = store.get(uid, {})
        summary = [
            {
                "id": cid,
                "title": c.get("title") or "New chat",
                "updated_at": c.get("updated_at", 0),
                "message_count": len(c.get("messages", [])),
            }
            for cid, c in chats.items()
        ]
    summary.sort(key=lambda x: -(x.get("updated_at") or 0))
    return {"chats": summary}


@router.get("/api/chat/history/{chat_id}")
def get_chat_history(chat_id: str, user: Optional[str] = None):
    """Return a single chat with its full message list."""
    uid = _normalise_user(user)
    with _CHAT_HISTORY_LOCK:
        store = _load_history()
        chat = store.get(uid, {}).get(chat_id)
    if not chat:
        raise HTTPException(404, "Chat not found")
    return chat


@router.post("/api/chat/history")
def save_chat_history(req: ChatHistorySaveRequest):
    """Create or update a chat. Returns the chat id and title. Empty chats
    (no messages) are not persisted."""
    uid = _normalise_user(req.user)
    messages = req.messages or []
    if not messages:
        raise HTTPException(400, "Cannot save an empty chat")
    now = int(time.time())
    cid = req.chat_id or uuid.uuid4().hex[:12]
    with _CHAT_HISTORY_LOCK:
        store = _load_history()
        user_chats = store.setdefault(uid, {})
        existing = user_chats.get(cid, {})
        chat = {
            "id": cid,
            "title": (req.title or "").strip() or _derive_title(messages),
            "created_at": existing.get("created_at", now),
            "updated_at": now,
            "messages": messages,
        }
        user_chats[cid] = chat
        _save_history(store)
    return {"id": cid, "title": chat["title"], "updated_at": now}


@router.delete("/api/chat/history/{chat_id}")
def delete_chat_history(chat_id: str, user: Optional[str] = None):
    """Delete one of a user's chats."""
    uid = _normalise_user(user)
    with _CHAT_HISTORY_LOCK:
        store = _load_history()
        user_chats = store.get(uid, {})
        if chat_id in user_chats:
            del user_chats[chat_id]
            _save_history(store)
            return {"ok": True}
    raise HTTPException(404, "Chat not found")
