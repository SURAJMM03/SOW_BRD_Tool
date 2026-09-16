"""
Phase 5: Inject Images into Research Context

Modifies conversation.py to include relevant images in the research context
that agents see when generating sections.
"""

import logging
import re
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


# Retrieval gates. BM25 recall is deliberately loose so that a genuinely
# relevant image is never missed at the retrieval stage; precision is then
# enforced by these thresholds and, finally, by verify_image_relevance.
_MIN_TERM_COVERAGE = 0.18
_MIN_SCORE_RATIO = 0.45


def get_relevant_images(query: str, top_k: int = 3,
                        section_hint: Optional[str] = None,
                        section_id: Optional[str] = None,
                        source_docs: Optional[set] = None,
                        prefer_diagrams: bool = False,
                        min_term_coverage: float = _MIN_TERM_COVERAGE,
                        min_score_ratio: float = _MIN_SCORE_RATIO) -> List[Dict]:
    """Search for images relevant to a query.

    section_id: hard filter — only images tagged to this BRD top-level section
                are returned (untagged images always pass through).
    section_hint: legacy soft boost, kept for backward compatibility.
    source_docs: hard filter on source filename, applied before the top_k cut
                so a project's own images aren't crowded out of the window by
                the globally-shared index (see ImageIndex.search).
    prefer_diagrams: down-weight logos/icons so real diagrams win the window.
    min_term_coverage / min_score_ratio: relevance floors — see
                ImageIndex.search. Defaults are non-zero here because every
                caller in this codebase wants relevant images rather than
                merely the top_k least-bad ones.
    """
    try:
        from app.image_index import search_images
        return search_images(
            query=query,
            top_k=top_k,
            section_hint=section_hint,
            section_id=section_id,
            source_docs=source_docs,
            prefer_diagrams=prefer_diagrams,
            min_term_coverage=min_term_coverage,
            min_score_ratio=min_score_ratio,
        )
    except Exception as e:
        logger.warning(f"Failed to search images: {e}")
        return []


def filter_already_used(image_hits: List[Dict],
                        used_image_ids: Optional[set]) -> List[Dict]:
    """Drop candidates already embedded elsewhere in the same document.

    Each section is drafted by its own model call with no view of what the
    other sections chose, so one strong diagram gets offered to — and taken
    by — several of them. Passing the ids claimed so far lets a later section
    fall through to its own next-best image instead of repeating one the
    reader has already seen. Export-time deduplication is the backstop that
    catches whatever slips past this (see _resolve_images_in_docx).
    """
    if not used_image_ids:
        return image_hits
    return [img for img in image_hits
            if img.get("image_id") not in used_image_ids]


_VERIFY_PROMPT = """You are deciding which figures may be embedded in one \
section of a formal client Blueprint/SOW document.

SECTION: {title}

WHAT THIS SECTION COVERS:
{context}

CANDIDATE IMAGES (retrieved by keyword match, which is why some will be wrong):
{candidates}

For each candidate decide whether it genuinely belongs in THIS section.

KEEP an image only if its subject matter is what this section is actually \
about, so that a client reading the section would see the figure as \
illustrating the text next to it.

DROP an image if any of these apply:
- it is a logo, icon, brand mark, decorative graphic, or stock illustration
- it is a cover slide, agenda, thank-you or divider slide
- it relates to the wider project but not to this specific section's topic
- it merely shares generic vocabulary with the section ("platform", \
"process", "solution") without depicting this section's subject
- you cannot tell what it depicts from its description

Being unsure is a reason to DROP. A section with no figure is correct and \
expected; a section carrying an unrelated figure is a defect.

Reply with one line per candidate, nothing else:
<number>: KEEP <short reason>
<number>: DROP <short reason>"""


def verify_image_relevance(image_hits: List[Dict], section_title: str,
                           section_context: str = "",
                           fail_open: bool = False) -> List[Dict]:
    """Second-stage filter: ask the model which retrieved images truly belong.

    Keyword retrieval cannot tell "supply chain network diagram" from "vendor
    logo that happens to sit on a slide about the supply chain network" — both
    carry the same vocabulary in their captions. This pass reads each
    candidate's caption/description against what the section is actually about
    and drops everything that is not clearly on-topic.

    fail_open: when the verification call itself fails, return the unverified
        candidates instead of dropping them. Defaults to False — an unrelated
        figure in a client deliverable is worse than a missing one, and the
        retrieval gates upstream are not tight enough to stand alone.
    """
    if not image_hits:
        return []

    lines = []
    for idx, img in enumerate(image_hits, 1):
        parts = [f"{idx}. Caption: {img.get('caption', 'Untitled')}"]
        if img.get("diagram_type"):
            parts.append(f"   Type: {img['diagram_type']}")
        if img.get("section_heading"):
            parts.append(f"   Appeared under: {img['section_heading']}")
        if img.get("keywords"):
            parts.append(f"   Keywords: {', '.join(img['keywords'][:8])}")
        if img.get("description"):
            parts.append(f"   Description: {img['description'][:400]}")
        lines.append("\n".join(parts))

    prompt = _VERIFY_PROMPT.format(
        title=section_title or "(untitled section)",
        context=(section_context or "(no additional context)")[:2000],
        candidates="\n\n".join(lines),
    )

    try:
        from app.claude_provider import completion_from_prompt
        reply = completion_from_prompt(prompt, max_tokens=500, temperature=0.0)
    except Exception as e:
        logger.warning("Image relevance verification failed (%s) — %s", e,
                       "keeping candidates" if fail_open else "dropping all candidates")
        return list(image_hits) if fail_open else []

    keep_idx = set()
    for line in (reply or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^\s*(\d+)\s*[:.)-]\s*(KEEP|DROP)\b", line, re.I)
        if m and m.group(2).upper() == "KEEP":
            keep_idx.add(int(m.group(1)))

    verified = [img for i, img in enumerate(image_hits, 1) if i in keep_idx]
    logger.info("Image relevance check for %r: %d candidate(s) -> %d kept",
                section_title, len(image_hits), len(verified))
    return verified


def format_images_for_context(image_hits: List[Dict],
                              require_at_least_one: bool = False) -> str:
    """Format image search results as an embed-instruction block.

    Each image entry includes a clear directive so the LLM places
    an <IMAGE id="..."/> tag at the most relevant spot in its output.
    That tag is later resolved to an actual embedded image in the DOCX.

    require_at_least_one: for sections the template marks as diagram-worthy
        (architecture, process flow, implementation approach …). The plain
        "omit it if not relevant" wording loses reliably against the density
        rules that end the prompt — offered four genuinely relevant flowcharts
        for a "To-Be Process Flow" sub-section, the model still emitted zero
        tags every time, because omitting is always the shorter answer. For
        those sections the choice of WHICH image stays with the model, but the
        choice of whether to include one at all does not.
    """
    if not image_hits:
        return ""

    block = "\n## Auto-suggested Images — EMBED WHERE RELEVANT\n"
    if require_at_least_one:
        block += (
            "This section is expected to carry a visual. You MUST place AT LEAST ONE "
            "of the images below, using the exact tag shown in its EMBED INSTRUCTION, "
            "on its own standalone line at the most appropriate point in the section "
            "text. Choose the one that best fits what you are writing; skip the rest. "
            "These images came from this project's own source material, so a "
            "reasonable fit exists — do not skip them all, and do not let brevity "
            "rules talk you out of including one. The tag itself costs almost no "
            "length. Never invent an id: copy one exactly as written below.\n\n"
        )
    else:
        block += (
            "For each image below, insert the exact tag shown in EMBED INSTRUCTION "
            "on a standalone line at the most appropriate location in the section text. "
            "If an image is not relevant to this section, omit it. Never invent an "
            "id: copy one exactly as written below.\n\n"
        )

    for idx, img in enumerate(image_hits, 1):
        iid = img.get("image_id", "")
        block += f"Image {idx}: {img.get('caption', 'Untitled')}\n"
        block += f"  Type: {img.get('diagram_type', 'unknown')}\n"
        block += f"  Keywords: {', '.join(img.get('keywords', [])[:6])}\n"
        if img.get("section_heading"):
            block += f"  Section: {img['section_heading']}\n"
        if img.get("description"):
            block += f"  Description: {img['description'][:200]}\n"
        block += f'  EMBED INSTRUCTION: <IMAGE id="{iid}"/>\n\n'

    block += "## End Auto-suggested Images\n\n"
    return block


def append_images_to_research_context(research_context: str,
                                      query: str,
                                      section_hint: Optional[str] = None,
                                      section_id: Optional[str] = None,
                                      top_k: int = 3,
                                      project_id: Optional[str] = None,
                                      section_title: Optional[str] = None,
                                      verify: bool = True) -> tuple:
    """Append image search results (with embed instructions) to research context.

    section_id: hard BRD section filter passed to the image index.
    project_id: accepted for call-site compatibility (prevents the TypeError that
        previously broke auto image injection); image retrieval is global today.
    verify: run the retrieved shortlist past verify_image_relevance before
        offering any of it for embedding. Keyword retrieval alone cannot tell a
        diagram of the section's subject from a logo on a slide that mentions
        it, and anything offered here is a candidate for the model to place in
        the finished document.
    Returns (updated_research_context, image_hits).
    """
    # Over-retrieve, then let verification cut it back to what actually fits;
    # starting at top_k leaves nothing once the wrong ones are removed.
    image_hits = get_relevant_images(
        query, top_k=(top_k * 3 if verify else top_k),
        section_hint=section_hint,
        section_id=section_id,
    )

    if image_hits and verify:
        image_hits = verify_image_relevance(
            image_hits,
            section_title=section_title or section_id or "",
            section_context=query,
        )[:top_k]

    if image_hits:
        images_block = format_images_for_context(image_hits)
        updated_context = research_context + "\n" + images_block
        logger.info("Injected %d auto-suggested images into research context", len(image_hits))
        return updated_context, image_hits

    return research_context, []


# ─── INTEGRATION INSTRUCTIONS ───────────────────────────────────────

INTEGRATION_INSTRUCTIONS = """
PHASE 5 INTEGRATION: Modify orchestration/brd_convo_app/backend/app/conversation.py

1. Add import at top of file:
   ```python
   from image_injection import append_images_to_research_context
   ```

2. Find the function that builds research_context for agents (typically something like:
   `def build_research_context_for_section(section_id, ...)`)

3. After building the text-based research_context, add:
   ```python
   # Get section keywords for image search
   section_keywords = "..." # Use section content or keywords
   
   # Append relevant images
   research_context, image_hits = append_images_to_research_context(
       research_context=research_context,
       query=section_keywords,
       section_hint=section_id,  # e.g. "3 Supply Chain Scope"
       top_k=3
   )
   
   # Store image_hits for later use (in Phase 6)
   # This might be in session state or passed along somehow
   ```

4. Example of where to add this:
   ```python
   def build_research_context_for_section(section_id, user_context, ...):
       # Existing text search
       text_results = doc_search(user_context)
       research_context = format_text_results(text_results)
       
       # NEW: Append images
       research_context, image_hits = append_images_to_research_context(
           research_context=research_context,
           query=user_context,
           section_hint=section_id,
           top_k=3
       )
       
       # Store image_hits somewhere for Phase 6
       # (e.g., session_state["current_image_hits"] = image_hits)
       
       return research_context
   ```

5. The agent will now see the [RELEVANT IMAGES] block in its research context
   and can cite images like: "As shown in Figure [a3f9c12b4d], ..."

6. The agent can emit placeholder tags: <IMAGE id="a3f9c12b4d"/>
   These will be processed in Phase 6 (DOCX embedding)
"""


if __name__ == "__main__":
    # Test image injection
    
    test_context = "Supply chain scope discusses distribution networks and S&OP process."
    
    updated_context, hits = append_images_to_research_context(
        research_context=test_context,
        query="distribution network S&OP",
        section_hint="3 Supply Chain Scope",
        top_k=3
    )
    
    print("ORIGINAL CONTEXT:")
    print(test_context)
    print("\n" + "="*60 + "\n")
    print("UPDATED CONTEXT WITH IMAGES:")
    print(updated_context)
    print("\n" + "="*60 + "\n")
    print(f"Found {len(hits)} images")
    
    print("\n" + "="*60)
    print(INTEGRATION_INSTRUCTIONS)