"""repository_routes.py — API routes for Repositories

A Repository is a named collection of source documents, decoupled from any
project. Projects attach to one or more repositories to access their documents
during BRD generation.

Endpoints:
  GET    /api/repositories                                   List all repositories
  POST   /api/repositories                                   Create a repository
  GET    /api/repositories/{repo_id}                         Get repository details
  DELETE /api/repositories/{repo_id}                         Delete repository + all files
  GET    /api/repositories/{repo_id}/files                   List files in a repository
  POST   /api/repositories/{repo_id}/upload                  Upload file to repository
  DELETE /api/repositories/{repo_id}/files/{file_id}         Delete file from repository
  GET    /api/repositories/{repo_id}/chunks                  Get all chunks in repository
  GET    /api/projects/{project_id}/repositories             List repos attached to project
  POST   /api/projects/{project_id}/repositories/{repo_id}   Attach repo to project
  DELETE /api/projects/{project_id}/repositories/{repo_id}   Detach repo from project

Storage:
  uploads/repos/{repo_id}/source/     — source document files
  uploads/repos/{repo_id}/chunks.json — chunked text for this repository
  uploads/repos_state.json            — persisted REPOSITORIES + REPO_FILES dicts
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import traceback
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

logger = logging.getLogger("RepositoryRoutes")

router = APIRouter(prefix="/api", tags=["repositories"])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STORAGE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UPLOAD_ROOT = Path(__file__).parent / "uploads"
UPLOAD_ROOT.mkdir(exist_ok=True)
REPOS_ROOT = UPLOAD_ROOT / "repos"
REPOS_ROOT.mkdir(exist_ok=True)
_STATE_FILE = UPLOAD_ROOT / "repos_state.json"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IN-MEMORY STATE  (persisted to repos_state.json)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
REPOSITORIES: dict[str, dict] = {}
REPO_FILES: dict[str, dict] = {}


def _save_state():
    try:
        _STATE_FILE.write_text(
            json.dumps({"repositories": REPOSITORIES, "repo_files": REPO_FILES}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not persist repository state: %s", exc)


def _load_state():
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            REPOSITORIES.update(data.get("repositories", {}))
            REPO_FILES.update(data.get("repo_files", {}))
            logger.info(
                "Loaded %d repositories, %d files from state",
                len(REPOSITORIES), len(REPO_FILES),
            )
        except Exception as exc:
            logger.warning("Could not load repository state: %s", exc)


_load_state()

VALID_SOURCE_EXTS = {
    ".pdf", ".docx", ".doc", ".txt",
    ".xlsx", ".xls", ".xlsm",
    ".pptx", ".ppt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
}
MAX_FILE_SIZE = 200 * 1024 * 1024


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RESPONSE MODELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RepoOut(BaseModel):
    id: str
    name: str
    description: str
    created: str
    updated: str
    file_count: int
    chunk_count: int = 0
    kind: str = ""  # "" = general BRD reference repo; "sow_samples" = real signed
                     # SOWs kept for the SOW skill's assisted learning workflow.


class RepoFileOut(BaseModel):
    id: str
    repo_id: str
    name: str
    size: int
    ext: str
    status: str
    progress: int
    chunk_count: int = 0
    uploaded_at: str
    error_detail: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND CHUNKING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _run_repo_chunking_background(file_id: str, repo_id: str, file_path: Path):
    """Background thread: chunk a repository file and update its status."""
    try:
        try:
            from app.chunking_pipeline import process_file as _process_file
        except ImportError:
            from chunking_pipeline import process_file as _process_file

        if file_id in REPO_FILES:
            REPO_FILES[file_id]["status"] = "chunking"
            REPO_FILES[file_id]["progress"] = 50
        _save_state()

        repo_dir = REPOS_ROOT / repo_id
        (repo_dir / "source").mkdir(parents=True, exist_ok=True)

        # Reuse chunking pipeline — pass repo_id in place of project_id for metadata
        new_chunks = _process_file(
            file_path=file_path,
            project_id=repo_id,
            file_id=file_id,
            existing_chunk_count=0,
        )
        file_chunk_count = len(new_chunks)

        try:
            from app.chunking_pipeline import (
                _get_project_lock, remove_file_chunks,
                load_project_chunks, save_project_chunks,
            )
        except ImportError:
            from chunking_pipeline import (
                _get_project_lock, remove_file_chunks,
                load_project_chunks, save_project_chunks,
            )

        lock = _get_project_lock(repo_id)
        with lock:
            remove_file_chunks(repo_dir, file_id)
            existing = load_project_chunks(repo_dir)
            for i, chunk in enumerate(new_chunks):
                chunk["chunk_id"] = len(existing) + i
                chunk["repo_id"] = repo_id  # tag chunk with its repository
            save_project_chunks(repo_dir, existing + new_chunks)

        if file_id in REPO_FILES:
            REPO_FILES[file_id]["status"] = "indexed"
            REPO_FILES[file_id]["progress"] = 100
            REPO_FILES[file_id]["chunk_count"] = file_chunk_count

        REPOSITORIES[repo_id]["updated"] = datetime.utcnow().isoformat()
        _save_state()
        logger.info("Repo chunking complete for %s — %d chunks", file_id, file_chunk_count)

        # ── Image extraction + vision analysis (runs after text chunking) ───────
        try:
            try:
                from app.image_extractor import extract_and_index_single_file
            except ImportError:
                from image_extractor import extract_and_index_single_file

            # Build page→section map from text chunks so PDF images inherit
            # the document heading that was active on each page.
            _page_sections: dict = {}
            for _chunk in new_chunks:
                _pn = _chunk.get("page_number")
                if _pn is not None and _pn not in _page_sections:
                    _page_sections[_pn] = {
                        "section_heading": _chunk.get("section_heading", ""),
                        "brd_section_id":  _chunk.get("brd_section_id", ""),
                    }

            _app_dir = Path(__file__).parent
            _index_file = _app_dir / "image_chunks.json"
            img_result = extract_and_index_single_file(
                file_path=str(file_path),
                output_dir=str(_app_dir / "extracted_images"),
                index_file=str(_index_file),
                page_sections=_page_sections or None,
            )
            logger.info(
                "Image extraction for %s — %d found, %d added to index",
                file_id, img_result["images_found"], img_result["images_added"],
            )

            # Safety net: if vision analysis failed inline (SSL blip, JSON error),
            # retry it now so the user never sees "No keywords" after upload.
            if img_result["images_found"] > 0:
                try:
                    from app.project_routes import _auto_rerun_vision_for_file
                except ImportError:
                    from project_routes import _auto_rerun_vision_for_file
                _auto_rerun_vision_for_file(
                    source_doc_name=file_path.name,
                    index_path=_index_file,
                    extracted_dir=_app_dir / "extracted_images",
                )
        except Exception as exc:
            logger.warning("Image extraction failed for %s: %s", file_id, exc)

        # Reload in-memory BM25 image index so auto-suggest picks up new images immediately
        try:
            try:
                from app.image_index import reload_image_index
            except ImportError:
                from image_index import reload_image_index
            reload_image_index()
            logger.info("Image index reloaded after repo indexing for %s", file_id)
        except Exception as exc:
            logger.debug("Image index reload skipped: %s", exc)

    except Exception as exc:
        logger.error("Repo chunking failed for %s: %s\n%s", file_id, exc, traceback.format_exc())
        if file_id in REPO_FILES:
            REPO_FILES[file_id]["status"] = "error"
            REPO_FILES[file_id]["progress"] = 100
            REPO_FILES[file_id]["error_detail"] = str(exc)
        _save_state()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REPOSITORY CRUD
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RepoCreate(BaseModel):
    name: str
    description: str = ""
    kind: str = ""


@router.get("/repositories", response_model=list[RepoOut])
def list_repositories(kind: str | None = None):
    result = []
    for r in REPOSITORIES.values():
        if kind is not None and r.get("kind", "") != kind:
            continue
        repo_files = [f for f in REPO_FILES.values() if f["repo_id"] == r["id"]]
        result.append(RepoOut(
            id=r["id"], name=r["name"], description=r.get("description", ""),
            created=r["created"], updated=r["updated"],
            file_count=len(repo_files),
            chunk_count=sum(f.get("chunk_count", 0) for f in repo_files),
            kind=r.get("kind", ""),
        ))
    result.sort(key=lambda x: x.updated, reverse=True)
    return result


@router.post("/repositories", response_model=RepoOut)
def create_repository(req: RepoCreate):
    rid = str(uuid4())[:8]
    now = datetime.utcnow().isoformat()
    repo = {
        "id": rid,
        "name": req.name.strip(),
        "description": req.description.strip(),
        "created": now,
        "updated": now,
        "kind": (req.kind or "").strip(),
    }
    REPOSITORIES[rid] = repo
    repo_dir = REPOS_ROOT / rid
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "source").mkdir(exist_ok=True)
    _save_state()
    logger.info("Created repository %s: %s (kind=%s)", rid, req.name, repo["kind"])
    return RepoOut(**repo, file_count=0, chunk_count=0)


@router.get("/repositories/{repo_id}", response_model=RepoOut)
def get_repository(repo_id: str):
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")
    r = REPOSITORIES[repo_id]
    repo_files = [f for f in REPO_FILES.values() if f["repo_id"] == repo_id]
    return RepoOut(
        id=r["id"], name=r["name"], description=r.get("description", ""),
        created=r["created"], updated=r["updated"],
        file_count=len(repo_files),
        chunk_count=sum(f.get("chunk_count", 0) for f in repo_files),
        kind=r.get("kind", ""),
    )


@router.delete("/repositories/{repo_id}")
def delete_repository(repo_id: str):
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")
    to_remove = [fid for fid, f in REPO_FILES.items() if f["repo_id"] == repo_id]
    for fid in to_remove:
        del REPO_FILES[fid]
    repo_dir = REPOS_ROOT / repo_id
    if repo_dir.exists():
        shutil.rmtree(repo_dir)
    name = REPOSITORIES[repo_id]["name"]
    del REPOSITORIES[repo_id]
    _save_state()
    logger.info("Deleted repository %s (%s) — %d files removed", repo_id, name, len(to_remove))
    return {"deleted": True, "repo_id": repo_id, "files_removed": len(to_remove)}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# REPOSITORY FILE MANAGEMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.post("/repositories/{repo_id}/upload", response_model=RepoFileOut)
async def upload_repo_file(repo_id: str, file: UploadFile = File(...)):
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")

    # Sanitize filename: strip any directory components to prevent path traversal
    safe_name = Path(file.filename or "").name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(400, "Invalid filename")

    ext = Path(safe_name).suffix.lower()
    if ext not in VALID_SOURCE_EXTS:
        raise HTTPException(400, f"Invalid file type '{ext}'. Accepted: {', '.join(VALID_SOURCE_EXTS)}")

    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds {MAX_FILE_SIZE // (1024 * 1024)} MB limit")

    repo_dir = REPOS_ROOT / repo_id / "source"
    repo_dir.mkdir(parents=True, exist_ok=True)
    file_path = (repo_dir / safe_name).resolve()
    if not str(file_path).startswith(str(repo_dir.resolve())):
        raise HTTPException(400, "Invalid filename")
    with open(file_path, "wb") as f:
        f.write(content)

    file_id = str(uuid4())[:8]
    now = datetime.utcnow().isoformat()
    file_record = {
        "id": file_id, "repo_id": repo_id,
        "name": safe_name, "size": len(content),
        "ext": ext.lstrip("."), "status": "uploaded",
        "progress": 100, "chunk_count": 0,
        "uploaded_at": now, "disk_path": str(file_path),
    }
    REPO_FILES[file_id] = file_record
    REPOSITORIES[repo_id]["updated"] = now
    _save_state()
    logger.info("Uploaded %s → repo %s (%d bytes)", file.filename, repo_id, len(content))

    thread = threading.Thread(
        target=_run_repo_chunking_background,
        args=(file_id, repo_id, file_path),
        daemon=True,
    )
    thread.start()

    return RepoFileOut(
        id=file_id, repo_id=repo_id, name=safe_name,
        size=len(content), ext=ext.lstrip("."),
        status="uploaded", progress=100, chunk_count=0, uploaded_at=now,
    )


@router.get("/repositories/{repo_id}/files", response_model=list[RepoFileOut])
def list_repo_files(repo_id: str):
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")
    result = []
    for f in REPO_FILES.values():
        if f["repo_id"] == repo_id:
            result.append(RepoFileOut(
                id=f["id"], repo_id=f["repo_id"], name=f["name"],
                size=f["size"], ext=f["ext"], status=f["status"],
                progress=f["progress"], chunk_count=f.get("chunk_count", 0),
                uploaded_at=f["uploaded_at"], error_detail=f.get("error_detail", ""),
            ))
    return result


@router.delete("/repositories/{repo_id}/files/{file_id}")
def delete_repo_file(repo_id: str, file_id: str):
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")
    if file_id not in REPO_FILES or REPO_FILES[file_id]["repo_id"] != repo_id:
        raise HTTPException(404, "File not found")

    file_record = REPO_FILES[file_id]

    # Block deletion while a chunking thread still holds the file open.
    # On Windows, unlink raises WinError 32 if any process has a handle on
    # the file (PyMuPDF/python-docx keep one open during chunking).
    if file_record.get("status") in ("uploaded", "chunking"):
        raise HTTPException(
            409,
            "File is still being indexed. Wait for chunking to finish, then try again.",
        )

    disk_path = Path(file_record.get("disk_path", ""))
    if disk_path.exists():
        # Retry the unlink a few times — on Windows the OS lock can linger for
        # a beat after the background thread releases its handle.
        import time as _time
        last_err = None
        for attempt in range(5):
            try:
                disk_path.unlink()
                last_err = None
                break
            except PermissionError as e:
                last_err = e
                _time.sleep(0.2 * (attempt + 1))
        if last_err is not None:
            logger.warning("Could not unlink %s: %s", disk_path, last_err)
            raise HTTPException(
                409,
                "Could not delete file — it's still locked by another process. "
                "Close any open handles (e.g. a Word/Excel preview) and retry.",
            )

    try:
        from app.chunking_pipeline import remove_file_chunks
        repo_dir = REPOS_ROOT / repo_id
        remove_file_chunks(repo_dir, file_id)
    except Exception as e:
        logger.warning("Could not clean up chunks for %s: %s", file_id, e)

    name = file_record["name"]
    del REPO_FILES[file_id]
    REPOSITORIES[repo_id]["updated"] = datetime.utcnow().isoformat()
    _save_state()
    logger.info("Deleted file %s (%s) from repo %s", file_id, name, repo_id)
    return {"deleted": True, "file_id": file_id}


@router.post("/repositories/{repo_id}/files/{file_id}/rechunk")
def rechunk_repo_file(repo_id: str, file_id: str):
    """Re-run chunking on an already-uploaded file (e.g. after installing a missing parser)."""
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")
    if file_id not in REPO_FILES or REPO_FILES[file_id]["repo_id"] != repo_id:
        raise HTTPException(404, "File not found")

    record = REPO_FILES[file_id]
    if record.get("status") in ("chunking",):
        raise HTTPException(409, "File is already being indexed.")

    disk_path = record.get("disk_path", "")
    if not disk_path or not Path(disk_path).exists():
        raise HTTPException(404, f"Source file missing on disk: {disk_path}")

    REPO_FILES[file_id]["status"] = "uploaded"
    REPO_FILES[file_id]["chunk_count"] = 0
    REPO_FILES[file_id]["error_detail"] = ""
    _save_state()

    thread = threading.Thread(
        target=_run_repo_chunking_background,
        args=(file_id, repo_id, Path(disk_path)),
        daemon=True,
    )
    thread.start()
    logger.info("Re-chunk triggered for file %s in repo %s", file_id, repo_id)
    return {"ok": True, "file_id": file_id, "status": "chunking"}


@router.post("/repositories/{repo_id}/embed-chunks")
def embed_repo_chunks(repo_id: str):
    """
    Backfill semantic embeddings for all chunks in this repository that are
    missing them. Call once after enabling SEARCH_BACKEND=semantic for
    existing repos. New uploads are embedded automatically.
    """
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")

    try:
        from app.semantic_search import is_semantic_enabled, embed_and_save_chunks
    except ImportError:
        from semantic_search import is_semantic_enabled, embed_and_save_chunks

    if not is_semantic_enabled():
        return {"ok": False, "message": "SEARCH_BACKEND is not 'semantic' — nothing to do."}

    try:
        from app.chunking_pipeline import load_project_chunks
    except ImportError:
        from chunking_pipeline import load_project_chunks

    repo_dir  = REPOS_ROOT / repo_id
    chunks    = load_project_chunks(repo_dir)
    if not chunks:
        return {"ok": True, "embedded": 0, "message": "No chunks found for this repository."}

    chunks_path = repo_dir / "chunks.json"
    try:
        count = embed_and_save_chunks(str(chunks_path), chunks)
        return {
            "ok": True,
            "embedded": count,
            "total_chunks": len(chunks),
            "message": f"Embedded {count} chunks. Restart server to reload.",
        }
    except Exception as e:
        logger.error("embed-chunks failed for repo %s: %s", repo_id, e)
        return {"ok": False, "message": str(e)}


@router.get("/repositories/{repo_id}/chunks")
def get_repo_chunks(repo_id: str, file_id: str = None):
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")
    try:
        from app.chunking_pipeline import load_project_chunks
        repo_dir = REPOS_ROOT / repo_id
        chunks = load_project_chunks(repo_dir)
    except Exception:
        chunks = []
    if file_id:
        chunks = [c for c in chunks if c.get("file_id") == file_id]
    return {"repo_id": repo_id, "total_chunks": len(chunks), "chunks": chunks}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PROJECT ↔ REPOSITORY ATTACHMENT
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _get_project_state():
    """Return live PROJECTS dict and save function from project_routes."""
    try:
        import app.project_routes as pr
    except ImportError:
        import project_routes as pr
    return pr.PROJECTS, pr._save_state


@router.get("/projects/{project_id}/repositories", response_model=list[RepoOut])
def list_project_repos(project_id: str):
    PROJECTS, _ = _get_project_state()
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")
    result = []
    for rid in PROJECTS[project_id].get("repositories", []):
        if rid not in REPOSITORIES:
            continue
        r = REPOSITORIES[rid]
        repo_files = [f for f in REPO_FILES.values() if f["repo_id"] == rid]
        result.append(RepoOut(
            id=r["id"], name=r["name"], description=r.get("description", ""),
            created=r["created"], updated=r["updated"],
            file_count=len(repo_files),
            chunk_count=sum(f.get("chunk_count", 0) for f in repo_files),
            kind=r.get("kind", ""),
        ))
    return result


@router.post("/projects/{project_id}/repositories/{repo_id}")
def attach_repo_to_project(project_id: str, repo_id: str):
    PROJECTS, save_projects = _get_project_state()
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")
    repos = PROJECTS[project_id].setdefault("repositories", [])
    if repo_id not in repos:
        repos.append(repo_id)
        PROJECTS[project_id]["updated"] = datetime.utcnow().isoformat()
        save_projects()
        logger.info("Attached repo %s to project %s", repo_id, project_id)
    return {"attached": True, "project_id": project_id, "repo_id": repo_id}


@router.delete("/projects/{project_id}/repositories/{repo_id}")
def detach_repo_from_project(project_id: str, repo_id: str):
    PROJECTS, save_projects = _get_project_state()
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")
    repos = PROJECTS[project_id].get("repositories", [])
    if repo_id in repos:
        repos.remove(repo_id)
        PROJECTS[project_id]["updated"] = datetime.utcnow().isoformat()
        save_projects()
        logger.info("Detached repo %s from project %s", repo_id, project_id)
    return {"detached": True, "project_id": project_id, "repo_id": repo_id}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HELPER — used by project_routes to merge repo chunks
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def load_chunks_for_repos(repo_ids: list[str]) -> list[dict]:
    """Return all chunks from the given repository IDs (for project context merging)."""
    try:
        from app.chunking_pipeline import load_project_chunks
    except ImportError:
        from chunking_pipeline import load_project_chunks

    all_chunks = []
    for rid in repo_ids:
        if rid not in REPOSITORIES:
            continue
        try:
            repo_dir = REPOS_ROOT / rid
            chunks = load_project_chunks(repo_dir)
            for c in chunks:
                c.setdefault("repo_id", rid)
                c.setdefault("source", "repository")
            all_chunks.extend(chunks)
        except Exception as e:
            logger.warning("Could not load chunks for repo %s: %s", rid, e)
    return all_chunks
