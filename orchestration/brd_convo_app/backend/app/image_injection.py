"""
Phase 5: Inject Images into Research Context

Modifies conversation.py to include relevant images in the research context
that agents see when generating sections.
"""

import logging
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


def get_relevant_images(query: str, top_k: int = 3,
                        section_hint: Optional[str] = None,
                        section_id: Optional[str] = None) -> List[Dict]:
    """Search for images relevant to a query.

    section_id: hard filter — only images tagged to this BRD top-level section
                are returned (untagged images always pass through).
    section_hint: legacy soft boost, kept for backward compatibility.
    """
    try:
        from app.image_index import search_images
        return search_images(
            query=query,
            top_k=top_k,
            section_hint=section_hint,
            section_id=section_id,
        )
    except Exception as e:
        logger.warning(f"Failed to search images: {e}")
        return []


def format_images_for_context(image_hits: List[Dict]) -> str:
    """Format image search results as an embed-instruction block.

    Each image entry includes a clear directive so the LLM places
    an <IMAGE id="..."/> tag at the most relevant spot in its output.
    That tag is later resolved to an actual embedded image in the DOCX.
    """
    if not image_hits:
        return ""

    block = "\n## Auto-suggested Images — EMBED WHERE RELEVANT\n"
    block += (
        "For each image below, insert the exact tag shown in EMBED INSTRUCTION "
        "on a standalone line at the most appropriate location in the section text. "
        "If an image is not relevant to this section, omit it.\n\n"
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
                                      project_id: Optional[str] = None) -> tuple:
    """Append image search results (with embed instructions) to research context.

    section_id: hard BRD section filter passed to the image index.
    project_id: accepted for call-site compatibility (prevents the TypeError that
        previously broke auto image injection); image retrieval is global today.
    Returns (updated_research_context, image_hits).
    """
    image_hits = get_relevant_images(
        query, top_k=top_k,
        section_hint=section_hint,
        section_id=section_id,
    )

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