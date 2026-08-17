# -*- coding: utf-8 -*-
"""
kinaxis_mcp_server.py

Native MCP server (stdio transport) combining:
  - web_search  : OpenAI Responses API w/ web_search_preview (default)
                  or Serper.dev (set WEB_SEARCH_BACKEND=serper + SERPER_API_KEY)
  - open_url    : fetch any URL → clean Markdown (local, no external API)
  - doc_search  : BM25 retrieval over DOCX/PDF/CSV in DOC_RETRIEVAL_DIR

Run (stdio — used by Claude Desktop / Claude Code MCP config):
  python unified_mcp/kinaxis_mcp_server.py

Self-contained — all dependencies live inside unified_mcp/:
  doc_retrieval_index.py   BM25 indexing engine
  serper_client.py         Serper.dev web search client (optional backend)
  reference_docs/          Default document corpus (DOCX/PDF/CSV)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Environment variables
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Web search (backend selection):
  WEB_SEARCH_BACKEND   openai (default) | serper

OpenAI backend (default):
  OPENAI_API_KEY       required
  OPENAI_SEARCH_MODEL  gpt-4o-mini-search-preview (default) | gpt-4o-search-preview
  OPENAI_SEARCH_CTX    high | medium | low  — search context size (default: medium)

Serper backend (optional):
  SERPER_API_KEY       required when WEB_SEARCH_BACKEND=serper
  SERPER_API_URL       https://google.serper.dev/search (default)
  SERPER_TIMEOUT_SECS  30 (default)

URL fetching:
  FETCH_TIMEOUT_SECS   30 (default)

Doc search:
  DOC_RETRIEVAL_DIR    unified_mcp/reference_docs (default)
  DOC_RETRIEVAL_GLOB   * (default)
  (All DOCRET_* tuning vars from doc_retrieval_index.py are respected)

PDF vision analysis (images/diagrams inside PDFs):
  PDF_VISION_ENABLED   false (default) | true — enable GPT-4.1 vision per page
  PDF_VISION_MODEL     gpt-4.1 (default) — any OpenAI vision-capable model
  PDF_VISION_DPI       150 (default) — render resolution; higher = better quality, larger payload
  Requires: OPENAI_API_KEY + pymupdf installed
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Claude Desktop config snippet  (~/.claude/claude_desktop_config.json):
  {
    "mcpServers": {
      "kinaxis": {
        "command": "python",
        "args": ["c:/BristleCone/Kinaxis-agent-build/unified_mcp/kinaxis_mcp_server.py"],
        "env": {
          "OPENAI_API_KEY": "sk-..."
        }
      }
    }
  }
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

from __future__ import annotations

import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional

# ---------------------------------------------------------------------------
# Path bootstrap — add this folder to sys.path so local modules resolve
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent          # .../unified_mcp/
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# ---------------------------------------------------------------------------
# Load .env from the same directory as this file (if present)
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv(_THIS_DIR / ".env")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Logging  (stderr only — stdout is reserved for MCP stdio protocol)
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
)
logger = logging.getLogger("KinaxisMCP")

# ---------------------------------------------------------------------------
# MCP SDK
# ---------------------------------------------------------------------------
from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Doc retrieval imports
# ---------------------------------------------------------------------------
from doc_retrieval_index import BM25Index, Chunk, build_index_from_folder, make_snippet

# ---------------------------------------------------------------------------
# Serper client (only used when WEB_SEARCH_BACKEND=serper)
# ---------------------------------------------------------------------------
from serper_client import SerperError, serper_search, to_markdown_search_results

# ---------------------------------------------------------------------------
# Doc index — module-level state, populated at server startup
# ---------------------------------------------------------------------------
_DEFAULT_DOC_DIR = str(_THIS_DIR / "reference_docs")
DOC_DIR  = Path(os.getenv("DOC_RETRIEVAL_DIR", _DEFAULT_DOC_DIR)).resolve()
DOC_GLOB = os.getenv("DOC_RETRIEVAL_GLOB", "*")

_doc_index:  Optional[BM25Index] = None
_doc_chunks: List[Chunk] = []


def _load_doc_index() -> None:
    global _doc_index, _doc_chunks
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    idx, chunks = build_index_from_folder(DOC_DIR, DOC_GLOB)
    _doc_index  = idx
    _doc_chunks = chunks
    logger.info("Doc index ready — %d chunks from %s", len(_doc_chunks), DOC_DIR)


# ---------------------------------------------------------------------------
# Lifespan: build doc index once when the server starts
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(server):
    try:
        _load_doc_index()
    except Exception as exc:
        logger.error("Doc index failed to load: %s — doc_search will return errors", exc)
    yield  # server runs here


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------
WEB_SEARCH_BACKEND = os.getenv("WEB_SEARCH_BACKEND", "openai").strip().lower()
if WEB_SEARCH_BACKEND not in ("openai", "serper"):
    logger.warning("Unknown WEB_SEARCH_BACKEND=%r — falling back to openai", WEB_SEARCH_BACKEND)
    WEB_SEARCH_BACKEND = "openai"

logger.info("Web search backend: %s", WEB_SEARCH_BACKEND)

mcp = FastMCP(
    name="Kinaxis MCP Server",
    instructions=(
        "Tools for web search, URL content extraction, and document retrieval "
        "over the Kinaxis/BRD reference corpus."
    ),
    lifespan=_lifespan,
)


# ===========================================================================
# Tool 1: web_search
# ===========================================================================

def _web_search_openai(query: str, max_results: int) -> str:
    """Call OpenAI Responses API with the web_search_preview built-in tool."""
    try:
        import openai
    except ImportError as exc:
        raise RuntimeError(
            "openai package is required for the OpenAI backend. "
            "Run: pip install openai>=1.60"
        ) from exc

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not set. Export it or add it to your MCP server env config."
        )

    model      = os.getenv("OPENAI_SEARCH_MODEL", "gpt-4o-mini-search-preview")
    search_ctx = os.getenv("OPENAI_SEARCH_CTX",   "medium")   # low | medium | high

    import httpx as _httpx
    _ssl_verify = os.getenv("OPENAI_SSL_VERIFY", "true").lower() not in ("false", "0", "no")
    client = openai.OpenAI(api_key=api_key, http_client=_httpx.Client(verify=_ssl_verify))
    logger.info("OpenAI web_search: model=%s query=%r max_results=%d", model, query, max_results)

    prompt = (
        f"{query}\n\n"
        f"Please return up to {max_results} relevant results as a concise Markdown list "
        f"with title, URL, and a one-sentence summary for each."
    )

    response = client.responses.create(
        model=model,
        tools=[{
            "type": "web_search_preview",
            "search_context_size": search_ctx,
        }],
        input=prompt,
    )

    for block in response.output:
        if block.type == "message":
            for content in block.content:
                if content.type == "output_text":
                    return content.text

    return "No search results returned."


def _web_search_serper(query: str, max_results: int) -> str:
    data = serper_search(query=query, num=max_results)
    return to_markdown_search_results(data, max_results=max_results)


@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web and return a Markdown-formatted summary of top results.

    Uses OpenAI web search by default (set OPENAI_API_KEY).
    Switch to Serper.dev by setting WEB_SEARCH_BACKEND=serper and SERPER_API_KEY.

    Args:
        query:       Natural language search query.
        max_results: Number of results to return (1-20, default 5).
    """
    max_results = max(1, min(int(max_results), 20))
    logger.info("web_search  backend=%s  query=%r", WEB_SEARCH_BACKEND, query)

    if WEB_SEARCH_BACKEND == "serper":
        return _web_search_serper(query, max_results)
    return _web_search_openai(query, max_results)


# ===========================================================================
# Tool 2: open_url
# ===========================================================================

@mcp.tool()
def open_url(url: str, max_chars: int = 12000) -> str:
    """
    Fetch a URL and return its content as clean Markdown.

    Strips scripts, styles, and navigation boilerplate. Runs locally — no
    external API is called.

    Args:
        url:       The URL to fetch.
        max_chars: Maximum characters to return (default 12000).
    """
    import requests
    from bs4 import BeautifulSoup
    from markdownify import markdownify as md_convert

    max_chars = max(100, min(int(max_chars), 100_000))
    timeout   = int(os.getenv("FETCH_TIMEOUT_SECS", "30"))

    logger.info("open_url  url=%r  max_chars=%d", url, max_chars)

    try:
        r = requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except requests.exceptions.RequestException as exc:
        return f"Error fetching URL: {exc}"

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    body    = soup.body or soup
    text_md = md_convert(str(body), heading_style="ATX")
    text_md = re.sub(r"\n{3,}", "\n\n", text_md).strip()

    if len(text_md) > max_chars:
        text_md = text_md[:max_chars].rstrip() + "\n\n...(truncated)"

    return text_md


# ===========================================================================
# Tool 3: doc_search
# ===========================================================================

def _format_doc_hits(results: list, query: str, max_snippet_chars: int) -> str:
    if not results:
        return "No relevant document sections found."

    lines = [
        "# Retrieved Reference Snippets",
        "",
        f"_Index: {len(_doc_chunks)} chunks — {DOC_DIR}_",
        "",
    ]
    for i, (chunk, score) in enumerate(results, 1):
        snippet = make_snippet(text=chunk.body_text, query=query, max_chars=max_snippet_chars)
        lines.append(f"## {i}. {chunk.doc_name} — {chunk.heading_path}")
        lines.append(f"**Score:** {score:.4f}  |  **Chunk:** `{chunk.chunk_id}`")
        lines.append(snippet or "_(empty snippet)_")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
def doc_search(
    query: str,
    top_k: int = 5,
    doc_filter: Optional[List[str]] = None,
    max_snippet_chars: int = 450,
) -> str:
    """
    Search the internal reference document corpus (DOCX, PDF, CSV) using
    BM25 ranking and return the most relevant sections as Markdown snippets.

    The corpus is loaded from DOC_RETRIEVAL_DIR at server startup.

    Args:
        query:             Natural language search query.
        top_k:             Number of hits to return (1-20, default 5).
        doc_filter:        Optional list of exact filenames to restrict search to,
                           e.g. ["Kraton Supply Planning Requirements.csv"].
        max_snippet_chars: Maximum length of each result snippet (default 450).
    """
    if _doc_index is None:
        return (
            "Document index is not available. "
            f"Check that DOC_RETRIEVAL_DIR exists and contains supported files: {DOC_DIR}"
        )

    q = (query or "").strip()
    if not q:
        return "Query is empty — provide a short phrase such as 'forecast consumption rules'."

    top_k             = max(1, min(int(top_k), 20))
    max_snippet_chars = max(100, min(int(max_snippet_chars), 3000))

    logger.info("doc_search  query=%r  top_k=%d  filter=%s", q, top_k, doc_filter)

    results = _doc_index.search(query=q, top_k=top_k, doc_filter=doc_filter)
    return _format_doc_hits(results, q, max_snippet_chars)


# ===========================================================================
# Entry point
# ===========================================================================

if __name__ == "__main__":
    mcp.run(transport="stdio")
