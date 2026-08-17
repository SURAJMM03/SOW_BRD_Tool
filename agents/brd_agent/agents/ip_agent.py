import logging
import os
import sys

from agents.brd_agent.agents.ip_prompt import SUPPLY_INVENTORY_INPUT_COLLECTION_PROMPT, SUPPLY_INVENTORY_GENERATE_PROMPT
from config.settings import AGENT_RECURSION_LIMIT, CLAUDE_MODEL
from agents.brd_agent.mcp_web_tools_bridge_http import search_web, fetch_url


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("BRDGenAgent")


def _completion(prompt: str, max_tokens: int = 3000, temperature: float = 0.2) -> str:
    """Call Claude for text completion. Matches the signature used by run_ipagent."""
    try:
        from app.claude_provider import completion_from_prompt
    except ImportError:
        from claude_provider import completion_from_prompt
    return completion_from_prompt(prompt, model=CLAUDE_MODEL, max_tokens=max_tokens, temperature=temperature)


class _ClaudeAgent:
    """Minimal LangChain-compatible wrapper so run_ipagent.py needs no changes."""
    def invoke(self, inputs: dict) -> dict:
        messages = inputs.get("messages", [])
        user_content = ""
        for m in messages:
            if isinstance(m, dict) and m.get("role") == "user":
                user_content = m.get("content", "")
            elif hasattr(m, "content"):
                user_content = m.content
        result = _completion(user_content)
        return {"messages": [type("Msg", (), {"content": result})()]}


def create_research_agent():
    """Research agent — uses Claude. Only called for standalone research runs."""
    logger.info("Creating research agent (model=%s)", CLAUDE_MODEL)
    return _ClaudeAgent()


def create_generation_agent():
    """Generation agent — uses Claude exclusively. No OpenAI calls."""
    logger.info("Creating generation agent (model=%s, no tools)", CLAUDE_MODEL)
    return _ClaudeAgent()