from __future__ import annotations

import sys
import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))


# Ensure backend/ and repo root are on sys.path so all absolute imports work.
# main.py lives at: <repo_root>/orchestration/brd_convo_app/backend/app/main.py
_app_dir = Path(__file__).parent                        # .../app
_backend_dir = str(_app_dir.parent)                     # .../backend
_repo_root = str(_app_dir.parent.parent.parent.parent)  # <repo_root>
for _p in (_backend_dir, _repo_root):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Load .env from backend/ directory
try:
    from dotenv import load_dotenv
    load_dotenv(_app_dir.parent / ".env")
except ImportError:
    pass

# An OPENAI_API_KEY left at the .env.example placeholder counts as "set"
# everywhere the pipeline checks for it, so every upload made doomed OpenAI
# calls (keywords, embeddings, vision) and waited out their retries before
# falling back. Treat it as unset so those paths go straight to the fallback.
if os.getenv("OPENAI_API_KEY", "").strip().strip('"').lower().startswith("your-"):
    os.environ.pop("OPENAI_API_KEY", None)

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.project_routes import router as project_router
from app.repository_routes import router as repository_router
from app.export_import_routes import router as export_import_router
from app.screen3_routes import router as screen3_router
from app.template_routes import router as template_router
from app.chat_routes import router as chat_router
from app.sow_routes import router as sow_router
from app.sow_template_routes import router as sow_template_router
from app.sow_section_routes import router as sow_section_router
from app.sow_skill_routes import router as sow_skill_router
from app.sow_review_routes import router as sow_review_router
from app.pitch_scorecard_routes import router as pitch_scorecard_router
from app.diagnostics_routes import router as diagnostics_router
# image_repo_routes is optional — keep startup resilient if the module
# hasn't been committed to this checkout yet.
try:
    from app.image_repo_routes import router as image_repo_router
except ImportError:
    image_repo_router = None
from app.identity import resolve_user, current_user

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("BRDConvoAPI")

app = FastAPI(title="Blueprint Document Conversational API", version="0.1.0")

BASE_DIR = Path(__file__).parent

app.mount(
    "/static",
    StaticFiles(directory=BASE_DIR / "static"),
    name="static",
)

# ── /brd-generator prefix compatibility ──────────────────────────────────────
# The deployed build mounts this app under /brd-generator. Strip that prefix
# here so pages/links built for the prefixed deployment also work locally,
# where the app runs at the root.
class _StripPrefixMiddleware:
    def __init__(self, app, prefix: str = "/brd-generator"):
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                new_path = path[len(self.prefix):] or "/"
                scope["path"] = new_path
                scope["raw_path"] = new_path.encode()
        return await self.app(scope, receive, send)

app.add_middleware(_StripPrefixMiddleware)

# Allow local dev UI
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["Content-Type", "Authorization"],
)

# ── Global error handler ──────────────────────────────────────────────────────
# Without this, an unhandled exception (e.g. Azure/Anthropic auth failures,
# LLM timeouts) falls through to Starlette's default plain-text/HTML 500 page.
# Every page's JS does `await res.json()` on API responses, so a non-JSON body
# crashes with a confusing "Unexpected token 'I', "Internal S"... is not valid
# JSON" error instead of showing the real problem. Return JSON here instead so
# the frontend can always parse the response and surface `detail` to the user.
from fastapi import Request as _Request
from fastapi.responses import JSONResponse as _JSONResponse

@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: _Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _JSONResponse(status_code=500, content={"detail": f"Internal server error: {exc}"})

# ── Upstream identity middleware ──────────────────────────────────────────────
# This app has no login of its own — the host application signs the user in
# and forwards the identity on every request (see identity.py). Attach it to
# the request here, and refuse /api/* calls that arrive without one so the API
# is never served to an unidentified caller.
from fastapi import Request
from fastapi.responses import JSONResponse

# DEV_FALLBACK_USER makes every request authenticate as one fixed person even
# with no identity header. That is what lets the app run standalone on a
# laptop — and on a reachable host it means anyone who finds the URL is signed
# in. It is invisible from the UI, so say it loudly at startup rather than
# letting a deployment quietly run open.
from app.identity import DEV_FALLBACK_USER as _FALLBACK_USER

if _FALLBACK_USER:
    logger.warning(
        "=" * 78 + "\n"
        "  DEV_FALLBACK_USER is set (%s).\n"
        "  Every request is treated as this user even with NO identity header,\n"
        "  so anyone who can reach this server is signed in. That is intended\n"
        "  only for running standalone on a developer machine.\n"
        "  On a hosted instance: unset DEV_FALLBACK_USER and have the fronting\n"
        "  proxy set %s — overwriting any client-supplied value.\n"
        "  Check GET /api/diagnostics to confirm.\n" + "=" * 78,
        _FALLBACK_USER, os.getenv("AUTH_USER_HEADER", "X-Forwarded-User"))

@app.middleware("http")
async def attach_upstream_user(request: Request, call_next):
    user = resolve_user(request)
    request.state.user = user
    if (
        user is None
        and request.url.path.startswith("/api/")
        and request.method != "OPTIONS"
    ):
        return JSONResponse(
            status_code=401,
            content={
                "detail": "No signed-in user was forwarded by the host "
                          "application. Check the AUTH_USER_HEADER setting."
            },
        )
    return await call_next(request)

# ── Identity route ────────────────────────────────────────────────────────────
from pydantic import BaseModel as _BaseModel

class ChatRequest(_BaseModel):
    message: str = ""

@app.get("/api/me")
def whoami(request: Request):
    """Whoever the host application says is signed in."""
    return current_user(request)

@app.post("/chat")
def legacy_chat(req: ChatRequest):
    return {
        "reply": (
            "The conversational chat flow has moved to the project workspace. "
            "Please create or open a project, upload documents, then use the Template Editor."
        ),
        "phase": "init",
        "download_url": None,
        "preview": None,
    }

# ── Register project/upload API routes ──
app.include_router(project_router)

# ── Register repository API routes ──
app.include_router(repository_router)

# ── Register export/import API routes ──
app.include_router(export_import_router)

# ── Register Screen 3 (Template Editor) API routes ──
app.include_router(screen3_router)

# ── Register AI Chatbot API routes ──
app.include_router(chat_router)

# ── Register Template Library API routes ──
app.include_router(template_router)

# ── Register SOW Generate/Review API routes (legacy one-shot Review mode) ──
app.include_router(sow_router)

# ── Register the new section-by-section SOW engine (templates, sections, skill learning) ──
app.include_router(sow_template_router)
app.include_router(sow_section_router)
app.include_router(sow_skill_router)
# Legal baseline (US-02) + mandatory pre-signature review gate (US-03)
app.include_router(sow_review_router)
# Advisory pitch-deck scorecard, run on the uploaded deck before the SOW is drafted
app.include_router(pitch_scorecard_router)
# Environment triage for hosted installs — readers, storage, LLM credentials
app.include_router(diagnostics_router)

# ── Register Image Repository API routes ──
if image_repo_router is not None:
    app.include_router(image_repo_router)

# ── Page rendering ────────────────────────────────────────────────────────────
# Every page is served with the host-provided identity injected into <head>,
# so page scripts can read it synchronously (window.CURRENT_USER, and the
# legacy sessionStorage "auth_user" key) without a round trip on load.
import json as _json
from fastapi.responses import Response as _Response
_LT = chr(92) + "u003c"   # the JS escape for "<", built without a backslash literal

# Every page does `const data = await res.json(); if (!res.ok) throw new
# Error(data.detail)`. Behind a proxy, a timeout or 502 comes back as an HTML
# page, so users saw "Unexpected token '<'"; and a 422 carries `detail` as a
# list, shown as "[object Object]". This makes both read as plain messages
# for every page at once.
_SAFE_JSON_JS = (
    "<script>(function(){var P=Response.prototype,J=P.json;"
    "P.json=async function(){var r=this,t=await r.clone().text(),d;"
    "try{d=JSON.parse(t)}catch(e){throw new Error(r.ok?'The server sent an unexpected response \\u2014 please try again.'"
    ":'Server error '+r.status+(r.statusText?' ('+r.statusText+')':'')+' \\u2014 please try again.')}"
    "if(!r.ok&&d&&d.detail&&typeof d.detail!=='string'){try{d.detail=Array.isArray(d.detail)?"
    "d.detail.map(function(x){return x&&x.msg?((x.loc||[]).slice(1).join('.')+' '+x.msg).trim():JSON.stringify(x)}).join('; ')"
    ":JSON.stringify(d.detail)}catch(e){}}return d};})();</script>"
)


def _page(name: str, request: Request) -> _Response:
    raw = (BASE_DIR / "static" / name).read_bytes()
    # _LT below keeps a "<" in the host header from closing this script tag.
    blob = _json.dumps(current_user(request)).replace("<", _LT)
    boot = (
        "<script>window.CURRENT_USER=" + blob + ";"
        "try{sessionStorage.setItem('auth_user',JSON.stringify(window.CURRENT_USER));}"
        "catch(e){}</script>"
        + _SAFE_JSON_JS +
        # Empty icon, so the browser doesn't request /favicon.ico — a 404
        # there shows up as a console error on every page.
        '<link rel="icon" href="data:,">'
    ).encode("utf-8")
    k = raw.lower().find(b"<head>")
    raw = raw[:k + 6] + boot + raw[k + 6:] if k != -1 else boot + raw
    return _Response(
        content=raw,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )

# ── Page routes ──
@app.get("/")
def landing(request: Request):
    """Entry point — the SOW tool.

    This deployment is SOW-only: opening the app goes straight to the SOW hub
    rather than a picker. The BRD screens are still routed (a bookmark or an
    in-flight project keeps working) but nothing navigates to them.

    Served directly rather than redirected so the /brd-generator mount prefix
    is preserved. There is no login screen: the host application has already
    signed the user in by the time they get here."""
    return _page("sow-hub.html", request)

@app.get("/choose")
def choose_page(request: Request):
    """Kept so an old bookmark still lands somewhere useful — the SOW hub."""
    return _page("sow-hub.html", request)

@app.get("/projects")
def projects_page(request: Request):
    return _page("projects.html", request)

@app.get("/upload")
def upload_page(request: Request):
    return _page("document-upload.html", request)

@app.get("/editor")
def editor_page(request: Request):
    """Blueprint Document Template Editor (Screen 3)."""
    return _page("screen3.html", request)

@app.get("/update-editor")
def update_editor_page(request: Request):
    """Template Editor for the Update Existing Document workflow (Screen 3 — Update variant)."""
    return _page("update-editor.html", request)

@app.get("/update-generating")
def update_generating_page(request: Request):
    """Generation monitor for the Update Existing Document workflow (merged export)."""
    return _page("update-generating.html", request)

@app.get("/generating")
def generating_page(request: Request):
    """Section-wise generation monitor."""
    return _page("generating.html", request)

@app.get("/sow-hub")
def sow_hub_page(request: Request):
    """SOW Tool landing page — choose Review a SOW vs. Draft a SOW. This is
    what /choose's "SOW Tool" card leads to; it never links to the BRD tool."""
    return _page("sow-hub.html", request)

@app.get("/sow")
def sow_page(request: Request):
    """SOW Tool — review an existing SOW against Bristlecone's checklist.
    The section-by-section drafting flow lives at /sow-projects onward."""
    return _page("sow.html", request)

@app.get("/sow-projects")
def sow_projects_page(request: Request):
    """SOW tool's own Projects hub (workflow=sow), separate from BRD projects."""
    return _page("sow-projects.html", request)

@app.get("/sow-upload")
def sow_upload_page(request: Request):
    """Upload source material (and link reference/sample-SOW repositories) for a SOW project."""
    return _page("sow-upload.html", request)

@app.get("/sow-pitch-score")
def sow_pitch_score_page(request: Request):
    """Advisory quality score for the uploaded pitch/RFP deck, before drafting."""
    return _page("sow-pitch-score.html", request)

@app.get("/sow-templates")
def sow_templates_page(request: Request):
    """Choose the Bristlecone default template or a client's custom template for a SOW project."""
    return _page("sow-templates.html", request)

@app.get("/sow-workflow")
def sow_workflow_page(request: Request):
    """Section-by-section SOW draft, review, and approval."""
    return _page("sow-workflow.html", request)

@app.get("/sow-review")
def sow_review_page(request: Request):
    """Mandatory pre-signature review gate (US-03) + legal baseline (US-02)."""
    return _page("sow-review.html", request)

@app.get("/sow-skill")
def sow_skill_page(request: Request):
    """Assisted skill-learning: analyze sample SOWs and propose an update to SOW_SKILL.md."""
    return _page("sow-skill.html", request)

@app.get("/repositories")
def repositories_page(request: Request):
    """Repository management page."""
    return _page("repositories.html", request)

@app.get("/templates")
def templates_page(request: Request):
    """Template library management page."""
    return _page("templates.html", request)

@app.get("/image-repo")
def image_repo_page(request: Request):
    """Image Repository — import and manage process diagrams and screenshots."""
    return _page("image_repo.html", request)

@app.get("/help")
def help_page(request: Request):
    """Author's guide and cheat sheet."""
    return _page("help.html", request)

@app.get("/assistant")
def assistant_page(request: Request):
    """Standalone full-page AI Optimization Assistant (BlueYonder-style Q&A)."""
    return _page("assistant.html", request)
