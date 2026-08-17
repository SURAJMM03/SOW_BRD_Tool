import re
from typing import Dict, List

from agents.brd_agent.agents.br_agent import create_generation_agent

# Match <SECTION id="X.Y"> blocks in LLM output
SECTION_RE = re.compile(
    r'<SECTION\s+id="([\d.]+)">(.*?)</SECTION>',
    re.DOTALL | re.IGNORECASE,
)

try:
    from app.section_titles import get_titles as _get_titles
except ImportError:
    try:
        from section_titles import get_titles as _get_titles
    except ImportError:
        def _get_titles(): return {}

def _live_titles():
    return _get_titles()


def generate_br_sections_from_qa(qa_records: list, research_context: str = "") -> dict:
    """
    Generate sections 2, 2.1, 2.2.
    Uses research_context only - no hardcoded content.
    research_context contains: document chunks, client context,
    section-specific format instructions, keywords.
    """
    gen_agent = create_generation_agent()

    # Extract client company name from research_context to override "Bristlecone" in system prompt
    import re as _re_name
    _client_match = _re_name.search(
        r"Client company:\s*([^\n]+)", research_context or ""
    )
    _client_name = _client_match.group(1).strip() if _client_match else ""
    # Build a name correction preamble so LLM doesn't confuse client with SI
    _name_correction = (
        f"IMPORTANT: The CLIENT company is {_client_name!r}. "
        "Bristlecone is the system integrator (SI), NOT the investing company. "
        f"All generated content must be about {_client_name!r}'s investment and business. "
        "Never say 'Bristlecone is investing' or 'Bristlecone's supply chain'.\n\n"
    ) if _client_name else ""

    # Detect single-section mode: the backend stamps "TARGET_SECTION: {id}" at the
    # top of research_context for all section-by-section generation calls so this
    # agent generates ONLY the requested section instead of all sub-sections at once.
    import re as _re_agent
    _single_match = _re_agent.search(
        r"TARGET_SECTION:\s*([\d.]+)",
        research_context or ""
    )
    if _single_match:
        _target_sid = _single_match.group(1)
        gen_prompt = (
            _name_correction +
            "You are a senior Blueprint Document writer for enterprise solution "
            "implementations at Bristlecone.\n\n"
            f"TASK: Generate ONLY section {_target_sid}. Do NOT generate any other sections.\n"
            f"Write ONLY the body for section {_target_sid} as flowing prose/paragraphs. Do NOT create "
            f"sub-sections, sub-headings, or numbered sub-parts (e.g. {_target_sid}.1, {_target_sid}.2) and "
            f"do NOT repeat the section number or title as a heading inside the body — those sub-sections "
            f"are generated separately on their own.\n\n"
            "Use the RESEARCH CONTEXT below as your only source of content and instructions.\n"
            f"Output ONLY <SECTION id=\"{_target_sid}\">...</SECTION> -- nothing else.\n\n"
            "RESEARCH CONTEXT:\n"
            f"{research_context or 'No context provided.'}\n\n"
            f"Generate ONLY section {_target_sid} using <SECTION id=\"{_target_sid}\"> tags."
        )
    else:
        gen_prompt = (
            _name_correction +
            "You are a senior Blueprint Document writer for enterprise solution "
            "implementations at Bristlecone.\n\n"
            "TASK: Generate sections 2, 2.1, and 2.2 for the Blueprint Document.\n\n"
            "=== CRITICAL COMPANY NAME RULE ===\n"
            f"The CLIENT is {_client_name!r} — this is the company INVESTING in the target solution.\n"
            "Bristlecone is the SYSTEM INTEGRATOR (SI/vendor). It does NOT invest. It implements.\n"
            f"Solution Overview MUST say '{_client_name} is investing in the target solution platform'.\n"
            f"NEVER say 'Bristlecone is investing' or 'Bristlecone\'s supply chain'.\n\n"
            "=== SECTION FORMAT RULES ===\n\n"
            "SECTION 2 — Benefit Realization:\n"
            f"One paragraph about {_client_name}\'s investment: platform, staged approach (Supply Planning first), transformation goal.\n\n"
            "SECTION 2.1 — Business Issues:\n"
            "PURPOSE: STRATEGIC executive pain points justifying the investment. NOT operational mechanics.\n"
            "DO INCLUDE: tool fragmentation (SAP ECC/APO/Excel), EOL dates (2027/2030), scenario planning "
            "limitations, S&OP visibility gaps, talent risks, regional standardization issues.\n"
            "DO NOT INCLUDE: part numbers, SKU codes, capacity units, MRP netting, pre-planning details, "
            "user story mechanics — these belong in sections 5-6, not section 2.1.\n"
            "FORMAT: 3-5 main bullets (bold headings), each with 2-4 sub-bullets.\n"
            "• **Strategic Heading** (summarises sub-points)\n"
            "  - Sub-point: 1-2 explanatory sentences from source documents.\n"
            "NEVER flat bullets. NEVER operational detail in this section.\n\n"
            "SECTION 2.2 — Value Drivers:\n"
            "Three subsections in this EXACT order:\n"
            f"  Solution Overview: {_client_name} is investing in the target solution platform as the integrated "
            "Supply and Demand Planning platform, starting with Supply Planning phase, replacing disparate "
            "regional processes/Excel models to improve profitability. [Add document-sourced context.]\n"
            "  Key Solution Components: • [extract from kickoff/executive docs — strategic capabilities + SI role]\n"
            "  Strategic Assumptions: • [extract from docs — typically market/technology conditions, 2 items]\n"
            "  Source: [cite the SPECIFIC document name actually present in the research context — never an example client name]\n"
            "NEVER: generic IT phrases ('leverage ERP', 'continuous improvement', 'KPI tracking').\n"
            "NEVER: fabricated percentages. Source must name the actual document.\n\n"
            "=== CONTENT RULES ===\n"
            "1. RESEARCH CONTEXT is your ONLY source — extract facts from it.\n"
            f"2. Use '{_client_name}' throughout — never 'the client'.\n"
            "3. Do NOT hallucinate. Write [TBD] if a fact is absent.\n"
            "4. Output ONLY <SECTION id=\"X\">...</SECTION> blocks — nothing else.\n\n"
            "RESEARCH CONTEXT:\n"
            f"{research_context or 'No context provided.'}\n\n"
            "Generate sections 2, 2.1, 2.2 using <SECTION id=\"X\"> tags only."
        )

    result = gen_agent.invoke(
        {"messages": [{"role": "user", "content": gen_prompt}]}
    )

    raw = result["messages"][-1].content
    return {sid: text.strip() for sid, text in SECTION_RE.findall(raw)}


def assemble_document(sections: dict) -> str:
    lines = ["=" * 60, "Blueprint Document Sections", "=" * 60, ""]
    for sid in sorted(sections.keys(), key=lambda s: [int(p) if p.isdigit() else p for p in s.split(".")]):
        title = _live_titles().get(sid, "")
        lines.append(f"Section {sid}: {title}" if title else f"Section {sid}")
        lines.append(sections[sid])
        lines.append("")
    return "\n".join(lines)