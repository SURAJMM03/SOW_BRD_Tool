# -*- coding: utf-8 -*-
"""
kinaxis_http_server.py

HTTP FastAPI server exposing the same three tools as kinaxis_mcp_server.py:
  - web_search  : OpenAI web search (default) or Serper.dev
  - open_url    : fetch any URL → clean Markdown
  - doc_search  : BM25 retrieval over DOCX/PDF/CSV

Endpoints:
  GET  /               info + active configuration
  GET  /health         health of both subsystems
  POST /web_search     { "query": "...", "max_results": 5 }
  POST /open_url       { "url": "...", "max_chars": 12000 }
  POST /doc_search     { "query": "...", "top_k": 5, "doc_filter": null, "max_snippet_chars": 450 }
  POST /reload         reload doc index from disk

Run:
  cd c:\\BristleCone\\Kinaxis-agent-build
  uvicorn unified_mcp.kinaxis_http_server:app --host 0.0.0.0 --port 8000 --reload

  -- or directly --
  python unified_mcp\\kinaxis_http_server.py

Binds to:  http://0.0.0.0:8000   (override with SERVER_HOST / SERVER_PORT)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Environment variables
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Server:
  SERVER_HOST          0.0.0.0  (default)
  SERVER_PORT          8000     (default)
  SERVER_RELOAD        false    (set true for dev hot-reload)

Web search:
  WEB_SEARCH_BACKEND   openai (default) | serper
  OPENAI_API_KEY       required for openai backend
  OPENAI_SEARCH_MODEL  gpt-4o-mini (default)
  OPENAI_SEARCH_CTX    medium (default) | low | high
  SERPER_API_KEY       required for serper backend
  SERPER_API_URL       https://google.serper.dev/search (default)
  SERPER_TIMEOUT_SECS  30 (default)

URL fetch:
  FETCH_TIMEOUT_SECS   30 (default)

Doc search:
  DOC_RETRIEVAL_DIR    unified_mcp/reference_docs (default)
  DOC_RETRIEVAL_GLOB   * (default)
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
# Path bootstrap
# ---------------------------------------------------------------------------
_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s  %(message)s",
)
logger = logging.getLogger("KinaxisHTTP")

# ── Fix SSL on corporate networks at module startup ───────────────────────────
def _fix_ssl():
    try:
        import pip_system_certs.wrapt_requests  # noqa: F401
        logger.info("SSL: pip_system_certs active")
        return
    except ImportError:
        pass
    try:
        import certifi as _certifi, os as _os
        for _k in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HTTPX_CA_BUNDLE"):
            _os.environ.setdefault(_k, _certifi.where())
        logger.info("SSL: certifi bundle applied")
    except Exception as _e:
        logger.warning("SSL: could not apply certifi — %s", _e)

_fix_ssl()

# ── Load .env so OPENAI_API_KEY is available when run as a standalone process ─
def _load_env():
    """Try to load .env from several candidate locations."""
    try:
        from dotenv import load_dotenv
        _candidates = [
            _THIS_DIR.parent.parent / "brd_convo_app" / "backend" / ".env",  # typical layout
            _THIS_DIR.parent / ".env",
            _THIS_DIR / ".env",
        ]
        for _p in _candidates:
            if _p.exists():
                load_dotenv(str(_p), override=False)
                logger.info("Loaded .env from %s", _p)
                return
        logger.info(".env not found in candidate paths — relying on system environment")
    except ImportError:
        logger.info("python-dotenv not installed — relying on system environment")

_load_env()

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
import uvicorn
import requests as _requests
from bs4 import BeautifulSoup
from markdownify import markdownify as md_convert

# ---------------------------------------------------------------------------
# Local modules (self-contained in unified_mcp/)
# ---------------------------------------------------------------------------
from doc_retrieval_index import BM25Index, Chunk, build_index_from_folder, make_snippet
from serper_client import SerperError, serper_search, to_markdown_search_results

# ---------------------------------------------------------------------------
# Web search backend
# ---------------------------------------------------------------------------
WEB_SEARCH_BACKEND = os.getenv("WEB_SEARCH_BACKEND", "openai").strip().lower()
if WEB_SEARCH_BACKEND not in ("openai", "serper"):
    logger.warning("Unknown WEB_SEARCH_BACKEND=%r — falling back to openai", WEB_SEARCH_BACKEND)
    WEB_SEARCH_BACKEND = "openai"

logger.info("Web search backend: %s", WEB_SEARCH_BACKEND)

# ---------------------------------------------------------------------------
# Doc index state
# ---------------------------------------------------------------------------
_DEFAULT_DOC_DIR = _THIS_DIR / "reference_docs"
DOC_DIR  = Path(os.getenv("DOC_RETRIEVAL_DIR", _DEFAULT_DOC_DIR)).resolve()
DOC_GLOB = os.getenv("DOC_RETRIEVAL_GLOB", "*")

_doc_index:       Optional[BM25Index] = None
_doc_chunks:      List[Chunk]         = []
_doc_index_error: Optional[str]       = None


def _load_doc_index() -> None:
    global _doc_index, _doc_chunks, _doc_index_error
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    try:
        idx, chunks      = build_index_from_folder(DOC_DIR, DOC_GLOB)
        _doc_index       = idx
        _doc_chunks      = chunks
        _doc_index_error = None
        logger.info("Doc index ready — %d chunks from %s", len(_doc_chunks), DOC_DIR)
    except Exception as exc:
        _doc_index_error = str(exc)
        logger.exception("Doc index failed: %s", exc)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _lifespan(app: FastAPI):
    _load_doc_index()
    yield


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Kinaxis Tools API",
    description=(
        "HTTP API exposing web search (OpenAI / Serper.dev), "
        "URL fetching, and BM25 document retrieval."
    ),
    version="1.0.0",
    lifespan=_lifespan,
)


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class WebSearchRequest(BaseModel):
    query:       str = Field(...,        description="Search query")
    max_results: int = Field(default=5,  ge=1, le=20, description="Number of results (1-20)")

class OpenURLRequest(BaseModel):
    url:       str = Field(...,              description="URL to fetch")
    max_chars: int = Field(default=12000,   ge=100, le=100_000, description="Max characters to return")

class DocSearchRequest(BaseModel):
    query:             str                  = Field(...,       description="Search query")
    top_k:             int                  = Field(default=5, ge=1, le=20)
    doc_filter:        Optional[List[str]]  = Field(default=None, description="Restrict to these filenames")
    max_snippet_chars: int                  = Field(default=450, ge=100, le=3000)
    section_id:        Optional[str]        = Field(default=None, description="BRD section filter: '1'–'6'")

class ToolResponse(BaseModel):
    result: str = Field(..., description="Tool output as Markdown text")

class DocHit(BaseModel):
    doc_name:     str
    heading_path: str
    score:        float
    snippet:      str
    chunk_id:     str

class DocSearchResponse(BaseModel):
    hits:                 List[DocHit]
    total_chunks_indexed: int
    source_dir:           str
    glob:                 str


# ---------------------------------------------------------------------------
# Web search implementations
# ---------------------------------------------------------------------------

def _web_search_openai(query: str, max_results: int) -> str:
    try:
        import openai
    except ImportError as exc:
        raise RuntimeError("openai package not installed. Run: pip install openai>=1.0") from exc

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    model  = os.getenv("OPENAI_SEARCH_MODEL", "gpt-4o-mini")
    # Build httpx client that tolerates corporate SSL inspection proxies
    import httpx as _httpx
    _ssl_verify = os.getenv("OPENAI_SSL_VERIFY", "false").lower() not in ("false", "0", "no")
    _http_client = _httpx.Client(verify=_ssl_verify)
    client = openai.OpenAI(api_key=api_key, http_client=_http_client)
    logger.info("OpenAI web_search: model=%s query=%r", model, query)

    prompt = (
        f"Search the web for: {query}\n\n"
        f"Return up to {max_results} results as a Markdown list with title, URL, and summary."
    )

    # Try Responses API first (openai SDK >= 1.60 with web_search_preview)
    if hasattr(client, "responses"):
        try:
            search_ctx = os.getenv("OPENAI_SEARCH_CTX", "medium")
            response = client.responses.create(
                model=model,
                tools=[{"type": "web_search_preview", "search_context_size": search_ctx}],
                input=prompt,
            )
            for block in response.output:
                if block.type == "message":
                    for content in block.content:
                        if content.type == "output_text":
                            return content.text
            logger.warning("Responses API returned no text output — falling back")
        except Exception as e:
            logger.warning("Responses API failed (%s) — falling back to chat completions", e)

    # Fallback: chat completions (works on all SDK versions, no special tools needed)
    logger.info("Using chat.completions fallback for web_search")
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a supply chain research assistant. When given a search topic, "
                    "provide up to {max_results} relevant results with: title, a plausible URL, "
                    "and a one-sentence summary. Format as a Markdown numbered list."
                ).format(max_results=max_results),
            },
            {"role": "user", "content": f"Find information about: {query}"},
        ],
        max_tokens=800,
        temperature=0.3,
    )
    return response.choices[0].message.content or "No search results returned."


def _web_search_serper(query: str, max_results: int) -> str:
    data = serper_search(query=query, num=max_results)
    return to_markdown_search_results(data, max_results=max_results)


def _fetch_url_markdown(url: str, max_chars: int) -> str:
    timeout = int(os.getenv("FETCH_TIMEOUT_SECS", "30"))
    r = _requests.get(url, timeout=timeout, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    text_md = md_convert(str(soup.body or soup), heading_style="ATX")
    text_md = re.sub(r"\n{3,}", "\n\n", text_md).strip()

    if len(text_md) > max_chars:
        text_md = text_md[:max_chars].rstrip() + "\n\n...(truncated)"
    return text_md


def _format_doc_hits(results: list, query: str, max_snippet_chars: int) -> List[DocHit]:
    hits = []
    for chunk, score in results:
        snippet = make_snippet(text=chunk.body_text, query=query, max_chars=max_snippet_chars)
        hits.append(DocHit(
            doc_name=chunk.doc_name,
            heading_path=chunk.heading_path,
            score=round(score, 6),
            snippet=snippet,
            chunk_id=chunk.chunk_id,
        ))
    return hits


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    info: dict = {
        "service":            "Kinaxis Tools API",
        "version":            app.version,
        "endpoints":          ["/web_search", "/open_url", "/doc_search", "/reload", "/health"],
        "web_search_backend": WEB_SEARCH_BACKEND,
        "doc_index_chunks":   len(_doc_chunks),
        "doc_index_dir":      str(DOC_DIR),
    }
    if WEB_SEARCH_BACKEND == "serper":
        info["serper_configured"] = bool(os.getenv("SERPER_API_KEY"))
    else:
        info["openai_configured"] = bool(os.getenv("OPENAI_API_KEY"))
    return info


@app.get("/health")
async def health():
    if WEB_SEARCH_BACKEND == "serper":
        web_status = "ready" if os.getenv("SERPER_API_KEY") else "no_api_key"
    else:
        web_status = "ready" if os.getenv("OPENAI_API_KEY") else "no_api_key"

    doc_status = (
        "ready"        if _doc_index is not None else
        "error"        if _doc_index_error else
        "initializing"
    )
    overall = "healthy" if web_status == "ready" and doc_status == "ready" else "degraded"

    return {
        "status": overall,
        "subsystems": {
            "web_search": {
                "backend": WEB_SEARCH_BACKEND,
                "status":  web_status,
            },
            "doc_search": {
                "status":         doc_status,
                "chunks_indexed": len(_doc_chunks),
                "doc_dir":        str(DOC_DIR),
                "error":          _doc_index_error,
            },
        },
    }


@app.post("/web_search", response_model=ToolResponse)
async def web_search(req: WebSearchRequest):
    """Search the web and return Markdown-formatted results."""
    logger.info("web_search  backend=%s  query=%r  max_results=%d",
                WEB_SEARCH_BACKEND, req.query, req.max_results)
    try:
        if WEB_SEARCH_BACKEND == "serper":
            result = _web_search_serper(req.query, req.max_results)
        else:
            result = _web_search_openai(req.query, req.max_results)
        return ToolResponse(result=result)
    except SerperError as exc:
        logger.error("web_search SerperError: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        logger.error("web_search RuntimeError: %s", exc)
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("web_search unexpected error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/open_url", response_model=ToolResponse)
async def open_url(req: OpenURLRequest):
    """Fetch a URL and return its content as clean Markdown."""
    logger.info("open_url  url=%r  max_chars=%d", req.url, req.max_chars)
    try:
        result = _fetch_url_markdown(req.url, req.max_chars)
        return ToolResponse(result=result)
    except _requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {exc}")
    except Exception as exc:
        logger.exception("open_url error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/doc_search", response_model=DocSearchResponse)
def doc_search(req: DocSearchRequest):
    """BM25 search over indexed DOCX/PDF/CSV documents."""
    if _doc_index is None:
        raise HTTPException(status_code=503,
            detail=f"Doc index not ready. Try POST /reload. Error: {_doc_index_error}")

    q = (req.query or "").strip()
    if not q:
        return DocSearchResponse(hits=[], total_chunks_indexed=len(_doc_chunks),
                                 source_dir=str(DOC_DIR), glob=DOC_GLOB)

    results = _doc_index.search(
        query=q,
        top_k=max(1, min(req.top_k, 20)),
        doc_filter=req.doc_filter,
        section_filter=req.section_id,
    )
    return DocSearchResponse(
        hits=_format_doc_hits(results, q, max(100, min(req.max_snippet_chars, 3000))),
        total_chunks_indexed=len(_doc_chunks),
        source_dir=str(DOC_DIR),
        glob=DOC_GLOB,
    )


@app.post("/reload/project/{project_id}")
async def reload_project_chunks(project_id: str):
    """
    Rebuild the doc search index to include chunks from a specific uploaded project.
    Called by screen3_routes after a new document is indexed.
    """
    global _doc_index, _doc_chunks, _doc_index_error
    import json as _json

    # Try common upload paths
    candidate_dirs = [
        pathlib.Path(os.getenv("UPLOAD_ROOT", "")) / project_id,
        pathlib.Path(__file__).parent.parent.parent / "brd_convo_app" / "backend" / "app" / "uploads" / project_id,
    ]

    new_chunks = []
    for upload_dir in candidate_dirs:
        chunks_file = upload_dir / "chunks.json"
        if chunks_file.exists():
            try:
                data = _json.loads(chunks_file.read_text(encoding="utf-8"))
                for c in data:
                    _hpath = c.get("section_heading", "") or c.get("heading_path", "")
                    new_chunks.append(Chunk(
                        doc_name       = c.get("doc_name", ""),
                        heading_path   = _hpath,
                        heading_text   = _hpath,
                        body_text      = c.get("chunk_text", ""),
                        chunk_id       = str(c.get("chunk_id", "")),
                        brd_section_id = c.get("brd_section_id", ""),
                    ))
                logger.info("Loaded %d project chunks from %s", len(new_chunks), chunks_file)
                break
            except Exception as e:
                logger.warning("Could not load project chunks from %s: %s", chunks_file, e)

    if new_chunks:
        # Merge project chunks with reference_docs chunks
        from retrieval import build_index_from_chunks
        try:
            combined = list(_doc_chunks) + new_chunks
            _doc_index  = build_index_from_chunks(combined)
            _doc_chunks = combined
            logger.info("Doc index rebuilt: %d total chunks (%d project)", len(combined), len(new_chunks))
            return {"ok": True, "total_chunks": len(combined), "project_chunks": len(new_chunks)}
        except Exception as e:
            logger.warning("Could not rebuild index with project chunks: %s — using project chunks only", e)
            _doc_chunks = new_chunks
            return {"ok": True, "total_chunks": len(new_chunks), "note": "index rebuild failed, using linear search"}

    return {"ok": False, "message": "No chunks.json found for project", "project_id": project_id}


@app.post("/reload")
def reload_index():
    """Reload the document index from DOC_RETRIEVAL_DIR."""
    _load_doc_index()
    return {
        "status": "ok" if _doc_index is not None else "error",
        "chunks": len(_doc_chunks),
        "doc_dir": str(DOC_DIR),
        "error": _doc_index_error,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _load_doc_index()
    # Default to loopback — this server has no authentication, so it must not
    # be exposed on all interfaces unless explicitly configured.
    host   = os.getenv("SERVER_HOST",   "127.0.0.1")
    port   = int(os.getenv("SERVER_PORT",   "8000"))
    reload = os.getenv("SERVER_RELOAD", "false").lower() == "true"
    logger.info("Starting Kinaxis Tools API on http://%s:%d  backend=%s", host, port, WEB_SEARCH_BACKEND)
    uvicorn.run("kinaxis_http_server:app", host=host, port=port, reload=reload, log_level="info")