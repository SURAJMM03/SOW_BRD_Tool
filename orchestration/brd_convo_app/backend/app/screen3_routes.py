"""
screen3_routes.py
─────────────────────────────────────────────────────────────────────────────
FastAPI routes for Screen 3 — Blueprint Document Template Editor.

Mount this in main.py by adding:
    from app.screen3_routes import router as screen3_router
    app.include_router(screen3_router)

Place this file at:
    blueprintdocgenagent_16-04/orchestration/brd_convo_app/backend/app/screen3_routes.py
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import difflib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from fastapi import APIRouter, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from pydantic import BaseModel

# ── Path bootstrap ────────────────────────────────────────────────────────────
# screen3_routes.py lives at:
#   BlueprintDocGenAgent_16-04/orchestration/brd_convo_app/backend/app/screen3_routes.py
# We need BlueprintDocGenAgent_16-04/ on sys.path so `agents.*` imports resolve.
_THIS_FILE   = Path(__file__).resolve()
_APP_DIR     = _THIS_FILE.parent                        # .../app
_BACKEND_DIR = _APP_DIR.parent                          # .../backend
_REPO_ROOT   = _APP_DIR.parent.parent.parent.parent     # BlueprintDocGenAgent_16-04/

for _p in (str(_BACKEND_DIR), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
# ─────────────────────────────────────────────────────────────────────────────

logger = logging.getLogger("Screen3Routes")

router = APIRouter()

# ─────────────────────────────────────────────────────────────────────────────
# SCREEN3 SESSION — single shared SessionState for the editor
# Holds all approved section content, ready for assembly into DOCX.
# ─────────────────────────────────────────────────────────────────────────────
from uuid import uuid4

_SCREEN3_SESSION_ID = "screen3-editor"
_screen3_state = None   # lazy-initialised on first use

def _get_screen3_state():
    """
    Return (or create) the persistent Screen3 SessionState.
    On first call, reloads any previously approved sections from disk
    so restarts never lose approved content.
    """
    global _screen3_state
    if _screen3_state is None:
        try:
            from app.models import SessionState
            _screen3_state = SessionState(
                session_id=_SCREEN3_SESSION_ID,
                phase="screen3",
                company="",
            )
            # ── Reload approved sections from disk ──────────────────────────
            store = _load_approved_store()
            if store:
                for top, field in SECTION_STATE_MAP.items():
                    sections = {
                        sid: content
                        for sid, content in store.get("sections", {}).items()
                        if sid.split(".")[0] == top
                    }
                    if sections:
                        setattr(_screen3_state, field, sections)
                if store.get("company"):
                    _screen3_state.company = store["company"]
                logger.info(
                    "Reloaded %d approved sections from disk.",
                    len(store.get("sections", {}))
                )
            else:
                logger.info("Screen3 SessionState initialised (no previous data).")
        except Exception as e:
            logger.error("Could not create SessionState: %s", e)
            return None
    return _screen3_state

# ── Section ID -> SessionState field mapping ─────────────────────────────────
# Maps top-level section number to the correct SessionState dict attribute
# and the title used in assemble_document()
SECTION_STATE_MAP = {
    "1": "section1_sections",
    "2": "br_sections",
    "3": "ssc_sections",
    "4": "dp_sections",
    "5": "ip_sections",
    "6": "di_sections",
}

# ── Persistence paths ────────────────────────────────────────────────────────
_THIS_DIR = Path(__file__).parent
KEYWORD_STORE_PATH   = _THIS_DIR / "screen3_keyword_store.json"
APPROVED_STORE_PATH  = _THIS_DIR / "screen3_approved_sections.json"
SECTIONS_STORE_PATH  = _THIS_DIR / "screen3_sections.json"
SOURCES_STORE_PATH   = _THIS_DIR / "screen3_sources.json"
DOCUMENT_IMPORT_ROOT = _THIS_DIR / "document_imports"
DOCUMENT_VERSION_ROOT = _THIS_DIR / "document_versions"

HTTP_BASE = "http://localhost:8010"   # kinaxis_http_server


DEFAULT_TEMPLATE_PATH = _THIS_DIR / "Solution_Blueprint_Default_Template.docx"


def _project_source_filenames(project_id: str) -> set:
    """Return a lowercase set of every source-document filename the user has
    uploaded for this project — both directly into the project's own source
    dir AND through any attached document Repository.

    Used to scope chunks-browser and images-browser to the user's true
    upload corpus while still honouring the Repositories tab.
    """
    names: set = set()
    try:
        try:
            from app.project_routes import UPLOAD_ROOT, PROJECTS
        except ImportError:
            from project_routes import UPLOAD_ROOT, PROJECTS
    except Exception:
        return names

    own_dir = Path(UPLOAD_ROOT) / project_id / "source"
    if own_dir.exists():
        try:
            for f in own_dir.iterdir():
                if f.is_file():
                    names.add(f.name.lower())
        except Exception:
            pass

    # Pull filenames from every attached document Repository.
    repo_ids = PROJECTS.get(project_id, {}).get("repositories", []) or []
    if repo_ids:
        try:
            try:
                from app.repository_routes import REPOS_ROOT, REPOSITORIES
            except ImportError:
                from repository_routes import REPOS_ROOT, REPOSITORIES
            for rid in repo_ids:
                if rid not in REPOSITORIES:
                    continue
                repo_src = Path(REPOS_ROOT) / rid / "source"
                if repo_src.exists():
                    try:
                        for f in repo_src.iterdir():
                            if f.is_file():
                                names.add(f.name.lower())
                    except Exception:
                        pass
        except Exception:
            pass
    return names


def _load_all_project_chunks(project_id: str) -> list:
    """
    Load chunks for a project including any linked repositories.
    Replaces bare load_project_chunks(UPLOAD_ROOT / project_id) calls so that
    projects which rely solely on attached repositories still return content.
    """
    try:
        from app.project_routes import UPLOAD_ROOT, PROJECTS
        from app.chunking_pipeline import load_project_chunks
    except ImportError:
        from project_routes import UPLOAD_ROOT, PROJECTS
        from chunking_pipeline import load_project_chunks

    project_dir = UPLOAD_ROOT / project_id
    own_chunks = load_project_chunks(project_dir)

    # Merge chunks from attached repositories
    repo_ids = PROJECTS.get(project_id, {}).get("repositories", [])
    if repo_ids:
        try:
            from app.repository_routes import load_chunks_for_repos
        except ImportError:
            from repository_routes import load_chunks_for_repos
        repo_chunks = load_chunks_for_repos(repo_ids)
        # Repo chunks have their OWN id space that can collide with the
        # project's. Re-id ONLY the repo chunks above the project's max so
        # the merged set is globally unique — otherwise the chunk browser
        # would resolve one source's chunk_id to another's (view/select/pin
        # all key on chunk_id). Project ids stay stable so existing pins hold.
        own_ids = [c.get("chunk_id") for c in own_chunks if isinstance(c.get("chunk_id"), int)]
        next_id = (max(own_ids) + 1) if own_ids else 0
        taken = set(own_ids)
        for c in repo_chunks:
            cid = c.get("chunk_id")
            if not isinstance(cid, int) or cid in taken:
                c["chunk_id"] = next_id
                next_id += 1
            taken.add(c["chunk_id"])
        return own_chunks + repo_chunks

    return own_chunks


def _template_path(project_id: str = None) -> Optional[Path]:
    """
    Return the .docx template to use for export.

    Per user request, the Solution_Blueprint_Default_Template.docx fallback
    has been DISABLED — exports now always go through the legacy "build a
    fresh docx" formatter unless the user has explicitly uploaded a custom
    template for the project via /api/project/{pid}/set-template.

    Resolution order:
      1. Per-project template uploaded via /api/project/{pid}/set-template
         (saved at uploads/{pid}/_template.docx, with a pointer at _template.txt).
      2. None → export falls back to the legacy "build a fresh docx" path.
    """
    if project_id:
        try:
            from app.project_routes import UPLOAD_ROOT
        except ImportError:
            from project_routes import UPLOAD_ROOT
        marker = UPLOAD_ROOT / project_id / "_template.txt"
        if marker.exists():
            try:
                p = Path(marker.read_text(encoding="utf-8").strip())
                if p.exists():
                    return p
            except Exception:
                pass
    return None


def _get_client_company(project_id: str = None, ctx_dict: dict = None) -> str:
    """Return client company name, ALWAYS scoped to the given project.

    Priority:
    1. Explicit ctx_dict (caller-supplied override)
    2. Per-project _client_context.json on disk  ← most reliable
    3. Project's own client/name fields from PROJECTS dict
    4. Return "" — NEVER fall back to global _screen3_state.company
       (that singleton is shared across all projects and causes cross-project
       client name bleed when multiple projects are used in the same session).
    """
    if ctx_dict and isinstance(ctx_dict, dict):
        company = (ctx_dict.get("company") or "").strip()
        if company:
            return company
    if project_id:
        try:
            try:
                from app.project_routes import UPLOAD_ROOT
            except ImportError:
                from project_routes import UPLOAD_ROOT
            ctx_file = UPLOAD_ROOT / project_id / "_client_context.json"
            if ctx_file.exists():
                import json as _j
                data = _j.loads(ctx_file.read_text(encoding="utf-8"))
                company = (data.get("company") or "").strip()
                if company:
                    return company
        except Exception:
            pass
        # Fallback: use the project's own client/name fields
        try:
            try:
                from app.project_routes import PROJECTS
            except ImportError:
                from project_routes import PROJECTS
            proj = PROJECTS.get(project_id)
            if proj:
                client = (proj.get("client") or "").strip()
                if client and client.lower() != "no description":
                    return client
                name = (proj.get("name") or "").strip()
                if name:
                    return name
        except Exception:
            pass
    # ── INTENTIONALLY NO global _screen3_state.company fallback ──────────────
    # The global state is shared across all projects. Falling back to it would
    # cause the last-active project's company name to bleed into any project
    # that lacks a client context file (e.g. a brand-new project created just
    # after working on another client). Return "" so callers can display a
    # placeholder or prompt the user to fill in the Client Context panel.
    return ""


# ── Section structure persistence (per-project) ──────────────────────────────
def _sections_store_path(project_id: str = None) -> Path:
    if project_id:
        return _THIS_DIR / f"screen3_sections_{project_id}.json"
    return SECTIONS_STORE_PATH


def _load_sections_store(project_id: str = None) -> dict:
    path = _sections_store_path(project_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read sections store: %s", e)
    return {}


def _save_sections_store(store: dict, project_id: str = None):
    try:
        _sections_store_path(project_id).write_text(
            json.dumps(store, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        logger.error("Could not save sections store: %s", e)


def _get_live_sections(project_id: str = None) -> list:
    store = _load_sections_store(project_id)
    active = store.get("active")
    if active is not None:
        return active
    return [dict(s) for s in ALL_SECTIONS]


def _get_archived_sections(project_id: str = None) -> list:
    store = _load_sections_store(project_id)
    return store.get("archived", [])


def _persist_sections(active: list, archived: list, project_id: str = None):
    # Merge instead of overwrite so extra store keys (e.g. template_source)
    # survive routine section updates.
    store = _load_sections_store(project_id)
    store["active"] = active
    store["archived"] = archived
    _save_sections_store(store, project_id)


def _approved_store_path(project_id: str = None) -> Path:
    if project_id:
        return _THIS_DIR / f"screen3_approved_{project_id}.json"
    return APPROVED_STORE_PATH

def _load_approved_store(project_id: str = None) -> Dict:
    """Load approved section content from disk."""
    path = _approved_store_path(project_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read approved store: %s", e)
    return {}


def _save_approved_store(store: Dict, project_id: str = None):
    """Persist approved section content to disk."""
    try:
        _approved_store_path(project_id).write_text(
            json.dumps(store, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        logger.error("Could not save approved store: %s", e)


def _sources_store_path(project_id: str = None) -> Path:
    if project_id:
        return _THIS_DIR / f"screen3_sources_{project_id}.json"
    return SOURCES_STORE_PATH

def _load_sources_store(project_id: str = None) -> Dict:
    path = _sources_store_path(project_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read sources store: %s", e)
    return {}


def _save_sources_store(store: Dict, project_id: str = None):
    try:
        _sources_store_path(project_id).write_text(
            json.dumps(store, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
    except Exception as e:
        logger.error("Could not save sources store: %s", e)


def _safe_project_key(project_id: str = None) -> str:
    """Filesystem-safe key for project-scoped document artifacts."""
    raw = (project_id or "global").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    return (safe or "global")[:100]


def _section_sort_key(section_id: str):
    key = []
    for part in str(section_id).split("."):
        key.append((0, int(part)) if part.isdigit() else (1, part.lower()))
    return key


def _project_upload_root() -> Optional[Path]:
    try:
        try:
            from app.project_routes import UPLOAD_ROOT
        except ImportError:
            from project_routes import UPLOAD_ROOT
        return Path(UPLOAD_ROOT)
    except Exception:
        return None


def _project_client_context_path(project_id: str = None) -> Optional[Path]:
    root = _project_upload_root()
    if not root or not project_id:
        return None
    return root / project_id / "_client_context.json"


def _load_project_client_context(project_id: str = None) -> Dict:
    path = _project_client_context_path(project_id)
    if path and path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read client context for %s: %s", project_id, e)
    return {}


def _save_project_client_context(project_id: str = None, context: Dict = None):
    path = _project_client_context_path(project_id)
    if not path:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(context or {}, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("Could not save client context for %s: %s", project_id, e)


def _sync_screen3_state_from_approved(project_id: str = None):
    """Refresh the in-memory export state from the approved store."""
    state = _get_screen3_state()
    if not state:
        return
    for field in SECTION_STATE_MAP.values():
        setattr(state, field, {})

    store = _load_approved_store(project_id)
    for sid, content in (store.get("sections") or {}).items():
        top = sid.split(".")[0]
        field = SECTION_STATE_MAP.get(top, "di_sections")
        current = getattr(state, field, None) or {}
        current[sid] = content
        setattr(state, field, current)
    if store.get("company"):
        state.company = store["company"]


def _version_project_dir(project_id: str = None) -> Path:
    return DOCUMENT_VERSION_ROOT / _safe_project_key(project_id)


def _snapshot_state(project_id: str = None) -> Dict[str, Any]:
    return {
        "active_sections": _get_live_sections(project_id),
        "archived_sections": _get_archived_sections(project_id),
        "approved_store": _load_approved_store(project_id),
        "keyword_store": _load_keyword_store(project_id),
        "sources_store": _load_sources_store(project_id),
        "client_context": _load_project_client_context(project_id),
    }


def _notify_git_mcp(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort Git MCP HTTP hook.

    The app remains fully usable without a Git MCP server. Set
    GIT_MCP_BASE_URL to forward snapshot/restore events to a server that can
    commit/push the JSON snapshot files.
    """
    base_url = (
        os.getenv("GIT_MCP_BASE_URL")
        or os.getenv("MCP_GIT_BASE_URL")
        or ""
    ).strip().rstrip("/")
    if not base_url:
        return {"enabled": False, "ok": False, "message": "GIT_MCP_BASE_URL not configured"}
    try:
        timeout_s = float(os.getenv("GIT_MCP_TIMEOUT_SECONDS", "5"))
    except Exception:
        timeout_s = 5.0

    configured = (os.getenv("GIT_MCP_TOOL_ENDPOINT") or "").strip()
    endpoints = []
    if configured:
        endpoints.append(configured if configured.startswith("/") else f"/{configured}")
    endpoints.extend([
        f"/document-version/{action}",
        f"/tools/{action}",
        f"/{action}",
    ])

    last_error = ""
    for endpoint in endpoints:
        try:
            resp = requests.post(
                f"{base_url}{endpoint}",
                json={"action": action, **payload},
                timeout=timeout_s,
                headers={"Content-Type": "application/json"},
            )
            if resp.status_code == 404:
                last_error = f"404 at {endpoint}"
                continue
            resp.raise_for_status()
            try:
                result = resp.json()
            except Exception:
                result = {"text": resp.text[:500]}
            return {"enabled": True, "ok": True, "endpoint": endpoint, "response": result}
        except Exception as e:
            last_error = str(e)
    return {"enabled": True, "ok": False, "error": last_error or "Git MCP call failed"}


def _write_version_snapshot(
    project_id: str = None,
    message: str = "",
    author: str = "",
    source: str = "manual",
) -> Dict[str, Any]:
    created_at = datetime.now().isoformat(timespec="seconds")
    version_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    state = _snapshot_state(project_id)
    approved_count = len((state.get("approved_store") or {}).get("sections") or {})
    metadata = {
        "version_id": version_id,
        "project_id": project_id,
        "created_at": created_at,
        "message": message or "Manual document snapshot",
        "author": author or "",
        "source": source or "manual",
        "counts": {
            "active_sections": len(state.get("active_sections") or []),
            "archived_sections": len(state.get("archived_sections") or []),
            "approved_sections": approved_count,
        },
    }

    version_dir = _version_project_dir(project_id) / version_id
    version_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = version_dir / "snapshot.json"
    snapshot_path.write_text(
        json.dumps({"metadata": metadata, "state": state}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    metadata["snapshot_path"] = str(snapshot_path)
    metadata["git"] = _notify_git_mcp(
        "snapshot",
        {
            "project_id": project_id,
            "version_id": version_id,
            "message": metadata["message"],
            "snapshot_path": str(snapshot_path),
            "source": metadata["source"],
        },
    )
    snapshot_path.write_text(
        json.dumps({"metadata": metadata, "state": state}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info("Document version snapshot saved: %s (project=%s)", version_id, project_id)
    return metadata


def _load_version_snapshot(project_id: str, version_id: str) -> Dict[str, Any]:
    safe_vid = re.sub(r"[^A-Za-z0-9_.-]+", "", version_id or "")
    if not safe_vid:
        raise HTTPException(status_code=400, detail="Invalid version id.")
    path = _version_project_dir(project_id) / safe_vid / "snapshot.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Version not found.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Could not load version: {e}")


def _list_version_snapshots(project_id: str) -> List[Dict[str, Any]]:
    root = _version_project_dir(project_id)
    if not root.exists():
        return []
    versions = []
    for snap in root.glob("*/snapshot.json"):
        try:
            payload = json.loads(snap.read_text(encoding="utf-8"))
            meta = payload.get("metadata", {})
            if meta:
                versions.append(meta)
        except Exception as e:
            logger.warning("Skipping unreadable snapshot %s: %s", snap, e)
    versions.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return versions


def _snapshot_title_map(snapshot: Dict[str, Any]) -> Dict[str, str]:
    state = snapshot.get("state") or {}
    sections = (state.get("active_sections") or []) + (state.get("archived_sections") or [])
    return {
        s.get("id"): s.get("title", "")
        for s in sections
        if s.get("id")
    }


def _content_preview(content: str, limit: int = 520) -> str:
    clean = re.sub(r"\s+", " ", content or "").strip()
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "..."


def _compact_unified_diff(before: str, after: str, max_lines: int = 48) -> List[str]:
    before_lines = (before or "").splitlines()
    after_lines = (after or "").splitlines()
    lines = list(difflib.unified_diff(
        before_lines,
        after_lines,
        fromfile="saved-version",
        tofile="current-editor",
        lineterm="",
        n=2,
    ))
    if len(lines) > max_lines:
        return lines[:max_lines] + [f"... diff truncated, {len(lines) - max_lines} more line(s)"]
    return lines


def _section_diff_entry(
    section_id: str,
    title_map: Dict[str, str],
    before: str = "",
    after: str = "",
    include_diff: bool = False,
) -> Dict[str, Any]:
    entry = {
        "id": section_id,
        "title": title_map.get(section_id, ""),
        "before_chars": len(before or ""),
        "after_chars": len(after or ""),
        "before_preview": _content_preview(before),
        "after_preview": _content_preview(after),
    }
    if include_diff:
        entry["diff_lines"] = _compact_unified_diff(before, after)
    return entry


def _diff_approved_sections(left: Dict[str, Any], right: Dict[str, Any]) -> Dict[str, Any]:
    left_sections = ((left.get("state") or {}).get("approved_store") or {}).get("sections") or {}
    right_sections = ((right.get("state") or {}).get("approved_store") or {}).get("sections") or {}
    title_map = {
        **_snapshot_title_map(left),
        **_snapshot_title_map(right),
    }
    left_ids = set(left_sections)
    right_ids = set(right_sections)
    added = sorted(right_ids - left_ids, key=_section_sort_key)
    removed = sorted(left_ids - right_ids, key=_section_sort_key)
    changed = sorted(
        [sid for sid in (left_ids & right_ids) if left_sections.get(sid) != right_sections.get(sid)],
        key=_section_sort_key,
    )
    unchanged = sorted((left_ids & right_ids) - set(changed), key=_section_sort_key)
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": len(unchanged),
        "details": {
            "added": [
                _section_diff_entry(sid, title_map, "", right_sections.get(sid, ""))
                for sid in added
            ],
            "removed": [
                _section_diff_entry(sid, title_map, left_sections.get(sid, ""), "")
                for sid in removed
            ],
            "changed": [
                _section_diff_entry(
                    sid,
                    title_map,
                    left_sections.get(sid, ""),
                    right_sections.get(sid, ""),
                    include_diff=True,
                )
                for sid in changed
            ],
        },
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": len(unchanged),
        },
    }


def _restore_version_state(project_id: str, snapshot: Dict[str, Any]):
    state = snapshot.get("state") or {}
    _persist_sections(
        state.get("active_sections") or [dict(s) for s in ALL_SECTIONS],
        state.get("archived_sections") or [],
        project_id,
    )
    _save_approved_store(state.get("approved_store") or {}, project_id)
    _save_keyword_store(state.get("keyword_store") or {}, project_id)
    _save_sources_store(state.get("sources_store") or {}, project_id)
    _save_project_client_context(project_id, state.get("client_context") or {})
    _sync_screen3_state_from_approved(project_id)


def _table_to_markdown(table) -> str:
    rows = []
    for row in table.rows:
        cells = [re.sub(r"\s+", " ", cell.text or "").strip() for cell in row.cells]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""

    width = max(len(r) for r in rows)
    padded = [r + [""] * (width - len(r)) for r in rows]
    lines = [
        "| " + " | ".join(padded[0]) + " |",
        "| " + " | ".join(["---"] * width) + " |",
    ]
    for row in padded[1:]:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _normalize_doc_title(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _is_toc_like(text: str, style_name: str = "") -> bool:
    low_style = (style_name or "").lower()
    low_text = (text or "").strip().lower()
    return (
        "toc" in low_style
        or low_text in {"contents", "table of contents"}
        or bool(re.search(r"\.{2,}\s*\d+\s*$", text or ""))
    )


def _possible_section_heading(text: str) -> Optional[tuple]:
    text = (text or "").strip()
    m = re.match(r"^section\s+(\d+(?:\.\d+)*)\s*[:.)-]?\s*(.*)$", text, re.IGNORECASE)
    if m:
        return "section", m.group(1), (m.group(2) or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)*)(?:\s*[:)-]\s+|\.\s+)(.+)$", text)
    if m:
        return "numbered", m.group(1), (m.group(2) or "").strip()
    m = re.match(r"^(\d+(?:\.\d+)*)\s+(.+)$", text)
    if m:
        return "numbered", m.group(1), (m.group(2) or "").strip()
    return None


_TITLE_STOPWORDS = {"the", "a", "an", "of", "and", "for", "to", "in", "on"}


def _headingish_text(title: str) -> bool:
    """True when a numbered line's remainder LOOKS like a heading title, so we
    can accept headings that use Normal/custom styles (very common in client
    docs, e.g. '3.6 Scenario Structure' or '5.10 Process: IBP Review')."""
    t = (title or "").strip()
    if not t or len(t) > 70 or t.endswith((".", ";", ",")):
        return False
    if len(t.split()) > 8:
        return False
    first = next((c for c in t if c.isalpha()), "")
    return bool(first) and first.isupper()


def _section_title_matches(section_id: str, title: str, id_to_title: Dict[str, str]) -> bool:
    expected = _normalize_doc_title(id_to_title.get(section_id, ""))
    incoming = _normalize_doc_title(title)
    if not expected or not incoming:
        return False
    if incoming == expected or incoming in expected or expected in incoming:
        return True
    # Fuzzy agreement: same title up to filler words / small variations, e.g.
    # "Current State of the Business unit" ≈ "Current State of Business".
    et = {w for w in expected.split() if w not in _TITLE_STOPWORDS}
    it = {w for w in incoming.split() if w not in _TITLE_STOPWORDS}
    if et and it and (et <= it or it <= et):
        return True
    from difflib import SequenceMatcher
    return SequenceMatcher(None, expected, incoming).ratio() >= 0.8


def _detect_import_heading(
    text: str,
    style_name: str,
    valid_ids: set,
    title_lookup: Dict[str, str],
    id_to_title: Dict[str, str],
) -> Optional[tuple]:
    if _is_toc_like(text, style_name):
        return None

    possible = _possible_section_heading(text)
    if possible:
        kind, sid, title = possible
        is_heading_style = "heading" in (style_name or "").lower()
        looks_like_heading = kind == "section" or is_heading_style or _headingish_text(title)
        title_agrees = _section_title_matches(sid, title, id_to_title)
        title_sid = title_lookup.get(_normalize_doc_title(title)) if title else None

        if sid in valid_ids:
            # Number and title agree (or bare number heading): safe match.
            if title_agrees:
                return sid, title
            if not title:
                return (sid, title) if looks_like_heading else None
            if not looks_like_heading:
                # Numbered body line like "3.7 million units" — not a heading.
                return None
            # TITLE-FIRST: the number exists in the template but the title
            # disagrees (e.g. doc "4 User Roles" vs template "4 Demand
            # Planning"). Never import into the wrong section by number.
            if title_sid and title_sid != sid:
                return title_sid, title      # title points at another section
            return ("__UNMATCHED__", sid)    # conflict → review, don't guess
        # Number not in the template.
        if looks_like_heading and title_sid:
            return title_sid, title          # renumbered doc, title matches
        if looks_like_heading:
            return ("__UNMATCHED__", sid)
        return None

    # Title-only headings. An exact (normalized) title match is a strong
    # signal even when the paragraph isn't styled as a Heading — covers docs
    # that use custom styles instead of Word's built-in Heading 1..5.
    # Bullet/list items are excluded: a list entry that happens to read like a
    # section title (e.g. "Data Sources") must not hijack the section flow.
    low_style = (style_name or "").lower()
    sid = title_lookup.get(_normalize_doc_title(text))
    if sid and "list" not in low_style and ("heading" in low_style or len(text.strip()) >= 4):
        return sid, text.strip()
    return None


def _para_image_ids(para, document) -> List[str]:
    """Content-hash ids (sha256[:12]) of inline images in a paragraph, matching
    the ids ImageExtractor assigns, so inserted <IMAGE> tags resolve on export."""
    import hashlib
    try:
        from docx.oxml.ns import qn
    except Exception:
        return []
    out = []
    try:
        for blip in para._element.findall('.//' + qn('a:blip')):
            rid = blip.get(qn('r:embed')) or blip.get(qn('r:link'))
            if not rid:
                continue
            try:
                part = document.part.related_parts.get(rid)
                blob = getattr(part, "blob", None)
                if blob:
                    out.append(hashlib.sha256(blob).hexdigest()[:12])
            except Exception:
                continue
    except Exception:
        pass
    return out


def _extract_sections_from_docx(docx_path: Path, project_id: str = None) -> Dict[str, Any]:
    try:
        from docx import Document
        from docx.oxml.table import CT_Tbl
        from docx.oxml.text.paragraph import CT_P
        from docx.table import Table
        from docx.text.paragraph import Paragraph
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"python-docx is required for import: {e}")

    live_sections = _get_live_sections(project_id)
    valid_ids = {s.get("id") for s in live_sections if s.get("id")}
    id_to_title = {
        s.get("id"): s.get("title", "")
        for s in live_sections
        if s.get("id")
    }
    title_lookup = {
        _normalize_doc_title(s.get("title", "")): s.get("id")
        for s in live_sections
        if s.get("title")
    }

    document = Document(str(docx_path))

    # Register the source document's embedded images in the shared image index
    # so the <IMAGE> tags inserted below are valid (kept by the sanitizer) and
    # resolve to real pictures on export — preserving images from the upload.
    try:
        try:
            from app.image_extractor import extract_and_index_single_file as _eidx
        except ImportError:
            from image_extractor import extract_and_index_single_file as _eidx
        _appdir = Path(__file__).parent
        _eidx(file_path=str(docx_path),
              output_dir=str(_appdir / "extracted_images"),
              index_file=str(_appdir / "image_chunks.json"))
        _image_warning = ""
    except Exception as _e:
        logger.warning("import: source-image extract/index failed: %s", _e)
        _image_warning = (
            "Embedded images could not be extracted from the uploaded document — "
            "pictures from it may be missing when you export."
        )

    imported: Dict[str, List[str]] = {}
    unmatched = []
    ignored_blocks = 0
    current_sid = None
    doc_id_map: Dict[str, str] = {}  # doc heading number → matched template id
    doc_titles: Dict[str, str] = {}  # matched template id → title as written in the doc

    def flush_line(line: str):
        nonlocal ignored_blocks
        clean = (line or "").strip()
        if not clean:
            return
        if current_sid:
            imported.setdefault(current_sid, []).append(clean)
        else:
            ignored_blocks += 1

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            para = Paragraph(child, document)
            text = re.sub(r"\s+", " ", para.text or "").strip()
            img_ids = _para_image_ids(para, document)
            if not text and not img_ids:
                continue
            if text:
                style_name = getattr(getattr(para, "style", None), "name", "") or ""
                heading = _detect_import_heading(text, style_name, valid_ids, title_lookup, id_to_title)
                if heading:
                    if heading[0] == "__UNMATCHED__":
                        # Keep the content under unmatched headings (key __U__<n>)
                        # so it can be manually mapped or — with
                        # template_source="document" — become a new section.
                        key = "__U__%d" % (len(unmatched) + 1)
                        unmatched.append({"id": heading[1], "key": key, "text": text})
                        current_sid = key
                        imported.setdefault(current_sid, [])
                    else:
                        current_sid = heading[0]
                        imported.setdefault(current_sid, [])
                        _ph = _possible_section_heading(text)
                        if _ph and _ph[1]:
                            doc_id_map[_ph[1]] = current_sid
                        _dt = (heading[1] or "").strip() if len(heading) > 1 else ""
                        if _dt:
                            doc_titles.setdefault(current_sid, _dt)
                    continue
                if not _is_toc_like(text, style_name):
                    flush_line(text)
            # Preserve inline images from the source doc as resolvable tags.
            for _iid in img_ids:
                flush_line('<IMAGE id="%s"/>' % _iid)
        elif isinstance(child, CT_Tbl):
            table_text = _table_to_markdown(Table(child, document))
            flush_line(table_text)

    sections = {
        sid: "\n\n".join(part for part in parts if part).strip()
        for sid, parts in imported.items()
    }
    sections = {sid: content for sid, content in sections.items() if content}
    # Split off content captured under unmatched headings (__U__ keys).
    unmatched_sections = {k: v for k, v in sections.items() if k.startswith("__U__")}
    sections = {k: v for k, v in sections.items() if not k.startswith("__U__")}
    for u in unmatched:
        u["chars"] = len(unmatched_sections.get(u.get("key", ""), ""))
    return {
        "sections": sections,
        "matched_ids": sorted(sections, key=_section_sort_key),
        "unmatched_headings": unmatched[:50],
        "unmatched_sections": unmatched_sections,
        "doc_id_map": doc_id_map,
        "doc_titles": doc_titles,
        "ignored_blocks": ignored_blocks,
        "image_warning": _image_warning,
    }


def _apply_imported_sections(project_id: str, imported_sections: Dict[str, str], mode: str = "merge") -> Dict[str, Any]:
    store = _load_approved_store(project_id)
    if mode == "replace":
        store["sections"] = {}
        store["heading_only_ids"] = []
        store["no_heading_ids"] = []
    store.setdefault("sections", {})

    heading_only_ids = set(store.get("heading_only_ids", []))
    no_heading_ids = set(store.get("no_heading_ids", []))
    updated_ids = []

    kw_store = _load_keyword_store(project_id)
    imported_at = datetime.now().isoformat(timespec="seconds")
    _valid_ids = {x.get("id") for x in _get_live_sections(project_id) if x.get("id")}
    for sid, content in imported_sections.items():
        clean = (content or "").strip()
        if not clean:
            continue
        clean = _strip_html_content(clean)
        clean = _strip_redundant_section_headers(clean, sid)
        # Also strip invented/duplicate section headings the imported doc may carry
        # from a prior generation (e.g. "Section 1:", "1.1 Background", a repeated
        # "1.1 Company Information"). The template/merge supplies the real heading.
        clean = _strip_invented_headings(clean, sid, _valid_ids)
        if not clean:
            continue
        store["sections"][sid] = clean
        heading_only_ids.discard(sid)
        no_heading_ids.discard(sid)
        # Re-importing a section clears any prior "removed-from-import" mark.
        if "removed_imports" in store and sid in store.get("removed_imports", []):
            store["removed_imports"] = [r for r in store["removed_imports"] if r != sid]

        entry = kw_store.get(sid, {})
        entry["status"] = "generated"
        entry["output"] = clean
        entry["imported_at"] = imported_at
        kw_store[sid] = entry
        updated_ids.append(sid)

    store["heading_only_ids"] = sorted(heading_only_ids, key=_section_sort_key)
    store["no_heading_ids"] = sorted(no_heading_ids, key=_section_sort_key)
    _save_approved_store(store, project_id)
    _save_keyword_store(kw_store, project_id)
    _sync_screen3_state_from_approved(project_id)

    return {
        "updated_ids": sorted(updated_ids, key=_section_sort_key),
        "updated_count": len(updated_ids),
    }


def _pending_import_dir(project_id: str) -> Path:
    return DOCUMENT_IMPORT_ROOT / _safe_project_key(project_id) / "pending"


def _safe_import_token(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "", token or "")[:80]


def _find_pending_import(project_id: str, token: str) -> Optional[Path]:
    safe_token = _safe_import_token(token)
    if not safe_token:
        return None
    pending_dir = _pending_import_dir(project_id)
    matches = sorted(pending_dir.glob(f"{safe_token}_*.docx"))
    if not matches:
        return None
    return matches[-1]


def _fuzzy_heading_suggestions(
    unmatched: List[Dict[str, Any]],
    live_sections: List[Dict[str, Any]],
    threshold: float = 0.45,
) -> List[Dict[str, Any]]:
    """For each unmatched heading return the top-3 closest template sections."""
    from difflib import SequenceMatcher

    def _score(a: str, b: str) -> float:
        a, b = a.lower().strip(), b.lower().strip()
        return SequenceMatcher(None, a, b).ratio()

    candidates = [(s.get("id", ""), s.get("title", "")) for s in live_sections if s.get("id")]
    result = []
    for h in unmatched:
        text = h.get("text", "")
        scored = sorted(
            [{"id": sid, "title": t, "score": round(_score(text, t), 2)} for sid, t in candidates],
            key=lambda x: x["score"],
            reverse=True,
        )
        suggestions = [s for s in scored if s["score"] >= threshold][:3]
        result.append({**h, "suggestions": suggestions})
    return result


def _extract_document_structure(docx_path: Path) -> List[Dict[str, Any]]:
    """template_source="document": read the uploaded DOCX as the single source
    of truth. Returns the document's own outline, in document order, with the
    document's own numbering:  [{id, title, content}, ...].

    Section boundaries are NUMBERED headings ("4 User roles", "5.10 Process:
    IBP Review" — styled or heading-ish plain paragraphs). Unnumbered heading
    lines inside a section (e.g. "Resources", "User Stories") stay in that
    section's content as bold lines instead of becoming sections."""
    from docx import Document
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = Document(str(docx_path))

    # Register embedded images (same as the matching-based import path).
    try:
        try:
            from app.image_extractor import extract_and_index_single_file as _eidx
        except ImportError:
            from image_extractor import extract_and_index_single_file as _eidx
        _appdir = Path(__file__).parent
        _eidx(file_path=str(docx_path),
              output_dir=str(_appdir / "extracted_images"),
              index_file=str(_appdir / "image_chunks.json"))
    except Exception as _e:
        logger.warning("doc-structure import: image extract failed: %s", _e)

    outline: List[Dict[str, Any]] = []
    seen_ids: set = set()
    current: Optional[Dict[str, Any]] = None

    def _flush(line: str):
        if current is not None and (line or "").strip():
            current["content"].append(line.strip())

    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            para = Paragraph(child, document)
            text = re.sub(r"\s+", " ", para.text or "").strip()
            img_ids = _para_image_ids(para, document)
            if not text and not img_ids:
                continue
            if text:
                style_name = getattr(getattr(para, "style", None), "name", "") or ""
                if _is_toc_like(text, style_name):
                    continue
                ph = _possible_section_heading(text)
                is_heading_style = "heading" in style_name.lower()
                if ph and ph[1] and (ph[0] == "section" or is_heading_style or _headingish_text(ph[2])):
                    sid, title = ph[1], (ph[2] or "").strip() or text
                    if sid not in seen_ids:
                        seen_ids.add(sid)
                        current = {"id": sid, "title": title, "content": []}
                        outline.append(current)
                    else:
                        current = next(o for o in outline if o["id"] == sid)
                    continue
                if is_heading_style:
                    _flush("**" + text + "**")   # unnumbered sub-heading → bold line
                else:
                    _flush(text)
            for _iid in img_ids:
                _flush('<IMAGE id="%s"/>' % _iid)
        elif isinstance(child, CT_Tbl):
            _flush(_table_to_markdown(Table(child, document)))

    for o in outline:
        o["content"] = "\n\n".join(o["content"]).strip()
    return outline


def _create_sections_from_import(
    project_id: str,
    unmatched_meta: List[Dict[str, Any]],
    unmatched_sections: Dict[str, str],
    imported_sections: Dict[str, str],
    doc_id_map: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """template_source="document": turn unmatched doc headings into new template
    sections (inserted in numeric order) and queue their content for import.

    doc_id_map (doc number → template id) keeps FAMILIES together: if the doc's
    "4 User Roles" becomes new section 8, its child "4.1 …" is created as 8.1 —
    not scattered under the template's unrelated section 4.
    Returns the created [{id, title}] list."""
    active = _get_live_sections(project_id)
    archived = _get_archived_sections(project_id)
    existing = {s["id"] for s in active} | {s["id"] for s in archived}
    meta_by_key = {u.get("key"): u for u in unmatched_meta if u.get("key")}
    remap: Dict[str, str] = dict(doc_id_map or {})
    created: List[Dict[str, Any]] = []

    def _free_id(doc_sid: str) -> str:
        if doc_sid and doc_sid not in existing:
            return doc_sid
        parts = doc_sid.split(".") if doc_sid else ["0"]
        parent = ".".join(parts[:-1])
        try:
            n = int(parts[-1]) + 1
        except ValueError:
            n = 1
        while True:
            cand = (parent + "." if parent else "") + str(n)
            if cand not in existing:
                return cand
            n += 1

    def _insert_pos(sid: str) -> int:
        key = _section_sort_key(sid)
        for i, s in enumerate(active):
            if _section_sort_key(s.get("id", "")) > key:
                return i
        return len(active)

    def _ukey_order(k: str) -> int:
        try:
            return int(k[5:])  # "__U__<n>" → document order
        except ValueError:
            return 0

    for ukey in sorted(unmatched_sections.keys(), key=_ukey_order):
        content = (unmatched_sections.get(ukey) or "").strip()
        if not content:
            continue
        meta = meta_by_key.get(ukey) or {}
        doc_sid = str(meta.get("id") or "").strip()
        text = meta.get("text") or ""
        parsed_h = _possible_section_heading(text)
        title = (parsed_h[2] if parsed_h and parsed_h[2] else text).strip() or ("Imported section " + doc_sid)
        # Follow a remapped parent: doc "4.1" whose parent "4" became "8" → "8.1".
        base_sid = doc_sid
        parts = doc_sid.split(".") if doc_sid else []
        if len(parts) > 1:
            mapped_parent = remap.get(".".join(parts[:-1]))
            if mapped_parent:
                base_sid = mapped_parent + "." + parts[-1]
        new_id = _free_id(base_sid)
        if doc_sid:
            remap[doc_sid] = new_id
        active.insert(_insert_pos(new_id), {"id": new_id, "title": title})
        existing.add(new_id)
        imported_sections[new_id] = content
        created.append({"id": new_id, "title": title, "from": text})

    if created:
        _persist_sections(active, archived, project_id)
        kw_store = _load_keyword_store(project_id)
        for c in created:
            if c["id"] not in kw_store:
                kw_store[c["id"]] = {"doc_keywords": [], "web_keywords": [], "user_edited": True}
        _save_keyword_store(kw_store, project_id)
        logger.info("import(template_source=document): created %d section(s): %s",
                    len(created), ", ".join(c["id"] for c in created))
    return created


def _import_preview_payload(project_id: str, parsed: Dict[str, Any], token: str, filename: str) -> Dict[str, Any]:
    live_sections = _get_live_sections(project_id)
    title_map = {s.get("id"): s.get("title", "") for s in live_sections if s.get("id")}
    current_store = _load_approved_store(project_id)
    current_sections = current_store.get("sections") or {}

    sections = parsed.get("sections") or {}
    matched = []
    for sid in sorted(sections, key=_section_sort_key):
        content = sections.get(sid, "")
        current = current_sections.get(sid, "")
        matched.append({
            "id": sid,
            "title": title_map.get(sid, ""),
            "chars": len(content or ""),
            "preview": _content_preview(content, limit=700),
            "has_current": bool(current),
            "current_preview": _content_preview(current, limit=700) if current else "",
        })

    unmatched_raw = parsed.get("unmatched_headings", [])
    unmatched_with_suggestions = _fuzzy_heading_suggestions(unmatched_raw, live_sections)

    return {
        "ok": True,
        "import_token": token,
        "filename": filename,
        "matched_count": len(matched),
        "matched_sections": matched,
        "unmatched_headings": unmatched_with_suggestions,
        "ignored_blocks": parsed.get("ignored_blocks", 0),
        "image_warning": parsed.get("image_warning", ""),
    }


# ─────────────────────────────────────────────────────────────────────────────
# LLM-BASED CHUNK → SECTION MATCHING
# Replaces naive word-overlap scoring with semantic understanding.
# Called once per project on Screen 3 load, cached for reuse by
# fetch_sources() and auto_keywords().
# ─────────────────────────────────────────────────────────────────────────────
import os as _os
import hashlib as _hashlib

# In-memory cache: { project_id: { "hash": <chunks_hash>, "mapping": { section_id: [chunk_ids] }, "best_keywords": { section_id: [keyword_str] } } }
_llm_match_cache: Dict[str, Dict] = {}


def _salvage_json_array_objects(raw: str) -> list:
    """Recover complete JSON objects from a (possibly truncated) JSON array.

    Scans the text for top-level {...} objects and json.loads each one,
    skipping a trailing object cut off by max_tokens. Returns the list of
    successfully parsed objects (empty if none)."""
    import json as __json
    out = []
    if not raw:
        return out
    depth = 0
    start = -1
    in_str = False
    esc = False
    for idx, ch in enumerate(raw):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = idx
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start != -1:
                    try:
                        out.append(__json.loads(raw[start:idx + 1]))
                    except Exception:
                        pass
                    start = -1
    return out


def _salvage_partial_json_object(raw: str) -> dict:
    """Best-effort recovery from a truncated `{...}` payload.

    The LLM occasionally hits max_tokens mid-value, leaving us with a JSON
    object whose final key/value is incomplete. We walk forward over the
    bytes, track string/escape and bracket state, and remember the index
    after the last fully-closed top-level entry. Truncating there and
    appending '}' yields valid JSON containing every entry the model managed
    to finish.
    """
    if not raw or not raw.lstrip().startswith("{"):
        return {}
    start = raw.index("{")
    depth = 0
    in_str = False
    escape = False
    last_safe = -1  # index right after the last completed top-level entry
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 1 and ch == "]":
                # closed a top-level value array — safe to truncate after it
                last_safe = i + 1
        elif ch == "," and depth == 1:
            last_safe = i  # safe boundary just before the next entry
    if last_safe <= start:
        return {}
    candidate = raw[start:last_safe].rstrip().rstrip(",") + "}"
    try:
        return json.loads(candidate)
    except Exception:
        return {}


def _chunks_fingerprint(proj_chunks: list) -> str:
    """Quick hash of chunk IDs + keywords to detect when chunks change."""
    sig = "|".join(
        f"{c.get('chunk_id','')}:{','.join(c.get('keywords',[]))}"
        for c in proj_chunks[:200]  # cap to avoid hashing huge lists
    )
    return _hashlib.md5(sig.encode()).hexdigest()


_LEX_NOISE = {
    "plan", "planning", "supply", "chain", "demand", "data", "process", "flow",
    "image", "text", "table", "title", "figure", "chart", "document", "file",
    "page", "section", "item", "part", "type", "value", "values", "level",
    "overview", "details", "information", "general", "report", "analysis",
    "scenario", "scenarios", "metric", "metrics", "review", "results", "result",
}


def _lex_kw_useful(kw: str) -> bool:
    kw_l = kw.lower()
    if len(kw) <= 4:
        return False
    if " " not in kw and kw_l in _LEX_NOISE:
        return False
    return True


def _lexical_rank_chunks(proj_chunks: list, sections: List[Dict], project_id: str) -> tuple:
    """Fast, fully-local replacement for the LLM topic-selection matcher.

    For each section, rank chunks by TF-IDF lexical overlap with the section
    title (and its parent title), then derive section keywords from the top
    chunks. No LLM calls — turns a ~4.5 min, 16k-token call into ~0.3 s.
    Returns (mapping, best_keywords, was_fresh) — identical shape to the LLM matcher.
    """
    import math
    global _llm_match_cache
    if not proj_chunks or not sections:
        return {}, {}, False

    stop = {"the", "and", "for", "with", "from", "that", "this", "are", "was",
            "will", "have", "been", "their", "into", "per", "via"}

    def _toks(s):
        return [w for w in re.findall(r"[a-zA-Z0-9]+", (s or "").lower())
                if len(w) > 2 and w not in stop]

    n = len(proj_chunks)
    docs = []          # (cid, heading_l, kws_l, text_l, token_set)
    df = {}
    chunk_by_id = {}
    for c in proj_chunks:
        cid = c.get("chunk_id")
        chunk_by_id[cid] = c
        text = (c.get("chunk_text", "") or "").lower()
        heading = (c.get("section_heading", "") or "").lower()
        kws = " ".join(c.get("keywords", []) or []).lower()
        tset = set(_toks(heading + " " + kws + " " + text))
        docs.append((cid, heading, kws, text, tset))
        for t in tset:
            df[t] = df.get(t, 0) + 1

    def _idf(t):
        return math.log(1.0 + n / (1.0 + df.get(t, 0)))

    title_by_id = {s["id"]: s.get("title", "") for s in sections}

    def _parent_title(sid):
        return title_by_id.get(sid.rsplit(".", 1)[0], "") if "." in sid else ""

    doc_hits = max(1, _safe_int(os.getenv("BDG_DOC_HITS", "6"), 6))
    mapping: Dict[str, List[int]] = {}
    best_keywords: Dict[str, List[str]] = {}

    for s in sections:
        sid = s["id"]
        qterms = set(_toks(s.get("title", ""))) | set(_toks(_parent_title(sid)))
        if not qterms:
            mapping[sid] = []
            best_keywords[sid] = []
            continue
        scored = []
        for cid, heading, kws, text, tset in docs:
            sc = 0.0
            for t in qterms:
                if t in tset:
                    tf = text.count(t) + 2 * kws.count(t) + 3 * heading.count(t)
                    if tf:
                        sc += (1.0 + math.log(tf)) * _idf(t)
            if sc > 0:
                sc /= (1.0 + len(text) / 4000.0)
                scored.append((sc, cid))
        scored.sort(key=lambda x: -x[0])
        top = [cid for _, cid in scored[:doc_hits]]
        mapping[sid] = top
        seen = set()
        pool = []
        for cid in top:
            for kw in (chunk_by_id.get(cid, {}) or {}).get("keywords", []) or []:
                kw_s = kw.strip()
                if kw_s and _lex_kw_useful(kw_s) and kw_s.lower() not in seen:
                    pool.append(kw_s)
                    seen.add(kw_s.lower())
        best_keywords[sid] = pool[:12]

    try:
        _llm_match_cache[project_id] = {
            "hash": _chunks_fingerprint(proj_chunks),
            "mapping": mapping, "best_keywords": best_keywords,
        }
    except Exception:
        pass
    logger.info("lexical matcher: ranked %d sections over %d chunks (local, no LLM)",
                len(mapping), n)
    return mapping, best_keywords, True


def _safe_int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _llm_match_chunks_to_sections(
    proj_chunks: list,
    sections: List[Dict],
    project_id: str,
) -> tuple:
    """
    Three-step semantic matching. Returns (mapping, best_keywords, was_fresh).

    Step 1 (LLM): Given the section list and a list of document topics,
    ask the LLM to pick the most relevant TOPICS for each section.
    Output: { section_id: [topic_string, ...] }

    Step 2 (Local): For each section, take the LLM-selected topics, look up
    which chunks contain those topics via the topic->chunk_id index, score
    chunks by how many selected topics they match, return top N.
    Output: mapping = { section_id: [chunk_id, ...] }

    Step 3 (LLM): For each section, collect all real keywords from its
    matched chunks and ask the LLM to pick the 10-12 most section-relevant
    ones. These are actual chunk keywords BM25 can resolve back to
    chunk_id -> doc_name + page_number -> correct source metadata.
    Output: best_keywords = { section_id: [keyword_str, ...] }

    The LLM never sees chunk IDs - it reasons about semantic concepts.
    We resolve to chunks locally with full control over ranking.
    """
    global _llm_match_cache

    if not proj_chunks or not sections:
        return {}, {}, False

    # Default to the fast local lexical ranker (no LLM). The original two-call
    # LLM matcher is preserved behind BDG_USE_LLM_MATCH=1 for comparison.
    if os.getenv("BDG_USE_LLM_MATCH", "0") != "1":
        return _lexical_rank_chunks(proj_chunks, sections, project_id)

    # ── Check cache ──────────────────────────────────────────────────────────
    fp = _chunks_fingerprint(proj_chunks)
    cached = _llm_match_cache.get(project_id)
    if cached and cached.get("hash") == fp:
        logger.info("LLM chunk matcher: cache hit for project %s", project_id)
        return cached["mapping"], cached.get("best_keywords", {}), False

    # ── Step 1A: Build topic inventory ────────────────────────────────────────
    # topic_key (lowercased) → { display, chunk_ids, docs }
    topic_map: Dict[str, Dict] = {}

    for c in proj_chunks:
        cid = c.get("chunk_id", 0)
        doc = c.get("doc_name", "")

        for kw in c.get("keywords", []):
            kw_stripped = kw.strip()
            if not kw_stripped or len(kw_stripped) < 3:
                continue
            key = kw_stripped.lower()
            if key not in topic_map:
                topic_map[key] = {"display": kw_stripped, "chunk_ids": [], "docs": set()}
            topic_map[key]["chunk_ids"].append(cid)
            topic_map[key]["docs"].add(doc)

        heading = c.get("section_heading", "").strip()
        if heading and len(heading) > 3:
            key = heading.lower()
            if key not in topic_map:
                topic_map[key] = {"display": heading, "chunk_ids": [], "docs": set()}
            topic_map[key]["chunk_ids"].append(cid)
            topic_map[key]["docs"].add(doc)

    # Dedupe chunk_ids within each topic
    for t in topic_map.values():
        t["chunk_ids"] = sorted(set(t["chunk_ids"]))

    all_topic_names = sorted(set(t["display"] for t in topic_map.values()))

    logger.info(
        "Topic inventory: %d unique topics from %d chunks (%d documents)",
        len(all_topic_names), len(proj_chunks),
        len(set(c.get("doc_name", "") for c in proj_chunks))
    )

    # ── Step 1B: Ask LLM to select topics per section ────────────────────────
    section_block = "\n".join(f"{s['id']} — {s['title']}" for s in sections)
    topic_list_str = "\n".join(f"- {t}" for t in all_topic_names)

    prompt = f"""You are a supply chain domain expert working on a Blueprint Document.

BLUEPRINT DOCUMENT SECTIONS:
{section_block}

AVAILABLE TOPICS (extracted from the client's uploaded documents):
{topic_list_str}

TASK:
For each Blueprint Document section, select 8-15 topics from the list above that are most relevant to that section's subject matter.

RULES:
1. SUBSECTIONS OVER PARENTS: Pick topics specific to each subsection. "Transit Time Mapping" and "Lead Time Consideration" should go to "3.4 Supply Foundation", not the parent "3 Supply Chain Scope".
2. BE SPECIFIC: Only pick topics that are directly relevant. Do NOT assign generic topics like "Supply Chain Management" or "Consulting-Led Approach" to every section.
3. CLIENT OVER VENDOR: Prefer topics about the client's actual processes, data, and systems (e.g. "JD Edwards Field", "Production Data Sync", "Forecast Disaggregation") over vendor capability topics (e.g. "Consulting and Implementation", "Health Checks and Buddy Planner").
4. DIFFERENTIATE SUBSECTIONS: Each subsection should have DIFFERENT topics from its siblings. "3.4 Supply Foundation" and "3.5 Inventory Management Foundation" should NOT have the same topics.
5. SPREAD COVERAGE: Use topics from across ALL documents, not just the first few.

OUTPUT: Return ONLY valid JSON mapping section IDs to arrays of topic strings:
{{
  "1": ["topic1", "topic2", ...],
  "1.1": ["topic3", "topic4", ...],
  ...
}}

Include every section ID. Use the exact topic strings from the list above."""

    try:
        try:
            from app.claude_provider import completion_from_prompt as _cp_topics
        except ImportError:
            from claude_provider import completion_from_prompt as _cp_topics
        raw = _cp_topics(prompt, max_tokens=16000, temperature=0.0).strip()

        # Strip markdown fences
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

        try:
            section_topics = json.loads(raw)  # { section_id: [topic_str, ...] }
        except json.JSONDecodeError as _jerr:
            # Recover from truncated output (e.g. model hit max_tokens mid-string).
            # Drop the partial trailing entry and close the JSON object.
            logger.warning(
                "LLM topic JSON truncated (%s) — attempting partial recovery from %d chars",
                _jerr, len(raw),
            )
            section_topics = _salvage_partial_json_object(raw)
            if not section_topics:
                raise

        logger.info(
            "LLM returned topics for %d sections (sample: %s)",
            len(section_topics),
            {k: v[:3] for k, v in list(section_topics.items())[:3]}
        )

    except Exception as e:
        logger.error("LLM topic selection failed: %s", e, exc_info=True)
        return {}, {}, False

    # ── Step 2: Resolve topics → chunk IDs locally ───────────────────────────
    # Build reverse index: topic_key (lowercased) → set of chunk_ids
    topic_to_chunks: Dict[str, set] = {}
    for key, t in topic_map.items():
        topic_to_chunks[key] = set(t["chunk_ids"])
        # Also index by display name lowercased (handles case variations)
        topic_to_chunks[t["display"].lower()] = set(t["chunk_ids"])

    def _resolve_topic(topic_str: str) -> set:
        """Find chunk IDs for a topic string. Tries exact match first,
        then substring containment as fallback for slight LLM rewording."""
        key = topic_str.strip().lower()
        if key in topic_to_chunks:
            return topic_to_chunks[key]
        for tkey, cids in topic_to_chunks.items():
            if key in tkey or tkey in key:
                return cids
        return set()

    clean_mapping: Dict[str, List[int]] = {}

    for sid, topics in section_topics.items():
        if not isinstance(topics, list):
            clean_mapping[sid] = []
            continue

        # Score each chunk: how many of the LLM-selected topics does it contain?
        chunk_scores: Dict[int, int] = {}
        matched_topic_count = 0
        for topic_str in topics:
            chunk_ids = _resolve_topic(topic_str)
            if chunk_ids:
                matched_topic_count += 1
            for cid in chunk_ids:
                chunk_scores[cid] = chunk_scores.get(cid, 0) + 1

        # Sort by score descending, take top 10
        ranked = sorted(chunk_scores.items(), key=lambda x: -x[1])
        clean_mapping[sid] = [cid for cid, _score in ranked[:10]]

        if topics and matched_topic_count < len(topics) // 2:
            unresolved = [t for t in topics if not _resolve_topic(t)]
            logger.warning(
                "Section %s: only %d/%d LLM topics resolved to chunks. Unresolved: %s",
                sid, matched_topic_count, len(topics), unresolved[:5]
            )

    # ── Step 3: LLM picks best ACTUAL keywords per section ────────────────
    # For each section, collect all real keywords from its matched chunks,
    # then ask the LLM to pick the most section-relevant ones.
    # These are real chunk keywords so BM25 can resolve them back to
    # chunk_id → doc_name + page_number → correct source metadata.
    chunk_by_id = {c.get("chunk_id"): c for c in proj_chunks}
    section_title_map = {s["id"]: s.get("title", s["id"]) for s in sections}

    # Words that are useless as standalone keywords — single-word noise that
    # adds no retrieval signal but pollutes the semantic query string.
    _NOISE_SINGLES = {
        "plan", "planning", "supply", "chain", "demand", "data", "process",
        "flow", "image", "text", "table", "title", "figure", "chart",
        "workbook", "worksheet", "sheet", "document", "file", "page",
        "section", "item", "part", "type", "value", "values", "level",
        "date", "time", "name", "code", "unit", "units", "order", "orders",
        "note", "notes", "summary", "overview", "details", "information",
        "embedded", "embedded image", "labeled", "modified", "resource",
        "there", "following", "general", "used", "using", "ability", "assign",
        "define", "update", "analysis", "description", "report", "output",
        "input", "inputs", "outputs", "implementation", "responsibility",
        "scenario", "scenarios", "metric", "metrics", "tracking", "months",
        "criteria", "results", "result", "review",
    }

    def _is_useful_keyword(kw: str) -> bool:
        """Return False for single-word generic terms and OCR artifacts."""
        kw_l = kw.lower()
        # Must be longer than 4 chars
        if len(kw) <= 4:
            return False
        # Single word that's in the noise set → drop
        if " " not in kw and kw_l in _NOISE_SINGLES:
            return False
        # Pure OCR artifacts — all caps or mixed-case with only one word
        if kw_l in ("image", "text", "table", "title", "figure"):
            return False
        return True

    # Build candidate keyword pools per section
    section_kw_pools: Dict[str, List[str]] = {}
    for sid, chunk_ids in clean_mapping.items():
        seen = set()
        pool = []
        for cid in chunk_ids:
            chunk = chunk_by_id.get(cid)
            if not chunk:
                continue
            for kw in chunk.get("keywords", []):
                kw_s = kw.strip()
                if kw_s and _is_useful_keyword(kw_s) and kw_s not in seen:
                    pool.append(kw_s)
                    seen.add(kw_s)
        section_kw_pools[sid] = pool

    # Ask LLM to rank keywords for all sections in one call
    # (reuses the same `client` instance created in Step 1)
    section_best_keywords: Dict[str, List[str]] = {}
    try:
        try:
            from app.claude_provider import completion_from_prompt as _cp_kw
        except ImportError:
            from claude_provider import completion_from_prompt as _cp_kw
        kw_rank_sections = []
        for sid, pool in section_kw_pools.items():
            if not pool:
                continue
            title = section_title_map.get(sid, sid)
            kw_rank_sections.append({
                "id": sid,
                "title": title,
                "candidates": pool,
            })

        if kw_rank_sections:
            kw_rank_block = "\n\n".join(
                f"SECTION {s['id']} — {s['title']}\nCandidate keywords:\n"
                + "\n".join(f"- {kw}" for kw in s["candidates"])
                for s in kw_rank_sections
            )

            kw_rank_prompt = f"""You are a supply chain domain expert. For each Blueprint Document section below, you are given a list of candidate keywords extracted from matched document chunks.

YOUR TASK: For each section, select the 8-12 keywords that are MOST RELEVANT to that section's subject matter. These keywords are used to build a semantic search query, so pick multi-word phrases that best describe the actual content.

RULES:
1. ONLY select keywords from the provided candidate list — do NOT invent new ones.
2. MULTI-WORD PHRASES ONLY: Never select single generic words like "Plan", "Supply", "Data", "Flow", "Workbook", "Process", "Image", "Text", "Table", "Tracking", "Analysis", "Overview". Only select them if they are part of a longer multi-word phrase.
3. DROP generic single words: "Plan", "Supply", "Chain", "Demand", "Data", "Process", "Flow", "Image", "Text", "Table", "Workbook", "Sheet", "Document", "Item", "Order", "Level", "Value", "Type", "Metric", "Result", "Update", "Modified", "Resource", "Following", "Implementation", "Ability", "There", "General", "Used".
4. PREFER client-specific multi-word phrases: workbook names, process names, system names, site names, part names, planning terms with context (e.g. "Forecast Consumption Intervals", "Safety Stock Policies", "ARC S&OP Annual Plan").
5. PREFER specific proper nouns and named entities over generic descriptions.

{kw_rank_block}

OUTPUT: Return ONLY valid JSON mapping section IDs to arrays of selected keyword strings:
{{
  "1": ["keyword1", "keyword2", ...],
  "1.1": ["keyword3", "keyword4", ...],
  ...
}}

Use the EXACT keyword strings from the candidate lists."""

            raw_kw = _cp_kw(kw_rank_prompt, max_tokens=16000, temperature=0.0).strip()

            # Strip markdown fences
            if raw_kw.startswith("```"):
                raw_kw = raw_kw.split("\n", 1)[1] if "\n" in raw_kw else raw_kw[3:]
            if raw_kw.endswith("```"):
                raw_kw = raw_kw[:-3]
            raw_kw = raw_kw.strip()

            llm_ranked = json.loads(raw_kw)

            # Validate: only keep keywords that actually exist in the candidate pool
            for sid, ranked_kws in llm_ranked.items():
                if not isinstance(ranked_kws, list):
                    continue
                pool_set = set(kw.lower() for kw in section_kw_pools.get(sid, []))
                validated = [
                    kw for kw in ranked_kws
                    if kw.strip().lower() in pool_set
                ]
                section_best_keywords[sid] = validated[:12]

            logger.info(
                "Step 3: LLM ranked keywords for %d sections (sample: %s)",
                len(section_best_keywords),
                {k: v[:3] for k, v in list(section_best_keywords.items())[:3]}
            )

    except Exception as e:
        logger.warning("Step 3 keyword ranking failed: %s — falling back to naive ordering", e)
        # Fallback: just use the pools as-is (first 12, same as before)

    # For sections where LLM ranking didn't produce results, fall back to pool order
    for sid, pool in section_kw_pools.items():
        if sid not in section_best_keywords or not section_best_keywords[sid]:
            section_best_keywords[sid] = pool[:12]

    # Post-filter: strip any single-word noise the LLM still kept
    for sid in list(section_best_keywords.keys()):
        cleaned = [kw for kw in section_best_keywords[sid] if _is_useful_keyword(kw)]
        # If filtering left fewer than 4 keywords, keep originals to avoid empty sets
        section_best_keywords[sid] = cleaned if len(cleaned) >= 4 else section_best_keywords[sid]

    # ── Cache ────────────────────────────────────────────────────────────────
    _llm_match_cache[project_id] = {
        "hash": fp,
        "mapping": clean_mapping,
        "best_keywords": section_best_keywords,
    }

    # ── DEV DEBUG: save mapping + LLM topics to disk ─────────────────────────
    try:
        from datetime import datetime as _dt
        _debug_dir = _THIS_DIR / "debug_llm_mapping"
        _debug_dir.mkdir(parents=True, exist_ok=True)

        _section_title_map = {s["id"]: s.get("title", s["id"]) for s in sections}
        _chunk_by_id = {c.get("chunk_id"): c for c in proj_chunks}

        _readable = {
            "_meta": {
                "project_id": project_id,
                "timestamp": _dt.utcnow().isoformat(),
                "total_chunks": len(proj_chunks),
                "total_topics": len(all_topic_names),
                "total_sections": len(sections),
                "sections_with_matches": sum(1 for v in clean_mapping.values() if v),
                "sections_without_matches": sum(1 for v in clean_mapping.values() if not v),
                "unique_chunk_ids_used": len(set(
                    cid for ids in clean_mapping.values() for cid in ids
                )),
            },
            "mapping": {}
        }

        for sid in sorted(clean_mapping.keys(), key=lambda x: [int(p) if p.isdigit() else p for p in x.split(".")]):
            chunk_ids = clean_mapping[sid]
            section_title = _section_title_map.get(sid, sid)
            llm_topics = section_topics.get(sid, [])
            resolved_chunks = []
            for cid in chunk_ids:
                chunk = _chunk_by_id.get(cid)
                if chunk:
                    resolved_chunks.append({
                        "chunk_id": cid,
                        "doc_name": chunk.get("doc_name", ""),
                        "section_heading": chunk.get("section_heading", ""),
                        "keywords": chunk.get("keywords", []),
                        "snippet": chunk.get("chunk_text", "")[:200],
                    })

            _readable["mapping"][sid] = {
                "section_title": section_title,
                "llm_selected_topics": llm_topics,
                "topics_resolved": sum(1 for t in llm_topics if _resolve_topic(t)),
                "topics_unresolved": [t for t in llm_topics if not _resolve_topic(t)],
                "best_keywords": section_best_keywords.get(sid, []),
                "matched_chunk_count": len(resolved_chunks),
                "matched_chunk_ids": chunk_ids,
                "chunks": resolved_chunks,
            }

        _ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        _debug_path = _debug_dir / f"llm_mapping_{project_id}_{_ts}.json"
        _debug_path.write_text(
            json.dumps(_readable, indent=2, ensure_ascii=False),
            encoding="utf-8"
        )
        logger.info("DEV DEBUG: LLM mapping saved to %s", _debug_path)
    except Exception as _dbg_err:
        logger.warning("DEV DEBUG: could not save mapping file: %s", _dbg_err)
    # ── END DEV DEBUG ────────────────────────────────────────────────────

    logger.info(
        "LLM chunk matcher: mapped %d sections for project %s (%d chunks → %d topics → %d unique chunks used)",
        len(clean_mapping), project_id, len(proj_chunks), len(all_topic_names),
        len(set(cid for ids in clean_mapping.values() for cid in ids))
    )
    return clean_mapping, section_best_keywords, True


def _get_chunks_for_section(
    section_id: str,
    proj_chunks: list,
    sections: List[Dict],
    project_id: str,
    top_k: int = 8,
) -> list:
    """
    Get the top-K most relevant chunks for a Blueprint Document section.
    Uses the three-step LLM matcher (cached). Returns list of chunk dicts.
    """
    mapping, _best_kw, _was_fresh = _llm_match_chunks_to_sections(proj_chunks, sections, project_id)

    if not mapping:
        logger.warning(
            "LLM mapping is empty for project %s — no chunks for section %s",
            project_id, section_id
        )
        return []

    chunk_by_id = {c.get("chunk_id"): c for c in proj_chunks}
    matched_ids = mapping.get(section_id, [])

    if not matched_ids:
        logger.warning(
            "Section '%s' has no matches in LLM mapping (keys: %s)",
            section_id, list(mapping.keys())[:10]
        )

    result = []
    for cid in matched_ids[:top_k]:
        chunk = chunk_by_id.get(cid)
        if chunk:
            result.append(chunk)

    return result


def _rerank_chunks(
    section_id: str,
    section_title: str,
    query: str,
    chunks: List[Dict],
    top_k: int = 8,
) -> List[Dict]:
    """
    Re-rank a list of candidate chunks using a single Claude call.

    Sends up to 20 candidate excerpts in one prompt and asks the LLM to return
    a JSON array of 1-based indices ordered from most to least relevant.
    Falls back to original order on any error so the calling path always gets results.
    """
    if len(chunks) <= top_k:
        return chunks[:top_k]

    pool = chunks[:20]  # cap to avoid context overflow

    lines = []
    for i, chunk in enumerate(pool, 1):
        snippet = (chunk.get("chunk_text", ""))[:300].replace("\n", " ").strip()
        heading = chunk.get("section_heading", "") or ""
        doc     = chunk.get("doc_name", "") or ""
        lines.append(f"[{i}] ({doc} / {heading})\n{snippet}")

    prompt = (
        f"You are ranking document excerpts for relevance to a Blueprint Document section.\n\n"
        f"Section: {section_id} — {section_title}\n"
        f"Keywords: {query}\n\n"
        f"Below are {len(pool)} excerpts. Return ONLY a JSON array of their numbers "
        f"ordered from MOST to LEAST relevant. Include all {len(pool)} numbers.\n\n"
        + "\n\n".join(lines)
        + "\n\nResponse (JSON array only, e.g. [3, 1, 5, ...]): "
    )

    try:
        import json as _json_rr
        try:
            from app.claude_provider import completion_from_prompt as _cp_rr
        except ImportError:
            from claude_provider import completion_from_prompt as _cp_rr
        raw = _cp_rr(prompt, max_tokens=200, temperature=0.0).strip()
        m = re.search(r'\[[\d,\s]+\]', raw)
        if m:
            indices = _json_rr.loads(m.group())
            seen, reranked = set(), []
            for idx in indices:
                i = int(idx) - 1
                if 0 <= i < len(pool) and i not in seen:
                    seen.add(i)
                    reranked.append(pool[i])
            # Append any chunks the LLM omitted (shouldn't happen, but be safe)
            for i, chunk in enumerate(pool):
                if i not in seen:
                    reranked.append(chunk)
            logger.info(
                "_rerank_chunks: re-ranked %d candidates → top %d for section %s",
                len(pool), top_k, section_id,
            )
            return reranked[:top_k]
    except Exception as _rr_err:
        logger.warning("_rerank_chunks failed for section %s: %s — using original order", section_id, _rr_err)

    return chunks[:top_k]


def _search_project_chunks(
    project_id: str,
    section_id: str,
    doc_keywords: List[str],
    top_k: int = 8,
) -> List[Dict]:
    """
    Semantic chunk search with LLM re-ranking.

    Retrieves up to 2× top_k candidates via cosine similarity, then re-ranks
    them with a single Claude call to surface the most section-relevant
    chunks before returning top_k.  Falls back to LLM matching when no
    embeddings exist, and to original semantic order if re-ranking fails.
    """
    try:
        proj_chunks = _load_all_project_chunks(project_id)
        if not proj_chunks:
            return []

        sections = _get_live_sections(project_id) + _get_archived_sections(project_id)

        _is_sec2 = (section_id or "").startswith("2")
        _op_docs = ["user stories", "user_stories", "workshop", "sprint"]

        candidates = proj_chunks
        if _is_sec2:
            candidates = [
                c for c in proj_chunks
                if not any(od in (c.get("doc_name", "") or "").lower() for od in _op_docs)
            ]

        # Fix A — cross-section de-duplication: during a full-document run, drop
        # chunks already consumed by earlier sections so the same source block is
        # not repeated. Guarded so a section is never starved of candidates.
        _used = _RUN_USED_CHUNKS.get(project_id)
        if _used:
            _dedup = [c for c in candidates if c.get("chunk_id") not in _used]
            if len(_dedup) >= 4:
                candidates = _dedup

        try:
            from app.semantic_search import semantic_search
        except ImportError:
            from semantic_search import semantic_search

        section_title = next(
            (s["title"] for s in sections if s["id"] == section_id), ""
        )
        query = f"{section_title} {' '.join(doc_keywords)}".strip()

        # Fetch more candidates than needed so re-ranking has something to work with
        fetch_k = min(max(top_k * 2, 12), 20)
        scored  = semantic_search(query, candidates, top_k=fetch_k, min_score=0.20)

        # Fix C (relevance routing): drop candidates scoring far below this
        # section's best match, so weakly-related content (e.g. governance text
        # matched into an inventory section) is not force-fit. Guarded to always
        # keep at least the strongest few so a section is never starved.
        if scored:
            _top = scored[0][0]
            _floor = max(0.20, _top * 0.55)
            _relevant = [(sc, ch) for (sc, ch) in scored if sc >= _floor]
            _min_keep = min(3, len(scored))
            if len(_relevant) < _min_keep:
                _relevant = scored[:_min_keep]
            scored = _relevant

        if scored:
            raw_chunks = [chunk for _score, chunk in scored]
            # Re-rank when we have surplus candidates
            if len(raw_chunks) > top_k:
                raw_chunks = _rerank_chunks(section_id, section_title, query, raw_chunks, top_k=top_k)
            else:
                raw_chunks = raw_chunks[:top_k]

            if _used is not None:
                for _c in raw_chunks:
                    _cid = _c.get("chunk_id")
                    if _cid is not None:
                        _used.add(_cid)

            hits = []
            for chunk in raw_chunks:
                heading = (
                    chunk.get("section_heading")
                    or (f"Page {chunk['page_number']}" if chunk.get("page_number") else "")
                )
                hits.append({
                    "doc_name":     chunk["doc_name"],
                    "heading_path": heading or "(No Heading)",
                    "snippet":      chunk.get("chunk_text", "")[:800],
                })
            logger.info(
                "_search_project_chunks [semantic+rerank]: %d results for %s (query: %.60s)",
                len(hits), section_id, query,
            )
            return hits

        # Fallback when no embeddings exist yet
        logger.warning(
            "_search_project_chunks: no embedded chunks for project %s — "
            "falling back to LLM matching. Call /embed-chunks to backfill.",
            project_id,
        )
        llm_matched = _get_chunks_for_section(
            section_id, candidates, sections, project_id, top_k=top_k
        )
        if _used is not None:
            for _c in llm_matched:
                _cid = _c.get("chunk_id")
                if _cid is not None:
                    _used.add(_cid)
        hits = []
        for chunk in llm_matched:
            heading = (
                chunk.get("section_heading")
                or (f"Page {chunk['page_number']}" if chunk.get("page_number") else "")
            )
            hits.append({
                "doc_name":     chunk["doc_name"],
                "heading_path": heading or "(No Heading)",
                "snippet":      chunk.get("chunk_text", "")[:800],
            })
        return hits

    except Exception as e:
        logger.warning("_search_project_chunks failed for project %s: %s", project_id, e)
        return []


# Cap the document content fed into a single section prompt. Reasoning
# deployments (gpt-5.x) can exhaust their completion-token budget reasoning over
# an oversized context and return empty output. These limits keep prompts lean;
# tune via env GEN_MAX_DOC_CHUNKS / GEN_MAX_DOC_CHARS.
_MAX_DOC_CHUNKS = int(os.getenv("GEN_MAX_DOC_CHUNKS", "24"))
_MAX_DOC_CHARS  = int(os.getenv("GEN_MAX_DOC_CHARS", "24000"))


def _cap_doc_hits(hits, section_id=None, label=""):
    """Limit doc-content hits by count and total characters (pinned/most-relevant first)."""
    capped = []
    total = 0
    for h in (hits or []):
        if len(capped) >= _MAX_DOC_CHUNKS:
            break
        snip = h.get("snippet", "") or ""
        if total + len(snip) > _MAX_DOC_CHARS:
            remaining = _MAX_DOC_CHARS - total
            if remaining < 200:
                break
            h = dict(h)
            h["snippet"] = snip[:remaining]
            snip = h["snippet"]
        capped.append(h)
        total += len(snip)
    if hits and len(capped) < len(hits):
        logger.info("fetch_sources: capped %s doc hits %d -> %d (%d chars) for section %s",
                    label, len(hits), len(capped), total, section_id)
    return capped


def fetch_sources(
    web_keywords: List[str],
    doc_keywords: List[str],
    project_id: Optional[str] = None,
    section_id: Optional[str] = None,
    pinned_chunk_ids: Optional[List[str]] = None,
    pinned_image_ids: Optional[List[str]] = None,
) -> tuple:
    """Fetch live web + doc sources. Returns (context_str, sources_dict).

    When pinned_chunk_ids is non-empty the doc keyword search is bypassed and
    those exact chunks are used as the document context instead.
    When pinned_image_ids is non-empty the matching image metadata (caption,
    description, keywords) is appended as additional context.
    """
    context_parts: List[str] = []
    sources: Dict = {"web": [], "docs": []}

    if web_keywords:
        web_query = " ".join(web_keywords[:5])
        try:
            r = requests.post(
                f"{HTTP_BASE}/web_search",
                json={"query": web_query, "max_results": 5},
                timeout=20,
            )
            if r.status_code == 200:
                result = r.json().get("result", "")
                context_parts.append(f"## Web Research\n{result}")
                for url in re.findall(r'https?://[^\s\)\]\n<>"]+', result):
                    url = url.rstrip(".,;")
                    if url not in [w["url"] for w in sources["web"]]:
                        sources["web"].append({"url": url})
        except Exception as e:
            logger.warning("web_search failed during fetch_sources: %s", e)

    # ── Pinned doc chunks — bypass keyword search ────────────────────────────
    if pinned_chunk_ids:
        hits = []
        try:
            all_chunks = _load_all_project_chunks(project_id)
            chunk_by_id = {str(c.get("chunk_id", "")): c for c in all_chunks}

            # Resolve the explicitly pinned chunks first.
            pinned_recs = []
            for cid in pinned_chunk_ids:
                rec = chunk_by_id.get(str(cid))
                if rec:
                    pinned_recs.append(rec)

            # Auto-include SIBLING chunks: any chunk from the same document
            # AND the same section_heading as a pinned chunk. Source content
            # under one heading is frequently split across multiple chunks
            # (e.g. "Planning Hierarchy Attributes" holds BOTH the
            # Demand/Supply table and the Supplier table as separate chunks).
            # Pulling siblings means pinning one piece brings the whole
            # heading's content, so no table/list is left half-generated.
            pinned_set = {str(r.get("chunk_id", "")) for r in pinned_recs}
            sibling_keys = {
                ((r.get("doc_name", "") or ""), (r.get("section_heading", "") or ""))
                for r in pinned_recs
                if (r.get("section_heading", "") or "").strip()
            }
            ordered = list(pinned_recs)
            if sibling_keys:
                for c in all_chunks:
                    if str(c.get("chunk_id", "")) in pinned_set:
                        continue
                    key = ((c.get("doc_name", "") or ""), (c.get("section_heading", "") or ""))
                    if key in sibling_keys:
                        ordered.append(c)
                        pinned_set.add(str(c.get("chunk_id", "")))

            for rec in ordered:
                hits.append({
                    "doc_name":    rec.get("doc_name", ""),
                    "heading_path": rec.get("section_heading", ""),
                    "snippet":     rec.get("chunk_text", "")[:1200],
                })
        except Exception as exc:
            logger.warning("fetch_sources: pinned chunk lookup failed: %s", exc)
        hits = _cap_doc_hits(hits, section_id, "pinned")
        if hits:
            context_parts.append("## Relevant Content from Uploaded Documents (Pinned Chunks)")
            context_parts.append("Use this content as PRIMARY source. Extract actual facts from it.")
            for h in hits:
                context_parts.append(
                    "[Source: " + h["doc_name"] + " | " + h["heading_path"] + "]\n" +
                    h["snippet"]
                )
                sources["docs"].append({
                    "doc_name":    h["doc_name"],
                    "heading_path": h["heading_path"],
                    "snippet":     h["snippet"][:600],
                })
        logger.info("fetch_sources: %d pinned chunks resolved for %s/%s",
                    len(hits), project_id, section_id)

    elif doc_keywords:
        # No pinned chunks — fall back to keyword search
        hits = []
        if project_id and section_id:
            hits = _search_project_chunks(project_id, section_id, doc_keywords, top_k=10)
            logger.info("fetch_sources: %d doc hits for %s/%s (keywords: %s)",
                        len(hits), project_id, section_id, doc_keywords[:5])

            # Sibling expansion: include any chunk sharing a matched chunk's
            # (doc_name, section_heading) so multi-chunk content under one
            # heading (e.g. two tables under "Planning Hierarchy Attributes")
            # is generated in full, not half.
            try:
                matched_keys = {
                    ((h.get("doc_name", "") or ""), (h.get("heading_path", "") or ""))
                    for h in hits if (h.get("heading_path", "") or "").strip()
                }
                if matched_keys:
                    seen_snips = {h.get("snippet", "")[:80] for h in hits}
                    for c in _load_all_project_chunks(project_id):
                        key = ((c.get("doc_name", "") or ""), (c.get("section_heading", "") or ""))
                        if key in matched_keys:
                            snip = (c.get("chunk_text", "") or "")[:1200]
                            if snip[:80] not in seen_snips:
                                hits.append({
                                    "doc_name":    c.get("doc_name", ""),
                                    "heading_path": c.get("section_heading", ""),
                                    "snippet":     snip,
                                })
                                seen_snips.add(snip[:80])
            except Exception as _sib_exc:
                logger.debug("fetch_sources: sibling expansion failed: %s", _sib_exc)

        hits = _cap_doc_hits(hits, section_id, "keyword")
        if hits:
            context_parts.append("## Relevant Content from Uploaded Documents")
            context_parts.append("Use this content as PRIMARY source. Extract actual facts from it.")
            for h in hits:
                context_parts.append(
                    "[Source: " + h["doc_name"] + " | " + h["heading_path"] + "]\n" +
                    h["snippet"][:1200]
                )
                sources["docs"].append({
                    "doc_name": h["doc_name"],
                    "heading_path": h["heading_path"],
                    "snippet": h["snippet"][:600],
                })
        else:
            logger.warning("fetch_sources: 0 doc hits for project=%s section=%s keywords=%s",
                           project_id, section_id, doc_keywords[:5])

    # ── Pinned images — include as context AND instruct LLM to embed them ───────
    if pinned_image_ids:
        try:
            try:
                from app.image_index import get_image_by_id
            except ImportError:
                from image_index import get_image_by_id
            img_parts = []
            for iid in pinned_image_ids:
                rec = get_image_by_id(iid)
                if not rec:
                    logger.warning("fetch_sources: pinned image id=%s not found in any index", iid)
                    continue
                caption = rec.get("caption", iid)
                desc    = rec.get("description", "")
                kws     = ", ".join(rec.get("keywords", []))
                src     = rec.get("source_doc", "")
                sec_h   = rec.get("section_heading", "")
                line = (
                    f"[Pinned Image | ID: {iid}]\n"
                    f"Caption: {caption}\n"
                    f"Source: {src}" + (f" — {sec_h}" if sec_h else "") + "\n"
                )
                if desc:
                    line += f"Description: {desc}\n"
                if kws:
                    line += f"Keywords: {kws}\n"
                line += (
                    f"EMBED INSTRUCTION: Place this image at the most relevant point in the "
                    f'section by writing exactly on its own line: <IMAGE id="{iid}"/>'
                )
                img_parts.append(line)
            if img_parts:
                context_parts.append(
                    "## Pinned Images — YOU MUST EMBED THESE\n"
                    "For each image below, insert the exact tag shown in EMBED INSTRUCTION "
                    "on a standalone line at the most appropriate location in the section text."
                )
                context_parts.extend(img_parts)
                logger.info("fetch_sources: %d pinned images added for %s/%s",
                            len(img_parts), project_id, section_id)
        except Exception as exc:
            logger.warning("fetch_sources: pinned image lookup failed: %s", exc)

    return "\n".join(context_parts), sources


# ─────────────────────────────────────────────────────────────────────────────
# SECTION MASTER LIST — matches brd_workflow.py ALL_TITLES exactly
# ─────────────────────────────────────────────────────────────────────────────
# Hint table removed — biased retrieval toward a legacy client's keywords.
# Section ranking is now driven by: (1) the section title's own tokens,
# (2) the semantic embedding query built from the user's structure + prompt
# + sibling titles, and (3) the LLM matcher's cached picks. See
# `_score_chunk_for_section` and `_semantic_section_scores`.
SECTION_DOC_HINTS: dict = {}


# ─────────────────────────────────────────────────────────────────────────────
# SEMANTIC SECTION → CHUNK RANKING
# ─────────────────────────────────────────────────────────────────────────────
# The hard-coded SECTION_DOC_HINTS scorer below is keyed by the original BRD
# section ids ("1", "1.1", "2", …). It returns 0 for custom-template sections
# (e.g. "7.1.1 Process Overview"), which used to leave them with no signal at
# all. The semantic scorer below uses each section's title text (+ parent
# title + any user-supplied keywords / prompt / structure) to embed-rank
# every chunk that has an embedding, returning {chunk_id: cosine_score} in
# the 0-1 range. Callers blend this with the keyword score so accuracy keeps
# scaling with the template, not just the legacy section list.

_SECTION_SIM_CACHE: dict = {}   # { (project_id, section_id, fingerprint): {cid: score} }


def _section_query_text(
    section_id: str,
    sec_title: str = "",
    parent_title: str = "",
    user_query_extra: str = "",
    structure: str = "",
    prompt: str = "",
    sibling_titles: list = None,
) -> str:
    """Build the natural-language query we embed for a section.

    A richer query gives the embedder more topical signal than a bare title
    like 'Process Overview'. We include — in order of weight — the section
    title chain, the user's structure description, the user's custom prompt,
    any free-text the caller passed in (typically existing doc keywords),
    and a small contrastive hint that names the siblings to push the
    embedding *away* from neighbouring sections.
    """
    parts = []
    if sec_title:
        parts.append(sec_title)
    if parent_title and parent_title != sec_title:
        parts.append(f"Parent section: {parent_title}")
    # User-specified structure usually describes WHAT the section should
    # contain (e.g. "table with site code, country, currency"), so it's a
    # strong topic signal.
    if structure:
        parts.append(f"Structure: {structure.strip()[:600]}")
    # The custom prompt describes the section's intent and constraints.
    if prompt:
        parts.append(f"Instructions: {prompt.strip()[:800]}")
    if user_query_extra:
        parts.append(user_query_extra)
    if sibling_titles:
        sibs = [t for t in sibling_titles if t and t != sec_title][:6]
        if sibs:
            # A contrastive hint nudges similarity AWAY from neighbouring
            # sections that often share vocabulary (e.g. "Demand Foundation"
            # vs "Supply Foundation"). The embedder treats it as a single
            # noisy passage so the topical center still dominates.
            parts.append("This section is NOT about: " + "; ".join(sibs))
    # Anchor on the section id too — helps when the title is generic ("Overview")
    parts.append(f"Blueprint Document section {section_id}")
    return " ".join(p for p in parts if p).strip()


def _semantic_section_scores(
    section_id: str,
    chunks: list,
    project_id: str = None,
    sec_title: str = "",
    parent_title: str = "",
    user_query_extra: str = "",
    structure: str = "",
    prompt: str = "",
    sibling_titles: list = None,
) -> dict:
    """Return a {chunk_id: cosine_score in [0,1]} mapping for `chunks` ranked
    against the section's natural-language query. Empty dict when semantic is
    disabled, the query is empty, or no chunks carry an embedding.
    """
    try:
        from app.semantic_search import (
            is_semantic_enabled, embed_query, _cosine_scores,
        )
    except ImportError:
        try:
            from semantic_search import (
                is_semantic_enabled, embed_query, _cosine_scores,
            )
        except ImportError:
            return {}

    if not is_semantic_enabled():
        return {}

    query = _section_query_text(
        section_id,
        sec_title,
        parent_title,
        user_query_extra,
        structure=structure,
        prompt=prompt,
        sibling_titles=sibling_titles,
    )
    if not query:
        return {}

    with_emb = [c for c in chunks if c.get("embedding") and c.get("chunk_id") is not None]
    if not with_emb:
        return {}

    cache_key = (project_id, section_id, query, len(with_emb))
    if cache_key in _SECTION_SIM_CACHE:
        return _SECTION_SIM_CACHE[cache_key]

    try:
        q_vec = embed_query(query)
        vecs = [c["embedding"] for c in with_emb]
        ids  = [c.get("chunk_id") for c in with_emb]
        scores = _cosine_scores(q_vec, vecs, chunk_ids=ids)
        out = {cid: float(s) for cid, s in zip(ids, scores)}
    except Exception as exc:
        logger.warning(
            "semantic section scorer failed for section %s: %s", section_id, exc,
        )
        return {}

    # Cap the cache so a long-lived process doesn't grow without bound. The
    # cache is keyed by query text so repeat opens of the Browse Chunks
    # panel on the same section reuse the work; clearing the oldest entries
    # is fine.
    if len(_SECTION_SIM_CACHE) > 256:
        try:
            _SECTION_SIM_CACHE.pop(next(iter(_SECTION_SIM_CACHE)))
        except Exception:
            _SECTION_SIM_CACHE.clear()
    _SECTION_SIM_CACHE[cache_key] = out
    return out


def _score_chunk_for_section(chunk: dict, section_id: str, title_tokens: set) -> int:
    """Lightweight keyword-overlap score between a chunk and a section.

    Pure title-token overlap (heading > keywords > body). The previous
    implementation contained client-specific overrides (Section 2.x exec-vs-
    operational doc filter, hardcoded SECTION_DOC_HINTS) that biased every
    project toward a legacy template's source documents. Those have been
    removed — the semantic scorer + LLM matcher in callers (see
    `_semantic_section_scores` and the chunks-browser blend) handle topical
    matching for any template, and we only use this scorer as a small
    keyword tiebreaker.
    """
    if not title_tokens:
        return 0
    heading = (chunk.get("section_heading", "") or "").lower()
    kws_str = " ".join(chunk.get("keywords", [])).lower()
    text    = (chunk.get("chunk_text", "") or "").lower()
    score = 0
    for tok in title_tokens:
        if tok in heading: score += 3
        if tok in kws_str: score += 2
        if tok in text:    score += 1
    return score


ALL_SECTIONS = [
    {"id": "1",     "title": "Introduction"},
    {"id": "1.1",   "title": "Company Information"},
    {"id": "1.2",   "title": "Current State of Business"},
    {"id": "1.3",   "title": "Purpose"},
    {"id": "1.4",   "title": "Scope"},
    {"id": "1.5",   "title": "Definitions & Acronyms"},
    {"id": "2",     "title": "Benefit Realization"},
    {"id": "2.1",   "title": "Business Issues"},
    {"id": "2.2",   "title": "Value Drivers"},
    {"id": "3",     "title": "Supply Chain Scope"},
    {"id": "3.1",   "title": "Supply Chain Maps"},
    {"id": "3.2",   "title": "Sites"},
    {"id": "3.3",   "title": "Demand Foundation"},
    {"id": "3.4",   "title": "Supply Foundation"},
    {"id": "3.5",   "title": "Inventory Management Foundation"},
    {"id": "3.6",   "title": "Constraints"},
    {"id": "3.7",   "title": "Scenario Structure"},
    {"id": "4",     "title": "Demand Planning"},
    {"id": "4.1",   "title": "Demand Planning Process Overview"},
    {"id": "4.2",   "title": "Forecast Consumption"},
    {"id": "4.2.1", "title": "Solution Assumptions"},
    {"id": "4.2.2", "title": "Resources"},
    {"id": "5",     "title": "Supply & Inventory Planning"},
    {"id": "5.1",   "title": "Review and Adjust Planning Parameters"},
    {"id": "5.2",   "title": "Manage Capacity Constraints"},
    {"id": "5.3",   "title": "Resolve Supply Plan Exceptions"},
    {"id": "5.4",   "title": "Resolve Inventory Exceptions"},
    {"id": "6",     "title": "Data Integration"},
    {"id": "6.1",   "title": "Architecture"},
    {"id": "6.2",   "title": "Data Sources"},
    {"id": "6.3",   "title": "Data Files"},
    {"id": "6.4",   "title": "Data Frequency"},
    {"id": "7",     "title": "Project Governance & Implementation Approach"},
    {"id": "7.1",   "title": "Project Organization & Roles"},
    {"id": "7.2",   "title": "Governance & Decision-Making"},
    {"id": "7.3",   "title": "Change Management Process"},
    {"id": "7.4",   "title": "Acceptance & Defect Management"},
]

# ── No hardcoded default keywords ─────────────────────────────────────────────
# Keywords are derived ONLY from project document chunks or user edits.
# If chunking failed or no documents were uploaded, keywords will be empty
# and the UI will surface a warning so the user knows to fix it.


# ── Section-specific prompt templates ─────────────────────────────────────────
# Per user request the hardcoded per-section prompts have been removed.
# The agent now uses only the section title + the user's Structure / Custom
# Prompt fields (entered in the editor) as authoritative guidance, plus the
# section's research context. The dict is kept as an empty placeholder so
# any leftover reference (`section_id in SECTION_PROMPTS`) safely returns
# False and the user-provided instructions become the single source of truth.
SECTION_PROMPTS: dict = {}

# Legacy hardcoded prompts removed entirely. Previously this file shipped
# 30+ per-section prompt templates with client-specific column orderings,
# example site names, "FORBIDDEN" / "WHITELIST" lists, and exec-vs-
# operational doc filters. They actively biased generation away from
# arbitrary client templates. The single source of truth is now the
# Structure + Custom Prompt fields the user fills in for each section.
_LEGACY_SECTION_PROMPTS_DO_NOT_USE_REMOVED: dict = {}

# ── Section default structures ─────────────────────────────────────────────────
# REMOVED per user request. The default placeholders were biased toward a
# legacy template's section ids (1.1 - 6.4) and inserted column-order
# instructions that don't apply to other clients. The structure textarea
# now starts blank and the user's own description is the single source of
# truth. Kept as an empty dict so any old `.get(sid, default)` call returns
# the empty default gracefully.
SECTION_DEFAULT_STRUCTURES: dict = {}


# ── Keyword store: load from disk (per-project) ──────────────────────────────
def _keyword_store_path(project_id: str = None) -> Path:
    if project_id:
        return _THIS_DIR / f"screen3_keyword_{project_id}.json"
    return KEYWORD_STORE_PATH

def _load_keyword_store(project_id: str = None) -> Dict:
    path = _keyword_store_path(project_id)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def _save_keyword_store(store: Dict, project_id: str = None):
    _keyword_store_path(project_id).write_text(
        json.dumps(store, indent=2), encoding="utf-8"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Pydantic models for request bodies
# ─────────────────────────────────────────────────────────────────────────────
class KeywordUpdateRequest(BaseModel):
    web_keywords:     List[str] = []
    doc_keywords:     List[str] = []
    doc_kw_top_n:     Optional[int] = None
    prompt:           Optional[str] = None
    structure:        Optional[str] = None
    output:           Optional[str] = None
    user_edited:      Optional[bool] = None
    heading_only:     Optional[bool] = None
    no_heading:       Optional[bool] = None
    project_id:       Optional[str] = None
    # None = "not provided" → preserve existing pins. Only overwrite when the
    # client explicitly sends a list (so a flag-only save can't wipe pins).
    pinned_chunk_ids: Optional[List[str]] = None
    pinned_image_ids: Optional[List[str]] = None

class ClientContext(BaseModel):
    company:  str = ""
    industry: str = ""
    erp:      str = ""
    maturity: str = ""
    pain:     str = ""
    scope:    str = ""

class RegeneratePromptRequest(BaseModel):
    web_keywords:   List[str] = []
    doc_keywords:   List[str] = []
    prompt:         Optional[str] = None
    structure:      Optional[str] = None
    client_context: Optional[ClientContext] = None
    project_id:     Optional[str] = None

class GenerateRequest(BaseModel):
    web_keywords:     List[str] = []
    doc_keywords:     List[str] = []
    doc_kw_top_n:     Optional[int] = None
    prompt:           Optional[str] = None
    structure:        Optional[str] = None
    client_context:   Optional[ClientContext] = None
    project_id:       Optional[str] = None
    # "both" (default), "doc", or "web" — enforced server-side so we never
    # accidentally run a search the user opted out of.
    gen_source:       Optional[str] = "both"
    pinned_chunk_ids: List[str] = []
    pinned_image_ids: List[str] = []


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/editor", response_class=HTMLResponse)
def screen3_ui():
    """
    Serve Screen 3 HTML.
    File lives at: orchestration/brd_convo_app/backend/app/static/screen3.html
    """
    html_path = Path(__file__).parent / "static" / "screen3.html"
    # Tell the browser never to use a cached copy — we ship UI fixes
    # frequently and stale cached HTML is the #1 cause of "fix not working"
    # reports.
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        status_code=200,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@router.get("/api/sections")
def get_sections(project_id: str = None, light: bool = False):
    """
    Returns the live section list with keyword config and status.
    Fast path: doc-hint + title-token scoring (no LLM blocking call).
    LLM semantic matching runs in background via /api/sections/refresh-keywords.
    """
    store    = _load_keyword_store(project_id)
    active   = _get_live_sections(project_id)
    archived = _get_archived_sections(project_id)

    chunk_keywords: dict = {}
    chunk_error: str = ""

    if project_id and not light:
        try:
            proj_chunks = _load_all_project_chunks(project_id)

            if not proj_chunks:
                # Build a precise diagnostic: distinguish between "no project
                # uploads AND no attached repositories" and "repositories
                # attached but they contain no chunks". The previous message
                # only mentioned Document Upload, which misled users who had
                # actually uploaded into a Repository but not attached it.
                try:
                    try:
                        from app.project_routes import PROJECTS as _PROJECTS, UPLOAD_ROOT as _UR
                    except ImportError:
                        from project_routes import PROJECTS as _PROJECTS, UPLOAD_ROOT as _UR
                    _own_dir = Path(_UR) / project_id / "source"
                    _own_files = [f for f in _own_dir.iterdir() if f.is_file()] if _own_dir.exists() else []
                    _repo_ids = (_PROJECTS.get(project_id, {}) or {}).get("repositories", []) or []
                except Exception:
                    _own_files = []
                    _repo_ids = []
                if not _own_files and not _repo_ids:
                    chunk_error = (
                        "No source documents linked to this project yet. "
                        "Either upload files on the Document Upload screen, "
                        "OR attach an existing Repository (Repositories → pick "
                        "your repo → Attach to project). Once attached, return "
                        "here and click Refresh Keywords."
                    )
                elif _repo_ids and not _own_files:
                    chunk_error = (
                        "This project has Repositories attached but no indexed "
                        "chunks were found in them. Check that each repo finished "
                        "chunking (Repositories screen → file status = Indexed), "
                        "then click Refresh Keywords here."
                    )
                else:
                    chunk_error = (
                        "No document chunks found for this project. "
                        "Wait for chunking to finish on the Document Upload "
                        "screen, then click Refresh Keywords."
                    )
                logger.warning(
                    "No chunks found for project %s — own_files=%d, attached_repos=%d",
                    project_id, len(_own_files), len(_repo_ids),
                )
            else:
                def dedupe(lst):
                    seen = set()
                    return [x for x in lst if x and not (x in seen or seen.add(x))]

                all_sections = active + archived
                STOP_WORDS = {"the","a","an","and","or","of","in","to","for",
                              "is","are","was","be","by","with","on","at","this",
                              "that","from","as","it","its","were","has","have"}

                # Precompute semantic similarity per section if embeddings exist
                # — this drives chunk selection for *every* section in the
                # template, including custom ones that SECTION_DOC_HINTS
                # doesn't recognise.
                section_titles = {s["id"]: s["title"] for s in all_sections}
                # Index sections by parent prefix so we can supply contrastive
                # sibling titles when embedding each section's query.
                _siblings_by_parent: dict = {}
                for s in all_sections:
                    sid = s.get("id", "")
                    pid = sid.rsplit(".", 1)[0] if "." in sid else ""
                    _siblings_by_parent.setdefault(pid, []).append(s.get("title", ""))
                # Pull whatever the LLM matcher already cached for this
                # project so its picks dominate when available.
                _llm_cached = (_llm_match_cache.get(project_id) or {}).get("mapping", {}) if project_id else {}
                # Pull existing user-saved structure/prompt per section so the
                # embedding query gets the user's own description of intent.
                _kw_store_for_query = _load_keyword_store(project_id) if project_id else {}
                for sec in all_sections:
                    sid   = sec["id"]
                    title = sec["title"]
                    parent_id = sid.rsplit(".", 1)[0] if "." in sid else ""
                    parent_title = section_titles.get(parent_id, "")
                    title_tokens = {
                        w.lower() for w in title.split()
                        if len(w) > 3 and w.lower() not in STOP_WORDS
                    }
                    sibs = [t for t in _siblings_by_parent.get(parent_id, []) if t != title]
                    saved = _kw_store_for_query.get(sid, {}) if isinstance(_kw_store_for_query, dict) else {}
                    sim_map = _semantic_section_scores(
                        section_id=sid,
                        chunks=proj_chunks,
                        project_id=project_id,
                        sec_title=title,
                        parent_title=parent_title,
                        structure=saved.get("structure", "") if isinstance(saved, dict) else "",
                        prompt=saved.get("prompt", "") if isinstance(saved, dict) else "",
                        sibling_titles=sibs,
                    )
                    llm_ids = set(_llm_cached.get(sid, []) or [])
                    scored = []
                    for chunk in proj_chunks:
                        kw_score = _score_chunk_for_section(chunk, sid, title_tokens)
                        sim_score = sim_map.get(chunk.get("chunk_id"), 0.0)
                        # Blended score: semantic dominates (×40); LLM
                        # picks dominate everything (+50); heading hits and
                        # pre-tagged brd_section_id are small confirmations.
                        total = kw_score + max(0.0, sim_score) * 40.0
                        if chunk.get("chunk_id") in llm_ids:
                            total += 50.0
                        if title_tokens:
                            h_low = (chunk.get("section_heading","") or "").lower()
                            total += 5.0 * sum(1 for t in title_tokens if t in h_low)
                        pre = (chunk.get("brd_section_id") or "").strip()
                        if pre and (pre == sid or sid.startswith(pre + ".") or pre.startswith(sid + ".")):
                            total += 15.0
                        if total > 0:
                            scored.append((total, chunk))
                    scored.sort(key=lambda x: -x[0])
                    top_chunks = [c for _, c in scored[:15]]

                    doc_kw = dedupe([
                        kw for c in top_chunks
                        for kw in c.get("keywords", [])
                        if kw and len(kw) > 3
                    ])[:30]

                    _brand = _get_client_company(project_id=project_id)
                    web_kw = []
                    if _brand:
                        web_kw.append(f"{_brand} {title}")
                    web_kw += [f"{title} supply chain planning", f"{title} best practices"]
                    parent_id = sid.split(".")[0]
                    parent_title = next(
                        (s["title"] for s in active if s["id"] == parent_id and s["id"] != sid),
                        None
                    )
                    if parent_title:
                        web_kw.append(f"{parent_title} {title}")
                    tl = title.lower()
                    if any(w in tl for w in ["demand","forecast"]):
                        web_kw += ["statistical forecasting supply chain","consensus demand planning"]
                    elif any(w in tl for w in ["supply","inventory","replenish"]):
                        if _brand: web_kw.append(f"inventory optimization {_brand}")
                        web_kw.append("exception-based supply planning")
                    elif any(w in tl for w in ["data","integration","architecture"]):
                        web_kw.append("supply chain data integration ERP")
                        if _brand: web_kw.append(f"{_brand} data connector")
                    elif any(w in tl for w in ["benefit","value","business"]):
                        web_kw.append("supply chain ROI business value")
                        if _brand: web_kw.append(f"{_brand} digital transformation")
                    web_kw = dedupe(web_kw)[:8]
                    chunk_keywords[sid] = {"web": web_kw, "doc": doc_kw}

                kw_store = _load_keyword_store(project_id)
                changed = 0
                for sid, kws in chunk_keywords.items():
                    entry = kw_store.get(sid, {})
                    if not entry.get("user_edited"):
                        entry["web_keywords"] = kws["web"]
                        entry["doc_keywords"] = kws["doc"]
                        kw_store[sid] = entry
                        changed += 1
                if changed:
                    _save_keyword_store(kw_store, project_id)
                logger.info(
                    "Built hint-scored keywords for %d sections from project %s (%d chunks, %d persisted)",
                    len(chunk_keywords), project_id, len(proj_chunks), changed
                )
        except Exception as e:
            chunk_error = (
                f"Failed to load document chunks: {str(e)}. "
                "Keywords will be empty. Please check that documents were "
                "uploaded and indexed correctly on the Document Upload screen."
            )
            logger.warning("Could not load project chunks for keyword init: %s", e)
    else:
        chunk_error = (
            "No project selected. Keywords cannot be populated without a "
            "project context. Please navigate from the Document Upload screen."
        )

    def enrich(sec):
        sid   = sec["id"]
        saved = store.get(sid, {})
        if "web_keywords" in saved:
            web_kw = saved["web_keywords"]
        elif sid in chunk_keywords:
            web_kw = chunk_keywords[sid]["web"]
        else:
            web_kw = []
        if saved.get("user_edited") and "doc_keywords" in saved:
            # CR1 — the user has committed to these document keywords. Never
            # overwrite them with freshly recomputed chunk keywords on reload;
            # users must be able to review the exact keywords driving generation.
            doc_kw = saved["doc_keywords"]
        elif sid in chunk_keywords and chunk_keywords[sid]["doc"]:
            doc_kw = chunk_keywords[sid]["doc"]
        elif "doc_keywords" in saved:
            doc_kw = saved["doc_keywords"]
        else:
            doc_kw = []
        return {
            "id":               sid,
            "title":            sec["title"],
            "web_keywords":     web_kw,
            "doc_keywords":     doc_kw,
            "doc_kw_top_n":     saved.get("doc_kw_top_n", None),
            "prompt":           saved.get("prompt", ""),
            "structure":        saved.get("structure", ""),
            "output":           saved.get("output", ""),
            "user_edited":      bool(saved.get("user_edited", False)),
            "heading_only":     bool(saved.get("heading_only", False)),
            "no_heading":       bool(saved.get("no_heading", False)),
            "status":           saved.get("status", "not-started"),
            "pinned_chunk_ids": saved.get("pinned_chunk_ids", []),
            "pinned_image_ids": saved.get("pinned_image_ids", []),
            # True when this section came from an imported (Update workflow) .docx
            # and is still locked/preserved. Lets the editor restore lock state
            # after a page refresh instead of treating it as a normal section.
            "imported":         bool(saved.get("imported_at")),
        }

    # removed_imports = sections the user explicitly removed (unlocked) from the
    # import, so the editor can keep them shown as unlocked after a refresh.
    try:
        _removed_imports = list((_load_approved_store(project_id).get("removed_imports", [])) ) if project_id else []
    except Exception:
        _removed_imports = []

    return {
        "sections":        [enrich(s) for s in active],
        "archived":        [enrich(s) for s in archived],
        "chunk_error":     chunk_error,
        "removed_imports": _removed_imports,
    }
@router.put("/api/sections/{section_id}/keywords")
def update_section_keywords(section_id: str, body: KeywordUpdateRequest):
    """
    Persist keyword edits + optional prompt override for a section.
    Saved to screen3_keyword_store.json next to main.py.
    """
    store = _load_keyword_store(body.project_id)
    entry = store.get(section_id, {})
    entry["web_keywords"] = body.web_keywords
    entry["doc_keywords"] = body.doc_keywords
    if body.doc_kw_top_n is not None:
        entry["doc_kw_top_n"] = body.doc_kw_top_n
    else:
        entry.pop("doc_kw_top_n", None)
    if body.prompt is not None:
        entry["prompt"] = body.prompt
    if body.structure is not None:
        entry["structure"] = body.structure
    if body.output is not None:
        entry["output"] = body.output
    if body.user_edited is not None:
        entry["user_edited"] = bool(body.user_edited)
    # Only overwrite pins when explicitly provided — a flag-only save (None)
    # must not wipe previously saved pinned chunks/images.
    if body.pinned_chunk_ids is not None:
        entry["pinned_chunk_ids"] = body.pinned_chunk_ids
    if body.pinned_image_ids is not None:
        entry["pinned_image_ids"] = body.pinned_image_ids
    if body.heading_only is not None or body.no_heading is not None:
        # Mutually exclusive — set one clears the other.
        ho_flag = bool(body.heading_only) if body.heading_only is not None else entry.get("heading_only", False)
        nh_flag = bool(body.no_heading)   if body.no_heading   is not None else entry.get("no_heading", False)
        if ho_flag and nh_flag:
            ho_flag = False
        entry["heading_only"] = ho_flag
        entry["no_heading"]   = nh_flag
        # Mirror into the approved store so the export route picks it up even
        # when the section was approved before the toggle.
        ap = _load_approved_store(body.project_id)
        ho = set(ap.get("heading_only_ids", []))
        nh = set(ap.get("no_heading_ids", []))
        ho.discard(section_id); nh.discard(section_id)
        if ho_flag: ho.add(section_id)
        if nh_flag: nh.add(section_id)
        ap["heading_only_ids"] = sorted(ho)
        ap["no_heading_ids"]   = sorted(nh)
        _save_approved_store(ap, body.project_id)
    store[section_id] = entry
    _save_keyword_store(store, body.project_id)
    logger.info("Keywords saved for section %s (project=%s, top_n=%s)", section_id, body.project_id, body.doc_kw_top_n)
    return {"ok": True, "section_id": section_id}


@router.post("/api/sections/{section_id}/regenerate-prompt")
def regenerate_section_prompt(section_id: str, body: RegeneratePromptRequest):
    """
    Build a section-specific prompt using:
    1. User-provided structure (takes full precedence — SECTION_PROMPTS is skipped)
    2. SECTION_PROMPTS base template (only when no structure defined)
    3. Client context, document keywords, web keywords
    """
    # Prefer the LIVE section title (handles renames) over the static
    # ALL_SECTIONS list, which would otherwise yield a stale heading.
    _live = _get_live_sections(body.project_id) if getattr(body, "project_id", None) else []
    sec = next((s for s in _live if s.get("id") == section_id), None) \
        or next((s for s in ALL_SECTIONS if s["id"] == section_id), None)
    title = (sec.get("title") if isinstance(sec, dict) else None) or section_id

    # ── Authoritative company from server-side project context ───────────────
    # Never trust body.client_context.company as the sole source: a stale
    # browser session may send the wrong client name. Always verify against
    # the project's persisted _client_context.json and override if different.
    authoritative_company = _get_client_company(project_id=body.project_id)

    company_name = ""
    if body.client_context and body.client_context.company:
        company_name = body.client_context.company.strip()
    if not company_name:
        company_name = authoritative_company
    elif authoritative_company and company_name.lower() != authoritative_company.lower():
        logger.warning(
            "regenerate_section_prompt %s: client_context.company=%r does NOT match "
            "project %s authoritative company=%r — using authoritative value to prevent "
            "cross-project contamination.",
            section_id, company_name, body.project_id, authoritative_company,
        )
        company_name = authoritative_company
    brand_name = company_name  # never fall back to "Kinaxis"

    ctx = body.client_context
    # Echo the resolved company back onto the request so any downstream
    # consumers (and ctx_lines below) see the auto-filled value.
    if ctx is not None and brand_name and not (ctx.company or "").strip():
        try:
            ctx.company = brand_name
        except Exception:
            pass

    ctx_lines = []
    if ctx:
        if ctx.company:  ctx_lines.append(f"Client company: {ctx.company}")
        if ctx.industry: ctx_lines.append(f"Industry: {ctx.industry}")
        if ctx.erp:      ctx_lines.append(f"ERP system: {ctx.erp}")
        if ctx.maturity: ctx_lines.append(f"Planning maturity: {ctx.maturity}")
        if ctx.pain:     ctx_lines.append(f"Key pain points: {ctx.pain}")
        if ctx.scope:    ctx_lines.append(f"Phase 1 scope: {ctx.scope}")
    ctx_block = (
        "\n\nCLIENT CONTEXT — make ALL content specific to this client:\n"
        + "\n".join(ctx_lines)
    ) if ctx_lines else ""

    doc_kw = ", ".join(body.doc_keywords) if body.doc_keywords else ""
    doc_block = (
        f"\n\nDOCUMENT KEYWORDS — extract content matching these terms:\n{doc_kw}"
    ) if doc_kw else ""

    web_kw_subst = (
        [k.replace("Kinaxis", brand_name) for k in (body.web_keywords or [])]
        if brand_name else list(body.web_keywords or [])
    )
    web_kw = ", ".join(web_kw_subst) if web_kw_subst else ""
    web_block = (
        f"\n\nWEB SEARCH CONTEXT — supplementary research:\n{web_kw}"
    ) if web_kw else ""

    user_structure = (body.structure or "").strip()

    # When user has defined a structure, skip SECTION_PROMPTS entirely —
    # it contains hardcoded content topics that conflict with user intent.
    base = ""
    if not user_structure:
        base = SECTION_PROMPTS.get(section_id, "")
        if base:
            base = base.replace("[client company]", brand_name)
            base = base.replace("the client company", brand_name)

    # Build structure block
    if user_structure:
        structure_block = (
            "\n\n=== MANDATORY OUTPUT FORMAT (highest priority) ===\n\n"
            "USER INSTRUCTION:\n"
            f"{user_structure}\n\n"
            "HOW TO APPLY THIS FORMAT:\n"
            "1. Count items exactly as stated (e.g. '3-4 main points' = use 3 or 4).\n"
            "2. For hierarchical structures use this exact markdown:\n"
            "   \u2022 **Main Point Title** (bold — a summary heading of its sub-points)\n"
            "     - Sub-point one: Write 1-2 complete sentences of explanation.\n"
            "     - Sub-point two: Write 1-2 complete sentences of explanation.\n"
            "3. Every sub-point MUST have 1-2 full explanatory sentences.\n"
            "4. Every main point MUST summarise its sub-points like a heading.\n"
            "5. Do NOT flatten the hierarchy. Do NOT collapse sub-points.\n"
            "6. Extract all content from uploaded documents — no hallucinations.\n\n"
            "=== END MANDATORY FORMAT ==="
        )
    else:
        structure_block = ""

    # Persist structure
    if user_structure:
        kw_store = _load_keyword_store(body.project_id)
        entry = kw_store.get(section_id, {})
        entry["structure"] = user_structure
        kw_store[section_id] = entry
        _save_keyword_store(kw_store, body.project_id)

    preamble = (
        "You are a senior Blueprint Document writer for enterprise solution "
        "implementations at Bristlecone.\n\n"
        "UPLOADED DOCUMENT CONTENT will be injected as research context — "
        "use it as your PRIMARY source. Extract actual facts from it.\n\n"
    )

    if base:
        prompt = preamble + base + structure_block + ctx_block + doc_block + web_block
    else:
        prompt = (
            preamble
            + f"Generate Section {section_id}: {title} of the Blueprint Document.\n\n"
            + f"Write in professional Blueprint Document language. "
            + f"Reference {brand_name or 'the client'} throughout. Be specific.\n"
            + structure_block + ctx_block + doc_block + web_block
            + f"\n\nOutput inside <SECTION id=\"{section_id}\">...</SECTION> tags only."
        )

    store = _load_keyword_store(body.project_id)
    entry = store.get(section_id, {})
    entry["prompt"] = prompt
    entry["status"] = "in-progress"
    store[section_id] = entry
    _save_keyword_store(store, body.project_id)

    return {"prompt": prompt}

@router.post("/api/sections/{section_id}/generate")
def generate_section(section_id: str, body: GenerateRequest):
    """
    Generate Blueprint Document content for a single section.
    Routes to the correct run_*agent.py based on section number.
    When user_structure is set:
      - SECTION_PROMPTS is completely skipped (contains hardcoded topics)
      - custom_prompt (body.prompt) is ignored (may contain stale content)
      - MANDATORY OUTPUT FORMAT block goes FIRST in research_context
    """
    top = section_id.split(".")[0]
    ctx = body.client_context
    # Auto-fill client company from the project if the user never typed one in
    # ── Authoritative company — always read from the project's own file first ─
    # This is the ground-truth value that can never be spoofed by a stale
    # browser session or a cached prompt that still references a different client.
    _project_client = (_get_client_company(project_id=body.project_id) or "").strip()

    ctx_company = ""
    if ctx and ctx.company and ctx.company.strip():
        ctx_company = ctx.company.strip()
    if not ctx_company:
        ctx_company = _project_client
        if ctx_company and ctx is not None:
            # Mutate the in-memory model so downstream code (and the rest of
            # this request) sees the resolved company name without a special case.
            try:
                ctx.company = ctx_company
            except Exception:
                pass
            logger.info("generate_section %s: client.company auto-filled from project → %r",
                        section_id, ctx_company)
    elif _project_client and ctx_company.lower() != _project_client.lower():
        # Mismatch: the caller's client_context has a DIFFERENT company than the
        # project's own stored context. This is the hallmark of cross-project
        # contamination (e.g. a stale cached prompt from project A was sent to
        # project B). Override with the authoritative project value.
        logger.warning(
            "generate_section %s: client_context.company=%r MISMATCHES project %s "
            "authoritative company=%r — overriding to prevent content contamination.",
            section_id, ctx_company, body.project_id, _project_client,
        )
        ctx_company = _project_client
        if ctx is not None:
            try:
                ctx.company = ctx_company
            except Exception:
                pass

    ctx_lines = []
    if ctx_company:        ctx_lines.append(f"Client company: {ctx_company}")
    if ctx:
        if ctx.industry: ctx_lines.append(f"Industry: {ctx.industry}")
        if ctx.erp:      ctx_lines.append(f"ERP system: {ctx.erp}")
        if ctx.maturity: ctx_lines.append(f"Planning maturity: {ctx.maturity}")
        if ctx.pain:     ctx_lines.append(f"Key pain points: {ctx.pain}")
        if ctx.scope:    ctx_lines.append(f"Phase 1 scope: {ctx.scope}")

    # _project_client is already resolved above (authoritative from disk).
    # Use it directly — do NOT call _get_client_company a second time.
    client_block = (
        "\n\nCLIENT CONTEXT (every sentence must be specific to this client):\n"
        + "\n".join(ctx_lines)
    ) if ctx_lines else ""

    # ── CLIENT LOCK — prevents data bleed from example/sample clients ─────────
    # Some prompt templates and the model's prior training memory contain
    # specifics from earlier example clients (e.g. "Kraton"). If the project's
    # client is something else (e.g. "Arclin"), the output must never reference
    # the example client by name or copy its facts.
    if _project_client:
        client_lock = (
            "\n\nCLIENT LOCK — STRICT:\n"
            f"  - The ONLY client this document is being written for is: {_project_client}.\n"
            f"  - Refer to the client exclusively as '{_project_client}' (or its formal entity name).\n"
            "  - Do NOT mention any other client, organisation, brand, plant, or product\n"
            "    line by name unless that name appears verbatim in the RESEARCH CONTEXT\n"
            "    or uploaded documents for this project.\n"
            "  - Specifically: example client names that may appear in prompt templates\n"
            "    (such as 'Kraton') are illustrative only — never copy their data, site\n"
            "    lists, product families, geographies, ERP details, or quoted text into\n"
            "    this document.\n"
            "  - If a fact cannot be supported by this project's RESEARCH CONTEXT, write\n"
            f"    \"<information not available in {_project_client} source documents>\"\n"
            "    rather than substituting another client's data.\n"
        )
        client_block = (client_block or "") + client_lock

    custom_prompt  = body.prompt or ""
    user_structure = (body.structure or "").strip()

    # ── Stale-prompt sanity check ─────────────────────────────────────────────
    # The generating.html queue passes sec.prompt (saved in the keyword store
    # by build_prompt / regenerate-prompt). If that cached prompt was built
    # while the wrong client context was active (e.g. Apollo Hospital context
    # was erroneously associated with the Mahindra project), it will contain a
    # foreign client's name in "Reference <X> throughout" or "Client company:
    # <X>".  When we detect that mismatch, discard the stale custom_prompt so
    # the generation falls through to the safe SECTION_PROMPTS / writing-
    # standards path — which then picks up the correct company from client_block.
    if custom_prompt and _project_client:
        _prompt_lower = custom_prompt.lower()
        _client_lower = _project_client.lower()
        # Build a list of known OTHER client names that appear in this prompt.
        _foreign_names = []
        # Regex: "Reference <Name> throughout" or "Client company: <Name>"
        for _m in re.finditer(
            r'(?:reference\s+(.+?)\s+throughout|client company:\s*(.+?)(?:\\n|$))',
            _prompt_lower
        ):
            _candidate = (_m.group(1) or _m.group(2) or "").strip()
            if _candidate and _candidate != _client_lower and len(_candidate) > 2:
                _foreign_names.append(_candidate)
        if _foreign_names:
            logger.warning(
                "generate_section %s (project=%s): custom_prompt references foreign "
                "client name(s) %r (authoritative=%r) — discarding stale prompt to "
                "prevent cross-project contamination.",
                section_id, body.project_id, _foreign_names, _project_client,
            )
            custom_prompt = ""

    logger.info(
        "generate_section %s: user_structure=%r (len=%d), custom_prompt_len=%d",
        section_id,
        user_structure[:80] if user_structure else "(empty)",
        len(user_structure),
        len(custom_prompt),
    )

    # SECTION_PROMPTS skipped when user has supplied their own instructions
    # (either a custom prompt or a structure override).
    section_base_prompt = ""
    if not custom_prompt and not user_structure and section_id in SECTION_PROMPTS:
        section_base_prompt = SECTION_PROMPTS[section_id]
        if ctx_lines:
            brand = ctx_lines[0].replace("Client company: ", "") if ctx_lines else "the client"
            section_base_prompt = section_base_prompt.replace("[client company]", brand)

    top_section = section_id.split(".")[0]
    writing_standards = ""
    # Only fall back to generic writing standards when the user has given the
    # agent NOTHING to work with — no structure AND no custom prompt AND no
    # section template. Any sliver of user direction wins.
    if (top_section not in ("2", "3")
            and not section_base_prompt
            and not user_structure
            and not custom_prompt):
        writing_standards = (
            "\n\nWRITING STANDARDS:\n"
            "1. Write in professional Blueprint Document language.\n"
            "2. Reference the client company name throughout.\n"
            "3. Use precise supply chain terminology.\n"
            "4. Cover current state, solution, implementation, and outcomes."
        )

    if client_block and "CLIENT CONTEXT" in custom_prompt:
        client_block = ""

    eff_doc_kw = (
        body.doc_keywords[:body.doc_kw_top_n]
        if body.doc_kw_top_n and body.doc_kw_top_n < len(body.doc_keywords)
        else body.doc_keywords
    )
    if body.doc_kw_top_n:
        logger.info("generate_section %s: top_n=%d → using %d of %d doc keywords",
                    section_id, body.doc_kw_top_n, len(eff_doc_kw), len(body.doc_keywords))

    # Enforce the user's "Generation source" choice server-side so a forgotten
    # frontend filter can't slip a doc/web search through.
    gen_source = (body.gen_source or "both").lower()
    eff_web_kw = list(body.web_keywords or [])
    if gen_source == "doc":
        eff_web_kw = []
        logger.info("generate_section %s: gen_source=doc → web search suppressed", section_id)
    elif gen_source == "web":
        eff_doc_kw = []
        logger.info("generate_section %s: gen_source=web → doc search suppressed", section_id)

    # Read pinned IDs from keyword store (server-of-record); merge with any
    # IDs the client also sent (future-proof for batch generate paths).
    _kw_entry = _load_keyword_store(body.project_id).get(section_id, {})
    pinned_chunk_ids = list(dict.fromkeys(
        list(body.pinned_chunk_ids or []) + list(_kw_entry.get("pinned_chunk_ids", []))
    ))
    pinned_image_ids = list(dict.fromkeys(
        list(body.pinned_image_ids or []) + list(_kw_entry.get("pinned_image_ids", []))
    ))

    fetched_context, sources = fetch_sources(
        eff_web_kw, eff_doc_kw, body.project_id, section_id,
        pinned_chunk_ids=pinned_chunk_ids or None,
        pinned_image_ids=pinned_image_ids or None,
    )
    # Stamp the effective mode onto sources so the frontend can render the
    # right panes even when one side returns zero results.
    if isinstance(sources, dict):
        sources["gen_source"] = gen_source

    research_context = ""

    # GROUNDING & ACCURACY RULES (Fix D) -- always prepended.
    # Universal anti-fabrication guardrails applied to every section, on top of
    # any per-agent instructions. Prevents invented products/numbers/roles/
    # priorities and forbids empty sub-headings.
    research_context += (
        "=== GROUNDING & ACCURACY RULES (must always be followed) ===\n"
        "1. Use ONLY facts that appear in the RESEARCH CONTEXT / uploaded documents below.\n"
        "2. Do NOT invent or assume product names, tool names, system names, numbers, dates, "
        "quantities, site/warehouse counts, percentages, formulas, or person/role titles. If a "
        "specific fact is not present in the context, write 'TBD' instead of guessing.\n"
        "3. Never state a number unless it appears in the context. Do NOT average, reconcile, or "
        "round conflicting figures; use the source figure exactly, and stay consistent with numbers "
        "used elsewhere in the document.\n"
        "4. Do NOT generalise about scope or priority (e.g. 'all requirements are Must Have') unless "
        "the context explicitly states it. Preserve Must Have / Nice to Have distinctions exactly.\n"
        "5. Do NOT assign a person a role or title (e.g. 'data steward') unless the context says so.\n"
        "6. If you introduce a sub-heading (e.g. 'Current State'), you MUST write real sourced content "
        "beneath it. If you have no sourced content for it, OMIT the heading entirely. Never output a "
        "heading with nothing under it.\n"
        "=== END GROUNDING RULES ===\n\n"
    )

    # ── USER STRUCTURE — ALWAYS FIRST, NON-NEGOTIABLE ──────────────────
    if user_structure:
        research_context += (
            "=== MANDATORY OUTPUT FORMAT (highest priority — overrides everything) ===\n\n"
            "USER INSTRUCTION:\n"
            f"{user_structure}\n\n"
            "HOW TO APPLY THIS FORMAT:\n"
            "1. Count required items exactly as stated (e.g. '3-4 main points' = 3 or 4).\n"
            "2. For hierarchical bullet structures, use this exact markdown:\n"
            "   \u2022 **Main Point Title** (bold — summary heading of its sub-points)\n"
            "     - Sub-point one: Write 1-2 complete sentences of explanation.\n"
            "     - Sub-point two: Write 1-2 complete sentences of explanation.\n"
            "3. Every sub-point MUST have 1-2 full sentences — not just a label.\n"
            "4. Every main point MUST summarise its sub-points like a heading.\n"
            "5. Do NOT flatten the hierarchy. Do NOT collapse sub-points.\n"
            "6. Extract all content from uploaded documents — no hallucinations.\n\n"
            "EXAMPLE (for '2 main bullets, 2 sub-bullets each'):\n"
            "\u2022 **Planning Process Inefficiency**\n"
            "  - Manual file maintenance dominates planner time: teams spend hours updating "
            "Excel, introducing errors and slowing the S&OP cycle.\n"
            "  - Fragmented tools prevent integrated visibility: SAP ECC, APO, and BI operate "
            "in silos, preventing planners from accessing a unified supply chain view.\n"
            "\u2022 **Strategic Agility Gaps**\n"
            "  - What-if modelling is too slow: current tools require hours per scenario, "
            "limiting ability to respond quickly to disruptions.\n"
            "  - End-of-life risk creates urgency: SAP APO and ECC reach end-of-life in "
            "2027-2030, requiring immediate planning for replacement.\n\n"
            "=== END MANDATORY FORMAT ===\n\n"
        )

    # ── CLIENT CONTEXT ──────────────────────────────────────────────────
    if client_block:
        research_context += client_block + "\n\n"

    # ── USER INSTRUCTIONS — FIRST PRIORITY ─────────────────────────────
    # The user's prompt always takes precedence over the agent's section
    # template. If the user has any custom prompt text (whether they edited
    # the regenerated prompt or wrote their own), include it BEFORE the
    # agent's section instructions and mark it as overriding.
    if custom_prompt:
        research_context += (
            "=== USER INSTRUCTIONS (FIRST PRIORITY — follow these before any agent template) ===\n"
            f"{custom_prompt}\n"
            "=== END USER INSTRUCTIONS ===\n\n"
        )

    # ── AGENT SECTION INSTRUCTIONS — SECOND PRIORITY ───────────────────
    # Only the agent's SECTION_PROMPTS template runs when the user did NOT
    # supply their own prompt or structure. This preserves "user first,
    # agent second" ordering.
    if section_base_prompt:
        research_context += (
            "AGENT SECTION INSTRUCTIONS (apply only where they do not conflict with the user instructions above):\n"
            f"{section_base_prompt}\n\n"
        )

    # ── DOCUMENT CONTENT ────────────────────────────────────────────────
    if fetched_context:
        research_context += fetched_context + "\n\n"
    
    # ── SPECIAL: Complete sites data for Section 3.2 ─────────────────────
    # For Section 3.2, append the COMPLETE sites data so LLM has all sites
    if section_id == "3.2":
        
        if body.project_id:
            try:
                proj_chunks = _load_all_project_chunks(body.project_id)
                if proj_chunks:
                    complete_sites = _extract_complete_sites_data(proj_chunks)
                    if complete_sites:
                        research_context += (
                            "\n\n========== COMPLETE SITES DATA (Overview_sites.xlsx) ==========\n"
                            "CRITICAL: This is the COMPLETE sites list from Overview_sites.xlsx.\n"
                            "You MUST include ALL of these sites in the table.\n"
                            "Do NOT skip any sites. Do NOT limit the number of rows.\n\n"
                            f"{complete_sites}\n"
                            "========== END COMPLETE SITES DATA ==========\n\n"
                        )
            except Exception as e:
                logger.warning("Failed to load complete sites data for Section 3.2: %s", e)

    research_context += (
        f"Doc search keywords used: {', '.join(body.doc_keywords)}\n"
        f"Web search keywords used: {', '.join(body.web_keywords)}\n"
    )
    if writing_standards:
        research_context += writing_standards

    # ── Inject relevant images into research context ─────────────────
    try:
        from app.image_injection import append_images_to_research_context
    except ImportError:
        try:
            from image_injection import append_images_to_research_context
        except ImportError:
            append_images_to_research_context = None

    if append_images_to_research_context:
        try:
            # Section-specific image search keywords — covers all BRD sections.
            # The query is combined with the user's own doc/web keywords so even
            # bespoke section names surface the right images.
            _SECTION_IMG_KEYWORDS = {
                # ── Section 1: Introduction ──────────────────────────────────
                "1":   "introduction company overview background project scope objectives",
                "1.1": "company profile corporate overview organisation background",
                "1.2": "current state business process challenges pain points as-is",
                "1.3": "purpose objectives blueprint document scope",
                "1.4": "project scope phase timeline boundaries",
                "1.5": "glossary acronyms definitions terminology abbreviations",
                # ── Section 2: Benefit Realization ───────────────────────────
                "2":   "benefit realization KPI ROI quantitative qualitative value drivers business case",
                "2.1": "quantitative benefits ROI cost saving financial metrics business case",
                "2.2": "qualitative benefits strategic value operational improvement",
                "2.3": "KPI key performance indicator metrics measurement dashboard",
                "2.4": "benefit realization plan timeline milestones tracking",
                # ── Section 3: Supply Chain Scope ────────────────────────────
                "3":   "supply chain scope network overview capabilities process",
                "3.1": "map location plant production facility geography site distribution network",
                "3.2": "sites table warehouse facility DC distribution center location",
                "3.3": "demand planning forecast variability scenario",
                "3.4": "supply planning lead time safety stock sourcing",
                "3.5": "inventory min max ABC analysis obsolete stock",
                "3.6": "constraints capacity transportation logistics",
                "3.7": "scenario simulation plan forecast what-if",
                # ── Section 4: Demand Planning ───────────────────────────────
                "4":   "demand planning forecasting demand review process methodology",
                "4.1": "demand review process consensus demand S&OP cycle unconstrained forecast",
                "4.2": "statistical forecast model algorithm baseline history",
                "4.3": "forecast consumption net-off demand signal override",
                "4.4": "demand management promotion event new product launch",
                "4.5": "demand test cases validation scenario demand planning",
                # ── Section 5: Supply & Inventory Planning ───────────────────
                "5":   "supply planning inventory management replenishment safety stock",
                "5.1": "supply review process constrained plan capacity supply order",
                "5.2": "inventory policy safety stock min max reorder point buffer",
                "5.3": "replenishment sourcing supplier procurement purchase order",
                "5.4": "supply exceptions alerts shortage overstock capacity breach",
                "5.5": "supply test cases validation scenario inventory planning",
                # ── Section 6: Data Integration ──────────────────────────────
                "6":   "data integration architecture ETL interface data flow system",
                "6.1": "data sources ERP SAP system of record master data",
                "6.2": "inbound interface data file feed integration ETL",
                "6.3": "outbound interface data extract output downstream",
                "6.4": "data frequency schedule batch real-time refresh",
                "6.5": "architecture diagram middleware integration platform",
            }
            # Fall back to parent section keywords when a sub-section isn't listed
            section_img_kw = (
                _SECTION_IMG_KEYWORDS.get(section_id)
                or _SECTION_IMG_KEYWORDS.get(top, "")
            )
            user_kw = " ".join(body.doc_keywords + body.web_keywords)
            img_query = f"{section_img_kw} {user_kw}".strip()
            research_context, _ = append_images_to_research_context(
                research_context,
                query=img_query,
                section_hint=section_id,
                section_id=top,       # hard filter on BRD top-level section
                top_k=3,
                project_id=body.project_id,
            )
        except Exception as _img_err:
            logger.warning("Image injection failed for section %s: %s", section_id, _img_err)

    # ── Cross-section consistency context ─────────────────────────────────────
    # Inject approved outputs from related sections so the LLM maintains
    # consistent terminology, entity names, and scope across the document.
    _SECTION_RELATIONS = {
        "1": ["2"],
        "2": ["1", "3"],
        "3": ["4", "5"],
        "4": ["3", "5"],
        "5": ["3", "4"],
        "6": ["3"],
    }
    _SECTION_TITLES_XCTX = {
        "1": "Introduction",
        "2": "Benefit Realization",
        "3": "Supply Chain Scope",
        "4": "Demand Planning",
        "5": "Supply & Inventory Planning",
        "6": "Data Integration",
    }
    _related = _SECTION_RELATIONS.get(top, [])
    if _related and body.project_id:
        try:
            _ap = _load_approved_store(body.project_id)
            _ap_secs = _ap.get("sections", {})
            if _ap_secs:
                _cross_parts = []
                for _rel in _related:
                    _rel_entries = {
                        sid: content
                        for sid, content in _ap_secs.items()
                        if sid.split(".")[0] == _rel and content
                    }
                    if _rel_entries:
                        _rel_title = _SECTION_TITLES_XCTX.get(_rel, f"Section {_rel}")
                        _entries_text = "\n\n".join(
                            f"[Section {sid}]\n{content[:500].strip()}..."
                            for sid, content in sorted(_rel_entries.items())
                        )
                        _cross_parts.append(f"### {_rel_title}\n{_entries_text}")
                if _cross_parts:
                    research_context += (
                        "\n\n=== ALREADY-APPROVED SECTIONS (maintain consistency) ===\n"
                        "These sections have been reviewed and approved. Your output MUST "
                        "be consistent with the terminology, entity names, and scope below. "
                        "Do NOT contradict or duplicate content from these sections.\n\n"
                        + "\n\n---\n\n".join(_cross_parts)
                        + "\n\n=== END CONSISTENCY CONTEXT ===\n\n"
                    )
                    logger.info(
                        "generate_section %s: injected cross-section context from %s",
                        section_id, _related,
                    )
        except Exception as _cx_err:
            logger.warning("Cross-section context injection failed: %s", _cx_err)

    # System directive prepended when the user has given ANY instructions —
    # either a custom prompt or a structure override. Both are treated as
    # absolute and the EXCLUSIVITY rules apply equally to both.
    if user_structure or custom_prompt:
        _kind = "STRUCTURE" if user_structure else "CUSTOM PROMPT"
        research_context = (
            f"CRITICAL INSTRUCTION — SECTION {section_id} ONLY:\n"
            f"Generate ONLY section {section_id}. Do NOT generate any other sections.\n\n"
            f"The USER {_kind} in the context below is ABSOLUTE — it is the entire spec for\n"
            "what this section's output must look like. Treat every other block in the context\n"
            "(document content, research notes, web research, cross-section context, agent\n"
            "templates) as SOURCE MATERIAL ONLY. Do not echo, summarise, paraphrase, or invent\n"
            "anything the user did not explicitly request.\n\n"
            "EXCLUSIVITY — if the user's instruction mentions 'only', 'just', 'no <something>',\n"
            "'include only', or names a single component (image only / table only / bullets\n"
            "only / heading only / list only / one paragraph only / N sentences only), output\n"
            "EXACTLY that — nothing else:\n"
            "  - 'image only' / 'insert image' → emit only the <IMAGE id=\"…\"/> tag(s) the\n"
            "    user pinned (one tag per image, each on its own line). No intro sentence, no\n"
            "    figure description, no caption unless the user asked for one, no surrounding\n"
            "    prose. No fetched document content.\n"
            "  - 'table only' → emit only the Markdown table. No intro sentence, no closing\n"
            "    sentence, no source citation unless requested.\n"
            "  - 'bullets only' / 'bullet points only' → emit only a bullet list. No intro\n"
            "    paragraph, no closing summary.\n"
            "  - 'N sentences' / 'one paragraph' → match the count exactly.\n"
            "  - 'heading only' → emit only the section heading.\n\n"
            "COMPONENT MATCHING:\n"
            "  - Preserve the EXACT ORDER and number of components the user listed.\n"
            "  - For tables: use the exact column NAMES and column ORDER the user specified.\n"
            "    Do not add, drop, rename, or reorder columns.\n"
            "  - For lists: keep the requested number of bullets / sub-bullets.\n"
            "  - Do NOT add headings, bullets, paragraphs, tables, source/citation lines,\n"
            "    figure captions, or image references the user did not explicitly request.\n"
            "  - Do NOT remove a component the user requested. If the source documents lack\n"
            "    the data, still emit the component with a clear\n"
            "    '<information not available in source documents>' placeholder so the\n"
            "    requested structure stays intact.\n\n"
            "WHEN IN DOUBT, OUTPUT LESS, NOT MORE. It is better to emit only what the user\n"
            "asked for than to pad the section with material from the source documents.\n\n"
        ) + research_context

    def _call_with_retry(fn, *args, max_retries=3):
        for attempt in range(max_retries):
            try:
                return fn(*args)
            except Exception as e:
                err = str(e)
                if "429" in err or "rate_limit" in err.lower() or "too many requests" in err.lower():
                    wait = (2 ** attempt) * 5
                    logger.warning("Rate limit hit (attempt %d/%d) — waiting %ds", attempt+1, max_retries, wait)
                    import time; time.sleep(wait)
                    if attempt == max_retries - 1: raise
                else:
                    raise
        return fn(*args)

    # The per-top-level specialized agents (run_introagent, run_bragent, …)
    # carry their OWN rigid output templates and ignore user-provided
    # structure / prompt / pinned-image instructions. So whenever the user
    # has actually told us what they want — a custom prompt, a structure
    # override, or pinned images — we BYPASS those agents and use the generic
    # writer that treats `research_context` (which already contains the
    # CRITICAL INSTRUCTION + USER INSTRUCTIONS + pinned-image embed tags) as
    # authoritative. This is what makes "insert image X and its name" or
    # "image only" actually be obeyed.
    _user_directed = bool(custom_prompt or user_structure or pinned_image_ids)

    # Stamp the target section_id into research_context so the specialized agent
    # runners can detect single-section mode via TARGET_SECTION marker and generate
    # ONLY this section — not all sub-sections at once. Without this, they would
    # ask Claude for e.g. "sections 1, 1.1, 1.2, 1.3, 1.4, 1.5" in a single
    # 3000-token call, causing output truncation and empty content errors.
    _stamped_context = f"TARGET_SECTION: {section_id}\n\n" + research_context

    try:
        if _user_directed:
            sections = None  # fall through to the generic user-directed writer
        elif top == "1":
            from agents.brd_agent.run_introagent import generate_intro_sections_from_qa
            sections = _call_with_retry(generate_intro_sections_from_qa, [], _stamped_context)
        elif top == "2":
            from agents.brd_agent.run_bragent import generate_br_sections_from_qa
            sections = _call_with_retry(generate_br_sections_from_qa, [], _stamped_context)
        elif top == "3":
            from agents.brd_agent.run_sscagent import generate_ssc_sections_from_qa
            sections = _call_with_retry(generate_ssc_sections_from_qa, [], _stamped_context)
        elif top == "4":
            from agents.brd_agent.run_dpagent import generate_dp_sections_from_qa
            sections = generate_dp_sections_from_qa([], _stamped_context)
        elif top == "5":
            from agents.brd_agent.run_ipagent import generate_ip_sections_from_qa
            sections = generate_ip_sections_from_qa([], _stamped_context)
        elif top == "6":
            from agents.brd_agent.run_diagent import generate_di_sections_from_qa
            sections = generate_di_sections_from_qa([], _stamped_context)

        # Fallback: if the specialized agent returned empty or didn't include the
        # requested section (e.g. due to max_tokens truncation before all </SECTION>
        # closing tags were written), fall through to the direct writer which
        # generates exactly ONE section per call with sufficient token budget.
        if sections is not None and (not sections or section_id not in sections):
            logger.warning(
                "generate_section %s: specialized agent returned no content for this "
                "section (keys=%s) — falling through to direct writer.",
                section_id, list(sections.keys()) if sections else "{}",
            )
            sections = None

        if _user_directed or sections is None:
            try:
                from app.claude_provider import completion_from_prompt as _cp_gen
            except ImportError:
                from claude_provider import completion_from_prompt as _cp_gen
            sec_title = next(
                (s["title"] for s in _get_live_sections(body.project_id) if s["id"] == section_id),
                section_id
            )
            # Pinned-image directive: list the exact tags the writer must emit
            # so "insert image X and its name" is honoured verbatim.
            _img_directive = ""
            if pinned_image_ids:
                _img_directive = (
                    "\n\nPINNED IMAGES — MANDATORY:\n"
                    "Insert each of the following images at the appropriate place by writing the\n"
                    "exact tag on its own line:\n"
                    + "\n".join(f'<IMAGE id="{iid}"/>' for iid in pinned_image_ids)
                    + "\n\nRULES:\n"
                    " - If you reference an image with a caption like '*Figure 1: …*', the\n"
                    "   <IMAGE id=\"…\"/> tag for that image MUST appear on the line\n"
                    "   immediately above the caption. Never write a caption without the tag.\n"
                    " - Use every pinned id ABOVE — once each, in order — somewhere inside\n"
                    "   THIS section. Do not skip any. Do not place them outside this section.\n"
                    " - Do NOT invent extra figure captions for images that are not pinned.\n"
                )
            _prompt = (
                "You are a senior Blueprint Document writer for enterprise solution implementations.\n"
                "The USER INSTRUCTIONS in the context below are ABSOLUTE and override any default\n"
                "writing behaviour. If the user asks only for an image and its name, output only\n"
                "that — do not add generated prose, headings, or document content the user did\n"
                "not request.\n"
                "TABLE FORMAT: output every table in GitHub-flavored Markdown pipe syntax\n"
                "(| Col A | Col B |\\n| --- | --- |\\n| v1 | v2 |). NEVER output HTML <table> markup.\n"
                "Include ONLY rows that exist in the source content — do NOT pad tables with\n"
                "empty or '<information not available…>' filler rows.\n"
                "If the user's structure (or the source) names a table, print that exact name\n"
                "as a bold line on its own (e.g. **Demand/Supply Planning Attributes**)\n"
                "immediately ABOVE its table. When the user asks for SEPARATE tables, emit each\n"
                "as its own Markdown table with its own header row, separated by a blank line —\n"
                "never merge them into one table even if they share column names. Use the EXACT\n"
                "column names the user specified.\n\n"
                "BULLET LISTS: use Markdown bullets with '- ' (hyphen + space). If the user asks\n"
                "for a HIERARCHY (main point + sub-points / explanations / examples), nest the\n"
                "sub-bullets by INDENTING THEM WITH TWO SPACES before the '-'. Example:\n"
                "  - Main point\n"
                "    - Explanation 1\n"
                "    - Explanation 2\n"
                "  - Next main point\n"
                "    - Explanation 1\n"
                "Use the exact count of main points and sub-points the user requested.\n\n"
                "EMPHASIS / BOLD: when you want a word or phrase emphasised, wrap it in **two\n"
                "asterisks** OR rely on a heading style. The renderer turns **like this** into\n"
                "actual bold text — readers will NEVER see the literal asterisks. If you do not\n"
                "want emphasis, write the word plainly with no asterisks at all. Do NOT leave\n"
                "stray single asterisks (`*foo`) or mismatched `**foo bar` in the output.\n\n"
                f"Generate Section {section_id}: {sec_title}\n\n"
                f"{research_context}\n"
                f"{_img_directive}\n\n"
                f"Output inside <SECTION id=\"{section_id}\">...</SECTION> tags only."
            )
            raw = _cp_gen(_prompt, max_tokens=16000, temperature=0.2).strip()
            import re as _re2
            _m = _re2.search(
                rf'<SECTION\s+id="{re.escape(section_id)}">(.*?)</SECTION>',
                raw, _re2.DOTALL | _re2.IGNORECASE
            )
            sections = {section_id: _m.group(1).strip() if _m else raw}

        if section_id in sections:
            content = sections[section_id]
        else:
            content = "\n\n".join(
                f"[{sid}]\n{text}" for sid, text in sections.items()
            )

        # Sanitize image references BEFORE the safety net. The rigid agents
        # (and occasionally the LLM) invent markdown image tags like
        # ![caption](some_made_up_name.png) that point at files which do not
        # exist, so they never render. Convert any reference that maps to a
        # REAL indexed image into a proper <IMAGE id="..."/> tag and DELETE
        # the hallucinated ones (along with their italic *Figure N: …* caption
        # line) so the output never shows broken/fake image placeholders.
        # Bind pinned images to caption placeholders FIRST so the sanitizer's
        # orphan-figure-caption pass below sees a proper <IMAGE …/> tag
        # immediately above each caption and keeps them both. (If we sanitize
        # first, every caption gets dropped because it has no image above it
        # yet — then there's nothing to bind to and the pinned tags end up
        # appended at the end of the section by the safety net.)
        if pinned_image_ids:
            content = _bind_pinned_images_to_captions(content, pinned_image_ids)

        content = _sanitize_image_refs(content, pinned_image_ids)
        content = _clean_generated_tables(content)
        content = _strip_html_content(content)
        content = _strip_redundant_section_headers(content, section_id)
        try:
            _valid_ids = {x.get("id") for x in _get_live_sections(body.project_id) if x.get("id")}
            content = _strip_invented_headings(content, section_id, _valid_ids)
        except Exception as _se:
            logger.warning("strip invented headings failed for %s: %s", section_id, _se)
        try:
            content = _strip_empty_headings(content)   # Fix B: drop empty sub-headings
        except Exception as _ee:
            logger.warning("strip empty headings failed for %s: %s", section_id, _ee)

        # Safety net: any pinned image the LLM didn't place gets appended so
        # it is never silently dropped from the Word export.
        if pinned_image_ids:
            placed = set(re.findall(r'<IMAGE id="([^"]+)"/>', content))
            missing = [iid for iid in pinned_image_ids if iid not in placed]
            if missing:
                tail = "\n\n".join(f'<IMAGE id="{iid}"/>' for iid in missing)
                content = content.rstrip() + "\n\n" + tail
                logger.info(
                    "generate_section: appended %d unplaced pinned image(s) to section %s",
                    len(missing), section_id,
                )

        store = _load_keyword_store(body.project_id)
        entry = store.get(section_id, {})
        entry["status"] = "generated"
        store[section_id] = entry
        _save_keyword_store(store, body.project_id)

        src_store = _load_sources_store(body.project_id)
        src_store[section_id] = sources
        _save_sources_store(src_store, body.project_id)

        return {"content": content, "sources": sources}

    except Exception as e:
        logger.exception("Generation failed for section %s", section_id)
        # Return HTTP 500 with a structured error body — NOT a 200 with error
        # text embedded in "content".  Returning the error as content caused it
        # to be silently saved to the approved store and exported into the DOCX,
        # and prevented the frontend retry mechanism from kicking in.
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(
            status_code=500,
            detail=f"Generation failed for section {section_id}: {str(e)}",
        )

@router.get("/api/sections/sources")
def get_all_sources(project_id: str = None):
    """Return all persisted section sources keyed by section_id."""
    return _load_sources_store(project_id)


@router.post("/api/sections/{section_id}/sources")
def save_section_sources(section_id: str, body: dict):
    """Persist sources for a single section (called client-side after generation)."""
    pid = body.pop("project_id", None)
    src_store = _load_sources_store(pid)
    src_store[section_id] = body
    _save_sources_store(src_store, pid)
    return {"ok": True}


# ─────────────────────────────────────────────────────────────────────────────
# SECTION MANAGEMENT — add / edit / delete / restore / reorder / auto-keywords
# ─────────────────────────────────────────────────────────────────────────────

class SectionAddRequest(BaseModel):
    id:            str
    title:         str
    insert_after:  Optional[str] = None
    project_id:    Optional[str] = None

class SectionEditRequest(BaseModel):
    new_id:     Optional[str] = None
    new_title:  Optional[str] = None
    project_id: Optional[str] = None

class SectionReorderRequest(BaseModel):
    ordered_ids: List[str]
    project_id:  Optional[str] = None


@router.post("/api/sections/add")
def add_section(body: SectionAddRequest):
    """Add a new section or subsection. Inserts after insert_after if given."""
    pid      = body.project_id
    active   = _get_live_sections(pid)
    archived = _get_archived_sections(pid)

    # Only ACTIVE sections block an add. If the id merely lingers in the
    # archive (e.g. left over from a template replacement or a deleted
    # section), drop the archived copy so the user can create it fresh.
    if body.id in {s["id"] for s in active}:
        return {"ok": False, "error": f"Section ID '{body.id}' already exists."}
    if any(s["id"] == body.id for s in archived):
        archived = [s for s in archived if s["id"] != body.id]
        logger.info("Section add: purged archived duplicate of %s", body.id)

    new_sec = {"id": body.id, "title": body.title}

    if body.insert_after:
        idx = next((i for i, s in enumerate(active) if s["id"] == body.insert_after), None)
        if idx is not None:
            active.insert(idx + 1, new_sec)
        else:
            active.append(new_sec)
    else:
        active.append(new_sec)

    _persist_sections(active, archived, pid)

    # Seed kw_store entry as user_edited=True so enrich() won't auto-generate
    # keywords for a freshly-authored subsection. The user is in control until
    # they explicitly request enrichment.
    kw_store = _load_keyword_store(pid)
    if body.id not in kw_store:
        kw_store[body.id] = {
            "doc_keywords": [],
            "web_keywords": [],
            "user_edited": True,
        }
        _save_keyword_store(kw_store, pid)

    logger.info("Section added: %s - %s (project=%s)", body.id, body.title, pid)
    return {"ok": True, "section": new_sec}


@router.put("/api/sections/{section_id}/edit")
def edit_section(section_id: str, body: SectionEditRequest):
    """Rename a section title and/or change its ID."""
    pid      = body.project_id
    active   = _get_live_sections(pid)
    archived = _get_archived_sections(pid)

    idx = next((i for i, s in enumerate(active) if s["id"] == section_id), None)
    if idx is None:
        return {"ok": False, "error": f"Section '{section_id}' not found."}

    old_id    = active[idx]["id"]
    old_title = active[idx]["title"]
    new_id    = body.new_id    or old_id
    new_title = body.new_title or old_title

    if new_id != old_id:
        existing = {s["id"] for s in active} | {s["id"] for s in archived}
        if new_id in existing:
            return {"ok": False, "error": f"Section ID '{new_id}' already exists."}

        kw_store = _load_keyword_store(pid)
        if old_id in kw_store:
            kw_store[new_id] = kw_store.pop(old_id)
            _save_keyword_store(kw_store, pid)

        ap_store = _load_approved_store(pid)
        sections = ap_store.get("sections", {})
        if old_id in sections:
            sections[new_id] = sections.pop(old_id)
            ap_store["sections"] = sections
            _save_approved_store(ap_store, pid)

    active[idx] = {"id": new_id, "title": new_title}
    _persist_sections(active, archived, pid)
    logger.info("Section edited: %s -> %s (%s) (project=%s)", old_id, new_id, new_title, pid)
    return {"ok": True, "old_id": old_id, "section": active[idx]}


@router.delete("/api/sections/{section_id}/cascade")
def delete_section_cascade(section_id: str, project_id: Optional[str] = None):
    """Archive a section AND all its subsections in one click."""
    active   = _get_live_sections(project_id)
    archived = _get_archived_sections(project_id)
    prefix   = section_id + "."
    to_archive = [s for s in active if s["id"] == section_id or s["id"].startswith(prefix)]
    if not to_archive:
        already = [s["id"] for s in archived if s["id"] == section_id or s["id"].startswith(prefix)]
        if already:
            return {"ok": True, "archived": already, "note": "already archived"}
        return {"ok": False, "error": f"Section '{section_id}' not found in active sections."}
    archived_ids = []
    for sec in to_archive:
        idx = next((i for i, s in enumerate(active) if s["id"] == sec["id"]), None)
        if idx is not None:
            archived.append(active.pop(idx)); archived_ids.append(sec["id"])
    _persist_sections(active, archived, project_id)
    # Drop approved content + render flags for ALL archived ids so none of them
    # appear in the export (and the update-export self-heal won't restore them).
    try:
        ap = _load_approved_store(project_id)
        secs = ap.get("sections") or {}
        ho = set(ap.get("heading_only_ids", [])); nh = set(ap.get("no_heading_ids", []))
        changed = False
        for aid in archived_ids:
            if secs.pop(aid, None) is not None: changed = True
            if aid in ho: ho.discard(aid); changed = True
            if aid in nh: nh.discard(aid); changed = True
        if changed:
            ap["sections"] = secs
            ap["heading_only_ids"] = sorted(ho); ap["no_heading_ids"] = sorted(nh)
            _save_approved_store(ap, project_id)
            _sync_screen3_state_from_approved(project_id)
    except Exception as _e:
        logger.warning("cascade delete: could not drop approved content: %s", _e)
    logger.info("Cascade archived %d sections: %s (project=%s)", len(archived_ids), archived_ids, project_id)
    return {"ok": True, "archived": archived_ids}


@router.delete("/api/sections/{section_id}")
def delete_section(section_id: str, project_id: Optional[str] = None):
    """Move a section to the archive (soft delete)."""
    active   = _get_live_sections(project_id)
    archived = _get_archived_sections(project_id)

    idx = next((i for i, s in enumerate(active) if s["id"] == section_id), None)
    if idx is None:
        return {"ok": False, "error": f"Section '{section_id}' not found in active list."}

    sec = active.pop(idx)
    archived.append(sec)
    _persist_sections(active, archived, project_id)
    # Drop its approved content + render flags so it is excluded from any export
    # (and so the update-export self-heal does not resurrect imported content).
    try:
        ap = _load_approved_store(project_id)
        changed = False
        if (ap.get("sections") or {}).pop(section_id, None) is not None:
            changed = True
        ho = [i for i in ap.get("heading_only_ids", []) if i != section_id]
        nh = [i for i in ap.get("no_heading_ids", []) if i != section_id]
        if ho != ap.get("heading_only_ids", []) or nh != ap.get("no_heading_ids", []):
            ap["heading_only_ids"] = ho; ap["no_heading_ids"] = nh; changed = True
        if changed:
            _save_approved_store(ap, project_id)
            # Refresh the in-memory export state so the deleted section is gone
            # from BOTH the approved store and the state the exporter reads.
            _sync_screen3_state_from_approved(project_id)
    except Exception as _e:
        logger.warning("delete_section: could not drop approved content for %s: %s", section_id, _e)
    logger.info("Section archived: %s (project=%s)", section_id, project_id)
    return {"ok": True, "archived": sec}


@router.post("/api/sections/{section_id}/unlock")
def unlock_section(section_id: str, body: dict):
    """Unlock an imported section for regeneration: clear its imported content and
    flags but KEEP it in the live template, so it becomes a normal generatable
    (unlocked) section the user can select and generate fresh."""
    pid = (body or {}).get("project_id")
    changed = False
    try:
        ap = _load_approved_store(pid)
        secs = ap.get("sections") or {}
        if secs.pop(section_id, None) is not None:
            changed = True
        ho = [i for i in ap.get("heading_only_ids", []) if i != section_id]
        nh = [i for i in ap.get("no_heading_ids", []) if i != section_id]
        if ho != ap.get("heading_only_ids", []) or nh != ap.get("no_heading_ids", []):
            changed = True
        ap["sections"] = secs
        ap["heading_only_ids"] = ho
        ap["no_heading_ids"] = nh
        # Mark this section as removed-from-import so the update-export self-heal
        # does NOT pull it back from the original uploaded .docx. It only returns
        # to the document if the user ticks it and regenerates fresh content.
        _rem = list(ap.get("removed_imports", []))
        if section_id not in _rem:
            _rem.append(section_id); changed = True
        ap["removed_imports"] = _rem
        if changed:
            _save_approved_store(ap, pid)
            _sync_screen3_state_from_approved(pid)
    except Exception as e:
        logger.warning("unlock_section: could not clear approved content for %s: %s", section_id, e)
    try:
        kw = _load_keyword_store(pid)
        entry = kw.get(section_id, {})
        entry.pop("imported_at", None)
        entry.pop("output", None)
        entry["status"] = "not-started"
        kw[section_id] = entry
        _save_keyword_store(kw, pid)
    except Exception as e:
        logger.warning("unlock_section: could not reset keyword entry for %s: %s", section_id, e)
    logger.info("Section unlocked for regeneration: %s (project=%s)", section_id, pid)
    return {"ok": True, "section_id": section_id}


# ─────────────────────────────────────────────────────────────────────────────
# PRE-EXPORT QUALITY CHECK
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/projects/{project_id}/quality-check")
def quality_check(project_id: str):
    """
    Run a pre-export quality check on all approved section outputs.

    Returns a list of findings (errors, warnings, info) per section and an
    overall readiness verdict: "ready", "warnings", or "incomplete".
    """
    import re as _re

    approved  = _load_approved_store(project_id)
    sections  = _get_live_sections(project_id)
    ap_secs   = approved.get("sections", {})

    # ── Pass 1: Local checks (fast, no LLM) ─────────────────────────────────
    local_findings: List[Dict] = []
    generated_ids: set = set()

    for sec in sections:
        sid   = sec["id"]
        title = sec.get("title", sid)
        raw   = ap_secs.get(sid, "")
        # approved store values are plain strings (or dicts with "output" key in legacy format)
        output = (raw if isinstance(raw, str) else (raw or {}).get("output", "")).strip()

        if not output:
            local_findings.append({
                "section_id": sid, "title": title,
                "severity": "error",
                "message": "Section has no generated content — not ready for export.",
            })
            continue

        generated_ids.add(sid)

        # Too short — likely a stub
        if len(output) < 80:
            local_findings.append({
                "section_id": sid, "title": title,
                "severity": "warning",
                "message": f"Content is very short ({len(output)} chars) — may be incomplete.",
            })

        # Unresolved IMAGE tags — image_id not resolved during export will be skipped
        unresolved = _re.findall(r'<IMAGE id="([^"]+)"/>', output)
        if unresolved:
            local_findings.append({
                "section_id": sid, "title": title,
                "severity": "info",
                "message": f"{len(unresolved)} embedded image tag(s) present — verify images are accessible before export.",
            })

        # Placeholder text not replaced
        placeholders = _re.findall(
            r'\[(?:CLIENT|COMPANY|DATE|TBD|TODO|PLACEHOLDER|INSERT|ADD HERE)[^\]]*\]',
            output, flags=_re.IGNORECASE
        )
        if placeholders:
            local_findings.append({
                "section_id": sid, "title": title,
                "severity": "error",
                "message": f"Unfilled placeholder(s) found: {', '.join(placeholders[:3])}",
            })

        # Generic "Client Name" or "Company Name" not replaced
        if _re.search(r'\b(?:client name|company name|your company)\b', output, _re.IGNORECASE):
            local_findings.append({
                "section_id": sid, "title": title,
                "severity": "warning",
                "message": "Generic 'Client Name' / 'Company Name' text detected — should be replaced with actual client.",
            })

    total_sections  = len(sections)
    generated_count = len(generated_ids)
    missing_count   = total_sections - generated_count

    # ── Pass 2: LLM review of approved content ───────────────────────────────
    llm_findings: List[Dict] = []
    if generated_ids:
        try:
            # Build a digest — first 400 chars of each generated section
            digest_parts = []
            for sec in sections:
                sid   = sec["id"]
                title = sec.get("title", sid)
                raw   = ap_secs.get(sid, "")
                output = (raw if isinstance(raw, str) else (raw or {}).get("output", "")).strip()
                if output:
                    digest_parts.append(
                        f"[{sid}] {title}\n{output[:400]}"
                    )

            digest = "\n\n---\n\n".join(digest_parts[:30])  # cap at 30 sections

            llm_prompt = f"""You are a senior Kinaxis Blueprint Document reviewer. Review the following section excerpts from a client Blueprint Document (BRD) and identify quality issues.

CHECK FOR:
1. Inconsistencies across sections — e.g. different site names, conflicting scope statements, mismatched terminology
2. Generic / template language not customised to the client — e.g. "the client", "your company", "TBD", "to be determined"
3. Sections that seem to describe a different client or use wrong company names
4. Content that appears to be boilerplate copy-pasted without client customisation
5. Section content that is off-topic or doesn't match the section heading

SECTION EXCERPTS:
{digest}

OUTPUT: Return ONLY a JSON array of findings. Each finding:
{{"section_id": "...", "severity": "error"|"warning"|"info", "message": "..."}}

If no issues found, return [].
No explanation, no markdown — pure JSON array only."""

            try:
                from app.claude_provider import completion_from_prompt as _claude_qc
            except ImportError:
                from claude_provider import completion_from_prompt as _claude_qc
            raw = _claude_qc(llm_prompt, max_tokens=4000, temperature=0.0)
            raw = raw.replace("```json", "").replace("```", "").strip()
            llm_findings = json.loads(raw)
            if not isinstance(llm_findings, list):
                llm_findings = []
            logger.info("Quality check LLM: %d findings", len(llm_findings))
        except Exception as e:
            logger.warning("Quality check LLM step failed: %s", e)
            llm_findings = [{"section_id": None, "severity": "info",
                             "message": f"LLM review step could not run: {e}"}]

    all_findings = local_findings + llm_findings

    # ── Verdict ──────────────────────────────────────────────────────────────
    has_errors   = any(f["severity"] == "error"   for f in all_findings)
    has_warnings = any(f["severity"] == "warning" for f in all_findings)

    if missing_count > 0 or has_errors:
        verdict = "incomplete"
    elif has_warnings:
        verdict = "warnings"
    else:
        verdict = "ready"

    return {
        "verdict":           verdict,
        "total_sections":    total_sections,
        "generated_sections": generated_count,
        "missing_sections":  missing_count,
        "findings":          all_findings,
        "finding_counts": {
            "error":   sum(1 for f in all_findings if f["severity"] == "error"),
            "warning": sum(1 for f in all_findings if f["severity"] == "warning"),
            "info":    sum(1 for f in all_findings if f["severity"] == "info"),
        },
    }


@router.post("/api/sections/{section_id}/restore")
def restore_section(section_id: str, body: dict):
    """Restore an archived section back into the active list."""
    pid      = body.get("project_id")
    active   = _get_live_sections(pid)
    archived = _get_archived_sections(pid)

    idx = next((i for i, s in enumerate(archived) if s["id"] == section_id), None)
    if idx is None:
        return {"ok": False, "error": f"Section '{section_id}' not found in archive."}

    sec = archived.pop(idx)
    insert_after = body.get("insert_after")
    if insert_after:
        pos = next((i for i, s in enumerate(active) if s["id"] == insert_after), None)
        active.insert((pos + 1) if pos is not None else len(active), sec)
    else:
        active.append(sec)

    _persist_sections(active, archived, pid)
    logger.info("Section restored: %s (project=%s)", section_id, pid)
    return {"ok": True, "restored": sec}


@router.post("/api/sections/reorder")
def reorder_sections(body: SectionReorderRequest):
    """Reorder the active section list to match the given ordered_ids."""
    pid      = body.project_id
    active   = _get_live_sections(pid)
    archived = _get_archived_sections(pid)

    sec_map = {s["id"]: s for s in active}
    reordered = [sec_map[sid] for sid in body.ordered_ids if sid in sec_map]
    mentioned = set(body.ordered_ids)
    for s in active:
        if s["id"] not in mentioned:
            reordered.append(s)

    _persist_sections(reordered, archived, pid)
    logger.info("Sections reordered: %d (project=%s)", len(reordered), pid)
    return {"ok": True, "count": len(reordered)}


@router.post("/api/sections/{section_id}/auto-keywords")
def auto_keywords(section_id: str, body: dict):
    """
    Auto-generate doc search keywords for a section from uploaded project chunks.
    
    Priority:
    1. If user has manually selected keywords (user_edited=True), MERGE new
       auto-generated keywords WITH existing ones (don't wipe user selections).
    2. Use LLM best_keywords if available from cached mapping.
    3. Fall back to hint-scoring + title-token matching.
    4. Last resort: global chunk keyword pool.
    
    Web keywords: built from section title + client company name.
    """
    import re as _re

    section_title = body.get("title", section_id)
    parent_title  = body.get("parent_title", "")
    project_id    = body.get("project_id")

    STOP = {"the","a","an","and","or","of","in","to","for","is","are","was",
            "be","by","with","on","at","this","that","from","as","it","its",
            "were","has","have","had","will","can","not","also","their","which",
            "when","what","how","been","being"}

    doc_keywords: list = []
    doc_kw_warning = ""

    if not project_id:
        doc_kw_warning = "No project selected — cannot extract document keywords."
        logger.warning("auto_keywords: no project_id for section %s", section_id)
    else:
        try:
            proj_chunks = _load_all_project_chunks(project_id)
            logger.info("auto_keywords: loaded %d chunks for project %s section %s",
                        len(proj_chunks), project_id, section_id)

            if not proj_chunks:
                doc_kw_warning = (
                    "No document chunks found for this project. "
                    "Please upload documents and trigger chunking first."
                )
            else:
                all_sections = _get_live_sections(project_id) + _get_archived_sections(project_id)

                # ── Tier 1: Use LLM best_keywords from cached mapping ──────────
                best_kws: list = []
                cached_mapping = _llm_match_cache.get(project_id, {})
                if cached_mapping:
                    best_kws = cached_mapping.get("best_keywords", {}).get(section_id, [])
                    if best_kws:
                        seen: set = set()
                        for kw in best_kws:
                            kw = kw.strip()
                            if kw and len(kw) > 3 and kw.lower() not in STOP and kw not in seen:
                                doc_keywords.append(kw)
                                seen.add(kw)
                        logger.info("auto_keywords Tier1: %d keywords from LLM best_keywords for %s",
                                    len(doc_keywords), section_id)

                # ── Tier 2: Hint-scoring + title-token chunk matching ──────────
                if not doc_keywords:
                    title_tokens = {
                        w.lower() for w in (section_title + " " + parent_title).split()
                        if len(w) > 3 and w.lower() not in STOP
                    }
                    scored = []
                    for chunk in proj_chunks:
                        s = _score_chunk_for_section(chunk, section_id, title_tokens)
                        if s > 0:
                            scored.append((s, chunk))
                    scored.sort(key=lambda x: -x[0])
                    top_chunks = [c for _, c in scored[:25]]
                    logger.info("auto_keywords Tier2: %d chunks scored >0 for %s (top=%d)",
                                len(scored), section_id, scored[0][0] if scored else 0)

                    # Also merge LLM matched chunks if available
                    if cached_mapping:
                        mapping = cached_mapping.get("mapping", {})
                        chunk_by_id = {c.get("chunk_id"): c for c in proj_chunks}
                        existing_ids = {c.get("chunk_id") for c in top_chunks}
                        for cid in mapping.get(section_id, [])[:20]:
                            if cid in chunk_by_id and cid not in existing_ids:
                                top_chunks.append(chunk_by_id[cid])
                                existing_ids.add(cid)

                    seen: set = set()
                    # Priority 1: chunk keywords
                    for chunk in top_chunks:
                        for kw in chunk.get("keywords", []):
                            kw = kw.strip()
                            if kw and len(kw) > 3 and kw.lower() not in STOP and kw not in seen:
                                doc_keywords.append(kw)
                                seen.add(kw)
                    # Priority 2: section headings from matched chunks
                    for chunk in top_chunks:
                        heading = (chunk.get("section_heading") or "").strip()
                        if heading and heading not in seen and len(heading) > 4:
                            doc_keywords.append(heading)
                            seen.add(heading)
                    # Priority 3: doc name stems
                    for chunk in top_chunks:
                        doc_nm = (chunk.get("doc_name") or "").strip()
                        stem = _re.sub(r"[.](pdf|docx|doc|pptx|ppt|xlsx|xls)$", "", doc_nm,
                                       flags=_re.IGNORECASE)
                        stem = stem.replace("_", " ").replace("-", " ").strip()
                        if stem and stem not in seen and len(stem) > 4:
                            doc_keywords.append(stem)
                            seen.add(stem)

                    doc_keywords = doc_keywords[:30]
                    logger.info("auto_keywords Tier2: %d doc keywords for %s", len(doc_keywords), section_id)

                # ── Tier 3: Global pool fallback ──────────────────────────────
                if not doc_keywords:
                    seen_g: set = set()
                    for chunk in proj_chunks:
                        for kw in chunk.get("keywords", []):
                            kw = kw.strip()
                            if kw and len(kw) > 3 and kw not in seen_g:
                                doc_keywords.append(kw)
                                seen_g.add(kw)
                        if len(doc_keywords) >= 12:
                            break
                    doc_keywords = doc_keywords[:12]
                    if doc_keywords:
                        logger.info("auto_keywords: Tier3 global pool used for %s", section_id)

                if not doc_keywords:
                    doc_kw_warning = (
                        f"No document chunks matched section '{section_title}'. "
                        "Use Browse Chunks to manually select relevant chunks, "
                        "or upload more relevant documents."
                    )
                    logger.warning("auto_keywords: 0 doc keywords for section %s", section_id)

        except Exception as e:
            logger.error("auto_keywords: failed to load chunks for %s: %s", project_id, e)
            doc_kw_warning = f"Error loading documents: {e}"

    # ── Web keywords ──────────────────────────────────────────────────────────
    company = _get_client_company(
        project_id=project_id,
        ctx_dict=body if isinstance(body, dict) else None,
    )
    brand = company if company else "the client"

    web_keywords_raw = []
    if brand:
        web_keywords_raw.append(f"{brand} {section_title}")
    if parent_title:
        web_keywords_raw.append(f"{parent_title} {section_title}")
        if brand:
            web_keywords_raw.append(f"{brand} {parent_title}")
    web_keywords_raw.append(f"{section_title} supply chain planning")
    web_keywords_raw.append(f"{section_title} best practices")

    tl = section_title.lower()
    if any(w in tl for w in ["demand", "forecast"]):
        if brand: web_keywords_raw.append(f"{brand} demand planning")
        web_keywords_raw.append("consensus demand planning S&OP")
    elif any(w in tl for w in ["supply", "inventory"]):
        if brand: web_keywords_raw.append(f"{brand} supply planning optimization")
        web_keywords_raw.append("inventory optimization supply chain")
    elif any(w in tl for w in ["benefit", "value", "business"]):
        if brand: web_keywords_raw.append(f"{brand} business value ROI")
        web_keywords_raw.append("supply chain digital transformation")
    elif any(w in tl for w in ["data", "integration"]):
        if brand: web_keywords_raw.append(f"{brand} data integration ERP")
        web_keywords_raw.append("supply chain data connector")

    web_keywords = list(dict.fromkeys(w for w in web_keywords_raw if w.strip()))[:10]

    # ── Persist — RESPECT user_edited flag ───────────────────────────────────
    kw_store = _load_keyword_store(project_id)
    entry = kw_store.get(section_id, {})

    if entry.get("user_edited") and (entry.get("doc_keywords") or entry.get("web_keywords")):
        # User has manually edited keywords — MERGE: keep user selections + add new auto ones
        existing_doc = entry.get("doc_keywords", [])
        existing_web = entry.get("web_keywords", [])

        # Merge: existing user keywords first, then new auto ones (deduplicated)
        existing_set = set(existing_doc)
        merged_doc = list(existing_doc)
        for kw in doc_keywords:
            if kw not in existing_set:
                merged_doc.append(kw)
                existing_set.add(kw)
        merged_doc = merged_doc[:30]

        existing_web_set = set(existing_web)
        merged_web = list(existing_web)
        for kw in web_keywords:
            if kw not in existing_web_set:
                merged_web.append(kw)
                existing_web_set.add(kw)
        merged_web = merged_web[:10]

        entry["doc_keywords"] = merged_doc
        entry["web_keywords"] = merged_web
        logger.info("auto_keywords: section %s has user edits — merged (doc=%d, web=%d)",
                    section_id, len(merged_doc), len(merged_web))
        # Return the merged keywords
        doc_keywords  = merged_doc
        web_keywords  = merged_web
    else:
        # No user edits — replace with fresh auto-generated keywords
        entry["doc_keywords"] = doc_keywords
        entry["web_keywords"] = web_keywords
        # Mark as NOT user-edited so future refreshes can update it
        entry.pop("user_edited", None)

    kw_store[section_id] = entry
    _save_keyword_store(kw_store, project_id)

    logger.info("auto_keywords: section %s → %d doc kw, %d web kw",
                section_id, len(doc_keywords), len(web_keywords))
    return {
        "ok":             True,
        "section_id":     section_id,
        "web_keywords":   web_keywords,
        "doc_keywords":   doc_keywords,
        "doc_kw_warning": doc_kw_warning,
    }


@router.post("/api/sections/{section_id}/approve")
def approve_section(section_id: str, body: dict):
    """
    Save approved content for a section into the SessionState.
    Maps section_id to the correct *_sections dict field.
    Called when user clicks "Save to Blueprint Document" in the UI.
    """
    content = body.get("content", "").strip()
    heading_only = bool(body.get("heading_only"))
    no_heading   = bool(body.get("no_heading"))
    # "No heading" wins if both were ticked — they're mutually exclusive
    # presentation modes and "suppress everything" is the stronger statement.
    if no_heading and heading_only:
        heading_only = False
    if not content and not heading_only and not no_heading:
        return {"ok": False, "error": "No content provided."}

    state = _get_screen3_state()
    if not state:
        return {"ok": False, "error": "Session not available."}

    top = section_id.split(".")[0]
    field = SECTION_STATE_MAP.get(top)

    # For custom sections (top > 6), store in the closest existing group
    # or fall back to di_sections (last group) as a catch-all
    if not field:
        field = "di_sections"
        logger.info(
            "Section %s has no mapped field — storing in di_sections as custom section.",
            section_id
        )

    # Get or create the dict for this section group
    current = getattr(state, field, None) or {}
    current[section_id] = content
    setattr(state, field, current)

    # Also update company name from client context if present
    client = body.get("client_context", {})
    if client.get("company"):
        state.company = client["company"]

    pid = body.get("project_id")

    # ── Persist to disk so restarts don't lose approved content ─────────────
    store = _load_approved_store(pid)
    if "sections" not in store:
        store["sections"] = {}
    store["sections"][section_id] = content
    # Persist heading_only / no_heading sets so export and reload-after-refresh
    # know which sections render heading-only (no body) vs are fully suppressed.
    ho = set(store.get("heading_only_ids", []))
    nh = set(store.get("no_heading_ids", []))
    ho.discard(section_id)
    nh.discard(section_id)
    if heading_only:
        ho.add(section_id)
    if no_heading:
        nh.add(section_id)
    store["heading_only_ids"] = sorted(ho)
    store["no_heading_ids"]   = sorted(nh)
    if state.company:
        store["company"] = state.company
    _save_approved_store(store, pid)

    # Also update status in keyword store so sidebar dot turns green and
    # persist the heading_only flag here too (the screen3 UI reads it back
    # via the /sections enrich payload).
    kw_store = _load_keyword_store(pid)
    kw_entry = kw_store.get(section_id, {})
    kw_entry["status"] = "generated"
    kw_entry["heading_only"] = heading_only
    kw_entry["no_heading"]   = no_heading
    kw_store[section_id] = kw_entry
    _save_keyword_store(kw_store, pid)

    logger.info("Section %s approved, saved to disk.", section_id)
    return {"ok": True, "section_id": section_id, "field": field}


# ─────────────────────────────────────────────────────────────────────────────
# SERVER-SIDE BACKGROUND GENERATION
# ─────────────────────────────────────────────────────────────────────────────
# Generation runs in a background thread on the server (not in the browser), so
# the run keeps going — and the queue/log keep updating — even when the user
# navigates back to the Template Editor in the same window. The Generating page
# starts a run, then polls /api/generation/status for live progress and
# reconnects to an in-progress run when it reloads.
import threading as _threading
import time as _time

_GEN_RUNS: Dict[str, dict] = {}     # project_id -> run state dict
_GEN_LOCK = _threading.Lock()
# Generation attempts per section. Default 1 (the provider now handles 429/empty
# internally); raise via BDG_GEN_ATTEMPTS if you want section-level retries.
_GEN_MAX_RETRIES = max(1, _safe_int(os.getenv("BDG_GEN_ATTEMPTS", "1"), 1))
_GEN_RETRY_DELAYS = [2, 4, 8]       # seconds before retry attempts 2 and 3


def _gen_log(run: dict, text: str, level: str = "info") -> None:
    run["log"].append({
        "ts": datetime.now().strftime("%I:%M:%S %p"),
        "text": text,
        "type": level,
    })
    if len(run["log"]) > 800:        # keep memory bounded
        del run["log"][:-800]


def _gen_counts(run: dict) -> dict:
    st = run["statuses"]
    return {
        "done":    sum(1 for v in st.values() if v == "done"),
        "failed":  sum(1 for v in st.values() if v == "failed"),
        "running": sum(1 for v in st.values() if v == "running"),
        "pending": sum(1 for v in st.values() if v in ("pending", None)),
    }


def _build_generate_request(project_id: str, sid: str, kw_store: dict, ctx: dict,
                            include_web: bool) -> "GenerateRequest":
    """Construct a GenerateRequest for one section from the server-of-record
    keyword store. Mirrors what the editor used to send from the browser,
    including the per-section document-keyword cap (CR3) which generate_section
    applies via doc_kw_top_n."""
    saved = kw_store.get(sid, {}) or {}
    ctx = ctx or {}
    return GenerateRequest(
        web_keywords=saved.get("web_keywords", []) or [],
        doc_keywords=saved.get("doc_keywords", []) or [],
        doc_kw_top_n=saved.get("doc_kw_top_n", None),
        # Only send a stored prompt when the user explicitly edited it — matches
        # the editor's behaviour and avoids stale auto-prompts.
        prompt=(saved.get("prompt", "") if saved.get("user_edited") else ""),
        structure=saved.get("structure", "") or "",
        client_context=ClientContext(**{
            k: (ctx.get(k) or "") for k in
            ("company", "industry", "erp", "maturity", "pain", "scope")
        }),
        project_id=project_id,
        gen_source=("both" if include_web else "doc"),
        pinned_chunk_ids=saved.get("pinned_chunk_ids", []) or [],
        pinned_image_ids=saved.get("pinned_image_ids", []) or [],
    )


# Cross-section retrieval de-duplication (Phase 1 / Fix A).
# During a full-document generation run we remember which document chunks have
# already been used by earlier sections so later sections do not re-use the same
# source block — this stops the "same data repeated 5-6x" problem. Keyed by
# project_id; present only while a run is active (set in _generation_worker).
_RUN_USED_CHUNKS: Dict[str, set] = {}


def _generation_worker(project_id: str) -> None:
    run = _GEN_RUNS.get(project_id)
    if not run:
        return
    _RUN_USED_CHUNKS[project_id] = set()   # Fix A: fresh per-run chunk-use memory
    try:
        while True:
            with _GEN_LOCK:
                if run["stop_requested"]:
                    _gen_log(run, "Stop requested — finishing run after the current section.", "warn")
                    break
                sid = next((s for s in run["queue"]
                            if run["statuses"].get(s) in (None, "pending")), None)
                if sid is None:
                    break
                run["statuses"][sid] = "running"
                run["current"] = sid
            # ── Heavy work outside the lock ───────────────────────────────────
            include_web = run["options"].get("include_web", False)
            kw_store = _load_keyword_store(project_id)
            ctx = _load_project_client_context(project_id) or {}
            saved = kw_store.get(sid, {}) or {}
            ok, last_err = False, ""
            for attempt in range(1, _GEN_MAX_RETRIES + 1):
                if run["stop_requested"]:
                    break
                if attempt > 1:
                    delay = _GEN_RETRY_DELAYS[attempt - 2]
                    _gen_log(run, f"[Retry {attempt}/{_GEN_MAX_RETRIES}] {sid} — waiting {delay}s before retry.", "warn")
                    _time.sleep(delay)
                _gen_log(run, f"Generating {sid} — attempt {attempt}/{_GEN_MAX_RETRIES}.")
                try:
                    req = _build_generate_request(project_id, sid, kw_store, ctx, include_web)
                    result = generate_section(sid, req)
                    content = (result or {}).get("content", "") if isinstance(result, dict) else ""
                    heading_only = bool(saved.get("heading_only"))
                    no_heading = bool(saved.get("no_heading"))
                    if not content and not heading_only and not no_heading:
                        raise RuntimeError("Empty generation output — model returned no content")
                    approve_section(sid, {
                        "content": content,
                        "heading_only": heading_only,
                        "no_heading": no_heading,
                        "client_context": ctx,
                        "project_id": project_id,
                    })
                    ok = True
                    _gen_log(run, f"Saved {sid}: {len(content)} chars generated.", "ok")
                    break
                except HTTPException as he:
                    last_err = f"HTTP {he.status_code} — {he.detail}"
                except Exception as e:
                    last_err = str(e)
                if not ok and attempt < _GEN_MAX_RETRIES:
                    _gen_log(run, f"Section {sid} attempt {attempt}/{_GEN_MAX_RETRIES} failed: {last_err} — scheduling retry.", "warn")
            with _GEN_LOCK:
                run["statuses"][sid] = "done" if ok else "failed"
                run["current"] = None
                if not ok:
                    _gen_log(run, f"Failed {sid}: {last_err} (all {_GEN_MAX_RETRIES} attempts exhausted).", "err")
                    # Persist failure status to keyword store so it survives
                    # server restarts — without this, failed sections revert to
                    # "pending" after a restart and the metrics are misleading.
                    try:
                        _kw = _load_keyword_store(project_id)
                        _entry = _kw.get(sid, {})
                        _entry["status"] = "failed"
                        _kw[sid] = _entry
                        _save_keyword_store(_kw, project_id)
                    except Exception as _ke:
                        logger.warning("Could not persist failed status for %s: %s", sid, _ke)
    except Exception as e:
        logger.exception("Background generation worker crashed for project %s", project_id)
        try:
            _gen_log(run, f"Generation worker error: {e}", "err")
        except Exception:
            pass
    finally:
        _RUN_USED_CHUNKS.pop(project_id, None)   # Fix A: clear per-run chunk-use memory
        with _GEN_LOCK:
            run["running"] = False
            run["current"] = None
            run["finished_at"] = datetime.now().isoformat()
            c = _gen_counts(run)
            _gen_log(run, f"Run complete. {c['done']} saved, {c['failed']} failed.",
                     "warn" if c["failed"] else "ok")


class GenerationStartRequest(BaseModel):
    project_id:  Optional[str] = None
    section_ids: List[str] = []
    include_web: bool = False


@router.post("/api/generation/start")
def start_generation(body: GenerationStartRequest):
    """Start a background generation run, or — if one is already running for this
    project — merge the requested sections into the live queue."""
    pid = body.project_id or "default"
    sids = [s for s in (body.section_ids or []) if s]
    if not sids:
        return {"ok": False, "error": "No sections to generate."}
    with _GEN_LOCK:
        run = _GEN_RUNS.get(pid)
        if run and run.get("running"):
            added = []
            for s in sids:
                if s not in run["queue"]:
                    run["queue"].append(s)
                    run["statuses"].setdefault(s, "pending")
                    added.append(s)
                elif run["statuses"].get(s) == "done":
                    # Explicit re-request of a finished section → re-queue it.
                    run["statuses"][s] = "pending"
                    added.append(s)
            if body.include_web:
                run["options"]["include_web"] = True
            _gen_log(run, f"Merged {len(added)} section(s) into the running queue.", "ok")
            return {"ok": True, "merged": True, "added": added,
                    "queue_id": run["queue_id"], "running": True}
        # Fresh run
        run = {
            "running": True,
            "stop_requested": False,
            "queue": list(sids),
            "statuses": {s: "pending" for s in sids},
            "current": None,
            "log": [],
            "options": {"include_web": bool(body.include_web)},
            "queue_id": uuid4().hex[:6].upper(),
            "started_at": datetime.now().isoformat(),
            "finished_at": None,
        }
        _GEN_RUNS[pid] = run
        _gen_log(run, f"Starting optimized run: {len(sids)} section(s), "
                      f"per-section doc keyword limit, web {'on' if body.include_web else 'off'}.", "ok")
        t = _threading.Thread(target=_generation_worker, args=(pid,), daemon=True)
        t.start()
        return {"ok": True, "started": True, "queue_id": run["queue_id"], "running": True}


@router.get("/api/generation/status")
def generation_status(project_id: str = None, since: int = 0):
    """Live status of the background run. `since` is the log index the client
    already has, so only new log entries are returned."""
    pid = project_id or "default"
    run = _GEN_RUNS.get(pid)
    if not run:
        return {"active": False, "running": False, "statuses": {}, "queue": [],
                "current": None, "log": [], "log_total": 0,
                "counts": {"done": 0, "failed": 0, "running": 0, "pending": 0}}
    with _GEN_LOCK:
        log = run["log"]
        since = max(0, int(since or 0))
        return {
            "active": True,
            "running": run["running"],
            "queue_id": run["queue_id"],
            "statuses": dict(run["statuses"]),
            "queue": list(run["queue"]),
            "current": run["current"],
            "log": log[since:] if since < len(log) else [],
            "log_total": len(log),
            "counts": _gen_counts(run),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
        }


@router.post("/api/generation/stop")
def stop_generation(body: dict = None):
    pid = ((body or {}).get("project_id")) or "default"
    run = _GEN_RUNS.get(pid)
    if not run or not run.get("running"):
        return {"ok": False, "error": "No active run."}
    with _GEN_LOCK:
        run["stop_requested"] = True
        _gen_log(run, "Stop requested by user.", "warn")
    return {"ok": True}


@router.get("/api/document/status")
def document_status(project_id: str = None):
    """
    Return which sections have been approved and their content.
    Reads from disk so this survives restarts.
    The frontend uses this to restore outputs[] after a page refresh.
    """
    store = _load_approved_store(project_id)
    sections = store.get("sections", {})
    approved  = {sid: True for sid in sections}
    return {
        "approved": approved,
        "total":    len(approved),
        "sections": sections,        # full content keyed by section_id
        "company":  store.get("company", ""),
    }


@router.post("/api/document/import-existing/preview")
async def preview_existing_document_import(
    project_id: str = Form(...),
    file: UploadFile = File(...),
):
    """
    Parse a DOCX and return the section mapping before applying it.

    The uploaded file is stored in a pending import folder. The frontend then
    calls /api/document/import-existing/apply with the returned import_token.
    """
    if not project_id:
        return {"ok": False, "error": "No project_id supplied."}
    filename = file.filename or "document.docx"
    if not filename.lower().endswith(".docx"):
        return {"ok": False, "error": "Please upload a .docx document."}

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename).name).strip("._-") or "document.docx"
    import_token = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    pending_dir = _pending_import_dir(project_id)
    pending_dir.mkdir(parents=True, exist_ok=True)
    pending_path = pending_dir / f"{import_token}_{safe_name}"

    content = await file.read()
    if not content:
        return {"ok": False, "error": "Uploaded file is empty."}
    pending_path.write_bytes(content)

    parsed = _extract_sections_from_docx(pending_path, project_id)
    payload = _import_preview_payload(project_id, parsed, import_token, safe_name)
    if not payload["matched_sections"]:
        payload["ok"] = False
        payload["error"] = (
            "No matching Blueprint sections were found in this DOCX. "
            "Use headings like 'Section 1: Introduction' or '1.1 Company Information'."
        )
    return payload


@router.post("/api/document/import-existing/extract-context")
def extract_client_context_from_import(body: dict):
    """Read the imported DOCX and use the LLM to extract client-context fields.

    Body: { project_id, import_token }
    Returns: { ok, context: {company, industry, erp, maturity, pain, scope} }
    """
    project_id = (body or {}).get("project_id")
    import_token = _safe_import_token((body or {}).get("import_token", ""))
    if not project_id:
        return {"ok": False, "error": "No project_id supplied."}

    # Locate the uploaded file: pending first, else the most recent applied import.
    path = _find_pending_import(project_id, import_token)
    if not path or not path.exists():
        final_dir = DOCUMENT_IMPORT_ROOT / _safe_project_key(project_id)
        if final_dir.exists():
            docs = sorted(final_dir.glob("*.docx"), key=lambda f: f.stat().st_mtime, reverse=True)
            path = docs[0] if docs else None

    text = ""
    # Prefer the imported DOCX (paragraphs + tables) when one is available.
    if path and path.exists():
        try:
            from docx import Document
            doc = Document(str(path))
            parts = []
            for para in doc.paragraphs:
                t = (para.text or "").strip()
                if t:
                    parts.append(t)
            for tbl in doc.tables:
                for row in tbl.rows:
                    cells = [c.text.strip() for c in row.cells if c.text and c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            text = "\n".join(parts)[:9000]
        except Exception as e:
            logger.warning("extract-context: could not read docx %s: %s", path, e)

    # Fallback: build the text from the project's indexed chunks (the retrieved
    # document content) so auto-fill works even without a fresh DOCX import
    # (e.g. after a page refresh, or when content came in via Repositories).
    if not text.strip():
        try:
            _chunks = _load_all_project_chunks(project_id)
            text = "\n".join((c.get("chunk_text", "") or "") for c in _chunks)[:9000]
        except Exception as e:
            logger.warning("extract-context: chunk fallback failed: %s", e)

    if not text.strip():
        return {"ok": True, "context": {"company": "", "industry": "", "erp": "", "maturity": "", "pain": "", "scope": ""}}

    prompt = (
        "You are analyzing a supply chain / Kinaxis Blueprint document. "
        "Extract the client context from the document text below. "
        "Return ONLY a JSON object with EXACTLY these keys: "
        "company, industry, erp, maturity, pain, scope.\n"
        "- company: client/customer company name\n"
        "- industry: the client's industry\n"
        "- erp: ERP system mentioned (e.g. SAP S/4HANA, JD Edwards, Oracle)\n"
        "- maturity: planning maturity in a few words (e.g. 'partially automated with siloed systems')\n"
        "- pain: key pain points (short phrase)\n"
        "- scope: phase 1 / project scope (short phrase)\n"
        "If a field is not found, use an empty string. "
        "Do not include any text outside the JSON object.\n\n"
        "DOCUMENT:\n" + text
    )

    try:
        try:
            from app.claude_provider import completion_from_prompt as _cp_ctx
        except ImportError:
            from claude_provider import completion_from_prompt as _cp_ctx
        raw = _cp_ctx(prompt, max_tokens=400, temperature=0.0) or ""
    except Exception as e:
        logger.warning("Context extraction LLM call failed: %s", e)
        return {"ok": False, "error": f"LLM extraction failed: {e}"}

    ctx = {}
    try:
        i, j = raw.find("{"), raw.rfind("}")
        if i != -1 and j != -1 and j > i:
            ctx = json.loads(raw[i:j + 1])
        else:
            ctx = json.loads(_salvage_partial_json_object(raw) or "{}") if isinstance(_salvage_partial_json_object(raw), str) else (_salvage_partial_json_object(raw) or {})
    except Exception:
        try:
            ctx = _salvage_partial_json_object(raw) or {}
        except Exception:
            ctx = {}

    out = {k: str(ctx.get(k, "") or "").strip() for k in ("company", "industry", "erp", "maturity", "pain", "scope")}

    # Persist so it survives reloads and feeds generation prompts.
    try:
        _save_project_client_context(project_id, out)
    except Exception as e:
        logger.warning("Could not persist extracted client context: %s", e)

    return {"ok": True, "context": out}


@router.post("/api/document/import-existing/apply")
def apply_existing_document_import(body: dict):
    """
    Apply a previously previewed DOCX import using merge or replace mode.

    Optional body fields:
    - selected_section_ids: list[str]  — import only these section IDs (all if omitted)
    - manual_mappings: dict[str, str]  — map unmatched heading IDs to template section IDs
    - template_source: "template" (default) | "document" — with "document", the
      uploaded doc's structure wins: unmatched headings are created as new
      template sections and their content is imported (nothing is dropped)
    """
    project_id = (body or {}).get("project_id")
    import_token = _safe_import_token((body or {}).get("import_token", ""))
    mode = (body or {}).get("mode") or "merge"
    author = (body or {}).get("author") or ""
    selected_ids = (body or {}).get("selected_section_ids")  # None = all
    manual_mappings = (body or {}).get("manual_mappings") or {}  # {"unmatchedId": "templateSectionId"}
    if not project_id:
        return {"ok": False, "error": "No project_id supplied."}
    if mode not in {"merge", "replace"}:
        mode = "merge"

    pending_path = _find_pending_import(project_id, import_token)
    if not pending_path or not pending_path.exists():
        return {"ok": False, "error": "Import preview expired or was not found. Please choose the DOCX again."}

    original_name = pending_path.name[len(import_token) + 1:] if pending_path.name.startswith(import_token + "_") else pending_path.name

    pre_version = None
    try:
        pre_version = _write_version_snapshot(
            project_id=project_id,
            message=f"Before DOCX import: {original_name}",
            author=author,
            source="import-pre",
        )
    except Exception as e:
        logger.warning("Could not create pre-import snapshot: %s", e)

    template_source = ((body or {}).get("template_source") or "template").lower()
    template_reset = False
    if template_source == "template":
        # Toggle OFF on a project whose template was previously REPLACED by a
        # document-structure import: restore the tool's standard template first,
        # so "map onto the tool's template" actually maps onto the tool's template.
        _st = _load_sections_store(project_id)
        default_ids = {s["id"] for s in ALL_SECTIONS}
        # Flag set by a document-structure import — or (for projects imported
        # before the flag existed) most standard id+title pairs are gone, which
        # means the template was replaced by a document import. A user-curated
        # template keeps most default sections intact, so it stays untouched.
        _active_pairs = {(s.get("id"), _normalize_doc_title(s.get("title", "")))
                         for s in _get_live_sections(project_id)}
        _default_pairs = {(s["id"], _normalize_doc_title(s["title"])) for s in ALL_SECTIONS}
        _missing_frac = len(_default_pairs - _active_pairs) / max(len(_default_pairs), 1)
        if _st.get("template_source") == "document" or _missing_frac >= 0.5:
            _persist_sections([dict(s) for s in ALL_SECTIONS], [], project_id)
            _st = _load_sections_store(project_id)
            _st.pop("template_source", None)
            _save_sections_store(_st, project_id)
            ap0 = _load_approved_store(project_id)
            ap0["sections"] = {k: v for k, v in (ap0.get("sections") or {}).items() if k in default_ids}
            ap0["heading_only_ids"] = [i for i in ap0.get("heading_only_ids", []) if i in default_ids]
            ap0["no_heading_ids"] = [i for i in ap0.get("no_heading_ids", []) if i in default_ids]
            _save_approved_store(ap0, project_id)
            kw0 = _load_keyword_store(project_id)
            _save_keyword_store({k: v for k, v in kw0.items() if k in default_ids}, project_id)
            template_reset = True
            logger.info("import(template_source=template): restored tool default template for %s", project_id)

    parsed = _extract_sections_from_docx(pending_path, project_id)
    imported_sections = parsed.get("sections") or {}
    unmatched_sections = parsed.get("unmatched_sections") or {}
    unmatched_meta = parsed.get("unmatched_headings") or []

    # Apply manual mappings: content from unmatched heading → mapped template
    # section. src_id may be a matched section id, an unmatched heading's doc
    # number, or its __U__ key.
    live_ids = {s.get("id") for s in _get_live_sections(project_id) if s.get("id")}
    _by_doc_id = {str(u.get("id")): u.get("key") for u in unmatched_meta if u.get("key")}
    for src_id, dest_id in (manual_mappings or {}).items():
        if dest_id not in live_ids:
            continue
        content = imported_sections.pop(src_id, None)
        if content is None:
            ukey = src_id if src_id in unmatched_sections else _by_doc_id.get(str(src_id))
            if ukey:
                content = unmatched_sections.pop(ukey, None)
        if content:
            imported_sections[dest_id] = content

    # template_source="document": the uploaded document's structure wins.
    # Unmatched headings become NEW template sections and their content is
    # imported — nothing from the document is dropped.
    created_sections = []
    renamed_sections = []
    if template_source == "document":
        # The document IS the template: show ONLY the document's sections, with
        # the document's own numbering and order. Tool-template sections are
        # archived; users add extras via "Add section / subsection".
        outline = _extract_document_structure(pending_path)
        if not outline:
            return {"ok": False, "error": "No numbered sections were found in this DOCX."}
        new_active = [{"id": o["id"], "title": o["title"]} for o in outline]
        new_ids = {s["id"] for s in new_active}
        # The document is the single source of truth: replaced tool-template
        # sections are DELETED (not archived) and the archive is cleared.
        # Extra sections are added manually via "Add section / subsection".
        _persist_sections(new_active, [], project_id)
        # Remember this project runs on the document's structure, so a later
        # toggle-OFF import knows to restore the tool's standard template.
        _st = _load_sections_store(project_id)
        _st["template_source"] = "document"
        _save_sections_store(_st, project_id)
        # Content now keys on the DOCUMENT's numbers.
        imported_sections = {o["id"]: o["content"] for o in outline if o["content"]}
        created_sections = [{"id": o["id"], "title": o["title"]} for o in outline]
        # Prune stores so nothing from earlier template/imports lingers.
        ap = _load_approved_store(project_id)
        ap["sections"] = {k: v for k, v in (ap.get("sections") or {}).items() if k in new_ids}
        ap["heading_only_ids"] = [i for i in ap.get("heading_only_ids", []) if i in new_ids]
        ap["no_heading_ids"] = [i for i in ap.get("no_heading_ids", []) if i in new_ids]
        _save_approved_store(ap, project_id)
        kw = _load_keyword_store(project_id)
        kw = {k: v for k, v in kw.items() if k in new_ids}
        for nid in new_ids:
            kw.setdefault(nid, {"doc_keywords": [], "web_keywords": [], "user_edited": True})
        _save_keyword_store(kw, project_id)
        logger.info("import(template_source=document): template replaced with %d doc section(s)",
                    len(new_active))

    # Filter to only selected sections when the frontend sends a subset
    if selected_ids is not None:
        selected_set = set(selected_ids)
        imported_sections = {k: v for k, v in imported_sections.items() if k in selected_set}

    if not imported_sections:
        return {
            "ok": False,
            "error": "No matching Blueprint sections were found in this DOCX.",
            "pre_version": pre_version,
            "unmatched_headings": parsed.get("unmatched_headings", []),
            "ignored_blocks": parsed.get("ignored_blocks", 0),
        }

    applied = _apply_imported_sections(project_id, imported_sections, mode=mode)

    final_dir = DOCUMENT_IMPORT_ROOT / _safe_project_key(project_id)
    final_dir.mkdir(parents=True, exist_ok=True)
    final_path = final_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{original_name}"
    try:
        pending_path.replace(final_path)
    except Exception:
        final_path.write_bytes(pending_path.read_bytes())
        try:
            pending_path.unlink()
        except Exception:
            pass

    # Index the imported document into the project's chunk corpus so its content
    # is browsable in Browse Chunks and usable as research context for generating
    # the missing sections — same as an uploaded document in the new-document flow.
    try:
        import threading as _t
        from uuid import uuid4 as _uuid4
        try:
            from app.project_routes import UPLOAD_ROOT as _UR
        except ImportError:
            from project_routes import UPLOAD_ROOT as _UR
        try:
            from app.chunking_pipeline import run_pipeline_for_file as _rpff
        except ImportError:
            from chunking_pipeline import run_pipeline_for_file as _rpff
        _proj_dir = Path(_UR) / project_id
        _proj_dir.mkdir(parents=True, exist_ok=True)
        _fid = "import_" + _uuid4().hex[:8]
        _fp = final_path
        def _chunk_imported():
            try:
                n = _rpff(_fp, project_id, _fid, _proj_dir)
                logger.info("import-existing: chunked imported doc into project %s (%s chunks)", project_id, n)
            except Exception as _ce:
                logger.warning("import-existing: chunking of imported doc failed: %s", _ce)
        _t.Thread(target=_chunk_imported, daemon=True).start()
    except Exception as _e:
        logger.warning("import-existing: could not start chunking of imported doc: %s", _e)

    post_version = None
    try:
        post_version = _write_version_snapshot(
            project_id=project_id,
            message=f"Imported existing document ({mode}): {original_name}",
            author=author,
            source="import",
        )
    except Exception as e:
        logger.warning("Could not create post-import snapshot: %s", e)

    return {
        "ok": True,
        "filename": original_name,
        "saved_path": str(final_path),
        "mode": mode,
        "matched_count": applied["updated_count"],
        "matched_ids": applied["updated_ids"],
        "created_sections": created_sections,
        "renamed_sections": renamed_sections,
        "template_source": template_source,
        "template_reset": template_reset,
        "unmatched_headings": parsed.get("unmatched_headings", []),
        "ignored_blocks": parsed.get("ignored_blocks", 0),
        "image_warning": parsed.get("image_warning", ""),
        "pre_version": pre_version,
        "post_version": post_version,
    }


@router.post("/api/document/import-existing/reapply")
def reapply_existing_document_import(body: dict):
    """Re-apply the MOST RECENTLY imported document in a (possibly different)
    template_source mode — lets users switch between "document structure" and
    "tool template" after importing, without re-uploading the file."""
    project_id = (body or {}).get("project_id")
    if not project_id:
        return {"ok": False, "error": "No project_id supplied."}
    import_dir = DOCUMENT_IMPORT_ROOT / _safe_project_key(project_id)
    files = sorted(import_dir.glob("*.docx"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if import_dir.exists() else []
    if not files:
        return {"ok": False, "error": "No previously imported document found — use Re-import to choose the file."}
    src = files[0]
    # Stage a fresh pending copy and run the normal apply pipeline on it.
    token = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"
    pending_dir = _pending_import_dir(project_id)
    pending_dir.mkdir(parents=True, exist_ok=True)
    parts = src.name.split("_", 2)  # stored as "<YYYYmmdd>_<HHMMSS>_<name>"
    orig_name = parts[2] if len(parts) == 3 else src.name
    (pending_dir / f"{token}_{orig_name}").write_bytes(src.read_bytes())
    return apply_existing_document_import({
        "project_id": project_id,
        "import_token": token,
        "mode": (body or {}).get("mode") or "merge",
        "template_source": (body or {}).get("template_source") or "template",
        "author": (body or {}).get("author") or "",
    })


@router.post("/api/document/import-existing")
async def import_existing_document(
    project_id: str = Form(...),
    mode: str = Form("merge"),
    file: UploadFile = File(...),
):
    """
    Import an existing DOCX and map its section content into the editor.

    The importer reads section headings from the current project's live
    template, saves matched section bodies into the approved/output stores,
    and creates safety snapshots before and after the import.
    """
    if not project_id:
        return {"ok": False, "error": "No project_id supplied."}
    filename = file.filename or "document.docx"
    if not filename.lower().endswith(".docx"):
        return {"ok": False, "error": "Please upload a .docx document."}
    if mode not in {"merge", "replace"}:
        mode = "merge"

    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(filename).name).strip("._-") or "document.docx"
    import_dir = DOCUMENT_IMPORT_ROOT / _safe_project_key(project_id)
    import_dir.mkdir(parents=True, exist_ok=True)
    dest = import_dir / f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{safe_name}"

    content = await file.read()
    if not content:
        return {"ok": False, "error": "Uploaded file is empty."}
    dest.write_bytes(content)

    pre_version = None
    try:
        pre_version = _write_version_snapshot(
            project_id=project_id,
            message=f"Before DOCX import: {safe_name}",
            source="import-pre",
        )
    except Exception as e:
        logger.warning("Could not create pre-import snapshot: %s", e)

    parsed = _extract_sections_from_docx(dest, project_id)
    imported_sections = parsed.get("sections") or {}
    if not imported_sections:
        return {
            "ok": False,
            "error": "No matching Blueprint sections were found in this DOCX. Use headings like 'Section 1: Introduction' or '1.1 Company Information'.",
            "saved_path": str(dest),
            "pre_version": pre_version,
            "unmatched_headings": parsed.get("unmatched_headings", []),
            "ignored_blocks": parsed.get("ignored_blocks", 0),
        }

    applied = _apply_imported_sections(project_id, imported_sections, mode=mode)

    post_version = None
    try:
        post_version = _write_version_snapshot(
            project_id=project_id,
            message=f"Imported existing document: {safe_name}",
            source="import",
        )
    except Exception as e:
        logger.warning("Could not create post-import snapshot: %s", e)

    return {
        "ok": True,
        "filename": safe_name,
        "saved_path": str(dest),
        "mode": mode,
        "matched_count": applied["updated_count"],
        "matched_ids": applied["updated_ids"],
        "unmatched_headings": parsed.get("unmatched_headings", []),
        "ignored_blocks": parsed.get("ignored_blocks", 0),
        "pre_version": pre_version,
        "post_version": post_version,
    }


@router.get("/api/projects/{project_id}/versions")
def list_document_versions(project_id: str):
    return {
        "ok": True,
        "project_id": project_id,
        "versions": _list_version_snapshots(project_id),
        "git_mcp_configured": bool((os.getenv("GIT_MCP_BASE_URL") or os.getenv("MCP_GIT_BASE_URL") or "").strip()),
    }


@router.post("/api/projects/{project_id}/versions/snapshot")
def create_document_version(project_id: str, body: dict = None):
    body = body or {}
    meta = _write_version_snapshot(
        project_id=project_id,
        message=body.get("message") or "Manual document snapshot",
        author=body.get("author") or "",
        source=body.get("source") or "manual",
    )
    return {"ok": True, "version": meta}


@router.get("/api/projects/{project_id}/versions/{version_id}")
def get_document_version(project_id: str, version_id: str):
    snapshot = _load_version_snapshot(project_id, version_id)
    current = {"state": _snapshot_state(project_id)}
    return {
        "ok": True,
        "metadata": snapshot.get("metadata", {}),
        "diff_from_current": _diff_approved_sections(snapshot, current),
    }


@router.get("/api/projects/{project_id}/versions/{version_id}/diff")
def diff_document_version(project_id: str, version_id: str, other: str = "current"):
    left = _load_version_snapshot(project_id, version_id)
    right = {"state": _snapshot_state(project_id)} if other == "current" else _load_version_snapshot(project_id, other)
    return {
        "ok": True,
        "left": left.get("metadata", {}),
        "right": {"version_id": "current"} if other == "current" else right.get("metadata", {}),
        "diff": _diff_approved_sections(left, right),
    }


@router.post("/api/projects/{project_id}/versions/{version_id}/restore")
def restore_document_version(project_id: str, version_id: str, body: dict = None):
    body = body or {}
    snapshot = _load_version_snapshot(project_id, version_id)
    before = None
    try:
        before = _write_version_snapshot(
            project_id=project_id,
            message=f"Before restoring version {version_id}",
            author=body.get("author") or "",
            source="restore-pre",
        )
    except Exception as e:
        logger.warning("Could not create pre-restore snapshot: %s", e)

    _restore_version_state(project_id, snapshot)
    restored = _write_version_snapshot(
        project_id=project_id,
        message=f"Restored version {version_id}",
        author=body.get("author") or "",
        source="restore",
    )
    git = _notify_git_mcp(
        "restore",
        {
            "project_id": project_id,
            "version_id": version_id,
            "restored_snapshot": restored.get("snapshot_path"),
        },
    )
    return {
        "ok": True,
        "restored_version": snapshot.get("metadata", {}),
        "pre_restore_version": before,
        "post_restore_version": restored,
        "git": git,
    }




def _extract_complete_sites_data(proj_chunks: list) -> str:
    """
    Extract COMPLETE sites data from Overview_sites.xlsx without chunking limits.
    
    This function finds the Overview_sites.xlsx document and reconstructs the complete
    sites table by combining all chunks that belong to it, in order.
    
    Strategy:
    1. First, find all chunks from Overview_sites.xlsx by doc_name matching
    2. Then, search for chunks containing site data patterns (Value, Description, Country, Currency, LicenseType)
    3. Combine all relevant chunks to ensure complete data
    
    Returns: Complete sites data as formatted text ready for LLM consumption
    """
    import re
    
    # Strategy 1: Find chunks by doc_name matching
    sites_chunks = [c for c in proj_chunks if 'overview' in c.get('doc_name', '').lower() 
                    and 'site' in c.get('doc_name', '').lower()]
    
    # Strategy 2: If not found by name, search by content patterns
    if not sites_chunks:
        # Look for chunks containing site data patterns
        sites_chunks = [c for c in proj_chunks 
                       if any(pattern in c.get('chunk_text', '').lower() 
                             for pattern in ['country.value', 'licensetype', 'value | description'])]
    
    # Strategy 3: If still not found, look for chunks with site-like content
    if not sites_chunks:
        sites_chunks = [c for c in proj_chunks 
                       if re.search(r'^\|?\s*\d+\s*\|', c.get('chunk_text', ''), re.MULTILINE)]
    
    # Strategy 4: As last resort, include ALL chunks that have pipe characters (table format)
    if not sites_chunks:
        sites_chunks = [c for c in proj_chunks 
                       if c.get('chunk_text', '').count('|') > 5]
    
    if not sites_chunks:
        logger.warning("Could not find Overview_sites.xlsx chunks")
        return ""
    
    logger.info(f"Found {len(sites_chunks)} chunks for sites data extraction")
    
    # Sort by chunk index/sequence and chunk_id
    sites_chunks_sorted = sorted(sites_chunks, 
                                 key=lambda x: (x.get('chunk_index', 0) or 0, 
                                               x.get('chunk_id', 0) or 0))
    
    # Reconstruct complete content
    complete_content = []
    for chunk in sites_chunks_sorted:
        text = chunk.get('chunk_text', '').strip()
        if text:
            complete_content.append(text)
    
    if not complete_content:
        logger.warning("No content found in sites chunks")
        return ""
    
    # Join with proper spacing
    result = '\n\n'.join(complete_content)
    
    # Clean up and ensure table structure is preserved
    result = re.sub(r'\n{3,}', '\n\n', result)  # Remove excessive newlines
    result = re.sub(r'^\s+', '', result, flags=re.MULTILINE)  # Remove leading spaces
    
    logger.info(f"Extracted {len(result)} characters of sites data")
    return result


_CAPTION_RE = re.compile(
    r'^\s*[\*_]\s*(?:Figure|Fig\.?|Image|Diagram|Chart|Table)\s*[\w.\-]*\s*[:\-]\s*.+?[\*_]\s*$',
    re.IGNORECASE,
)


def _bind_pinned_images_to_captions(content: str, pinned_image_ids) -> str:
    """Place pinned image tags above their caption placeholders.

    The LLM frequently writes italic figure captions ("*Figure X-1: …*")
    without the corresponding <IMAGE id="…"/> tag at that location. The
    safety net then appends the tags at the END of the section, separating
    the caption from its image. This helper walks the content line-by-line,
    inserts the next unused pinned image_id's tag directly ABOVE each lone
    caption, and skips captions that already have an IMAGE tag in the
    immediately preceding non-blank line.
    """
    if not pinned_image_ids:
        return content
    lines = content.split("\n")
    already_placed = set(re.findall(r'<IMAGE id="([^"]+)"\s*/?>', content))
    queue = [iid for iid in pinned_image_ids if iid not in already_placed]
    if not queue:
        return content

    out = []
    qi = 0
    for ln in lines:
        if qi < len(queue) and _CAPTION_RE.match(ln):
            # Check if an IMAGE tag is already directly above this caption
            prev = next((x for x in reversed(out) if x.strip()), "")
            if '<IMAGE id=' not in prev:
                out.append(f'<IMAGE id="{queue[qi]}"/>')
                qi += 1
        out.append(ln)
    return "\n".join(out)


_VALID_IMAGE_IDS_CACHE: dict = {"mtime": None, "ids": set()}


def _valid_image_ids() -> set:
    """Set of real image_ids currently in the global image index (cached by mtime)."""
    idx = Path(__file__).parent / "image_chunks.json"
    try:
        mtime = idx.stat().st_mtime if idx.exists() else None
    except Exception:
        mtime = None
    if _VALID_IMAGE_IDS_CACHE["mtime"] == mtime:
        return _VALID_IMAGE_IDS_CACHE["ids"]
    ids: set = set()
    if idx.exists():
        try:
            for rec in json.loads(idx.read_text(encoding="utf-8")):
                iid = rec.get("image_id")
                if iid:
                    ids.add(iid)
        except Exception:
            pass
    _VALID_IMAGE_IDS_CACHE["mtime"] = mtime
    _VALID_IMAGE_IDS_CACHE["ids"] = ids
    return ids


_IMG_TAG_NORM_RE = re.compile(
    r'<[ \t]*image[ \t]+id[ \t]*=[ \t]*["\']?([A-Za-z0-9_\-]+)["\']?[ \t]*/?[ \t]*>'
    r'([ \t]*<[ \t]*/[ \t]*image[ \t]*>)?',
    re.IGNORECASE,
)


def _normalize_image_tags(content: str) -> str:
    """Collapse every <IMAGE> variant (case, spacing, missing slash/quotes,
    trailing </IMAGE>) to canonical <IMAGE id="x"/>. [ \\t] only, so it never
    swallows newlines or merges tags on separate lines."""
    if not content:
        return content
    return _IMG_TAG_NORM_RE.sub(lambda m: '<IMAGE id="%s"/>' % m.group(1), content)


def _sanitize_image_refs(content: str, pinned_image_ids=None) -> str:
    """Normalise image references in generated section text.

    - `<IMAGE id="X"/>` where X is a real image → kept.
    - `<IMAGE id="X"/>` where X is NOT real → dropped (hallucinated).
    - Markdown `![alt](target)`:
        * if target (or its basename sans extension) is a real image_id →
          converted to `<IMAGE id="realid"/>`.
        * otherwise → removed entirely, together with an immediately-adjacent
          italic caption line like `*Figure 2: ...*` so no orphan caption or
          broken image placeholder remains.
    """
    if not content:
        return content
    content = _normalize_image_tags(content)   # canonicalise all <IMAGE> variants first
    valid = _valid_image_ids()
    pinned = set(pinned_image_ids or [])

    def _norm(target: str) -> str:
        t = (target or "").strip()
        t = t.split("attachment:")[-1]
        # strip any directory + extension to expose a bare id
        base = t.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        base_noext = base.rsplit(".", 1)[0]
        for cand in (t, base, base_noext):
            if cand in valid:
                return cand
        return ""

    # 1) Drop <IMAGE> tags whose id isn't real (keep real + pinned ones).
    def _img_tag_sub(m):
        iid = m.group(1)
        if iid in valid or iid in pinned:
            return m.group(0)
        return ""   # hallucinated id → remove
    content = re.sub(r'<IMAGE id="([^"]+)"\s*/?>', _img_tag_sub, content)

    # 2) Handle markdown image syntax line-by-line so we can also drop the
    #    trailing italic caption for hallucinated images.
    md_img = re.compile(r'!\[[^\]]*\]\(([^)]*)\)')
    out_lines = []
    lines = content.split("\n")
    for ln in lines:
        m = md_img.search(ln)
        if not m:
            out_lines.append(ln)
            continue
        # Replace each markdown image on this line.
        def _md_sub(mm):
            real = _norm(mm.group(1))
            return f'<IMAGE id="{real}"/>' if real else "\x00DROP\x00"
        new_ln = md_img.sub(_md_sub, ln)
        if "\x00DROP\x00" in new_ln:
            # The image was hallucinated — drop the whole line, and also a
            # following caption line if present.
            continue
        out_lines.append(new_ln)
    # Second pass: drop orphan "*Figure …*" caption lines that immediately
    # follow a now-removed image (heuristic: a standalone italic Figure line).
    cleaned = []
    for ln in out_lines:
        s = ln.strip()
        if re.match(r'^\*?\s*Figure\s+[\d.]+\s*:.*\*?$', s) and not cleaned[-1:] == [""]:
            # keep captions only when the previous non-empty line is an IMAGE tag
            prev = next((x for x in reversed(cleaned) if x.strip()), "")
            if "<IMAGE id=" not in prev:
                continue
        cleaned.append(ln)
    return "\n".join(cleaned)


_PLACEHOLDER_RE = re.compile(r'<\s*information not available[^>]*>', re.IGNORECASE)


def _clean_generated_tables(content: str) -> str:
    """Normalise tables in generated section text.

    1. Convert any HTML <table>…</table> the LLM emitted into GitHub-flavored
       Markdown pipe tables (our preview + DOCX export only render Markdown
       tables; raw HTML shows up as escaped text / fails to convert).
    2. Drop table rows whose cells are ALL "<information not available…>"
       placeholders — the model sometimes pads a table with an empty extra
       row, which adds a meaningless line to the document.
    """
    if not content:
        return content

    # ── 1) HTML tables → Markdown ────────────────────────────────────────────
    def _html_table_to_md(m):
        html = m.group(0)
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)
        md_rows = []
        for r in rows:
            cells = re.findall(r'<t[hd][^>]*>(.*?)</t[hd]>', r, re.DOTALL | re.IGNORECASE)
            if not cells:
                continue
            clean = [re.sub(r'<[^>]+>', '', c).replace('\n', ' ').strip() for c in cells]
            md_rows.append(clean)
        if not md_rows:
            return ""
        width = max(len(r) for r in md_rows)
        md_rows = [r + [""] * (width - len(r)) for r in md_rows]
        out = ["| " + " | ".join(md_rows[0]) + " |",
               "| " + " | ".join(["---"] * width) + " |"]
        for r in md_rows[1:]:
            out.append("| " + " | ".join(r) + " |")
        return "\n" + "\n".join(out) + "\n"

    content = re.sub(r'<table[^>]*>.*?</table>', _html_table_to_md,
                     content, flags=re.DOTALL | re.IGNORECASE)

    # ── 2) Drop placeholder-only Markdown rows ───────────────────────────────
    out_lines = []
    for ln in content.split("\n"):
        s = ln.strip()
        if s.startswith("|") and s.endswith("|") and "---" not in s:
            cells = [c.strip() for c in s.strip("|").split("|")]
            non_empty = [c for c in cells if c]
            # Drop a data row that is entirely blank, or whose every populated
            # cell is an "<information not available…>" placeholder. This trims
            # the padded/empty filler rows the model sometimes appends without
            # touching real data rows or the header (the header is followed by
            # a --- separator row, never matched here).
            if not non_empty or all(_PLACEHOLDER_RE.search(c) for c in non_empty):
                continue
        out_lines.append(ln)
    return "\n".join(out_lines)


def _strip_html_content(content: str) -> str:
    """Convert non-table HTML tags in generated section text to plain markdown.

    The LLM occasionally copies HTML markup verbatim from uploaded reference
    documents into the section body.  This pass converts common structural tags
    to their markdown equivalents and strips everything else so the DOCX export
    never sees raw HTML angle-bracket syntax.

    <table>…</table> blocks are intentionally left untouched — they are handled
    upstream by _clean_generated_tables() which converts them to pipe tables.
    <IMAGE …/> tags are also left untouched as they are resolved later.
    """
    if not content:
        return content

    # Protect <table>…</table> and <IMAGE …/> so we don't touch them.
    GUARD = "\x00TABLE\x00"
    IMAGE_GUARD = "\x00IMAGE\x00"
    tables: list[str] = []
    images: list[str] = []

    def _save_table(m):
        tables.append(m.group(0))
        return f"{GUARD}{len(tables) - 1}{GUARD}"

    def _save_image(m):
        images.append(m.group(0))
        return f"{IMAGE_GUARD}{len(images) - 1}{IMAGE_GUARD}"

    content = re.sub(r'<table[^>]*>.*?</table>', _save_table,
                     content, flags=re.DOTALL | re.IGNORECASE)
    content = re.sub(r'<IMAGE\b[^>]*/>', _save_image,
                     content, flags=re.IGNORECASE)

    # Headings → bold markdown so they still stand out visually.
    def _heading_to_bold(m):
        inner = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        return f"**{inner}**" if inner else ""

    content = re.sub(r'<h[1-6][^>]*>(.*?)</h[1-6]>',
                     _heading_to_bold, content, flags=re.DOTALL | re.IGNORECASE)

    # <title> — treat like a heading.
    content = re.sub(r'<title[^>]*>(.*?)</title>',
                     _heading_to_bold, content, flags=re.DOTALL | re.IGNORECASE)

    # <li> → markdown bullet.
    content = re.sub(r'<li[^>]*>(.*?)</li>',
                     lambda m: f"- {re.sub(r'<[^>]+>', '', m.group(1)).strip()}",
                     content, flags=re.DOTALL | re.IGNORECASE)

    # Block-level wrappers that should produce a blank line between paragraphs.
    for tag in ('p', 'div', 'section', 'article', 'blockquote', 'ul', 'ol'):
        content = re.sub(rf'</{tag}>', '\n', content, flags=re.IGNORECASE)
        content = re.sub(rf'<{tag}[^>]*>', '', content, flags=re.IGNORECASE)

    # Strip all remaining HTML tags (inline formatting etc.).
    content = re.sub(r'<[^>]+>', '', content)

    # Collapse runs of blank lines left by removed block tags (max 1 blank).
    content = re.sub(r'\n{3,}', '\n\n', content)

    # Unescape HTML entities (e.g. "&lt;information not available&gt;" or "&amp;")
    # so placeholders and ampersands render as plain text, never raw entity codes.
    try:
        import html as _html
        content = _html.unescape(content)
    except Exception:
        pass

    # Restore guarded blocks.
    for i, tbl in enumerate(tables):
        content = content.replace(f"{GUARD}{i}{GUARD}", tbl)
    for i, img in enumerate(images):
        content = content.replace(f"{IMAGE_GUARD}{i}{IMAGE_GUARD}", img)

    return content.strip()


def _strip_invented_headings(content: str, section_id: str, valid_ids=None) -> str:
    """Remove standalone section-heading lines the model invents inside a section
    body (e.g. "Section 1: Introduction", "1. Introduction", "1.1 Background",
    "1.3 Purpose"). The template/assembler supplies every real heading, so a
    generated body must be prose only — otherwise invented sub-headings collide
    with template/imported sections and produce DUPLICATE headings."""
    if not content:
        return content
    valid_ids = valid_ids or set()
    out = []
    for ln in content.split("\n"):
        bare = re.sub(r"^#{1,6}\s*", "", ln.strip())
        ph = _possible_section_heading(bare)
        if ph:
            kind, sid, _title = ph
            # Drop "Section N:" lines, and short numbered-heading lines whose id is a
            # real template section. The word cap avoids eating prose that merely
            # starts with a number.
            if (kind == "section" or sid in valid_ids) and len(bare.split()) <= 8:
                continue
        out.append(ln)
    res = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return res.strip()


def _strip_empty_headings(content: str) -> str:
    """Fix B — remove generated sub-headings that have no body beneath them.

    The model sometimes emits a sub-heading such as "Current State" or a bold
    "**Solution Design**" line and then writes nothing under it, leaving an empty
    heading in the client-facing document. This drops any heading line whose
    following lines (up to the next heading or end of section) contain no real
    text. Conservative: only markdown ATX headings (#..) and standalone
    fully-bold lines are treated as headings.
    """
    if not content:
        return content
    lines = content.split("\n")

    def is_heading(ln):
        t = ln.strip()
        if not t:
            return False
        if re.match(r"^#{1,6}\s+\S", t):
            return True
        # A line that is entirely bold text, optionally ending with a colon,
        # and reasonably short (a label, not a bold sentence).
        if re.match(r"^\*\*[^*]{1,60}\*\*:?$", t):
            return True
        return False

    keep = [True] * len(lines)
    for i, ln in enumerate(lines):
        if not is_heading(ln):
            continue
        has_body = False
        for j in range(i + 1, len(lines)):
            nxt = lines[j].strip()
            if nxt == "":
                continue
            if is_heading(lines[j]):
                break  # ran into the next heading with nothing in between
            has_body = True
            break
        if not has_body:
            keep[i] = False
    out = [ln for i, ln in enumerate(lines) if keep[i]]
    res = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return res.strip()


def _strip_redundant_section_headers(content: str, section_id: str) -> str:
    """Remove markdown # headings that duplicate the section header.

    assemble_document() already prepends 'Section X.Y: Title' above each block,
    so any '# X.Y …' or '## X.Y …' line the LLM placed at the top of the
    content creates a duplicate heading in the final document.
    """
    if not content:
        return content

    lines = content.split("\n")
    out = []
    for ln in lines:
        stripped = ln.strip()
        # Match lines like "# 1.1 Company Information" or "## 1.2 Business Context"
        m = re.match(r'^#{1,6}\s+([\d.]+)\b', stripped)
        if m and m.group(1).startswith(section_id.split(".")[0]):
            continue
        out.append(ln)

    # Drop leading blank lines that may have been created by the removal.
    result = "\n".join(out)
    return result.lstrip("\n")


def _iter_all_paragraphs(parent):
    """Yield every paragraph in the document, INCLUDING those inside table
    cells and nested tables (python-docx's doc.paragraphs misses table cells,
    so images placed inside tables were never embedded)."""
    paras = list(parent.paragraphs)
    for tbl in getattr(parent, "tables", []):
        for row in tbl.rows:
            for cell in row.cells:
                paras.extend(_iter_all_paragraphs(cell))
    return paras


def _resolve_images_in_docx(docx_path: Path) -> list:
    """Post-process DOCX to replace <IMAGE id="..."/> tags with embedded images.

    Returns a list of image ids that could NOT be embedded (missing files),
    so callers can surface a warning instead of silently dropping images."""
    import re, json
    from docx import Document
    from docx.shared import Inches

    EXTRACTED_DIR = Path(__file__).parent / "extracted_images"
    IMAGE_CHUNKS = Path(__file__).parent / "image_chunks.json"
    # Match both <IMAGE id="abc"/> and ![caption](attachment:abc) / ![caption](abc)
    TAG_RE = re.compile(
        r'<IMAGE id="([^"]+)"/>'
        r'|!\[[^\]]*\]\(attachment:([a-f0-9]{8,})\)'
        r'|!\[[^\]]*\]\(([a-f0-9]{8,})\)'
    )

    missing_ids: list = []
    try:
        image_meta: dict = {}
        if IMAGE_CHUNKS.exists():
            for rec in json.loads(IMAGE_CHUNKS.read_text(encoding="utf-8")):
                image_meta[rec["image_id"]] = rec

        doc = Document(str(docx_path))
        modified = False

        paras_to_process = [
            (i, p) for i, p in enumerate(_iter_all_paragraphs(doc)) if TAG_RE.search(p.text)
        ]

        for _, para in reversed(paras_to_process):
            # Extract image_id from whichever capture group matched
            ids = [g1 or g2 or g3 for g1, g2, g3 in TAG_RE.findall(para.text) if g1 or g2 or g3]
            p_elem = para._element
            p_parent = p_elem.getparent()
            pos = list(p_parent).index(p_elem)
            p_parent.remove(p_elem)

            insert_pos = pos
            for image_id in reversed(ids):
                meta = image_meta.get(image_id, {})
                fp = meta.get("file_path", "")
                img_path = EXTRACTED_DIR / Path(fp).name if fp else None
                if not img_path or not img_path.exists():
                    for ext in ("png", "jpeg", "jpg"):
                        candidate = EXTRACTED_DIR / f"{image_id}.{ext}"
                        if candidate.exists():
                            img_path = candidate
                            break

                if img_path and img_path.exists():
                    img_para = doc.add_paragraph()
                    img_para.add_run().add_picture(str(img_path), width=Inches(5.5))
                    ip = img_para._element
                    ip.getparent().remove(ip)
                    p_parent.insert(insert_pos, ip)

                    # Auto-caption removed. The generated content normally
                    # already carries the model's own '*Figure N: …*' line
                    # right below the IMAGE tag; appending the image index's
                    # generic caption produced a duplicate. If you ever want
                    # the auto-caption back as a fallback, only add it when
                    # the immediately-following paragraph is NOT already a
                    # 'Figure …' line — for now we trust the content.

                    modified = True
                    logger.info("Embedded image %s from %s", image_id, img_path.name)
                else:
                    logger.warning("Image file not found for id=%s", image_id)
                    missing_ids.append(image_id)

        if modified:
            doc.save(str(docx_path))
            logger.info("Image placeholders resolved in %s", docx_path.name)

    except Exception as e:
        logger.warning("_resolve_images_in_docx failed: %s", e)
        missing_ids.append("(image embedding failed: %s)" % e)
    return missing_ids


def _convert_markdown_tables_in_docx(docx_path: Path) -> None:
    """
    Post-process DOCX file to convert Markdown table syntax to actual DOCX tables.
    Finds all markdown table text in the document and converts to real tables.
    """
    import re
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    
    try:
        doc = Document(str(docx_path))
        
        # Pattern to find markdown tables
        table_pattern = r'\|(?:[^\n\|]*\|)+\n\|(?:\s*-+\s*\|)+\n(?:\|(?:[^\n\|]*\|)+\n)*'
        
        modified = False
        paragraphs_to_process = []
        
        # Find all paragraphs containing markdown tables
        for para_idx, para in enumerate(doc.paragraphs):
            text = para.text
            if '|' in text and re.search(table_pattern, text):
                paragraphs_to_process.append((para_idx, para, text))
        
        # Process from end to beginning (to maintain indices)
        for para_idx, para, text in reversed(paragraphs_to_process):
            # Split by tables
            parts = re.split(f'({table_pattern})', text)
            
            # Find paragraph position in document
            p_element = para._element
            p_parent = p_element.getparent()
            p_position = list(p_parent).index(p_element)
            
            insert_position = p_position
            
            for part in parts:
                if re.match(table_pattern, part):
                    # This block may contain MULTIPLE adjacent markdown tables
                    # (e.g. two attribute tables with the same columns). Split
                    # into (header, rows) groups: a line immediately followed
                    # by a separator (|---|) is a header that begins a new
                    # table. Build one DOCX table per group so they don't merge.
                    _is_sep = lambda r: bool(re.match(r'^\|[\s\-:|]+\|$', r.strip()))
                    blk = [ln for ln in part.strip().split('\n') if ln.strip()]
                    groups = []
                    gi = 0
                    while gi < len(blk):
                        if gi + 1 < len(blk) and _is_sep(blk[gi + 1]):
                            headers = [c.strip() for c in blk[gi].split('|') if c.strip()]
                            rows = []
                            k = gi + 2
                            while k < len(blk):
                                if _is_sep(blk[k]):
                                    break
                                if k + 1 < len(blk) and _is_sep(blk[k + 1]):
                                    break  # next line is a new header
                                cells = [c.strip() for c in blk[k].split('|') if c.strip()]
                                if len(cells) == len(headers):
                                    rows.append(cells)
                                k += 1
                            if headers:
                                groups.append((headers, rows))
                            gi = k
                        else:
                            gi += 1

                    for headers, rows in groups:
                        if not headers or not rows:
                            continue
                        tbl = doc.add_table(rows=len(rows) + 1, cols=len(headers))
                        tbl.style = 'Light Grid Accent 1'
                        hdr_cells = tbl.rows[0].cells
                        for col_idx, hdr_text in enumerate(headers):
                            if col_idx < len(hdr_cells):
                                hdr_cells[col_idx].text = hdr_text
                                for paragraph in hdr_cells[col_idx].paragraphs:
                                    for run in paragraph.runs:
                                        run.bold = True
                                        run.font.size = Pt(11)
                                    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        for row_idx, row_data in enumerate(rows):
                            row_cells = tbl.rows[row_idx + 1].cells
                            for col_idx, cell_text in enumerate(row_data):
                                if col_idx < len(row_cells):
                                    row_cells[col_idx].text = cell_text
                                    for paragraph in row_cells[col_idx].paragraphs:
                                        for run in paragraph.runs:
                                            run.font.size = Pt(10)
                                        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
                        tbl_element = tbl._element
                        tbl_element.getparent().remove(tbl_element)
                        p_parent.insert(insert_position, tbl_element)
                        insert_position += 1
                        modified = True
                else:
                    # Regular text - add as paragraph
                    if part.strip():
                        new_para = doc.add_paragraph(part)
                        new_para_elem = new_para._element
                        new_para_elem.getparent().remove(new_para_elem)
                        p_parent.insert(insert_position, new_para_elem)
                        insert_position += 1
            
            # Remove original paragraph with markdown table text
            p_parent.remove(p_element)
        
        if modified:
            doc.save(str(docx_path))
            logger.info("Converted markdown tables to DOCX tables: %s", docx_path.name)
    
    except Exception as e:
        logger.warning("Failed to post-process tables in DOCX: %s", e)
        # Non-fatal - document still works, just with markdown text


def _process_sections_for_template(sections):
    """
    Process sections to ensure tables are properly formatted for template insertion.
    Handles Markdown table syntax and ensures proper table formatting.
    """
    import re
    
    processed = {}
    
    for section_id, content in sections.items():
        if not isinstance(content, str):
            processed[section_id] = content
            continue
        
        # Pattern to find Markdown tables
        # | header1 | header2 |
        # | --- | --- |
        # | data1 | data2 |
        table_pattern = r'\|(?:[^\n]*\|)+\n\|(?:\s*-+\s*\|)+\n(?:\|(?:[^\n]*\|)+\n)*'
        
        # Check if content has tables
        if '|' in content and re.search(table_pattern, content):
            # Tables exist - ensure they're properly formatted
            # Split content by tables to preserve them
            parts = re.split(f'({table_pattern})', content)
            
            processed_content = []
            for i, part in enumerate(parts):
                if re.match(table_pattern, part):
                    # This is a table - clean it up
                    lines = part.strip().split('\n')
                    
                    # Reconstruct table with consistent formatting
                    if len(lines) >= 2:
                        cleaned_lines = []
                        for line in lines:
                            # Clean up extra spaces around pipes
                            line = line.strip()
                            if line.startswith('|'):
                                line = line[1:]  # Remove leading pipe
                            if line.endswith('|'):
                                line = line[:-1]  # Remove trailing pipe
                            # Split and rejoin to normalize
                            cells = [cell.strip() for cell in line.split('|')]
                            cleaned_line = '| ' + ' | '.join(cells) + ' |'
                            cleaned_lines.append(cleaned_line)
                        
                        cleaned_table = '\n'.join(cleaned_lines)
                        processed_content.append(cleaned_table)
                else:
                    # Regular text
                    if part.strip():
                        processed_content.append(part)
            
            processed[section_id] = '\n'.join(processed_content)
        else:
            # No tables - keep as is
            processed[section_id] = content
    
    return processed


def _prepare_research_context_with_sites(research_context, proj_chunks):
    """
    Enhance research_context with COMPLETE sites data from Overview_sites.xlsx.
    
    Appends the full sites table to the research context so LLM has access to
    ALL sites, not just a chunked subset.
    """
    # Extract complete sites data
    complete_sites = _extract_complete_sites_data(proj_chunks)
    
    if complete_sites:
        sites_section = f"""

========== COMPLETE SITES DATA (Overview_sites.xlsx) ==========
This is the COMPLETE sites list from Overview_sites.xlsx.
Include ALL of these sites in Section 3.2 - do not omit any sites.

{complete_sites}

========== END SITES DATA ==========
"""
        research_context = research_context + sites_section
    
    return research_context

@router.post("/api/image_search")
def image_search_handler(body: dict):
    """Search image index"""
    try:
        from image_index import search_images
        
        query = body.get("query", "")
        top_k = body.get("top_k", 5)
        
        results = search_images(query=query, top_k=top_k)
        return {"results": results, "total": len(results)}
    except Exception as e:
        logger.error(f"Image search failed: {e}")
        return {"results": [], "total": 0}


@router.get("/api/images/{image_id}")
def get_image_handler(image_id: str):
    """Serve an extracted image by ID."""
    try:
        EXTRACTED_DIR = Path(__file__).parent / "extracted_images"

        # Try by filename derived from image_id first (fastest, no index needed)
        for ext in ("png", "jpeg", "jpg", "gif", "webp"):
            candidate = EXTRACTED_DIR / f"{image_id}.{ext}"
            if candidate.exists():
                return FileResponse(path=str(candidate), media_type=f"image/{ext}")

        # Fall back to index lookup (handles non-standard filenames)
        try:
            from image_index import get_image_by_id
        except ImportError:
            from app.image_index import get_image_by_id

        record = get_image_by_id(image_id)
        if not record:
            return JSONResponse({"error": "Image not found"}, status_code=404)

        # Resolve stored path — stored as relative or absolute
        stored = Path(record.get("file_path", ""))
        if stored.is_absolute() and stored.exists():
            file_path = stored
        else:
            # Try as filename inside extracted_images/
            file_path = EXTRACTED_DIR / stored.name
            if not file_path.exists():
                file_path = Path(__file__).parent / stored

        if not file_path.exists():
            return JSONResponse({"error": "File not found"}, status_code=404)

        ext = record.get("ext", file_path.suffix.lstrip(".")) or "png"
        return FileResponse(path=str(file_path), media_type=f"image/{ext}")
    except Exception as e:
        logger.error("Image serve failed for %s: %s", image_id, e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Image tag management ──────────────────────────────────────────────────────

_IMAGE_CHUNKS_PATH = Path(__file__).parent / "image_chunks.json"
_IMG_TAG_LOCK = __import__("threading").Lock()


def _load_image_chunks() -> list:
    if _IMAGE_CHUNKS_PATH.exists():
        try:
            return json.loads(_IMAGE_CHUNKS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


def _save_image_chunks(records: list) -> None:
    with _IMG_TAG_LOCK:
        _IMAGE_CHUNKS_PATH.write_text(
            json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
        )


class ImageTagRequest(BaseModel):
    action: str  # "add" or "remove"
    tag: str


@router.put("/api/images/{image_id}/tags")
def update_image_tags(image_id: str, req: ImageTagRequest):
    """Add or remove a tag on a single image record in image_chunks.json."""
    tag = req.tag.strip()
    if not tag:
        raise HTTPException(400, "tag cannot be empty")
    if req.action not in ("add", "remove"):
        raise HTTPException(400, "action must be 'add' or 'remove'")
    records = _load_image_chunks()
    for rec in records:
        if rec.get("image_id") == image_id:
            tags: list = rec.setdefault("tags", [])
            if req.action == "add" and tag not in tags:
                tags.append(tag)
            elif req.action == "remove" and tag in tags:
                tags.remove(tag)
            _save_image_chunks(records)
            try:
                from app.image_index import reload_image_index
            except ImportError:
                from image_index import reload_image_index
            reload_image_index()
            return {"image_id": image_id, "tags": tags}
    raise HTTPException(404, "Image not found")


_KINAXIS_CAPTION_RE = re.compile(r"^\s*(Level\s*\d+|L\d+)\s+", re.IGNORECASE)
_KINAXIS_TAG = "Kinaxis Process Diagram"


@router.post("/api/images/reload-index")
def reload_image_index_endpoint():
    """Rebuild the in-memory BM25 image index from image_chunks.json on disk.
    Call this after uploading/re-indexing files so auto-suggest picks up new images
    without a full server restart.
    """
    try:
        try:
            from app.image_index import reload_image_index
        except ImportError:
            from image_index import reload_image_index
        count = reload_image_index()
        logger.info("Image index reloaded via API: %d records", count)
        return {"ok": True, "records": count}
    except Exception as e:
        logger.error("reload-image-index failed: %s", e)
        raise HTTPException(500, str(e))


@router.post("/api/images/auto-tag-kinaxis")
def auto_tag_kinaxis():
    """Scan image_chunks.json; tag images whose caption starts with 'Level N'."""
    records = _load_image_chunks()
    tagged = 0
    for rec in records:
        if _KINAXIS_CAPTION_RE.match(rec.get("caption", "")):
            tags = rec.setdefault("tags", [])
            if _KINAXIS_TAG not in tags:
                tags.append(_KINAXIS_TAG)
                tagged += 1
    _save_image_chunks(records)
    try:
        from app.image_index import reload_image_index
    except ImportError:
        from image_index import reload_image_index
    reload_image_index()
    logger.info("auto-tag-kinaxis: tagged %d of %d images", tagged, len(records))
    return {"tagged": tagged, "total": len(records)}


def _verbatim_merge_export(pid: str):
    """Build the final doc FROM the original uploaded .docx (imported content kept
    byte-for-byte) and insert the generated sections in template order.

    Returns a Path on success, or None when verbatim merge is NOT safe to use
    (e.g. an imported section was edited or deleted — those edits live in the
    store, not the file, so the text merge must handle them). Raises on hard
    errors; the caller falls back to the text export in every non-success case.
    """
    from docx import Document
    base_dir = DOCUMENT_IMPORT_ROOT / _safe_project_key(pid)
    docs = sorted([f for f in base_dir.glob("*.docx") if f.is_file()],
                  key=lambda f: f.stat().st_mtime, reverse=True) if base_dir.exists() else []
    if not docs:
        return None
    original = docs[0]

    live = _get_live_sections(pid)
    order = [s["id"] for s in live]
    pos = {sid: i for i, sid in enumerate(order)}
    titles = {s["id"]: (s.get("title") or "") for s in live}
    valid = set(order)

    kw = _load_keyword_store(pid)
    imported_ids = {sid for sid, e in kw.items() if isinstance(e, dict) and e.get("imported_at")}
    ap = (_load_approved_store(pid).get("sections") or {})

    # SAFETY: if any imported section was deleted (archived) or edited (its stored
    # content differs from the file), verbatim-from-file would be wrong — bail out.
    archived_ids = {x.get("id") for x in _get_archived_sections(pid) if x.get("id")}
    if archived_ids & imported_ids:
        return None
    try:
        file_secs = (_extract_sections_from_docx(original, pid).get("sections") or {})
    except Exception:
        file_secs = {}
    def _norm(t): return re.sub(r"\s+", " ", (t or "")).strip()
    for sid in imported_ids:
        if sid in ap and sid in file_secs and _norm(ap[sid]) != _norm(file_secs[sid]):
            return None  # an imported section was edited → let text export handle it
    # If any section present in the original file is no longer locked (was unlocked
    # for regeneration), fall back to the text merge so the regenerated content is
    # used instead of the old file copy (avoids duplicates).
    if any(fsid not in imported_ids for fsid in file_secs.keys()):
        return None

    # Sections to insert = generated/new (not imported), present in template + store.
    gen_ids = [sid for sid in order if sid in ap and sid not in imported_ids and (ap.get(sid) or "").strip()]

    doc = Document(str(original))

    def heading_map():
        m = {}
        for i, par in enumerate(doc.paragraphs):
            ph = _possible_section_heading(par.text)
            if ph and ph[1] in valid and ph[1] not in m:
                m[ph[1]] = i
        return m

    for gid in gen_ids:
        body = (ap.get(gid) or "").strip()
        hmap = heading_map()
        # reference = heading paragraph of the first existing section AFTER gid
        ref = None
        later = sorted([(pos[s], idx) for s, idx in hmap.items() if pos.get(s, 9999) > pos.get(gid, 9999)])
        if later:
            ref = doc.paragraphs[later[0][1]]
        # build heading + body paragraphs (appended, then moved before ref)
        built = []
        h = doc.add_paragraph()
        r = h.add_run((gid + "  " + titles.get(gid, "")).strip()); r.bold = True
        built.append(h)
        for line in body.split("\n"):
            built.append(doc.add_paragraph(line))
        if ref is not None:
            for par in built:
                ref._p.addprevious(par._p)

    out_dir = _APP_DIR / "static" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ("BD_merged_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".docx")
    doc.save(str(out))
    try: _convert_markdown_tables_in_docx(out)
    except Exception as e: logger.warning("verbatim merge: table convert failed: %s", e)
    global _LAST_EXPORT_MISSING_IMGS
    _LAST_EXPORT_MISSING_IMGS = []
    try: _LAST_EXPORT_MISSING_IMGS = _resolve_images_in_docx(out)
    except Exception as e: logger.warning("verbatim merge: image resolve failed: %s", e)
    # SAFETY NET: scrub any previous-client (Arclin) name carried in from the
    # original imported .docx.
    try:
        try:
            from app.brd_workflow import neutralize_legacy_client_name as _neut
        except ImportError:
            from brd_workflow import neutralize_legacy_client_name as _neut
        _n = _neut(out, _get_client_company(project_id=pid))
        if _n:
            logger.info("verbatim merge: neutralized %d legacy client-name occurrence(s)", _n)
    except Exception as _e:
        logger.warning("verbatim merge: legacy client-name neutralize skipped: %s", _e)
    # Validate the result actually opens; otherwise discard and fall back.
    try:
        Document(str(out))
    except Exception as e:
        logger.warning("verbatim merge produced an unreadable file: %s", e)
        return None
    logger.info("verbatim merge: built %s (inserted %d generated section(s))", out.name, len(gen_ids))
    return out


@router.post("/api/document/update-export")
def update_export(body: dict):
    """Merge export for the Update Existing Document workflow.

    Guarantees the final document contains BOTH the imported (preserved) sections
    AND the newly generated sections — in template order — by:
      1. Re-applying the imported sections from the original uploaded DOCX (self-heal),
         so they are present in the approved store even if Apply was skipped.
      2. Exporting ALL approved sections with NO section_ids filter (the generic
         export filters to a subset when section_ids is passed, which is what
         produced a "separate document" containing only the generated sections).
    """
    pid = (body or {}).get("project_id")
    if not pid:
        return {"ok": False, "error": "No project_id supplied."}
    # 1) Self-heal: re-apply imported sections from the stored original DOCX.
    try:
        base_dir = DOCUMENT_IMPORT_ROOT / _safe_project_key(pid)
        docs = []
        if base_dir.exists():
            docs = sorted([f for f in base_dir.glob("*.docx") if f.is_file()],
                          key=lambda f: f.stat().st_mtime, reverse=True)
        if docs:
            parsed = _extract_sections_from_docx(docs[0], pid)
            imported = parsed.get("sections") or {}
            if imported:
                _ap = _load_approved_store(pid)
                _already = set((_ap.get("sections") or {}).keys())
                _archived = {x.get("id") for x in _get_archived_sections(pid) if x.get("id")}
                _removed = set(_ap.get("removed_imports", []))
                # Only restore imported sections that are MISSING, NOT deleted,
                # and NOT removed-from-import (unlocked) by the user.
                # Never overwrite a user edit, resurrect a deleted section, or
                # bring back a section the user explicitly removed.
                _to_apply = {sid: c for sid, c in imported.items()
                             if sid not in _already and sid not in _archived
                             and sid not in _removed}
                if _to_apply:
                    _apply_imported_sections(pid, _to_apply, mode="merge")
                logger.info("update-export: restored %d of %d imported section(s) for %s",
                            len(_to_apply), len(imported), pid)
    except Exception as e:
        logger.warning("update-export: self-heal import re-apply failed: %s", e)
    # 2a) Preferred: verbatim merge that preserves the original file byte-for-byte
    #     and inserts the generated sections. Safe-guards inside return None when
    #     it should not be used (edited/deleted imported sections, etc.).
    try:
        _vb = _verbatim_merge_export(pid)
        if _vb is not None and _vb.exists():
            _miss = globals().get("_LAST_EXPORT_MISSING_IMGS") or []
            _warns = []
            if _miss:
                _shown = ", ".join(_miss[:5]) + ("…" if len(_miss) > 5 else "")
                _warns.append(
                    f"{len(_miss)} image(s) could not be embedded in the document ({_shown}). "
                    "They were removed from the export."
                )
            return {"ok": True,
                    "docx_url": f"/api/document/download/docx/{_vb.name}",
                    "filename": _vb.name,
                    "merge": "verbatim",
                    "warnings": _warns}
    except Exception as _ve:
        logger.warning("update-export: verbatim merge failed, using text merge: %s", _ve)

    # 2b) Fallback text merge — assembles ALL approved sections (no section_ids
    #     filter) through the template. Handles edited/deleted imported sections.
    return export_document({
        "project_id": pid,
        "user_email": (body or {}).get("user_email", ""),
        "user_name": (body or {}).get("user_name", ""),
        "revision_description": (body or {}).get("revision_description", "Updated via Update Existing Document"),
    })


@router.post("/api/document/export")
def export_document(body: dict):
    """
    Assemble all approved sections into a Blueprint Document DOCX.
    Uses the existing template if available, otherwise creates new document.
    Returns a download URL.
    """
    state = _get_screen3_state()
    if not state:
        return {"ok": False, "error": "No session available."}

    try:
        from app.brd_workflow import assemble_document, save_docx, save_txt, save_docx_from_template
    except ImportError:
        try:
            from brd_workflow import assemble_document, save_docx, save_txt, save_docx_from_template
        except ImportError as e:
            return {"ok": False, "error": f"Could not import brd_workflow: {e}"}

    # Check at least one section has been approved
    has_content = any(
        getattr(state, field, None)
        for field in SECTION_STATE_MAP.values()
    )
    if not has_content:
        ap_store_check = _load_approved_store(body.get("project_id"))
        has_content = bool(ap_store_check.get("sections"))
    if not has_content:
        return {"ok": False, "error": "No sections have been approved yet. Use 'Save to Blueprint Document' on each section first, then export."}

    try:
        # Get live titles and approved content
        # Read the live sections for THIS project — custom sections like
        # "7.1.3" live in screen3_sections_{pid}.json, not the global file.
        _pid_for_titles = body.get("project_id") if body else None
        live_sections = _get_live_sections(_pid_for_titles)
        archived_sections = _get_archived_sections(_pid_for_titles)
        live_title_map: dict = {s["id"]: s["title"] for s in live_sections if s.get("title")}
        # Include archived too so renamed-then-archived sections still resolve.
        for s in archived_sections:
            if s.get("id") and s.get("title") and s["id"] not in live_title_map:
                live_title_map[s["id"]] = s["title"]

        _missing_imgs: list = []  # image ids that fail to embed (surfaced as warnings)
        all_approved: dict = {}
        for field in SECTION_STATE_MAP.values():
            sec_data = getattr(state, field, None) or {}
            all_approved.update(sec_data)

        ap_store = _load_approved_store(body.get("project_id"))
        for sid, content in ap_store.get("sections", {}).items():
            if sid not in all_approved:
                all_approved[sid] = content

        # Clean any hallucinated markdown image refs from approved content so
        # the export never carries broken ![caption](made_up.png) placeholders.
        # Real <IMAGE id="..."/> tags survive and are embedded downstream by
        # _resolve_images_in_docx.
        _kw_exp = _load_keyword_store(body.get("project_id")) if body.get("project_id") else {}
        for sid in list(all_approved.keys()):
            if all_approved[sid]:
                _pins = (_kw_exp.get(sid, {}) or {}).get("pinned_image_ids", []) or []
                all_approved[sid] = _clean_generated_tables(
                    _sanitize_image_refs(all_approved[sid], pinned_image_ids=_pins)
                )

        # Heading-only sections: keep them in the section list (so the
        # heading still prints) but blank their body content so save_docx /
        # save_docx_from_template emit nothing under them.
        heading_only_ids = set(ap_store.get("heading_only_ids", []))
        if heading_only_ids:
            for sid in heading_only_ids:
                all_approved[sid] = ""
            logger.info("Export: %d sections flagged heading-only", len(heading_only_ids))

        # Caller-driven section filter: when the UI sends `section_ids`, only
        # export EXACTLY those sections. No ancestor auto-inclusion — if the
        # user picked "1.1" but not "1", we must not emit a "1 Introduction"
        # heading just to host it. Same rule for new/modified sections.
        requested = (body or {}).get("section_ids") or []
        if requested:
            wanted = set(requested)
            all_approved = {sid: c for sid, c in all_approved.items() if sid in wanted}
            heading_only_ids &= wanted
            for field in SECTION_STATE_MAP.values():
                cur = getattr(state, field, None) or {}
                filtered = {sid: v for sid, v in cur.items() if sid in wanted}
                setattr(state, field, filtered)
            logger.info(
                "Export: strict filter to %d requested section ids — no ancestor expansion",
                len(requested),
            )

        # "No heading" sections — drop them from the section list entirely so
        # neither their heading nor body shows up in the export.
        no_heading_ids = set(ap_store.get("no_heading_ids", []))
        if requested:
            no_heading_ids &= set(requested)
        for sid in no_heading_ids:
            all_approved.pop(sid, None)
            for field in SECTION_STATE_MAP.values():
                cur = getattr(state, field, None) or {}
                cur.pop(sid, None)
                setattr(state, field, cur)
            heading_only_ids.discard(sid)

        live_order = [s["id"] for s in live_sections]
        def _order_key(sid):
            try:   return live_order.index(sid)
            except ValueError: return 9999

        for sid in sorted(all_approved.keys(), key=_order_key):
            top   = sid.split(".")[0]
            field = SECTION_STATE_MAP.get(top, "di_sections")
            current = getattr(state, field, None) or {}
            current[sid] = all_approved[sid]
            setattr(state, field, current)

        logger.info("Assembling with %d live titles", len(live_title_map))

        # Assemble plain text document
        doc_text = assemble_document(
            section1_sections=state.section1_sections or {},
            br_sections=state.br_sections or {},
            ssc_sections=state.ssc_sections or {},
            dp_sections=state.dp_sections or {},
            ip_sections=state.ip_sections or {},
            di_sections=state.di_sections or {},
        )

        # Replace section headers with live titles
        import re as _re
        def _replace_section_header(m):
            sid   = m.group(1)
            title = live_title_map.get(sid, m.group(2))
            return f"Section {sid}: {title}"
        doc_text = _re.sub(
            r"Section (\d+): ([^\n]+)",
            _replace_section_header,
            doc_text
        )

        # Save DOCX
        output_dir = _APP_DIR / "static" / "exports"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Check if template exists for this project
        project_id_export = body.get("project_id") if body else None
        tmpl_path = _template_path(project_id_export)
        
        logger.info("Looking for template for project: %s", project_id_export)
        if tmpl_path:
            logger.info("Template found at: %s", tmpl_path)
            # Use template - this is the preferred method
            try:
                client_name_export = state.company if state and state.company else ""
                if not client_name_export:
                    # Fall back to the project's own client/name so the
                    # exported template's [customer] placeholder is filled
                    # even when client context was never provided.
                    client_name_export = _get_client_company(project_id=project_id_export)
                logger.info("Processing sections for template compatibility")
                # Process sections to ensure proper table formatting
                processed_sections = _process_sections_for_template(all_approved)
                logger.info("Exporting with template using save_docx_from_template")
                # Forward the logged-in user + description so the template's
                # Revision History row gets a real name/date instead of a
                # placeholder, and so untouched sections can be stripped.
                _user_email = (body or {}).get("user_email") or ""
                _user_name  = (body or {}).get("user_name")  or _user_email or "Author"
                _rev_desc   = (body or {}).get("revision_description") or "Initial draft"
                docx_path = save_docx_from_template(
                    sections=processed_sections,
                    template_path=tmpl_path,
                    output_dir=output_dir,
                    client_name=client_name_export,
                    revision_user=_user_name,
                    revision_email=_user_email,
                    revision_description=_rev_desc,
                    strip_empty_sections=True,
                    heading_only_ids=heading_only_ids,
                    strict_selection=bool(requested),
                    title_map=live_title_map,
                )
                logger.info("Successfully exported using template: %s", docx_path.name)
                # Post-process: convert markdown tables + embed images
                _convert_markdown_tables_in_docx(docx_path)
                _missing_imgs = _resolve_images_in_docx(docx_path)
            except Exception as tmpl_err:
                logger.error("Template export failed: %s", tmpl_err, exc_info=True)
                logger.warning("Falling back to default format")
                docx_path = save_docx(
                    doc_text, output_dir, state=state,
                    heading_only_ids=heading_only_ids,
                    no_heading_ids=no_heading_ids,
                    strict_selection=bool(requested),
                    title_map=live_title_map,
                )
                _missing_imgs = _resolve_images_in_docx(docx_path)
        else:
            # No template found - use default format
            logger.info("No template found for project %s, using default format", project_id_export)
            docx_path = save_docx(
                doc_text, output_dir, state=state,
                heading_only_ids=heading_only_ids,
                no_heading_ids=no_heading_ids,
                strict_selection=bool(requested),
                title_map=live_title_map,
            )
            _missing_imgs = _resolve_images_in_docx(docx_path)

        state.docx_path = str(docx_path)

        # SAFETY NET: guarantee no previous-client (Arclin) name survives in the
        # finished document, whatever path produced it.
        try:
            try:
                from app.brd_workflow import neutralize_legacy_client_name as _neut
            except ImportError:
                from brd_workflow import neutralize_legacy_client_name as _neut
            _cn = _get_client_company(project_id=project_id_export)
            _n = _neut(docx_path, _cn)
            if _n:
                logger.info("export: neutralized %d legacy client-name occurrence(s)", _n)
        except Exception as _e:
            logger.warning("export: legacy client-name neutralize skipped: %s", _e)

        txt_path = save_txt(doc_text, output_dir)
        state.txt_path = str(txt_path)

        logger.info("Blueprint Document exported: %s", docx_path.name)
        version_meta = None
        try:
            version_meta = _write_version_snapshot(
                project_id=project_id_export,
                message=f"Exported Blueprint Document: {docx_path.name}",
                author=(body or {}).get("user_name") or (body or {}).get("user_email") or "",
                source="export",
            )
        except Exception as e:
            logger.warning("Could not create export version snapshot: %s", e)

        _warnings = []
        if _missing_imgs:
            _shown = ", ".join(_missing_imgs[:5]) + ("…" if len(_missing_imgs) > 5 else "")
            _warnings.append(
                f"{len(_missing_imgs)} image(s) could not be embedded in the document ({_shown}). "
                "They were removed from the export."
            )
        return {
            "ok":      True,
            "docx_url": f"/api/document/download/docx/{docx_path.name}",
            "txt_url":  f"/api/document/download/txt/{txt_path.name}",
            "filename": docx_path.name,
            "version":  version_meta,
            "warnings": _warnings,
        }

    except Exception as e:
        logger.exception("Export failed")
        return {"ok": False, "error": str(e)}


@router.get("/api/document/download/{fmt}/{filename}")
def download_document(fmt: str, filename: str):
    """Serve the exported DOCX or TXT file as a forced download."""
    from fastapi.responses import FileResponse as FR
    from fastapi import HTTPException
    export_dir = _APP_DIR / "static" / "exports"
    # Sanitize: strip directory components to prevent path traversal
    filename = Path(filename).name
    if not filename or filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    file_path = (export_dir / filename).resolve()
    if not str(file_path).startswith(str(export_dir.resolve())):
        raise HTTPException(status_code=400, detail="Invalid filename.")
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found.")
    if fmt == "docx":
        media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    else:
        media = "text/plain"
    response = FR(
        path=str(file_path),
        media_type=media,
        filename=filename,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )
    return response


@router.post("/api/document/reset")
def reset_document(body: dict = None):
    """Clear all approved sections from memory and disk."""
    global _screen3_state
    _screen3_state = None
    pid = (body or {}).get("project_id")
    # Wipe the disk store
    _save_approved_store({}, pid)
    # Re-initialise fresh state
    _get_screen3_state()
    logger.info("Blueprint Document reset — all approved sections cleared from memory and disk.")
    return {"ok": True, "message": "Blueprint Document reset. All approved sections cleared."}


@router.post("/api/sections/snapshot")
def apply_snapshot(body: dict):
    """
    Overwrite the entire active+archived section structure in one call.
    Used by undo/redo to apply a previous state snapshot.
    """
    pid      = body.get("project_id")
    active   = body.get("active", [])
    archived = body.get("archived", [])
    _persist_sections(active, archived, pid)
    logger.info("Snapshot applied: %d active, %d archived (project=%s)", len(active), len(archived), pid)
    return {"ok": True}


@router.post("/api/sections/reset-to-defaults")
def reset_to_defaults(body: dict = None):
    """
    Full reset of the Template Editor for a specific project.
    Deletes per-project keyword, approved, and sources stores.
    """
    global _screen3_state
    body = body or {}
    project_id = body.get("project_id")
    try:
        if project_id:
            paths_to_delete = [
                _sections_store_path(project_id),
                _keyword_store_path(project_id),
                _approved_store_path(project_id),
                _sources_store_path(project_id),
            ]
        else:
            paths_to_delete = [
                SECTIONS_STORE_PATH, KEYWORD_STORE_PATH,
                APPROVED_STORE_PATH, SOURCES_STORE_PATH,
            ]
        for path in paths_to_delete:
            if path.exists():
                path.unlink()
                logger.info("Deleted %s", path.name)
        
        # Reinitialize with ALL sections as active (none archived)
        # This ensures next load will have all sections, not partial list
        _persist_sections(
            active=[dict(s) for s in ALL_SECTIONS],
            archived=[],
            project_id=project_id
        )
        logger.info("Reinitialized sections store with all %d sections as active.", len(ALL_SECTIONS))
        
        _screen3_state = None
        logger.info("Template Editor reset (project=%s).", project_id or "global")
        return {"ok": True}
    except Exception as e:
        logger.exception("Reset failed")
        return {"ok": False, "error": str(e)}


@router.get("/api/project/{project_id}/chunks-browser")
def chunks_browser(project_id: str, section_id: str = None, search: str = ""):
    """Return all chunks scored by relevance for the chunk browser UI.

    Strictly scoped to documents the user uploaded to THIS project's source
    directory. Chunks from linked Repositories, stale prior projects, or
    orphan entries are filtered out so the panel matches the file list on
    the Document Upload screen.
    """
    try:
        from app.semantic_search import is_semantic_enabled
    except ImportError:
        from semantic_search import is_semantic_enabled
    try:
        from app.project_routes import UPLOAD_ROOT
    except ImportError:
        from project_routes import UPLOAD_ROOT

    semantic_on = is_semantic_enabled()
    logger.info(
        "chunks-browser request: project_id=%s section_id=%s search=%r semantic_on=%s search_backend_env=%s openai_key_present=%s",
        project_id, section_id, search, semantic_on,
        os.getenv("SEARCH_BACKEND", "bm25"), bool(os.getenv("OPENAI_API_KEY", "").strip()),
    )

    all_chunks = _load_all_project_chunks(project_id)
    before_scope = len(all_chunks)
    logger.info(
        "chunks-browser pre-scope: project_id=%s loaded_chunks=%d embedded=%d missing_embedding=%d first_chunk_id=%s last_chunk_id=%s",
        project_id, len(all_chunks),
        sum(1 for c in all_chunks if c.get("embedding")),
        sum(1 for c in all_chunks if not c.get("embedding")),
        (all_chunks[0].get("chunk_id") if all_chunks else None),
        (all_chunks[-1].get("chunk_id") if all_chunks else None),
    )

    # Build the set of filenames the user uploaded — both directly to this
    # project AND through any attached Repository. We want the panel to
    # mirror the editor's true source corpus, so adding a doc via either
    # upload flow makes it show up here.
    project_docs: set = _project_source_filenames(project_id)

    if project_docs:
        before = len(all_chunks)
        all_chunks = [
            c for c in all_chunks
            if (c.get("doc_name", "") or "").lower() in project_docs
        ]
        if before != len(all_chunks):
            logger.info(
                "chunks-browser: scoped to project uploads — %d of %d chunks kept (project %s, %d source files)",
                len(all_chunks), before, project_id, len(project_docs),
            )

    logger.info(
        "chunks-browser post-scope: project_id=%s kept_chunks=%d embedded=%d source_files=%d",
        project_id, len(all_chunks),
        sum(1 for c in all_chunks if c.get("embedding")),
        len(project_docs),
    )
    if not all_chunks:
        return {"total": 0, "chunks": [], "semantic_enabled": semantic_on, "debug": f"no chunks found for project {project_id}"}

    # ── Semantic search path (free-text query only) ───────────────────────────
    if search and semantic_on:
        try:
            from app.semantic_search import semantic_search
        except ImportError:
            from semantic_search import semantic_search

        logger.info(
            "chunks-browser semantic path: query=%r candidates=%d top_k=%d",
            search, len(all_chunks), len(all_chunks),
        )
        scored = semantic_search(search, all_chunks, top_k=len(all_chunks))
        results = []
        for score, chunk in scored:
            text = chunk.get("chunk_text", "") or ""
            results.append({
                "id":         chunk.get("chunk_id", 0),
                "doc_name":   chunk.get("doc_name", ""),
                "page":       chunk.get("page_number"),
                "heading":    chunk.get("section_heading") or "(No heading)",
                "keywords":   (chunk.get("keywords") or [])[:15],
                "snippet":    text[:300],
                "chunk_text": text,
                "score":      round(score, 4),
                "file_id":    chunk.get("file_id", ""),
                "brd_section_id": chunk.get("brd_section_id", ""),
            })
        logger.info("chunks-browser [semantic]: %d results for query %r", len(results), search)
        return {"total": len(results), "chunks": results, "search_backend": "semantic", "semantic_enabled": True, "debug": {"before_scope": before_scope, "after_scope": len(all_chunks)}}

    # ── BM25 keyword path (default or section_id browse) ─────────────────────
    STOP = {"the","and","for","with","from","that","this","are","was","will","have","been","their",
            "each","also","into","its","not","has"}
    search_words: set = set()
    sec_title = ""
    parent_title = ""
    if section_id:
        sections = _get_live_sections(project_id) + _get_archived_sections(project_id)
        sec_title = next((s.get("title","") for s in sections if s.get("id")==section_id), "")
        parent_id = section_id.split(".")[0]
        parent_title = next((s.get("title","") for s in sections
                             if s.get("id")==parent_id and s.get("id")!=section_id), "")
        search_words = {w.lower() for w in (sec_title+" "+parent_title).split()
                       if len(w)>3 and w.lower() not in STOP}
    elif search:
        search_words = {w.lower() for w in search.split() if len(w)>2}

    # Semantic similarity scores (chunk_id → score in [0,1]) for the
    # *section* — works for every template, including custom sections that
    # the hard-coded keyword scorer can't see. We blend this into the final
    # rank so the legacy heuristic still acts as a tiebreaker / fallback.
    sim_scores: dict = {}
    llm_picked_ids: set = set()
    if section_id:
        user_extra = ""
        kw_entry = {}
        try:
            kw_entry = _load_keyword_store(project_id).get(section_id, {}) if project_id else {}
            user_extra = " ".join(
                (kw_entry.get("doc_keywords") or [])[:10]
                + (kw_entry.get("web_keywords") or [])[:5]
            )
        except Exception:
            kw_entry = {}
            user_extra = ""
        # Sibling titles let the embedder push away from neighbouring
        # sections that share vocabulary.
        sibling_titles = [
            s.get("title", "") for s in sections
            if s.get("id", "").startswith(section_id.rsplit(".", 1)[0] + ".")
            and s.get("id") != section_id
            and s.get("id", "").count(".") == section_id.count(".")
        ][:8]
        sim_scores = _semantic_section_scores(
            section_id=section_id,
            chunks=all_chunks,
            project_id=project_id,
            sec_title=sec_title,
            parent_title=parent_title,
            user_query_extra=user_extra,
            structure=kw_entry.get("structure", "") if isinstance(kw_entry, dict) else "",
            prompt=kw_entry.get("prompt", "") if isinstance(kw_entry, dict) else "",
            sibling_titles=sibling_titles,
        )
        # The LLM matcher builds a global section→chunk map
        # in the background. When fresh, its picks are far more accurate
        # than keyword overlap and we boost them strongly here.
        try:
            cached = _llm_match_cache.get(project_id) or {}
            mapping = cached.get("mapping") or {}
            llm_picked_ids = set(mapping.get(section_id, []) or [])
        except Exception:
            llm_picked_ids = set()

    results = []
    for chunk in all_chunks:
        heading  = chunk.get("section_heading","") or ""
        text     = chunk.get("chunk_text","") or ""
        kws      = chunk.get("keywords",[]) or []
        doc_name = chunk.get("doc_name","") or ""
        score = 0.0
        if search_words:
            h = heading.lower(); t = text.lower()
            k = " ".join(kws).lower(); d = doc_name.lower()
            score += sum(10 for w in search_words if w in d)
            score += sum(3  for w in search_words if w in h)
            score += sum(2  for w in search_words if w in k)
            score += sum(1  for w in search_words if w in t)
        if section_id:
            title_tokens = {w.lower() for w in (sec_title+" "+parent_title).split() if len(w)>3}
            score += _score_chunk_for_section(chunk, section_id, title_tokens)
            # Boost: semantic similarity dominates because it generalises to
            # any template. Cosine scores live in [-1,1] but in practice
            # cluster in 0-0.8; scale ×40 so a strong semantic match easily
            # outweighs a noisy doc-hint match.
            sim = sim_scores.get(chunk.get("chunk_id"))
            if sim is not None:
                score += max(0.0, sim) * 40.0
            # Chunks the LLM matcher explicitly assigned to THIS section
            # get a large authoritative boost. The matcher reads the full
            # section list with titles + sample text, so its judgements are
            # the highest-signal evidence we have.
            if llm_picked_ids and chunk.get("chunk_id") in llm_picked_ids:
                score += 50.0
            # Chunks whose source-doc heading literally contains a title
            # token are usually the right ones — small extra boost so they
            # win ties against generic chunks from the same doc.
            if title_tokens:
                h_low = (chunk.get("section_heading","") or "").lower()
                heading_hits = sum(1 for t in title_tokens if t in h_low)
                if heading_hits:
                    score += 5.0 * heading_hits
            # If chunk pre-tagged with a brd_section_id matches this section
            # (or its top-level), treat it as confirmed.
            pre = (chunk.get("brd_section_id") or "").strip()
            if pre and (pre == section_id or section_id.startswith(pre + ".") or pre.startswith(section_id + ".")):
                score += 15.0
        results.append({
            "id":         chunk.get("chunk_id", 0),
            "doc_name":   doc_name,
            "page":       chunk.get("page_number"),
            "heading":    heading or "(No heading)",
            "keywords":   kws[:15],
            "snippet":    text[:300],
            "chunk_text": text,
            "score":      round(score, 4),
            "file_id":    chunk.get("file_id",""),
            "brd_section_id": chunk.get("brd_section_id", ""),
        })
    results.sort(key=lambda x: (-x["score"], x["doc_name"], x.get("page") or 0))
    backend = "bm25+semantic" if sim_scores else "bm25"
    return {"total": len(results), "chunks": results, "search_backend": backend, "semantic_enabled": semantic_on, "debug": {"before_scope": before_scope, "after_scope": len(all_chunks), "semantic_scores": len(sim_scores), "llm_picked_ids": len(llm_picked_ids)}}


@router.get("/api/project/{project_id}/images-browser")
def images_browser(project_id: str, search: str = "", tag_filter: str = ""):
    """Return images for this project — doc-extracted images plus images from
    any linked named image repositories."""
    try:
        from app.project_routes import UPLOAD_ROOT, PROJECTS
    except ImportError:
        from project_routes import UPLOAD_ROOT, PROJECTS

    IMAGE_CHUNKS = Path(__file__).parent / "image_chunks.json"

    # Collect source doc filenames for this project — own uploads PLUS any
    # attached document Repositories, so a file added through either route
    # still surfaces in Browse Images.
    project_docs: set = _project_source_filenames(project_id)

    # Load doc-extracted images from global image_chunks.json
    all_images: list = []
    if IMAGE_CHUNKS.exists():
        try:
            all_images = json.loads(IMAGE_CHUNKS.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("images-browser: failed to load image_chunks.json: %s", e)

    # Load images from linked named image repos
    linked_repo_ids = PROJECTS.get(project_id, {}).get("image_repositories", [])
    if linked_repo_ids:
        try:
            try:
                from app.image_repo_routes import load_chunks_for_image_repos
            except ImportError:
                from image_repo_routes import load_chunks_for_image_repos
            repo_images = load_chunks_for_image_repos(linked_repo_ids)
            # Mark repo images so the browser can show a badge
            for img in repo_images:
                img["_from_repo"] = True
            all_images = repo_images + all_images   # repo images first
            logger.info(
                "images-browser: loaded %d repo images from %d linked repos",
                len(repo_images), len(linked_repo_ids),
            )
        except Exception as e:
            logger.warning("images-browser: failed to load repo images: %s", e)

    # Dedupe by (image_id, source_doc) so the same binary embedded in two
    # different uploaded documents is preserved as two records. Otherwise a
    # shared logo / reference diagram across files would only appear under
    # the first document it was extracted from and the rest would silently
    # vanish from Browse Images.
    seen_pairs: set = set()
    unique_images = []
    for r in all_images:
        iid = r.get("image_id")
        if not iid:
            continue
        key = (iid, (r.get("source_doc") or "").lower())
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        unique_images.append(r)

    # Previously we filtered out records whose files couldn't be resolved on
    # disk so the gallery never showed gray placeholders. That had a nasty
    # side-effect: any image whose `file_path` used a different absolute root
    # (e.g. moved working directory, repo-uploaded image stored elsewhere)
    # disappeared from Browse Images even though the record was indexed. Per
    # user request, surface EVERY indexed image; the <img> tag in the UI will
    # show the browser's broken-image glyph for genuinely missing files
    # without hiding the metadata row.
    extract_dir = Path(__file__).parent / "extracted_images"
    on_disk = list(unique_images)
    _missing = 0
    for r in on_disk:
        fp = r.get("file_path")
        exists = False
        if fp:
            try:
                if Path(fp).exists():
                    exists = True
            except Exception:
                pass
        if not exists:
            ext = (r.get("ext") or "png").lstrip(".")
            iid = r.get("image_id")
            if iid and (extract_dir / f"{iid}.{ext}").exists():
                exists = True
        if not exists:
            _missing += 1
    if _missing:
        logger.info(
            "images-browser: %d of %d indexed images have no resolvable file on disk; surfacing anyway",
            _missing, len(on_disk),
        )

    # STRICT project scoping per user request: only surface images extracted
    # from documents the user uploaded to THIS project's source directory.
    # Orphans (images from prior projects or the global index whose
    # source_doc isn't in the current project's upload folder) are dropped
    # entirely so the panel mirrors the Document Upload file list.
    if project_docs:
        before = len(on_disk)
        filtered = [r for r in on_disk if (r.get("source_doc", "") or "").lower() in project_docs]
        if before != len(filtered):
            logger.info(
                "images-browser: scoped to project uploads — %d of %d images kept (project %s)",
                len(filtered), before, project_id,
            )
    else:
        filtered = on_disk

    # Apply tag filter — case-insensitive partial match against tags list
    if tag_filter:
        tf_lower = tag_filter.lower()
        filtered = [r for r in filtered if
                    any(tf_lower in t.lower() for t in r.get("tags", []))]

    # Apply text search — word-based AND matching across all metadata fields
    if search:
        words = [w for w in search.lower().split() if len(w) > 1]
        def _matches(r: dict) -> bool:
            haystack = " ".join([
                r.get("source_doc", ""), r.get("caption", ""),
                " ".join(r.get("keywords", [])), r.get("diagram_type", ""),
                r.get("section_hint", ""), r.get("section_heading", ""),
                r.get("description", ""), " ".join(r.get("tags", [])),
            ]).lower()
            return all(w in haystack for w in words)
        filtered = [r for r in filtered if _matches(r)]

    results = [{
        "image_id":        r["image_id"],
        "caption":         r.get("caption", r["image_id"]),
        "keywords":        r.get("keywords", []),
        "diagram_type":    r.get("diagram_type", "other"),
        "description":     r.get("description", ""),
        "section_hint":    r.get("section_hint", ""),
        "section_heading": r.get("section_heading", ""),
        "brd_section_id":  r.get("brd_section_id", ""),
        "h1":              r.get("h1", ""),
        "h2":              r.get("h2", ""),
        "h3":              r.get("h3", ""),
        "source_doc":      r.get("source_doc", ""),
        "size":            r.get("size", 0),
        "page_number":     r.get("page_number", 0),
        "ext":             r.get("ext", "png"),
        "tags":            r.get("tags", []),
        "from_repo":       r.get("_from_repo", False),
        "repo_id":         r.get("repo_id", ""),
    } for r in filtered]

    logger.info("images-browser: project=%s, project_docs=%d, returned=%d", project_id, len(project_docs), len(results))
    return {"total": len(results), "images": results}


@router.post("/api/project/{project_id}/extract-images")
def extract_project_images(project_id: str):
    """Backfill image extraction for EVERY source file in this project.

    The per-upload image extractor can miss files (uploaded before the
    feature existed, or a transient failure during the background pass), so
    some documents end up with no images in the index even though they
    contain figures. This walks the project's own source dir plus any
    attached document Repository, runs the single-file extractor (which
    appends + dedupes) on each image-bearing file, then reloads the index.
    Safe to run repeatedly — already-indexed images are skipped by the
    (image_id, source_doc) dedupe.
    """
    try:
        from app.project_routes import UPLOAD_ROOT, PROJECTS
    except ImportError:
        from project_routes import UPLOAD_ROOT, PROJECTS
    try:
        from app.image_extractor import extract_and_index_single_file
    except ImportError:
        from image_extractor import extract_and_index_single_file

    _app_dir = Path(__file__).parent
    index_file = str(_app_dir / "image_chunks.json")
    output_dir = str(_app_dir / "extracted_images")

    # Files that can carry embedded images.
    IMG_CONTAINER_EXTS = {".pdf", ".docx", ".pptx", ".xlsx", ".xlsm",
                          ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

    # Gather candidate files: project's own source dir + attached repos.
    dirs = [Path(UPLOAD_ROOT) / project_id / "source"]
    repo_ids = PROJECTS.get(project_id, {}).get("repositories", []) or []
    if repo_ids:
        try:
            try:
                from app.repository_routes import REPOS_ROOT, REPOSITORIES
            except ImportError:
                from repository_routes import REPOS_ROOT, REPOSITORIES
            for rid in repo_ids:
                if rid in REPOSITORIES:
                    dirs.append(Path(REPOS_ROOT) / rid / "source")
        except Exception:
            pass

    files: list = []
    for d in dirs:
        if d.exists():
            for f in d.iterdir():
                if f.is_file() and f.suffix.lower() in IMG_CONTAINER_EXTS:
                    files.append(f)

    scanned = 0
    total_found = 0
    total_added = 0
    per_file = []
    for f in files:
        try:
            res = extract_and_index_single_file(
                file_path=str(f),
                output_dir=output_dir,
                index_file=index_file,
            )
            scanned += 1
            total_found += res.get("images_found", 0)
            total_added += res.get("images_added", 0)
            if res.get("images_added"):
                per_file.append({"file": f.name, "added": res["images_added"]})
        except Exception as exc:
            logger.warning("extract-images: failed on %s: %s", f.name, exc)

    # Reload the in-memory image index so new images are searchable now.
    try:
        try:
            from app.image_index import reload_image_index
        except ImportError:
            from image_index import reload_image_index
        reload_image_index()
    except Exception as exc:
        logger.debug("extract-images: reload_image_index failed: %s", exc)

    logger.info(
        "extract-images: project=%s scanned=%d found=%d added=%d",
        project_id, scanned, total_found, total_added,
    )
    return {
        "ok": True,
        "scanned_files": scanned,
        "images_found": total_found,
        "images_added": total_added,
        "per_file": per_file,
    }


@router.post("/api/project/{project_id}/embed-chunks")
def embed_chunks(project_id: str):
    """
    Backfill semantic embeddings for all chunks in this project that are
    missing them. Call once after enabling SEARCH_BACKEND=semantic for
    existing projects. New uploads are embedded automatically.
    """
    try:
        from app.semantic_search import is_semantic_enabled, embed_and_save_chunks
    except ImportError:
        from semantic_search import is_semantic_enabled, embed_and_save_chunks

    if not is_semantic_enabled():
        return {
            "ok": False,
            "message": "SEARCH_BACKEND is not set to 'semantic' — nothing to do.",
        }

    all_chunks = _load_all_project_chunks(project_id)
    if not all_chunks:
        return {"ok": True, "embedded": 0, "message": "No chunks found for this project."}

    # Resolve chunks.json path (same logic as _load_all_project_chunks)
    try:
        from app.project_routes import UPLOAD_ROOT
    except ImportError:
        from project_routes import UPLOAD_ROOT

    chunks_path = Path(UPLOAD_ROOT) / project_id / "chunks.json"

    try:
        count = embed_and_save_chunks(str(chunks_path), all_chunks)
        return {
            "ok": True,
            "embedded": count,
            "total_chunks": len(all_chunks),
            "message": f"Embedded {count} chunks. Restart server to reload index.",
        }
    except Exception as e:
        logger.error("embed-chunks failed for project %s: %s", project_id, e)
        return {"ok": False, "message": str(e)}


@router.post("/api/project/{project_id}/client-context")
def save_client_context(project_id: str, body: dict):
    """Persist client context for a project."""
    try:
        from app.project_routes import UPLOAD_ROOT
    except ImportError:
        from project_routes import UPLOAD_ROOT
    ctx_file = UPLOAD_ROOT / project_id / "_client_context.json"
    (UPLOAD_ROOT / project_id).mkdir(parents=True, exist_ok=True)
    ctx_file.write_text(json.dumps(body, indent=2, ensure_ascii=False), encoding="utf-8")
    if body.get("company"):
        ap_store = _load_approved_store(project_id)
        ap_store["company"] = body["company"]
        _save_approved_store(ap_store, project_id)
    # NOTE: do NOT write to _screen3_state.company here — that global singleton
    # is shared across all projects and writing it would pollute subsequent
    # calls for OTHER projects that haven't filled in their context yet.
    logger.info("Client context saved for project %s: company=%s", project_id, body.get("company",""))
    return {"ok": True}


@router.get("/api/project/{project_id}/client-context")
def load_client_context(project_id: str):
    """Load persisted client context for a project."""
    try:
        from app.project_routes import UPLOAD_ROOT
    except ImportError:
        from project_routes import UPLOAD_ROOT
    ctx_file = UPLOAD_ROOT / project_id / "_client_context.json"
    if ctx_file.exists():
        try:
            return json.loads(ctx_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("Could not read client context for %s: %s", project_id, e)
    return {}


@router.post("/api/sections/refresh-keywords")
def refresh_keywords_background(body: dict):
    """Run LLM semantic matching in background and persist results."""
    project_id = body.get("project_id")
    if not project_id:
        return {"ok": False, "error": "No project_id"}
    try:
        proj_chunks = _load_all_project_chunks(project_id)
        if not proj_chunks:
            return {"ok": False, "error": "No chunks found"}
        active   = _get_live_sections(project_id)
        archived = _get_archived_sections(project_id)
        all_sections = active + archived
        llm_mapping, _best_kw_rf, was_fresh = _llm_match_chunks_to_sections(
            proj_chunks, all_sections, project_id
        )
        chunk_by_id = {c.get("chunk_id"): c for c in proj_chunks}
        STOP_WORDS = {"the","a","an","and","or","of","in","to","for",
                      "is","are","was","be","by","with","on","at","this",
                      "that","from","as","it","its","were","has","have"}
        def _dedupe(lst):
            seen = set(); return [x for x in lst if x and not (x in seen or seen.add(x))]
        updated: dict = {}
        kw_store = _load_keyword_store(project_id)
        for sec in all_sections:
            sid = sec["id"]; title = sec["title"]
            entry = kw_store.get(sid, {})
            if entry.get("user_edited"):
                continue
            title_tokens = {w.lower() for w in title.split()
                           if len(w) > 3 and w.lower() not in STOP_WORDS}
            scored = []
            for chunk in proj_chunks:
                s = _score_chunk_for_section(chunk, sid, title_tokens)
                if s > 0: scored.append((s, chunk))
            scored.sort(key=lambda x: -x[0])
            top_chunks = [c for _, c in scored[:15]]
            llm_ids = llm_mapping.get(sid, [])
            existing_ids = {c.get("chunk_id") for c in top_chunks}
            for cid in llm_ids[:10]:
                if cid in chunk_by_id and cid not in existing_ids:
                    top_chunks.append(chunk_by_id[cid]); existing_ids.add(cid)
            doc_kw = _dedupe([kw for c in top_chunks
                              for kw in c.get("keywords", []) if kw and len(kw) > 3])[:30]
            entry["doc_keywords"] = doc_kw
            kw_store[sid] = entry; updated[sid] = doc_kw
        _save_keyword_store(kw_store, project_id)
        logger.info("refresh-keywords: updated %d sections for project %s", len(updated), project_id)
        return {"ok": True, "updated": updated, "count": len(updated)}
    except Exception as e:
        logger.exception("refresh-keywords failed for project %s", project_id)
        return {"ok": False, "error": str(e)}


@router.post("/api/project/{project_id}/set-template")
async def set_project_template(project_id: str, file: UploadFile = File(...)):
    """
    Upload and save the Solution Blueprint template for a project.
    Stored as uploads/{project_id}/_template.docx with a pointer file.
    """
    try:
        from app.project_routes import UPLOAD_ROOT
    except ImportError:
        from project_routes import UPLOAD_ROOT
    project_dir = UPLOAD_ROOT / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    template_dest = project_dir / "_template.docx"
    content = await file.read()
    template_dest.write_bytes(content)
    # Write pointer file
    marker = project_dir / "_template.txt"
    marker.write_text(str(template_dest), encoding="utf-8")
    logger.info("Template saved for project %s: %s (%d bytes)", project_id, template_dest, len(content))
    return {"ok": True, "template_path": str(template_dest), "size": len(content)}


@router.get("/api/project/{project_id}/template-status")
def get_template_status(project_id: str):
    """Check whether a template has been saved for this project."""
    tmpl = _template_path(project_id)
    is_default = bool(tmpl) and DEFAULT_TEMPLATE_PATH.exists() and tmpl.resolve() == DEFAULT_TEMPLATE_PATH.resolve()
    return {
        "has_template":    tmpl is not None,
        "template_name":   tmpl.name if tmpl else None,
        "is_default":      is_default,
    }


@router.post("/api/bot/restart")
def restart_bot():
    """
    Signal a bot config reload.
    Extend this to do a warm restart of your agent if needed.
    """
    logger.info("Bot config reloaded via Screen 3 Save Config action.")
    return {"ok": True, "message": "Bot config reloaded successfully."}


# ─────────────────────────────────────────────────────────────────────────────
# USER STORY GENERATION
# Extracts structured user stories from approved BRD sections using the org's
# Azure LLM (via completion_from_prompt). Used by the Generation page.
# ─────────────────────────────────────────────────────────────────────────────

class UserStoriesRequest(BaseModel):
    project_id: Optional[str] = None
    section_ids: Optional[List[str]] = None  # None = all approved sections


def _user_stories_path(project_id: Optional[str]):
    return _THIS_DIR / (f"user_stories_{project_id}.json" if project_id else "user_stories_latest.json")


@router.post("/api/project/generate-user-stories")
def generate_user_stories(body: UserStoriesRequest):
    """Generate structured agile user stories from the approved BRD sections."""
    import json as _json

    ap_store = _load_approved_store(body.project_id)
    all_sections_content = dict(ap_store.get("sections", {}) or {})
    state = _get_screen3_state()
    if state:
        for field in SECTION_STATE_MAP.values():
            for sid, content in (getattr(state, field, None) or {}).items():
                all_sections_content.setdefault(sid, content)
    if body.section_ids:
        all_sections_content = {k: v for k, v in all_sections_content.items() if k in set(body.section_ids)}
    if not all_sections_content:
        raise HTTPException(status_code=400, detail="No approved BRD sections found. Generate and approve at least one section first.")

    live_sections = _get_live_sections(body.project_id)
    title_map = {x["id"]: x.get("title", "") for x in live_sections}

    parts = []
    for sid in sorted(all_sections_content.keys(), key=_section_sort_key):
        content = (all_sections_content.get(sid) or "").strip()
        if not content:
            continue
        parts.append(f"## Section {sid}: {title_map.get(sid, 'Section '+sid)}\n{content}")
    if not parts:
        raise HTTPException(status_code=400, detail="All approved sections are empty.")
    brd_text = "\n\n".join(parts)
    if len(brd_text) > 20000:
        brd_text = brd_text[:20000] + "\n\n[... content truncated ...]"

    prompt = (
        "You are a senior Business Analyst. Based on the following Blueprint Requirements "
        "Document (BRD), extract and generate structured agile user stories.\n\n"
        "BRD CONTENT:\n" + brd_text + "\n\n"
        "TASK: Generate comprehensive user stories directly traceable to the BRD.\n"
        "REQUIREMENTS:\n"
        "1. Cover all major functional areas in the BRD.\n"
        "2. Each story specific, actionable, independently deliverable.\n"
        "3. Acceptance criteria testable and measurable.\n"
        "4. Generate 8-20 stories (more for larger BRDs).\n\n"
        "OUTPUT: return ONLY a valid JSON array, each item shaped like:\n"
        '{"id":"US-001","title":"...","as_a":"role","i_want":"capability","so_that":"benefit",'
        '"acceptance_criteria":["Given..., When..., Then...","..."],'
        '"section_source":"Section ID and title","priority":"High | Medium | Low"}\n'
        "Make them specific to the client context in the BRD. Output the JSON array only, no prose, no markdown fences."
    )

    try:
        try:
            from app.claude_provider import completion_from_prompt as _cp_us
        except ImportError:
            from claude_provider import completion_from_prompt as _cp_us
        # Opt out of the global 2000-token generation cap — a JSON array of
        # 8-20 user stories needs more room (truncation → "malformed response").
        raw = (_cp_us(prompt, max_tokens=8000, temperature=0.0, max_output_cap=8000) or "").strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
        user_stories = None
        try:
            user_stories = _json.loads(raw)
        except Exception:
            i, j = raw.find("["), raw.rfind("]")
            if i != -1 and j != -1 and j > i:
                try:
                    user_stories = _json.loads(raw[i:j + 1])
                except Exception:
                    user_stories = None
        if not isinstance(user_stories, list):
            # Salvage a truncated array: keep every complete {...} object.
            user_stories = _salvage_json_array_objects(raw)
        if not isinstance(user_stories, list) or not user_stories:
            raise ValueError("LLM did not return a usable JSON array")
        _user_stories_path(body.project_id).write_text(
            _json.dumps({"stories": user_stories, "project_id": body.project_id}, indent=2, ensure_ascii=False),
            encoding="utf-8")
        logger.info("Generated %d user stories for project %s", len(user_stories), body.project_id)
        return {"ok": True, "stories": user_stories, "count": len(user_stories)}
    except _json.JSONDecodeError as e:
        logger.error("User stories: invalid JSON from LLM: %s", e)
        raise HTTPException(status_code=500, detail="The model returned a malformed response. Please try again.")
    except Exception as e:
        logger.error("User stories generation failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/project/user-stories")
def get_user_stories(project_id: Optional[str] = None):
    import json as _json
    path = _user_stories_path(project_id)
    if not path.exists():
        return {"ok": True, "stories": [], "count": 0}
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        stories = data.get("stories", [])
        return {"ok": True, "stories": stories, "count": len(stories)}
    except Exception as e:
        logger.warning("Could not read user stories file: %s", e)
        return {"ok": True, "stories": [], "count": 0}


@router.get("/api/project/user-stories/download")
def download_user_stories(project_id: Optional[str] = None):
    import json as _json
    from fastapi.responses import PlainTextResponse
    from datetime import datetime as _dt
    path = _user_stories_path(project_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="No user stories found. Generate them first.")
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        stories = data.get("stories", [])
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    lines = ["USER STORIES - Generated from Blueprint Requirements Document",
             f"Generated: {_dt.now().strftime('%Y-%m-%d %H:%M')}",
             f"Total Stories: {len(stories)}", "=" * 70, ""]
    for story in stories:
        lines.append(f"[{story.get('id','')}] {story.get('title','')}")
        lines.append(f"Priority: {story.get('priority','Medium')}")
        lines.append(f"Source:   {story.get('section_source','')}")
        lines.append("")
        lines.append(f"As a {story.get('as_a','')},")
        lines.append(f"I want {story.get('i_want','')},")
        lines.append(f"So that {story.get('so_that','')}.")
        lines.append("")
        ac = story.get("acceptance_criteria", []) or []
        if ac:
            lines.append("Acceptance Criteria:")
            for i, a in enumerate(ac, 1):
                lines.append(f"  {i}. {a}")
        lines.append("")
        lines.append("-" * 70)
        lines.append("")
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    fn = f"UserStories_{project_id or 'BRD'}_{ts}.txt"
    return PlainTextResponse(content="\n".join(lines),
                             headers={"Content-Disposition": f'attachment; filename="{fn}"'})



# ─────────────────────────────────────────────────────────────────────────────
# PROJECT ISOLATION AUDIT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/project/{project_id}/isolation-audit")
def project_isolation_audit(project_id: str):
    """
    Audit a project for cross-project content contamination.

    Checks:
    1. Approved store — scans section content for other clients' company names.
    2. Keyword store  — scans saved prompts and web_keywords for foreign names.
    3. Chunks         — verifies all chunk project_id fields match this project
                        (repo chunks legitimately have no project_id; they are skipped).
    4. Client context — reads the authoritative company name from disk.

    Returns a structured report with per-section findings so the caller can
    decide which sections need to be regenerated.
    """
    findings: List[Dict] = []

    # ── 1. Authoritative company ────────────────────────────────────────────
    authoritative = _get_client_company(project_id=project_id)

    # ── 2. Approved store scan ──────────────────────────────────────────────
    ap_store = _load_approved_store(project_id)
    ap_company = (ap_store.get("company") or "").strip()
    sections_content = ap_store.get("sections", {})

    if ap_company and authoritative and ap_company.lower() != authoritative.lower():
        findings.append({
            "severity": "error",
            "location": "approved_store.company",
            "message": (
                f"Approved store company field is '{ap_company}' but the project's "
                f"authoritative company is '{authoritative}'. "
                "Run /api/project/{project_id}/client-context POST to correct it."
            ),
        })

    # Scan each approved section for foreign company names
    for sid, content in sections_content.items():
        if not authoritative or not content:
            continue
        # Collect all other known project company names for comparison
        try:
            try:
                from app.project_routes import UPLOAD_ROOT as _UR2
            except ImportError:
                from project_routes import UPLOAD_ROOT as _UR2
            # Quick scan: check if any OTHER project's company appears in content
            parent_dir = Path(_UR2)
            for other_pid_dir in parent_dir.iterdir():
                other_pid = other_pid_dir.name
                if other_pid == project_id or not other_pid_dir.is_dir():
                    continue
                other_ctx = other_pid_dir / "_client_context.json"
                if not other_ctx.exists():
                    continue
                try:
                    other_data = json.loads(other_ctx.read_text(encoding="utf-8"))
                    other_company = (other_data.get("company") or "").strip()
                    if (other_company
                            and other_company.lower() != authoritative.lower()
                            and other_company.lower() in content.lower()):
                        findings.append({
                            "severity": "error",
                            "location": f"approved_store.sections.{sid}",
                            "message": (
                                f"Section '{sid}' content references foreign company "
                                f"'{other_company}' (belongs to project {other_pid}). "
                                "This section must be regenerated."
                            ),
                            "foreign_company": other_company,
                            "foreign_project": other_pid,
                        })
                except Exception:
                    pass
        except Exception:
            pass

    # ── 3. Keyword store scan ───────────────────────────────────────────────
    kw_store = _load_keyword_store(project_id)
    for sid, entry in kw_store.items():
        saved_prompt = (entry.get("prompt") or "").strip()
        if not saved_prompt or not authoritative:
            continue
        prompt_lower = saved_prompt.lower()
        for _m in re.finditer(
            r'(?:reference\s+(.+?)\s+throughout|client company:\s*(.+?)(?:\\n|\n|$))',
            prompt_lower
        ):
            _candidate = (_m.group(1) or _m.group(2) or "").strip()
            if _candidate and _candidate != authoritative.lower() and len(_candidate) > 2:
                findings.append({
                    "severity": "warning",
                    "location": f"keyword_store.{sid}.prompt",
                    "message": (
                        f"Saved prompt for section '{sid}' references company "
                        f"'{_candidate}' which does not match the authoritative "
                        f"company '{authoritative}'. Clear this prompt via "
                        "PUT /api/sections/{section_id}/keywords with prompt=''."
                    ),
                    "foreign_company": _candidate,
                })

    # ── 4. Chunk project_id check ───────────────────────────────────────────
    try:
        proj_chunks = _load_all_project_chunks(project_id)
        mismatched = [
            c for c in proj_chunks
            if c.get("project_id") and c["project_id"] != project_id
        ]
        if mismatched:
            foreign_projects = list({c["project_id"] for c in mismatched})
            findings.append({
                "severity": "error",
                "location": "chunks",
                "message": (
                    f"{len(mismatched)} chunk(s) have a project_id that does NOT match "
                    f"'{project_id}'. Foreign projects: {foreign_projects}. "
                    "These chunks may produce contaminated context during generation."
                ),
                "count": len(mismatched),
                "foreign_projects": foreign_projects,
            })
    except Exception as _ce:
        findings.append({
            "severity": "info",
            "location": "chunks",
            "message": f"Could not load chunks for audit: {_ce}",
        })

    verdict = (
        "contaminated" if any(f["severity"] == "error" for f in findings)
        else "warnings" if findings
        else "clean"
    )

    return {
        "project_id": project_id,
        "authoritative_company": authoritative,
        "verdict": verdict,
        "finding_count": len(findings),
        "findings": findings,
    }


@router.post("/api/project/{project_id}/clean-stale-prompts")
def clean_stale_prompts(project_id: str):
    """
    Remove saved prompts from the keyword store that reference a foreign
    client company. Safe to call before a generation run.

    Only clears the `prompt` field; leaves doc_keywords, web_keywords,
    structure, pinned_chunk_ids, and user_edited intact.
    """
    authoritative = _get_client_company(project_id=project_id)
    if not authoritative:
        return {"ok": False, "error": "Could not determine authoritative company for project."}

    kw_store = _load_keyword_store(project_id)
    cleaned: List[str] = []

    for sid, entry in kw_store.items():
        saved_prompt = (entry.get("prompt") or "").strip()
        if not saved_prompt:
            continue
        prompt_lower = saved_prompt.lower()
        is_stale = False
        for _m in re.finditer(
            r'(?:reference\s+(.+?)\s+throughout|client company:\s*(.+?)(?:\\n|\n|$))',
            prompt_lower
        ):
            _candidate = (_m.group(1) or _m.group(2) or "").strip()
            if _candidate and _candidate != authoritative.lower() and len(_candidate) > 2:
                is_stale = True
                logger.warning(
                    "clean_stale_prompts: clearing stale prompt for section %s "
                    "(project=%s) — found foreign company %r (authoritative=%r)",
                    sid, project_id, _candidate, authoritative,
                )
                break
        if is_stale:
            entry["prompt"] = ""
            # Do NOT set user_edited=False — the user may have set other fields
            kw_store[sid] = entry
            cleaned.append(sid)

    if cleaned:
        _save_keyword_store(kw_store, project_id)

    return {
        "ok": True,
        "project_id": project_id,
        "authoritative_company": authoritative,
        "cleaned_sections": cleaned,
        "message": (
            f"Cleared stale prompts for {len(cleaned)} section(s): {cleaned}. "
            "Re-run /api/sections/refresh-keywords to rebuild clean keywords."
            if cleaned else "No stale prompts found — keyword store is clean."
        ),
    }
