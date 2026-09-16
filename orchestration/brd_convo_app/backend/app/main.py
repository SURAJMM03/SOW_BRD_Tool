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
# image_repo_routes is optional — keep startup resilient if the module
# hasn't been committed to this checkout yet.
try:
    from app.image_repo_routes import router as image_repo_router
except ImportError:
    image_repo_router = None
from app.auth import (
    init_db,
    signup_user,
    login_user,
    approve_user,
    create_session,
    validate_session,
    delete_session,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("BRDConvoAPI")

app = FastAPI(title="Blueprint Document Conversational API", version="0.1.0")

# Initialise auth DB on startup (creates users.db + default admin if absent)
init_db()

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

# ── Session auth middleware ───────────────────────────────────────────────────
# When REQUIRE_API_AUTH=true, all /api/* routes require a valid session cookie
# (set by POST /login). Defaults to false for local single-user use — set it
# to true in .env before exposing this app to other users or a network.
from fastapi import Request
from fastapi.responses import JSONResponse

SESSION_COOKIE = "brd_session"
# Secure by default: API auth is ON unless explicitly disabled with
# REQUIRE_API_AUTH=false in .env (local single-user development only).
REQUIRE_API_AUTH = os.getenv("REQUIRE_API_AUTH", "true").lower() != "false"

if not REQUIRE_API_AUTH:
    logging.getLogger("BRDConvoAPI").warning(
        "API auth is DISABLED (REQUIRE_API_AUTH=false — local mode). Remove this "
        "override from .env before exposing this app beyond your own machine."
    )

@app.middleware("http")
async def require_session_on_api(request: Request, call_next):
    if (
        REQUIRE_API_AUTH
        and request.url.path.startswith("/api/")
        and request.method != "OPTIONS"
    ):
        user = validate_session(request.cookies.get(SESSION_COOKIE, ""))
        if user is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated. Please log in."},
            )
        request.state.user = user
    return await call_next(request)

# ── Auth routes ───────────────────────────────────────────────────────────────
from pydantic import BaseModel as _BaseModel
from fastapi.responses import HTMLResponse as _HTMLResponse

class SignupRequest(_BaseModel):
    email: str
    password: str
    role: str = "document_editor"

class LoginRequest(_BaseModel):
    email: str
    password: str

class ChatRequest(_BaseModel):
    message: str = ""

@app.post("/signup")
def signup(req: SignupRequest):
    return signup_user(req.email, req.password, req.role)

@app.post("/login")
def login(req: LoginRequest):
    result = login_user(req.email, req.password)
    if not result.get("success"):
        return result
    token = create_session(req.email.strip().lower(), result.get("role") or "document_editor")
    response = JSONResponse(content=result)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        httponly=True,
        samesite="lax",
        max_age=12 * 3600,
        path="/",
    )
    return response

@app.post("/logout")
def logout(request: Request):
    delete_session(request.cookies.get(SESSION_COOKIE, ""))
    response = JSONResponse(content={"success": True})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response

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

@app.get("/approve/{token}")
def approve(token: str):
    result = approve_user(token)
    if result["success"]:
        approved_page = BASE_DIR / "static" / "approved.html"
        return FileResponse(
            str(approved_page) if approved_page.exists()
            else str(BASE_DIR / "static" / "index.html")
        )
    return _HTMLResponse(
        content=f"""<html><body style="font-family:'Segoe UI',sans-serif;text-align:center;
        padding:60px;background:#F7F7F7;color:#1A1A1A;">
        <div style="max-width:420px;margin:0 auto;background:#fff;border-radius:12px;
        padding:40px;box-shadow:0 4px 24px rgba(0,0,0,0.08);border:1px solid #ddd;">
        <div style="font-size:48px;margin-bottom:16px;">⚠️</div>
        <h2 style="color:#A32D2D;">{result['message']}</h2>
        <p style="color:#888;margin-top:12px;">Please contact your administrator.</p>
        </div></body></html>""",
        status_code=400
    )

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

# ── Register Image Repository API routes ──
if image_repo_router is not None:
    app.include_router(image_repo_router)

# ── Page routes ──
@app.get("/")
def landing():
    """Landing page — Login screen (Screen 1)."""
    return FileResponse(BASE_DIR / "static" / "index.html")

@app.get("/choose")
def choose_page():
    """Post-login hub: pick BRD Tool or SOW Tool."""
    return FileResponse(BASE_DIR / "static" / "choose.html", headers={"Cache-Control": "no-store"})

@app.get("/projects")
def projects_page():
    return FileResponse(BASE_DIR / "static" / "projects.html", headers={"Cache-Control": "no-store"})

@app.get("/upload")
def upload_page():
    return FileResponse(BASE_DIR / "static" / "document-upload.html", headers={"Cache-Control": "no-store"})

@app.get("/editor")
def editor_page():
    """Blueprint Document Template Editor (Screen 3)."""
    return FileResponse(BASE_DIR / "static" / "screen3.html")

@app.get("/update-editor")
def update_editor_page():
    """Template Editor for the Update Existing Document workflow (Screen 3 — Update variant)."""
    return FileResponse(BASE_DIR / "static" / "update-editor.html", headers={"Cache-Control": "no-store"})

@app.get("/update-generating")
def update_generating_page():
    """Generation monitor for the Update Existing Document workflow (merged export)."""
    return FileResponse(BASE_DIR / "static" / "update-generating.html", headers={"Cache-Control": "no-store"})

@app.get("/generating")
def generating_page():
    """Section-wise generation monitor."""
    return FileResponse(BASE_DIR / "static" / "generating.html", headers={"Cache-Control": "no-store"})

@app.get("/sow-hub")
def sow_hub_page():
    """SOW Tool landing page — choose Review a SOW vs. Draft a SOW. This is
    what /choose's "SOW Tool" card leads to; it never links to the BRD tool."""
    return FileResponse(BASE_DIR / "static" / "sow-hub.html", headers={"Cache-Control": "no-store"})

@app.get("/sow")
def sow_page():
    """SOW Tool — review an existing SOW against Bristlecone's checklist.
    The section-by-section drafting flow lives at /sow-projects onward."""
    return FileResponse(BASE_DIR / "static" / "sow.html", headers={"Cache-Control": "no-store"})

@app.get("/sow-projects")
def sow_projects_page():
    """SOW tool's own Projects hub (workflow=sow), separate from BRD projects."""
    return FileResponse(BASE_DIR / "static" / "sow-projects.html", headers={"Cache-Control": "no-store"})

@app.get("/sow-upload")
def sow_upload_page():
    """Upload source material (and link reference/sample-SOW repositories) for a SOW project."""
    return FileResponse(BASE_DIR / "static" / "sow-upload.html", headers={"Cache-Control": "no-store"})

@app.get("/sow-templates")
def sow_templates_page():
    """Choose the Bristlecone default template or a client's custom template for a SOW project."""
    return FileResponse(BASE_DIR / "static" / "sow-templates.html", headers={"Cache-Control": "no-store"})

@app.get("/sow-workflow")
def sow_workflow_page():
    """Section-by-section SOW draft, review, and approval."""
    return FileResponse(BASE_DIR / "static" / "sow-workflow.html", headers={"Cache-Control": "no-store"})

@app.get("/sow-review")
def sow_review_page():
    """Mandatory pre-signature review gate (US-03) + legal baseline (US-02)."""
    return FileResponse(BASE_DIR / "static" / "sow-review.html", headers={"Cache-Control": "no-store"})

@app.get("/sow-skill")
def sow_skill_page():
    """Assisted skill-learning: analyze sample SOWs and propose an update to SOW_SKILL.md."""
    return FileResponse(BASE_DIR / "static" / "sow-skill.html", headers={"Cache-Control": "no-store"})

@app.get("/repositories")
def repositories_page():
    """Repository management page."""
    return FileResponse(BASE_DIR / "static" / "repositories.html", headers={"Cache-Control": "no-store"})

@app.get("/templates")
def templates_page():
    """Template library management page."""
    return FileResponse(BASE_DIR / "static" / "templates.html", headers={"Cache-Control": "no-store"})

@app.get("/image-repo")
def image_repo_page():
    """Image Repository — import and manage process diagrams and screenshots."""
    return FileResponse(BASE_DIR / "static" / "image_repo.html", headers={"Cache-Control": "no-store"})

@app.get("/help")
def help_page():
    """Author's guide and cheat sheet."""
    return FileResponse(BASE_DIR / "static" / "help.html", headers={"Cache-Control": "no-store"})

@app.get("/assistant")
def assistant_page():
    """Standalone full-page AI Optimization Assistant (BlueYonder-style Q&A)."""
    return FileResponse(BASE_DIR / "static" / "assistant.html", headers={"Cache-Control": "no-store"})
