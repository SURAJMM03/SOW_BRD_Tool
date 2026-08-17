from __future__ import annotations
import logging
import os
from pathlib import Path
from typing import Dict, Tuple, Optional

from .models import SessionState
try:
    from .brd_workflow import (
        build_question_research_context,
        parse_generation_blocks,
        rewrite_paragraph,
        assemble_document,
        save_txt,
        save_docx,
    )
except Exception:
    build_question_research_context = None
    parse_generation_blocks = None
    rewrite_paragraph = None
    assemble_document = None
    save_txt = None
    save_docx = None
from .claude_provider import completion_from_prompt
import os
# Agent section generators are imported lazily where used to avoid
# import-time dependencies on heavy agent packages during testing.


try:
    from agents.brd_agent.agents.intro_prompt import (
        INTRODUCTION_INPUT_COLLECTION_PROMPT,
    )
except Exception:
    INTRODUCTION_INPUT_COLLECTION_PROMPT = ""

try:
    from agents.brd_agent.agents.br_prompt import (
        BENEFIT_REALIZATION_INPUT_COLLECTION_PROMPT,
    )
except Exception:
    BENEFIT_REALIZATION_INPUT_COLLECTION_PROMPT = ""

try:
    from agents.brd_agent.agents.ssc_prompt import (
        SUPPLY_CHAIN_SCOPE_INPUT_COLLECTION_PROMPT,
    )
except Exception:
    SUPPLY_CHAIN_SCOPE_INPUT_COLLECTION_PROMPT = ""

try:
    from agents.brd_agent.agents.dp_prompts import (
        DEMAND_PLANNING_INPUT_COLLECTION_PROMPT,
    )
except Exception:
    DEMAND_PLANNING_INPUT_COLLECTION_PROMPT = ""

try:
    from agents.brd_agent.agents.ip_prompt import (
        SUPPLY_INVENTORY_INPUT_COLLECTION_PROMPT,
    )
except Exception:
    SUPPLY_INVENTORY_INPUT_COLLECTION_PROMPT = ""

try:
    from agents.brd_agent.agents.di_prompts import (
        DATA_INTEGRATION_INPUT_COLLECTION_PROMPT,
    )
except Exception:
    DATA_INTEGRATION_INPUT_COLLECTION_PROMPT = ""

logger = logging.getLogger("BDConversation")


# --------------------------------------------------
# Message helpers
# --------------------------------------------------
def _assistant(text: str) -> Dict[str, str]:
    return {"role": "assistant", "content": text}

def _user(text: str) -> Dict[str, str]:
    return {"role": "user", "content": text}

def is_non_answer(user_input: str) -> bool:
    if not user_input:
        return True

    normalized = user_input.strip().lower()

    KEYWORDS = [
        "idk", "don't know", "dont know",
        "not sure", "tbd",
        "you decide", "suggest", "propose"
    ]

    return any(k in normalized for k in KEYWORDS)


def propose_answer_from_research(question: str, research_context: str, company: str) -> str:
    prompt = f"""
        You are a senior business analyst drafting Blueprint Document inputs.

        Company: {company}

        Question:
        {question}

        Context:
        {research_context}

        Instructions:
        - Treat internal document insights and web search results with EQUAL weight
        - Combine both sources to form a well-rounded, accurate answer
        - If one source covers something the other doesn't, use both to complement each other
        - If company-specific data is missing from both, infer using industry standards
        - Keep it concise (2–4 lines)
        - Avoid generic statements

        Answer:
    """

    # Use Anthropic Claude via the adapter. Read model from CLAUDE_MODEL,
    # defaulting to "claude-sonnet". Preserve behavior of returning plain text.
    model = os.getenv("CLAUDE_MODEL", "claude-sonnet")
    max_tokens = int(os.getenv("CLAUDE_MAX_TOKENS", "800"))
    temperature = float(os.getenv("CLAUDE_TEMPERATURE", "0.0"))

    response_text = completion_from_prompt(
        prompt,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
    )

    return response_text.strip()


import re

def extract_sources_from_context(research_context: str) -> str:
    """Peek inside the research context string to detect which sources were used."""
    if not research_context:
        return "No sources found"
    sources = []
    if "## Web Insights" in research_context and "Web error" not in research_context:
        sources.append("🌐 Web Search")
    if "## Internal Docs" in research_context:
        doc_names = re.findall(r"- ([^:]+):", research_context.split("## Internal Docs")[-1])
        seen = []
        for d in doc_names:
            d = d.strip()
            if d and d not in seen:
                seen.append(d)
                sources.append(f"📄 {d}")
    return " | ".join(sources) if sources else "No external sources found"

def _extract_questions(prompt: str, root_section: str):
    questions = []
    current_section_id = root_section

    for raw_line in prompt.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        upper = line.upper()

        if "SECTION" in upper:
            for token in line.replace("—", " ").replace("–", " ").replace("&", " ").split():
                if token.isdigit():
                    current_section_id = token
                    break
                if len(token) > 0 and token[0].isdigit() and "." in token:
                    current_section_id = token.rstrip(":").rstrip(".").rstrip(",")
                    break
            continue

        if ")" in line and "SECTION" in upper:
            for token in line.split():
                if token[0].isdigit() and "." in token:
                    current_section_id = token.rstrip(":").rstrip(".")
                    break
            continue

        # ✅ FIX: only treat as section header if first token is a pure
        # section number like "1.5" or "3.2.1" — NOT plain list numbers like "1."
        if line[0].isdigit() and "." in line[:5] and "?" not in line:
            first_token = line.split()[0].rstrip(":").rstrip(".")
            if re.fullmatch(r'\d+(\.\d+)+', first_token):  # must have at least one sub-level
                current_section_id = first_token
                continue
            # plain numbered lines like "1. Some text" fall through to question check below

        if (
            line[0].isdigit()
            and "?" in line
        ):
            questions.append(
                {
                    "section_id": current_section_id,
                    "question": line,
                }
            )

    return questions

def _next_selected_section(current: str, selected: list) -> str | None:
    """Returns the next selected section after current, or None if last."""
    try:
        idx = selected.index(current)
        return selected[idx + 1] if idx + 1 < len(selected) else None
    except ValueError:
        return None

def _safe_intro_question(state, section_label, qna_phase, next_phase):
    """
    Renders section header + first question together.
    Sets phase to qna_phase so handle_qna picks up from index 0.
    Skips section only if there are genuinely no questions.
    """
    if not state.active_question_list:
        state.phase = next_phase
        return (
            state,
            _assistant(
                f"📌 **{section_label}**\n"
                f"✅ No input questions found. Skipping."
            ),
            next_phase,
            None,
            None,
        )

    # ✅ FIX: set phase to qna_phase BEFORE returning first question
    state.phase = qna_phase
    state.current_question_index = 0
    first_question = state.active_question_list[0]["question"]
    guidance = "\n\n💡 Tip: Type 'suggest' for AI suggestion, or just answer directly"
    guidance += "\n⏭️ Type 'skip' to skip this entire section and move to the next"

    return (
        state,
        _assistant(
            f"📌 **{section_label}**\n\n"
            + first_question
            + guidance
        ),
        qna_phase,
        None,
        None,
    )


# --------------------------------------------------
# Session start
# --------------------------------------------------
def start_session(session_id: str) -> SessionState:
    return SessionState(
        session_id=session_id,
        phase="init",
        messages=[
            _assistant(
                "Hi! I can help generate a **Blueprint Document** using a guided conversation. "
                "Which company should I research?"
            )
        ],
        section="1",
    )


# --------------------------------------------------
# Main state machine
# --------------------------------------------------
def handle_message(
    state: SessionState,
    user_input: str,
) -> Tuple[
    SessionState,
    Dict[str, str],
    str,
    Optional[str],
    Optional[str],
]:

    user_input = (user_input or "").strip()
    state.messages.append(_user(user_input))

    if state.phase == "init":
        if not user_input:
            return (
                state,
                _assistant(
                    "Hi! I can help generate a **Blueprint Document** using a guided conversation.\n"
                    "Which company should I work on?"
                ),
                "init",
                None,
                None,
            )

        state.company = user_input
        state.qa_records = []
        state.phase = "section_selection"

        return (
            state,
            _assistant(
                f"Great. Let's begin the Blueprint Document for **{state.company}**.\n\n"
                "📋 **Please select the section(s) you want to include:**\n\n"
                "1️⃣  Section 1 — Introduction\n"
                "2️⃣  Section 2 — Benefit Realization\n"
                "3️⃣  Section 3 — Supply Chain Scope\n"
                "4️⃣  Section 4 — Demand Planning\n"
                "5️⃣  Section 5 — Supply & Inventory Planning\n"
                "6️⃣  Section 6 — Data Integration\n\n"
                "Type the section numbers separated by commas (e.g. `1,2,4`)\n"
                "Or type **'all'** to include all sections."
            ),
            "section_selection",
            None,
            None,
        )

    # ===============================================================
    # SUGGESTION FLOW (GLOBAL — works for all sections)
    # ===============================================================

    # ✅ FIX 2: continue_qna_flow defined here (not inside init block)
    def continue_qna_flow(state, next_phase):
        if state.current_question_index < len(state.active_question_list):
            return (
                state,
                _assistant(
                    state.active_question_list[state.current_question_index]["question"]
                ),
                state.phase,
                None,
                None,
            )

        state.phase = next_phase
        return (
            state,
            _assistant("**✅ Section** completed."),
            next_phase,
            None,
            None,
        )
    SECTION_ORDER = ["1", "2", "3", "4", "5", "6"]
    SECTION_NAMES = {
        "1": "Introduction",
        "2": "Benefit Realization",
        "3": "Supply Chain Scope",
        "4": "Demand Planning",
        "5": "Supply & Inventory Planning",
        "6": "Data Integration",
    }

    if state.phase == "section_selection":
        raw = user_input.strip().lower()

        if raw == "all":
            state.selected_sections = SECTION_ORDER[:]
        else:
            selected = [s.strip() for s in raw.split(",") if s.strip() in SECTION_ORDER]
            if not selected:
                return (
                    state,
                    _assistant(
                        "❌ Invalid selection. Please type section numbers like `1,2,4` or type **'all'**."
                    ),
                    "section_selection",
                    None,
                    None,
                )
            state.selected_sections = sorted(selected, key=lambda x: SECTION_ORDER.index(x))

        section_list = "\n".join(
            f"✅ Section {s} — {SECTION_NAMES[s]}" for s in state.selected_sections
        )
        first_section = state.selected_sections[0]
        state.phase = f"section{first_section}_init"

        return (
            state,
            _assistant(
                f"Perfect! You've selected:\n\n{section_list}\n\n"
                f"Let's start with **Section {first_section}: {SECTION_NAMES[first_section]}**."
            ),
            f"section{first_section}_init",
            None,
            None,
        )

    # ===============================================================
    # ✅ ADD HERE — DONE PHASE (restart session)
    # ===============================================================
    if state.phase == "done":
        state.phase = "await_restart"
        return (
            state,
            _assistant(
                "Would you like to generate another **Blueprint Document**? (yes/no)"
            ),
            "await_restart",
            None,
            None,
        )

    if state.phase == "await_restart":
        decision = user_input.strip().lower()

        if decision in ["no", "n", "nope", "exit", "quit", "bye"]:
            state.phase = "terminated"
            return (
                state,
                _assistant("Thank you! Have a great day. 👋"),
                "terminated",
                None,
                None,
            )

        # yes or anything else — reset and restart
        state.company = None
        state.qa_records = []
        state.selected_sections = []
        state.section1_sections = None
        state.br_sections = None
        state.ssc_sections = None
        state.dp_sections = None
        state.ip_sections = None
        state.di_sections = None
        state.all_intro_questions = []
        state.all_br_questions = []
        state.all_sc_questions = []
        state.all_dp_questions = []
        state.all_ip_questions = []
        state.all_di_questions = []
        state.active_question_list = []
        state.current_question_index = 0
        state.pending_question = None
        state.proposed_answer = None
        state.return_phase = None
        state.next_phase = None
        state.research_cache = {}
        state.txt_path = None
        state.docx_path = None
        state.phase = "init"

        return (
            state,
            _assistant("Great! Which company should I work on?"),
            "init",
            None,
            None,
        )

    if state.phase == "terminated":
        return (
            state,
            _assistant("Session has ended. Refresh the page to start a new conversation."),
            "terminated",
            None,
            None,
        )
    
    if state.phase == "suggest_answer":
        question = state.pending_question["question"]

        if not hasattr(state, "research_cache"):
            state.research_cache = {}

        if question in state.research_cache:
            research_context = state.research_cache[question]
        else:
            _current_section_id = state.section.split(".")[0] if state.section else None
            research_context = build_question_research_context(question, section_id=_current_section_id)
            state.research_cache[question] = research_context

        proposed = propose_answer_from_research(
            question,
            research_context,
            state.company
        )

        sources = extract_sources_from_context(research_context)
        state.proposed_answer = proposed
        state.phase = "confirm_answer"

        return (
            state,
            _assistant(
                f"💡 Suggested answer:\n\n{proposed}\n\n"
                f"📚 Sources: {sources}\n\n"
                "What would you like to do?\n"
                "• Type 'yes' or 'ok' to accept this suggestion\n"
                "• Type 'no' to provide your own answer\n"
                "• Or edit the suggestion directly (e.g., 'yes, but add...')"
            ),
            "confirm_answer",
            None,
            None,
        )

    if state.phase == "confirm_answer":
        decision = user_input.lower()
        prev_q = state.pending_question

        if decision in ["yes", "y", "ok", "okay", "confirm", "accept"]:
            final_answer = state.proposed_answer
            source = "generated"

        elif decision in ["no", "n"]:
            state.phase = "await_manual_answer"
            return (
                state,
                _assistant("Alright, please provide your answer.\n\n💡 You can always type 'suggest' on any question to get an AI suggestion"),
                "await_manual_answer",
                None,
                None,
            )

        else:
            final_answer = user_input
            source = "user_edited"

        state.qa_records.append({
            "section_id": prev_q["section_id"],
            "section_title": prev_q.get("section_title", ""),
            "question": prev_q["question"],
            "answer": final_answer,
            "source": source,
            "confidence": "medium",
            "approved": True
        })

        state.current_question_index += 1
        state.phase = state.return_phase

        if state.current_question_index < len(state.active_question_list):
            next_question = state.active_question_list[state.current_question_index]["question"]
            guidance = "\n\n💡 Tip: Type 'suggest' for AI suggestion, or just answer directly"
            return (
                state,
                _assistant(next_question + guidance),
                state.phase,
                None,
                None,
            )

        state.phase = state.next_phase
        section_title = state.pending_question.get("section_title", "")
        return (
            state,
            _assistant(f"✅ **{section_title}** completed."),
            state.next_phase,
            None,
            None,
        )

    if state.phase == "await_manual_answer":
        prev_q = state.pending_question

        state.qa_records.append({
            "section_id": prev_q["section_id"],
            "section_title": prev_q.get("section_title", ""),
            "question": prev_q["question"],
            "answer": user_input,
            "source": "user",
            "confidence": "high",
            "approved": True
        })

        state.current_question_index += 1
        state.phase = state.return_phase

        if state.current_question_index < len(state.active_question_list):
            next_question = state.active_question_list[state.current_question_index]["question"]
            guidance = "\n\n💡 Tip: Type 'suggest' for AI suggestion, or just answer directly"
            return (
                state,
                _assistant(next_question + guidance),
                state.phase,
                None,
                None,
            )

        state.phase = state.next_phase
        section_title = state.pending_question.get("section_title", "")
        return (
            state,
            _assistant(f"✅ **{section_title}** completed."),
            state.next_phase,
            None,
            None,
        )

    # ===============================================================
    # GENERIC QNA HANDLER (used for all sections/subsections)
    # ===============================================================

    def handle_qna(state, user_input, section_title, next_phase):

        if user_input.strip().lower() in ["skip", "skip section", "next"]:
            current_section = section_title.split(":")[0].strip() if ":" in section_title else section_title

            # Determine which top-level section we are in
            sec_num = None
            for s in SECTION_ORDER:
                if current_section.startswith(s) or current_section == SECTION_NAMES[s]:
                    sec_num = s
                    break

            if sec_num:
                next_sec = _next_selected_section(sec_num, state.selected_sections)
                if next_sec:
                    state.phase = f"section{next_sec}_init"
                    return (state, _assistant(f"⏭️ Skipping Section {sec_num}. Moving to Section {next_sec}: {SECTION_NAMES[next_sec]}"), f"section{next_sec}_init", None, None)
                else:
                    # Last selected section — go to terminate prompt of current section
                    state.phase = f"section{sec_num}_terminate_prompt"
                    return (state, _assistant(f"⏭️ Skipping Section {sec_num}. No more sections selected. Type 'generate' to save the document."), f"section{sec_num}_terminate_prompt", None, None)
            
        current_index = state.current_question_index

        if current_index < 0 or current_index >= len(state.active_question_list):
            state.phase = next_phase
            return (state, _assistant(f"✅ **{section_title}** completed."), next_phase, None, None)

        prev_q = state.active_question_list[current_index]
        prev_q["section_title"] = section_title

        user_lower = user_input.strip().lower()
        if user_lower in ["suggest", "suggestion", "propose", "help", "hint"]:
            state.pending_question = prev_q
            state.return_phase = state.phase
            state.next_phase = next_phase
            state.phase = "suggest_answer"
            return (state, _assistant(" Let me suggest an answer based on available information."), "suggest_answer", None, None)

        elif is_non_answer(user_input):
            state.pending_question = prev_q
            state.return_phase = state.phase
            state.next_phase = next_phase
            state.phase = "suggest_answer"
            return (state, _assistant("Got it. Let me suggest an answer based on available information."), "suggest_answer", None, None)

        state.qa_records.append({
            "section_id": prev_q["section_id"],
            "section_title": section_title,
            "question": prev_q["question"],
            "answer": user_input,
            "source": "user",
            "confidence": "high",
            "approved": True
        })

        state.current_question_index += 1

        if state.current_question_index < len(state.active_question_list):
            next_question = state.active_question_list[state.current_question_index]["question"]
            guidance = "\n💡 Tip: Type 'suggest' for AI suggestion, or just answer directly"
            guidance += "\n⏭️ Type 'skip' to skip remaining questions in this section"
            return (state, _assistant(next_question + guidance), state.phase, None, None)

        state.phase = next_phase
        return (state, _assistant(f"✅ **{section_title}** completed."), next_phase, None, None)

    # ===============================================================
    # SECTION 1 — INTRODUCTION
    # ===============================================================

    if state.phase == "section1_init":
        state.all_intro_questions = _extract_questions(
            INTRODUCTION_INPUT_COLLECTION_PROMPT, "1"
        )
        for q in state.all_intro_questions:
            print(f"DEBUG [{q['section_id']}] {q['question'][:50]}")
        print(f"DEBUG questions extracted: {state.all_intro_questions}")
        state.phase = "section1_intro"
        return state, _assistant("📌 **Section 1: Introduction**"), "section1_intro", None, None


    if state.phase == "section1_intro":
        state.active_question_list = [
            q for q in state.all_intro_questions if q["section_id"] == "1"
        ]
        state.next_phase = "section1_1_intro"
        return _safe_intro_question(state, "Section 1: Introduction", "section1_qna", "section1_1_intro")

    if state.phase == "section1_qna":
        return handle_qna(state, user_input, "Introduction", "section1_1_intro")

    if state.phase == "section1_1_intro":
        state.active_question_list = [
            q for q in state.all_intro_questions if q["section_id"] == "1.1"
        ]
        state.next_phase = "section1_2_intro"
        return _safe_intro_question(state, "Section 1.1: Company Information", "section1_1_qna", "section1_2_intro")

    if state.phase == "section1_1_qna":
        return handle_qna(state, user_input, "1.1: Company Information", "section1_2_intro")

    if state.phase == "section1_2_intro":
        state.active_question_list = [
            q for q in state.all_intro_questions if q["section_id"] == "1.2"
        ]
        state.next_phase = "section1_3_intro"
        return _safe_intro_question(state, "Section 1.2: Current State of Business", "section1_2_qna", "section1_3_intro")

    if state.phase == "section1_2_qna":
        return handle_qna(state, user_input, "1.2: Current State of Business", "section1_3_intro")

    if state.phase == "section1_3_intro":
        state.active_question_list = [
            q for q in state.all_intro_questions if q["section_id"] == "1.3"
        ]
        state.next_phase = "section1_4_intro"
        return _safe_intro_question(state, "Section 1.3: Purpose", "section1_3_qna", "section1_4_intro")

    if state.phase == "section1_3_qna":
        return handle_qna(state, user_input, "1.3: Purpose", "section1_4_intro")

    if state.phase == "section1_4_intro":
        state.active_question_list = [
            q for q in state.all_intro_questions if q["section_id"] == "1.4"
        ]
        state.next_phase = "section1_5_intro"
        return _safe_intro_question(state, "Section 1.4: Scope", "section1_4_qna", "section1_5_intro")

    if state.phase == "section1_4_qna":
        return handle_qna(state, user_input, "1.4: Scope", "section1_5_intro")

    if state.phase == "section1_5_intro":
        state.active_question_list = [
            q for q in state.all_intro_questions if q["section_id"] == "1.5"
        ]
        print(f"DEBUG 1.5 questions: {state.active_question_list}")
        print(f"DEBUG all questions: {[(q['section_id'], q['question'][:30]) for q in state.all_intro_questions]}")
        state.next_phase = "section1_5_complete"
        return _safe_intro_question(state, "Section 1.5: Definitions & Acronyms", "section1_5_qna", "section1_5_complete")

    if state.phase == "section1_5_qna":
        return handle_qna(state, user_input, "1.5: Definitions & Acronyms", "section1_5_complete")

    # ===============================================================
    # SECTION 1 COMPLETE
    # ===============================================================

    if state.phase == "section1_5_complete":
        section1_records = [r for r in state.qa_records if r["section_id"].startswith("1")]

        if not section1_records:
            raise RuntimeError("No Q&A collected for Section 1")

        research_context = build_question_research_context(
            question=f"Supply chain planning requirements and overview for {state.company}",
            section_id="1",
        )
        if not research_context:
            research_context = "No external research context was provided."

        try:
            from agents.brd_agent.run_introagent import generate_intro_sections_from_qa
        except Exception:
            generate_intro_sections_from_qa = None

        if generate_intro_sections_from_qa:
            state.section1_sections = generate_intro_sections_from_qa(
                section1_records,
                research_context=research_context,
            )
        else:
            state.section1_sections = None

        state.phase = "section1_terminate_prompt"
        return (
            state,
            _assistant(
                "✅ **Section 1: Introduction** completed!\n\n"
                + (
                    f"**Continue** to Section {_next_selected_section('1', state.selected_sections)}: {SECTION_NAMES[_next_selected_section('1', state.selected_sections)]}\n"
                    if _next_selected_section("1", state.selected_sections) else ""
                )
                + "**Generate & Stop** (save the document now)\n\n"
                "Type 'stop' or 'generate'"
            ),
            "section1_terminate_prompt",
            None,
            None,
        )

    if state.phase == "section1_terminate_prompt":
        decision = user_input.strip().lower()

        if decision in ["stop", "generate", "done", "end", "exit"]:
            document_text = assemble_document(section1_sections=state.section1_sections)
            out_dir = Path("outputs")
            out_dir.mkdir(exist_ok=True)
            txt_path = save_txt(document_text, out_dir)
            docx_path = save_docx(document_text, out_dir, state=state)
            state.txt_path = str(txt_path)
            state.docx_path = str(docx_path)
            state.phase = "done"
            return (state, _assistant(f"✅ Document generated with Section 1.\n\n📄 TXT: {state.txt_path}\n📘 DOCX: {state.docx_path}"), "done", state.txt_path, state.docx_path)

        elif decision in ["continue", "next", "yes", "y"]:
            next_sec = _next_selected_section("1", state.selected_sections)
            if next_sec:
                state.phase = f"section{next_sec}_init"
                return (state, _assistant(f"Continuing to **Section {next_sec}: {SECTION_NAMES[next_sec]}**"), f"section{next_sec}_init", None, None)
            else:
                return (state, _assistant("✅ All selected sections completed! Type 'generate' or 'done' to save the document."), "section1_terminate_prompt", None, None)

        else:
            next_sec = _next_selected_section("1", state.selected_sections)
            next_name = SECTION_NAMES[next_sec] if next_sec else "none"
            return (state, _assistant(f"Please type 'continue' to go to Section {next_sec}: {next_name}, or 'stop'/'generate' to save the document now."), "section1_terminate_prompt", None, None)

    # ===============================================================
    # SECTION 2 — BENEFIT REALIZATION
    # ===============================================================

    if state.phase == "section2_init":
        state.all_br_questions = _extract_questions(
            BENEFIT_REALIZATION_INPUT_COLLECTION_PROMPT, "2"
        )
        for q in state.all_br_questions:
            print(f"DEBUG BR [{q['section_id']}] {q['question'][:50]}")
        state.phase = "section2_intro"
        return state, _assistant("📌 **Section 2: Benefit Realization**"), "section2_intro", None, None

    if state.phase == "section2_intro":
        state.active_question_list = [
            q for q in state.all_br_questions if q["section_id"] == "2"
        ]
        state.next_phase = "section2_1_intro"
        return _safe_intro_question(state, "Section 2: Benefit Realization", "section2_qna", "section2_1_intro")

    if state.phase == "section2_qna":
        return handle_qna(state, user_input, "Benefit Realization", "section2_1_intro")

    if state.phase == "section2_1_intro":
        state.active_question_list = [
            q for q in state.all_br_questions if q["section_id"] == "2.1"
        ]
        state.next_phase = "section2_2_intro"
        return _safe_intro_question(state, "Section 2.1: Business Issues", "section2_1_qna", "section2_2_intro")

    if state.phase == "section2_1_qna":
        return handle_qna(state, user_input, "2.1: Business Issues", "section2_2_intro")

    if state.phase == "section2_2_intro":
        state.active_question_list = [
            q for q in state.all_br_questions if q["section_id"] == "2.2"
        ]
        state.next_phase = "section2_complete"
        return _safe_intro_question(state, "Section 2.2: Value Drivers", "section2_2_qna", "section2_complete")

    if state.phase == "section2_2_qna":
        return handle_qna(state, user_input, "2.2: Value Drivers", "section2_complete")

    # ===============================================================
    # SECTION 2 COMPLETE
    # ===============================================================

    if state.phase == "section2_complete":
        br_records = [r for r in state.qa_records if r["section_id"].startswith("2")]

        if not br_records:
            raise RuntimeError("No Q&A collected for Section 2")

        research_context = build_question_research_context(
            question=f"Benefit realization, business value and KPIs for {state.company}",
            section_id="2",
        )
        if not research_context:
            research_context = "No external research context was provided."

        try:
            from agents.brd_agent.run_bragent import generate_br_sections_from_qa
        except Exception:
            generate_br_sections_from_qa = None

        if generate_br_sections_from_qa:
            state.br_sections = generate_br_sections_from_qa(br_records, research_context=research_context)
        else:
            state.br_sections = None
        print(f"DEBUG br_sections: {state.br_sections}")

        state.phase = "section2_terminate_prompt"
        return (
            state,
            _assistant(
                "✅ **Section 2: Benefit Realization** completed!\n\n"
                + (
                    f"**Continue** to Section {_next_selected_section('2', state.selected_sections)}: {SECTION_NAMES[_next_selected_section('2', state.selected_sections)]}\n"
                    if _next_selected_section("2", state.selected_sections) else ""
                )
                + "**Generate & Stop** (save the document now)\n\n"
                "Type 'stop' or 'generate'"
            ),
            "section2_terminate_prompt",
            None,
            None,
        )

    if state.phase == "section2_terminate_prompt":
        decision = user_input.strip().lower()

        if decision in ["stop", "generate", "done", "end", "exit"]:
            document_text = assemble_document(section1_sections=state.section1_sections, br_sections=state.br_sections)
            out_dir = Path("outputs")
            out_dir.mkdir(exist_ok=True)
            txt_path = save_txt(document_text, out_dir)
            docx_path = save_docx(document_text, out_dir, state=state)
            state.txt_path = str(txt_path)
            state.docx_path = str(docx_path)
            state.phase = "done"
            return (state, _assistant(f"✅ Document generated with Sections 1-2.\n\n📄 TXT: {state.txt_path}\n📘 DOCX: {state.docx_path}"), "done", state.txt_path, state.docx_path)

        elif decision in ["continue", "next", "yes", "y"]:
            next_sec = _next_selected_section("2", state.selected_sections)
            if next_sec:
                state.phase = f"section{next_sec}_init"
                return (state, _assistant(f"Continuing to **Section {next_sec}: {SECTION_NAMES[next_sec]}**"), f"section{next_sec}_init", None, None)
            else:
                return (state, _assistant("✅ All selected sections completed! Type 'generate' or 'done' to save the document."), "section2_terminate_prompt", None, None)

        else:
            next_sec = _next_selected_section("2", state.selected_sections)
            next_name = SECTION_NAMES[next_sec] if next_sec else "none"
            return (state, _assistant(f"Please type 'continue' to go to Section {next_sec}: {next_name}, or 'stop'/'generate' to save the document now."), "section2_terminate_prompt", None, None)

    # ===============================================================
    # SECTION 3 — SUPPLY CHAIN SCOPE
    # ===============================================================

    SSC_SECTION_TITLES = {
        "3":   "Supply Chain Scope",
        "3.1": "Supply Chain Maps",
        "3.2": "Sites",
        "3.3": "Demand Foundation",
        "3.4": "Supply Foundation",
        "3.5": "Inventory Management Foundation",
        "3.6": "Constraints",
        "3.7": "Scenario Structure",
    }

    if state.phase == "section3_init":
        state.all_sc_questions = _extract_questions(
            SUPPLY_CHAIN_SCOPE_INPUT_COLLECTION_PROMPT, "3"
        )
        for q in state.all_sc_questions:
            print(f"DEBUG SSC [{q['section_id']}] {q['question'][:50]}")
        state.phase = "section3_intro"
        return state, _assistant(f"📌 **Section 3: {SSC_SECTION_TITLES['3']}**"), "section3_intro", None, None

    if state.phase == "section3_intro":
        state.active_question_list = [
            q for q in state.all_sc_questions if q["section_id"] == "3"
        ]
        state.next_phase = "section3_1_intro"
        return _safe_intro_question(state, f"Section 3: {SSC_SECTION_TITLES['3']}", "section3_qna", "section3_1_intro")

    if state.phase == "section3_qna":
        return handle_qna(state, user_input, SSC_SECTION_TITLES["3"], "section3_1_intro")

    SECTION3_SEQUENCE = ["3.1", "3.2", "3.3", "3.4", "3.5", "3.6", "3.7"]

    for i, sec in enumerate(SECTION3_SEQUENCE):
        intro_phase = f"section{sec}_intro".replace(".", "_")
        qna_phase   = f"section{sec}_qna".replace(".", "_")
        next_phase  = (
            f"section{SECTION3_SEQUENCE[i+1]}_intro".replace(".", "_")
            if i < len(SECTION3_SEQUENCE) - 1
            else "section3_complete"
        )

        if state.phase == intro_phase:
            state.active_question_list = [
                q for q in state.all_sc_questions if q["section_id"] == sec
            ]
            state.next_phase = next_phase
            return _safe_intro_question(
                state,
                f"Section {sec}: {SSC_SECTION_TITLES[sec]}",
                qna_phase,
                next_phase,
            )

        if state.phase == qna_phase:
            return handle_qna(state, user_input, f"{sec}: {SSC_SECTION_TITLES[sec]}", next_phase)

    # ===============================================================
    # SECTION 3 COMPLETE
    # ===============================================================

    if state.phase == "section3_complete":
        ssc_records = [r for r in state.qa_records if r["section_id"].startswith("3")]

        if not ssc_records:
            raise RuntimeError("No Q&A collected for Section 3")

        research_context = build_question_research_context(
            question=f"Supply chain design, planning, constraints and scenario modeling for {state.company}",
            section_id="3",
        )
        if not research_context:
            research_context = "No external research context was provided."

        try:
            from agents.brd_agent.run_sscagent import generate_ssc_sections_from_qa
        except Exception:
            generate_ssc_sections_from_qa = None

        if generate_ssc_sections_from_qa:
            state.ssc_sections = generate_ssc_sections_from_qa(ssc_records, research_context=research_context)
        else:
            state.ssc_sections = None

        state.phase = "section3_terminate_prompt"
        return (
            state,
            _assistant(
                "✅ **Section 3: Supply Chain Scope** completed!\n\n"
                + (
                    f"**Continue** to Section {_next_selected_section('3', state.selected_sections)}: {SECTION_NAMES[_next_selected_section('3', state.selected_sections)]}\n"
                    if _next_selected_section("3", state.selected_sections) else ""
                )
                + "**Generate & Stop** (save the document now)\n\n"
                "Type 'stop' or 'generate'"
            ),
            "section3_terminate_prompt",
            None,
            None,
        )

    if state.phase == "section3_terminate_prompt":
        decision = user_input.strip().lower()

        if decision in ["stop", "generate", "done", "end", "exit"]:
            document_text = assemble_document(section1_sections=state.section1_sections, br_sections=state.br_sections, ssc_sections=state.ssc_sections)
            out_dir = Path("outputs")
            out_dir.mkdir(exist_ok=True)
            txt_path = save_txt(document_text, out_dir)
            docx_path = save_docx(document_text, out_dir, state=state)
            state.txt_path = str(txt_path)
            state.docx_path = str(docx_path)
            state.phase = "done"
            return (state, _assistant(f"✅ Document generated with Sections 1-3.\n\n📄 TXT: {state.txt_path}\n📘 DOCX: {state.docx_path}"), "done", state.txt_path, state.docx_path)

        elif decision in ["continue", "next", "yes", "y"]:
            next_sec = _next_selected_section("3", state.selected_sections)
            if next_sec:
                state.phase = f"section{next_sec}_init"
                return (state, _assistant(f"Continuing to **Section {next_sec}: {SECTION_NAMES[next_sec]}**"), f"section{next_sec}_init", None, None)
            else:
                return (state, _assistant("✅ All selected sections completed! Type 'generate' or 'done' to save the document."), "section3_terminate_prompt", None, None)

        else:
            next_sec = _next_selected_section("3", state.selected_sections)
            next_name = SECTION_NAMES[next_sec] if next_sec else "none"
            return (state, _assistant(f"Please type 'continue' to go to Section {next_sec}: {next_name}, or 'stop'/'generate' to save the document now."), "section3_terminate_prompt", None, None)

    # ===============================================================
    # SECTION 4 — DEMAND PLANNING
    # ===============================================================

    DP_SECTION_TITLES = {
        "4":     "Demand Planning",
        "4.1":   "Demand Planning Process Overview",
        "4.2":   "Forecast Consumption",
        "4.2.1": "Solution Assumptions",
        "4.2.2": "Resources",
    }

    if state.phase == "section4_init":
        state.all_dp_questions = _extract_questions(
            DEMAND_PLANNING_INPUT_COLLECTION_PROMPT, "4"
        )
        for q in state.all_dp_questions:
            print(f"DEBUG DP [{q['section_id']}] {q['question'][:50]}")
        state.phase = "section4_intro"
        return state, _assistant(f"📌 **Section 4: {DP_SECTION_TITLES['4']}**"), "section4_intro", None, None

    if state.phase == "section4_intro":
        state.active_question_list = [
            q for q in state.all_dp_questions if q["section_id"] == "4"
        ]
        state.next_phase = "section4_1_intro"
        return _safe_intro_question(state, f"Section 4: {DP_SECTION_TITLES['4']}", "section4_qna", "section4_1_intro")

    if state.phase == "section4_qna":
        return handle_qna(state, user_input, DP_SECTION_TITLES["4"], "section4_1_intro")

    SECTION4_SEQUENCE = ["4.1", "4.2", "4.2.1", "4.2.2"]

    for i, sec in enumerate(SECTION4_SEQUENCE):
        intro_phase = f"section{sec}_intro".replace(".", "_")
        qna_phase   = f"section{sec}_qna".replace(".", "_")
        next_phase  = (
            f"section{SECTION4_SEQUENCE[i+1]}_intro".replace(".", "_")
            if i < len(SECTION4_SEQUENCE) - 1
            else "section4_complete"
        )

        if state.phase == intro_phase:
            state.active_question_list = [
                q for q in state.all_dp_questions if q["section_id"] == sec
            ]
            state.next_phase = next_phase
            return _safe_intro_question(
                state,
                f"Section {sec}: {DP_SECTION_TITLES.get(sec, '')}",
                qna_phase,
                next_phase,
            )

        if state.phase == qna_phase:
            return handle_qna(state, user_input, f"{sec}: {DP_SECTION_TITLES.get(sec, '')}", next_phase)

    # ===============================================================
    # SECTION 4 COMPLETE
    # ===============================================================

    if state.phase == "section4_complete":
        dp_records = [r for r in state.qa_records if r["section_id"].startswith("4")]

        if not dp_records:
            raise RuntimeError("No Q&A collected for Section 4")

        try:
            research_context = build_question_research_context(
                question=f"Demand planning process, forecasting, consumption logic and assumptions for {state.company}",
                section_id="4",
            )
            if not research_context:
                research_context = "No external research context was provided."

            try:
                from agents.brd_agent.run_dpagent import generate_dp_sections_from_qa
            except Exception:
                generate_dp_sections_from_qa = None

            if generate_dp_sections_from_qa:
                state.dp_sections = generate_dp_sections_from_qa(dp_records, research_context=research_context)
            else:
                state.dp_sections = None

        except Exception as e:
            print(f"ERROR in section4_complete: {e}")
            return (state, _assistant(f"❌ Error while generating **Blueprint Document**: {str(e)}"), state.phase, None, None)

        state.phase = "section4_terminate_prompt"
        return (
            state,
            _assistant(
                "✅ **Section 4: Demand Planning** completed!\n\n"
                + (
                    f"**Continue** to Section {_next_selected_section('4', state.selected_sections)}: {SECTION_NAMES[_next_selected_section('4', state.selected_sections)]}\n"
                    if _next_selected_section("4", state.selected_sections) else ""
                )
                + "**Generate & Stop** (save the document now)\n\n"
                "Type 'stop' or 'generate'"
            ),
            "section4_terminate_prompt",
            None,
            None,
        )

    if state.phase == "section4_terminate_prompt":
        decision = user_input.strip().lower()

        if decision in ["stop", "generate", "done", "end", "exit", "yes", "y", "complete"]:
            document_text = assemble_document(section1_sections=state.section1_sections, br_sections=state.br_sections, ssc_sections=state.ssc_sections, dp_sections=state.dp_sections)
            out_dir = Path("outputs")
            out_dir.mkdir(exist_ok=True)
            txt_path = save_txt(document_text, out_dir)
            docx_path = save_docx(document_text, out_dir, state=state)
            state.txt_path = str(txt_path)
            state.docx_path = str(docx_path)
            state.phase = "done"
            return (state, _assistant(f"✅ Document generated with Sections 1-4.\n\n📄 TXT: {state.txt_path}\n📘 DOCX: {state.docx_path}"), "done", state.txt_path, state.docx_path)

        elif decision in ["continue", "next", "yes", "y"]:
            next_sec = _next_selected_section("4", state.selected_sections)
            if next_sec:
                state.phase = f"section{next_sec}_init"
                return (state, _assistant(f"Continuing to **Section {next_sec}: {SECTION_NAMES[next_sec]}**"), f"section{next_sec}_init", None, None)
            else:
                return (state, _assistant("✅ All selected sections completed! Type 'generate' or 'done' to save the document."), "section4_terminate_prompt", None, None)
    # ===============================================================
    # SECTION 5 — SUPPLY & INVENTORY PLANNING
    # ===============================================================

    IP_SECTION_TITLES = {
        "5":   "Supply & Inventory Planning",
        "5.1": "Review and Adjust Planning Parameters",
        "5.2": "Manage Capacity Constraints",
        "5.3": "Resolve Supply Plan Exceptions",
        "5.4": "Resolve Inventory Exceptions",
    }

    if state.phase == "section5_init":
        state.all_ip_questions = _extract_questions(
            SUPPLY_INVENTORY_INPUT_COLLECTION_PROMPT, "5"
        )
        for q in state.all_ip_questions:
            print(f"DEBUG IP [{q['section_id']}] {q['question'][:50]}")
        state.phase = "section5_intro"
        return state, _assistant(f"📌 **Section 5: {IP_SECTION_TITLES['5']}**"), "section5_intro", None, None

    if state.phase == "section5_intro":
        state.active_question_list = [
            q for q in state.all_ip_questions if q["section_id"] == "5"
        ]
        state.next_phase = "section5_1_intro"
        return _safe_intro_question(state, f"Section 5: {IP_SECTION_TITLES['5']}", "section5_qna", "section5_1_intro")

    if state.phase == "section5_qna":
        return handle_qna(state, user_input, IP_SECTION_TITLES["5"], "section5_1_intro")

    SECTION5_SEQUENCE = ["5.1", "5.2", "5.3", "5.4"]

    for i, sec in enumerate(SECTION5_SEQUENCE):
        intro_phase = f"section{sec}_intro".replace(".", "_")
        qna_phase   = f"section{sec}_qna".replace(".", "_")
        next_phase  = (
            f"section{SECTION5_SEQUENCE[i+1]}_intro".replace(".", "_")
            if i < len(SECTION5_SEQUENCE) - 1
            else "section5_complete"
        )

        if state.phase == intro_phase:
            state.active_question_list = [
                q for q in state.all_ip_questions if q["section_id"] == sec
            ]
            state.next_phase = next_phase
            return _safe_intro_question(
                state,
                f"Section {sec}: {IP_SECTION_TITLES.get(sec, '')}",
                qna_phase,
                next_phase,
            )

        if state.phase == qna_phase:
            return handle_qna(state, user_input, f"{sec}: {IP_SECTION_TITLES.get(sec, '')}", next_phase)

    # ===============================================================
    # SECTION 5 COMPLETE
    # ===============================================================

    if state.phase == "section5_complete":
        ip_records = [r for r in state.qa_records if r["section_id"].startswith("5")]

        if not ip_records:
            raise RuntimeError("No Q&A collected for Section 5")

        try:
            research_context = build_question_research_context(
                question=f"Supply planning, inventory management, capacity constraints and exceptions handling for {state.company}",
                section_id="5",
            )
            if not research_context:
                research_context = "No external research context was provided."

            try:
                from agents.brd_agent.run_ipagent import generate_ip_sections_from_qa
            except Exception:
                generate_ip_sections_from_qa = None

            if generate_ip_sections_from_qa:
                state.ip_sections = generate_ip_sections_from_qa(ip_records, research_context=research_context)
            else:
                state.ip_sections = None

        except Exception as e:
            print(f"ERROR in section5_complete: {e}")
            return (state, _assistant(f"❌ Error while generating **Blueprint Document**: {str(e)}"), state.phase, None, None)

        state.phase = "section5_terminate_prompt"
        return (
            state,
            _assistant(
                "✅ **Section 5: Supply & Inventory Planning** completed!\n\n"
                + (
                    f"**Continue** to Section {_next_selected_section('5', state.selected_sections)}: {SECTION_NAMES[_next_selected_section('5', state.selected_sections)]}\n"
                    if _next_selected_section("5", state.selected_sections) else ""
                )
                + "**Generate & Stop** (save the document now)\n\n"
                "Type 'stop' or 'generate'"
            ),
            "section5_terminate_prompt",
            None,
            None,
        )

    if state.phase == "section5_terminate_prompt":
        decision = user_input.strip().lower()

        if decision in ["stop", "generate", "done", "end", "exit", "yes", "y", "complete"]:
            document_text = assemble_document(
                section1_sections=state.section1_sections,
                br_sections=state.br_sections,
                ssc_sections=state.ssc_sections,
                dp_sections=state.dp_sections,
                ip_sections=state.ip_sections,
            )
            out_dir = Path("outputs")
            out_dir.mkdir(exist_ok=True)
            txt_path = save_txt(document_text, out_dir)
            docx_path = save_docx(document_text, out_dir, state=state)
            state.txt_path = str(txt_path)
            state.docx_path = str(docx_path)
            state.phase = "done"
            return (state, _assistant(f"✅ Document generated with all sections!\n\n📄 TXT: {state.txt_path}\n📘 DOCX: {state.docx_path}"), "done", state.txt_path, state.docx_path)

        elif decision in ["continue", "next", "yes", "y"]:
            next_sec = _next_selected_section("5", state.selected_sections)
            if next_sec:
                state.phase = f"section{next_sec}_init"
                return (state, _assistant(f"Continuing to **Section {next_sec}: {SECTION_NAMES[next_sec]}**"), f"section{next_sec}_init", None, None)
            else:
                return (state, _assistant("✅ All selected sections completed! Type 'generate' or 'done' to save the document."), "section5_terminate_prompt", None, None)

        else:
            next_sec = _next_selected_section("5", state.selected_sections)
            next_name = SECTION_NAMES[next_sec] if next_sec else "none"
            return (state, _assistant(f"Please type 'continue' to go to Section {next_sec}: {next_name}, or 'stop'/'generate' to save the document now."), "section5_terminate_prompt", None, None)
    # ===============================================================
    # SECTION 6 — DATA INTEGRATION
    # ===============================================================

    DI_SECTION_TITLES = {
        "6":   "Data Integration",
        "6.1": "Architecture",
        "6.2": "Data Sources",
        "6.3": "Data Files",
        "6.4": "Data Frequency",
    }

    if state.phase == "section6_init":
        state.all_di_questions = _extract_questions(
            DATA_INTEGRATION_INPUT_COLLECTION_PROMPT, "6"
        )
        for q in state.all_di_questions:
            print(f"DEBUG DI [{q['section_id']}] {q['question'][:50]}")
        state.phase = "section6_intro"
        return state, _assistant(f"📌 **Section 6: {DI_SECTION_TITLES['6']}**"), "section6_intro", None, None

    if state.phase == "section6_intro":
        state.active_question_list = [
            q for q in state.all_di_questions if q["section_id"] == "6"
        ]
        state.next_phase = "section6_1_intro"
        return _safe_intro_question(state, f"Section 6: {DI_SECTION_TITLES['6']}", "section6_qna", "section6_1_intro")

    if state.phase == "section6_qna":
        return handle_qna(state, user_input, DI_SECTION_TITLES["6"], "section6_1_intro")

    SECTION6_SEQUENCE = ["6.1", "6.2", "6.3", "6.4"]

    for i, sec in enumerate(SECTION6_SEQUENCE):
        intro_phase = f"section{sec}_intro".replace(".", "_")
        qna_phase   = f"section{sec}_qna".replace(".", "_")
        next_phase  = (
            f"section{SECTION6_SEQUENCE[i+1]}_intro".replace(".", "_")
            if i < len(SECTION6_SEQUENCE) - 1
            else "section6_complete"
        )

        if state.phase == intro_phase:
            state.active_question_list = [
                q for q in state.all_di_questions if q["section_id"] == sec
            ]
            state.next_phase = next_phase
            return _safe_intro_question(
                state,
                f"Section {sec}: {DI_SECTION_TITLES.get(sec, '')}",
                qna_phase,
                next_phase,
            )

        if state.phase == qna_phase:
            return handle_qna(state, user_input, f"{sec}: {DI_SECTION_TITLES.get(sec, '')}", next_phase)

    # ===============================================================
    # SECTION 6 COMPLETE
    # ===============================================================

    if state.phase == "section6_complete":
        di_records = [r for r in state.qa_records if r["section_id"].startswith("6")]

        if not di_records:
            raise RuntimeError("No Q&A collected for Section 6")

        try:
            research_context = build_question_research_context(
                question=f"Data integration architecture, data sources, files and frequency for {state.company}",
                section_id="6",
            )
            if not research_context:
                research_context = "No external research context was provided."

            try:
                from agents.brd_agent.run_diagent import generate_di_sections_from_qa
            except Exception:
                generate_di_sections_from_qa = None

            if generate_di_sections_from_qa:
                state.di_sections = generate_di_sections_from_qa(di_records, research_context=research_context)
            else:
                state.di_sections = None

        except Exception as e:
            print(f"ERROR in section6_complete: {e}")
            return (state, _assistant(f"❌ Error while generating **Blueprint Document**: {str(e)}"), state.phase, None, None)

        state.phase = "section6_terminate_prompt"
        return (
            state,
            _assistant(
                "✅ **Section 6: Data Integration** completed!\n\n"
                + (
                    f"**Continue** to Section {_next_selected_section('6', state.selected_sections)}: {SECTION_NAMES[_next_selected_section('6', state.selected_sections)]}\n"
                    if _next_selected_section("6", state.selected_sections) else ""
                )
                + "**Generate & Stop** (save the document now)\n\n"
                "Type 'stop' or 'generate'"
            ),
            "section6_terminate_prompt",
            None,
            None,
        )

    if state.phase == "section6_terminate_prompt":
        decision = user_input.strip().lower()

        if decision in ["stop", "generate", "done", "end", "exit", "yes", "y", "complete"]:
            document_text = assemble_document(
                section1_sections=state.section1_sections,
                br_sections=state.br_sections,
                ssc_sections=state.ssc_sections,
                dp_sections=state.dp_sections,
                ip_sections=state.ip_sections,
                di_sections=state.di_sections,
            )
            out_dir = Path("outputs")
            out_dir.mkdir(exist_ok=True)
            txt_path = save_txt(document_text, out_dir)
            docx_path = save_docx(document_text, out_dir, state=state)
            state.txt_path = str(txt_path)
            state.docx_path = str(docx_path)
            state.phase = "done"
            return (
                state,
                _assistant(
                    f"✅ Document generated!\n\n"
                    f"📄 TXT: {state.txt_path}\n"
                    f"📘 DOCX: {state.docx_path}"
                ),
                "done",
                state.txt_path,
                state.docx_path,
            )

        elif decision in ["continue", "next", "yes", "y"]:
            next_sec = _next_selected_section("6", state.selected_sections)
            if next_sec:
                state.phase = f"section{next_sec}_init"
                return (state, _assistant(f"Continuing to **Section {next_sec}: {SECTION_NAMES[next_sec]}**"), f"section{next_sec}_init", None, None)
            else:
                return (state, _assistant("✅ All selected sections completed! Type 'generate' or 'done' to save the document."), "section6_terminate_prompt", None, None)

        else:
            return (state, _assistant("Type 'generate' or 'done' to save and complete the **Blueprint document** with all sections."), "section6_terminate_prompt", None, None)
    # ===============================================================
    # FINAL DOCUMENT ASSEMBLY
    # ===============================================================

    if state.phase == "final_document":
        document_text = assemble_document(
            section1_sections=state.section1_sections,
            br_sections=state.br_sections,
            ssc_sections=state.ssc_sections,
            dp_sections=state.dp_sections,
            ip_sections=state.ip_sections,
            di_sections=state.di_sections,
        )

        out_dir = Path("outputs")
        out_dir.mkdir(exist_ok=True)
        txt_path = save_txt(document_text, out_dir)
        docx_path = save_docx(document_text, out_dir, state=state)
        state.txt_path = str(txt_path)
        state.docx_path = str(docx_path)
        state.phase = "done"

        return (
            state,
            _assistant(f"✅ **Blueprint Document** completed.\n\n📄 TXT: {state.txt_path}\n📘 DOCX: {state.docx_path}"),
            "done",
            state.txt_path,
            state.docx_path,
        )