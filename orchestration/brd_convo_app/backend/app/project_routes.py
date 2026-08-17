"""
project_routes.py — API routes for Projects, Document Upload, and Chunking

Endpoints:
  GET    /api/projects                              List all projects
  POST   /api/projects                              Create a new project
  DELETE /api/projects/{project_id}                  Delete project + all files + chunks
  GET    /api/projects/{project_id}/files            List files in a project (poll this for status)
  POST   /api/upload                                 Upload file → auto-triggers chunking
  DELETE /api/projects/{project_id}/files/{file_id}  Delete file + its chunks
  GET    /api/projects/{project_id}/chunks            Get all chunks for a project

Pipeline:
  Upload → status:"uploaded" → background task starts → status:"chunking" → done → status:"indexed"
  On error → status:"error"

Storage:
  uploads/{project_id}/source/       — source doc files
  uploads/{project_id}/prompt/       — prompt config files
  uploads/{project_id}/chunks.json   — all chunks with rich metadata
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

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

logger = logging.getLogger("ProjectRoutes")

router = APIRouter(prefix="/api", tags=["projects"])

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# STORAGE
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
UPLOAD_ROOT = Path(__file__).parent / "uploads"
UPLOAD_ROOT.mkdir(exist_ok=True)
_STATE_FILE = UPLOAD_ROOT / "projects_state.json"

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# IN-MEMORY STATE  (persisted to projects_state.json)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROJECTS: dict[str, dict] = {}
FILES: dict[str, dict] = {}


def _save_state():
    try:
        _STATE_FILE.write_text(
            json.dumps({"projects": PROJECTS, "files": FILES}, indent=2),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Could not persist project state: %s", exc)


def _load_state():
    if _STATE_FILE.exists():
        try:
            data = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
            PROJECTS.update(data.get("projects", {}))
            FILES.update(data.get("files", {}))
            logger.info("Loaded %d projects, %d files from state", len(PROJECTS), len(FILES))
        except Exception as exc:
            logger.warning("Could not load project state: %s", exc)


_load_state()

VALID_SOURCE_EXTS = {
    ".pdf", ".docx", ".doc", ".txt",
    ".xlsx", ".xls", ".xlsm",
    ".pptx", ".ppt",
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
}
VALID_PROMPT_EXTS = {".json", ".yaml", ".yml"}
MAX_FILE_SIZE = 50 * 1024 * 1024

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# RESPONSE MODELS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
class ProjectOut(BaseModel):
    id: str
    name: str
    client: str
    status: str
    created: str
    updated: str
    file_count: int
    chunk_count: int = 0
    workflow: str = "new"

class FileOut(BaseModel):
    id: str
    project_id: str
    name: str
    size: int
    ext: str
    category: str
    status: str         # "uploaded" | "chunking" | "indexed" | "error"
    progress: int
    chunk_count: int = 0
    uploaded_at: str
    error_detail: str = ""

class ChunkOut(BaseModel):
    chunk_id: int
    file_id: str
    doc_name: str
    page_number: int | None
    section_heading: str
    char_start: int
    char_end: int
    chunk_text: str
    keywords: list[str]

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# BACKGROUND CHUNKING
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _auto_rerun_vision_for_file(
    source_doc_name: str,
    index_path: Path,
    extracted_dir: Path,
) -> dict:
    """
    Retry GPT-4o vision analysis on any image records from a freshly-uploaded
    document that came out of extract_and_index_single_file with empty
    keywords. The inline call inside the extractor can fail silently (SSL
    timeouts, JSON parse errors) and leave records unindexed — running this
    pass after each upload means the user never has to invoke
    rerun_vision.py from the command line.

    Returns a small summary dict {"checked": N, "updated": M, "skipped": K}.
    """
    if not index_path.exists():
        return {"checked": 0, "updated": 0, "skipped": 0}

    # Local imports keep this helper cheap to load when there are no images.
    try:
        from app.rerun_vision import analyse_image, resolve_image_path
    except ImportError:
        from rerun_vision import analyse_image, resolve_image_path
    import json as _j

    try:
        records = _j.loads(index_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Vision retry: could not read %s: %s", index_path, e)
        return {"checked": 0, "updated": 0, "skipped": 0}

    targets = [
        r for r in records
        if (r.get("source_doc", "") or "").lower() == source_doc_name.lower()
        and not r.get("keywords")
    ]
    if not targets:
        logger.info(
            "Vision retry: nothing to do for %s (all images already analysed).",
            source_doc_name,
        )
        return {"checked": 0, "updated": 0, "skipped": 0}

    logger.info(
        "Vision retry: %d image(s) from %s still need analysis — running now.",
        len(targets), source_doc_name,
    )

    updated = 0
    skipped = 0
    for rec in targets:
        img_path = resolve_image_path(rec)
        if not img_path:
            skipped += 1
            continue
        source = rec.get("source_doc", "unknown")
        page   = rec.get("page_number", "?")
        vision_data = analyse_image(img_path, f"{source}, page {page}")
        if not vision_data:
            skipped += 1
            continue
        for r in records:
            if r.get("image_id") == rec.get("image_id"):
                r.update(vision_data)
                break
        updated += 1

    if updated:
        try:
            index_path.write_text(_j.dumps(records, indent=2), encoding="utf-8")
            logger.info(
                "Vision retry: updated %d image(s) from %s (skipped %d).",
                updated, source_doc_name, skipped,
            )
        except Exception as e:
            logger.warning("Vision retry: failed to save updated index: %s", e)

    return {"checked": len(targets), "updated": updated, "skipped": skipped}


def _run_chunking_background(file_id: str, project_id: str, file_path: Path):
    """
    Background thread that chunks a file and updates its status.
    Called after upload completes.
    """
    try:
        # Robust import — works whether uvicorn runs from backend/ or app/
        try:
            from app.chunking_pipeline import run_pipeline_for_file, load_project_chunks
        except ImportError:
            from chunking_pipeline import run_pipeline_for_file, load_project_chunks

        # Update status to chunking
        if file_id in FILES:
            FILES[file_id]["status"] = "chunking"
            FILES[file_id]["progress"] = 50
        _save_state()

        project_dir = UPLOAD_ROOT / project_id
        # Ensure project source dir exists
        (project_dir / "source").mkdir(parents=True, exist_ok=True)

        # process_file returns the chunks for this file specifically
        try:
            from app.chunking_pipeline import process_file as _process_file
        except ImportError:
            from chunking_pipeline import process_file as _process_file

        new_chunks = _process_file(
            file_path=file_path,
            project_id=project_id,
            file_id=file_id,
            existing_chunk_count=0,
        )
        file_chunk_count = len(new_chunks)

        # Now persist to disk using the lock-safe writer
        try:
            from app.chunking_pipeline import (
                _get_project_lock, remove_file_chunks,
                load_project_chunks, save_project_chunks
            )
        except ImportError:
            from chunking_pipeline import (
                _get_project_lock, remove_file_chunks,
                load_project_chunks, save_project_chunks
            )

        lock = _get_project_lock(project_id)
        with lock:
            remove_file_chunks(project_dir, file_id)
            existing = load_project_chunks(project_dir)
            for i, chunk in enumerate(new_chunks):
                chunk["chunk_id"] = len(existing) + i
            save_project_chunks(project_dir, existing + new_chunks)

        total_chunks = len(existing) + file_chunk_count

        # Update status to indexed
        if file_id in FILES:
            FILES[file_id]["status"] = "indexed"
            FILES[file_id]["progress"] = 100
            FILES[file_id]["chunk_count"] = file_chunk_count

        # Extract images + run vision analysis for this file (non-blocking, best-effort)
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
            img_result = extract_and_index_single_file(
                file_path=str(file_path),
                output_dir=str(_app_dir / "extracted_images"),
                index_file=str(_app_dir / "image_chunks.json"),
                page_sections=_page_sections or None,
            )
            logger.info(
                "Image extraction complete for %s: %d found, %d new",
                file_id, img_result["images_found"], img_result["images_added"],
            )

            # ── Auto-run vision on any images that came back without keywords ──
            # The inline vision call inside extract_and_index_single_file can
            # silently fail (transient API/SSL issues, JSON parse errors) and
            # leave a record with empty keywords. We do a single retry pass
            # here so the user never has to invoke rerun_vision.py manually.
            try:
                _auto_rerun_vision_for_file(
                    source_doc_name=Path(file_path).name,
                    index_path=_app_dir / "image_chunks.json",
                    extracted_dir=_app_dir / "extracted_images",
                )
            except Exception as _v_exc:
                logger.warning(
                    "Auto vision retry failed for %s (non-critical): %s",
                    file_id, _v_exc,
                )
        except Exception as _img_exc:
            logger.warning("Image extraction failed for %s (non-critical): %s", file_id, _img_exc)

        # Rebuild the in-memory image index so newly-extracted images are
        # searchable immediately (no server restart required).
        try:
            try:
                from app.image_index import reload_image_index
            except ImportError:
                from image_index import reload_image_index
            n = reload_image_index()
            logger.info("Image index reloaded after upload: %d records", n)
        except Exception as _ri_exc:
            logger.debug("reload_image_index failed (non-critical): %s", _ri_exc)

        # Notify MCP server to reload project chunks into its doc search index
        try:
            import requests as _req
            mcp_base = os.getenv("MCP_HTTP_BASE", "http://localhost:8010")
            _req.post(
                f"{mcp_base}/reload/project/{project_id}",
                timeout=5,
            )
            logger.info("MCP server notified to reload chunks for project %s", project_id)
        except Exception as _mcp_err:
            logger.debug("Could not notify MCP server (non-critical): %s", _mcp_err)
        _save_state()
        logger.info("Chunking complete for %s — %d chunks (project total: %d)", file_id, file_chunk_count, total_chunks)

    except Exception as exc:
        logger.error("Chunking failed for %s: %s\n%s", file_id, exc, traceback.format_exc())
        if file_id in FILES:
            FILES[file_id]["status"] = "error"
            FILES[file_id]["progress"] = 100
            FILES[file_id]["error_detail"] = str(exc)
        _save_state()


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PROJECT ENDPOINTS
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/projects", response_model=list[ProjectOut])
def list_projects(workflow: str | None = None, exclude_workflow: str | None = None):
    """List projects. Pass `workflow` to scope the list to one value — e.g.
    `workflow=sow` for the SOW tool's own project hub. Pass `exclude_workflow`
    to hide one value instead — e.g. the BRD hub uses `exclude_workflow=sow`
    so SOW projects (which live in this same store) never show up mixed in."""
    result = []
    for p in PROJECTS.values():
        p_workflow = p.get("workflow", "new")
        if workflow is not None and p_workflow != workflow:
            continue
        if exclude_workflow is not None and p_workflow == exclude_workflow:
            continue
        proj_files = [f for f in FILES.values() if f["project_id"] == p["id"]]
        chunk_count = sum(f.get("chunk_count", 0) for f in proj_files)
        result.append(ProjectOut(
            id=p["id"], name=p["name"], client=p["client"],
            status=p["status"], created=p["created"], updated=p["updated"],
            file_count=len(proj_files), chunk_count=chunk_count,
            workflow=p.get("workflow", "new"),
        ))
    result.sort(key=lambda x: x.updated, reverse=True)
    return result


class ProjectCreate(BaseModel):
    name: str
    client: str = ""
    workflow: str = "new"


def create_project_record(name: str, client: str = "", workflow: str = "new") -> dict:
    """Core project-creation logic, shared by the /api/projects route and any
    other module (e.g. sow_routes.py's SOW→BRD handoff) that needs to spin up
    a new BRD project programmatically without going through HTTP."""
    pid = str(uuid4())[:8]
    now = datetime.utcnow().isoformat()
    project = {
        "id": pid, "name": (name or "").strip(),
        "client": (client or "").strip() or "No description",
        "status": "draft", "created": now, "updated": now,
        "repositories": [], "workflow": (workflow or "new"),
    }
    PROJECTS[pid] = project
    proj_dir = UPLOAD_ROOT / pid
    proj_dir.mkdir(exist_ok=True)
    (proj_dir / "source").mkdir(exist_ok=True)
    (proj_dir / "prompt").mkdir(exist_ok=True)
    _save_state()
    logger.info("Created project %s: %s", pid, project["name"])
    return project


@router.post("/projects", response_model=ProjectOut)
def create_project(req: ProjectCreate):
    project = create_project_record(req.name, req.client, req.workflow)
    return ProjectOut(**project, file_count=0, chunk_count=0)


@router.get("/projects/{project_id}", response_model=ProjectOut)
def get_project(project_id: str):
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")
    p = PROJECTS[project_id]
    proj_files = [f for f in FILES.values() if f["project_id"] == project_id]
    return ProjectOut(
        id=p["id"], name=p["name"], client=p["client"],
        status=p["status"], created=p["created"], updated=p["updated"],
        file_count=len(proj_files),
        chunk_count=sum(f.get("chunk_count", 0) for f in proj_files),
        workflow=p.get("workflow", "new"),
    )


@router.delete("/projects/{project_id}")
def delete_project(project_id: str):
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")
    to_remove = [fid for fid, f in FILES.items() if f["project_id"] == project_id]
    for fid in to_remove:
        del FILES[fid]
    # Remove the project from the app state FIRST so the delete always succeeds
    # and it disappears from the list — even if a file in its folder is locked
    # (e.g. an exported .docx still open in Word on Windows).
    name = PROJECTS[project_id]["name"]
    del PROJECTS[project_id]
    _save_state()
    # Best-effort folder cleanup; never fail the request on a locked file.
    proj_dir = UPLOAD_ROOT / project_id
    folder_removed = True
    if proj_dir.exists():
        shutil.rmtree(proj_dir, ignore_errors=True)
        folder_removed = not proj_dir.exists()
        if not folder_removed:
            logger.warning(
                "Project %s removed from app, but its folder could not be fully "
                "deleted (files may be open/locked): %s", project_id, proj_dir,
            )
    logger.info("Deleted project %s (%s) — %d files removed, folder_removed=%s",
                project_id, name, len(to_remove), folder_removed)
    return {"deleted": True, "project_id": project_id,
            "files_removed": len(to_remove), "folder_removed": folder_removed}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE UPLOAD — now triggers chunking automatically
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def ingest_source_bytes(project_id: str, filename: str, content: bytes, category: str = "source") -> dict:
    """Core file-ingestion logic, shared by the /api/upload route and any other
    module (e.g. sow_routes.py's SOW→BRD handoff) that needs to hand a project
    an already-in-memory document — same validation, same on-disk layout, same
    background chunking kickoff a real multipart upload gets, so a
    programmatically-ingested source doc is indistinguishable from one a user
    dragged into the Upload page."""
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")
    if category not in ("source", "prompt"):
        raise HTTPException(400, "category must be 'source' or 'prompt'")

    safe_name = Path(filename or "").name
    if not safe_name or safe_name in (".", ".."):
        raise HTTPException(400, "Invalid filename")

    ext = Path(safe_name).suffix.lower()
    valid_exts = VALID_SOURCE_EXTS if category == "source" else VALID_PROMPT_EXTS
    if ext not in valid_exts:
        raise HTTPException(400, f"Invalid file type '{ext}'. Accepted: {', '.join(valid_exts)}")

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f"File exceeds {MAX_FILE_SIZE // (1024*1024)} MB limit")

    proj_dir = UPLOAD_ROOT / project_id / category
    proj_dir.mkdir(parents=True, exist_ok=True)
    file_path = (proj_dir / safe_name).resolve()
    if not str(file_path).startswith(str(proj_dir.resolve())):
        raise HTTPException(400, "Invalid filename")
    with open(file_path, "wb") as f:
        f.write(content)

    file_id = str(uuid4())[:8]
    now = datetime.utcnow().isoformat()
    file_record = {
        "id": file_id,
        "project_id": project_id,
        "name": safe_name,
        "size": len(content),
        "ext": ext.lstrip("."),
        "category": category,
        "status": "uploaded",
        "progress": 100,
        "chunk_count": 0,
        "uploaded_at": now,
        "disk_path": str(file_path),
    }
    FILES[file_id] = file_record

    PROJECTS[project_id]["updated"] = now
    if PROJECTS[project_id]["status"] == "draft":
        PROJECTS[project_id]["status"] = "active"

    _save_state()
    logger.info("Ingested %s → %s (%s, %d bytes)", safe_name, project_id, category, len(content))

    if category == "source":
        thread = threading.Thread(
            target=_run_chunking_background,
            args=(file_id, project_id, file_path),
            daemon=True,
        )
        thread.start()
        logger.info("Chunking started in background for %s", file_id)

    return file_record


@router.post("/upload", response_model=FileOut)
async def upload_file(
    file: UploadFile = File(...),
    project_id: str = Form(...),
    category: str = Form(...),
):
    """
    Upload a file → save to disk → start chunking in background.
    Poll GET /api/projects/{id}/files to watch status: uploaded → chunking → indexed.
    """
    content = await file.read()
    record = ingest_source_bytes(project_id, file.filename or "", content, category)
    return FileOut(
        id=record["id"], project_id=record["project_id"], name=record["name"],
        size=record["size"], ext=record["ext"], category=record["category"],
        status=record["status"], progress=record["progress"],
        chunk_count=record["chunk_count"], uploaded_at=record["uploaded_at"],
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# FILE LISTING (poll this for status updates)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/projects/{project_id}/files", response_model=list[FileOut])
def list_files(project_id: str):
    """List all files — frontend polls this every 3s to get chunking status."""
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")
    result = []
    for f in FILES.values():
        if f["project_id"] == project_id:
            result.append(FileOut(
                id=f["id"], project_id=f["project_id"], name=f["name"],
                size=f["size"], ext=f["ext"], category=f["category"],
                status=f["status"], progress=f["progress"],
                chunk_count=f.get("chunk_count", 0), uploaded_at=f["uploaded_at"],
                error_detail=f.get("error_detail", ""),
            ))
    return result


@router.post("/projects/{project_id}/rechunk")
def rechunk_project(project_id: str, file_id: str = None):
    """Re-chunk source files for a project in the background.

    Re-runs the chunking pipeline so improvements to extraction (e.g. DOCX
    table support) are applied to already-uploaded documents without the
    user having to delete and re-upload. Pass ?file_id=… to re-chunk one
    file, or omit it to re-chunk every source file in the project.
    """
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")

    targets = [
        f for f in FILES.values()
        if f["project_id"] == project_id
        and f.get("category") == "source"
        and (file_id is None or f["id"] == file_id)
    ]
    if not targets:
        raise HTTPException(404, "No matching source files to re-chunk")

    started = 0
    for f in targets:
        disk_path = Path(f.get("disk_path", ""))
        if not disk_path.exists():
            logger.warning("rechunk: disk file missing for %s (%s) — skipping", f["id"], f["name"])
            continue
        f["status"] = "chunking"
        f["progress"] = 25
        threading.Thread(
            target=_run_chunking_background,
            args=(f["id"], project_id, disk_path),
            daemon=True,
        ).start()
        started += 1
    _save_state()
    logger.info("rechunk: started %d re-chunk job(s) for project %s", started, project_id)
    return {"ok": True, "rechunking": started}


@router.delete("/projects/{project_id}/files/{file_id}")
def delete_file(project_id: str, file_id: str):
    """Delete a file and remove its chunks from the project index."""
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")
    if file_id not in FILES or FILES[file_id]["project_id"] != project_id:
        raise HTTPException(404, "File not found")

    file_record = FILES[file_id]

    # Remove file from disk
    disk_path = Path(file_record.get("disk_path", ""))
    if disk_path.exists():
        disk_path.unlink()

    # Remove this file's chunks from the project's chunks.json
    try:
        from app.chunking_pipeline import remove_file_chunks
        project_dir = UPLOAD_ROOT / project_id
        remove_file_chunks(project_dir, file_id, project_id=project_id)
    except Exception as e:
        logger.warning("Could not clean up chunks for %s: %s", file_id, e)

    name = file_record["name"]
    del FILES[file_id]
    PROJECTS[project_id]["updated"] = datetime.utcnow().isoformat()
    _save_state()
    logger.info("Deleted file %s (%s) from project %s", file_id, name, project_id)
    return {"deleted": True, "file_id": file_id}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# CHUNK ACCESS (for Template Editor / debugging)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@router.get("/projects/{project_id}/chunks")
def get_chunks(project_id: str, file_id: str = None, include_repos: bool = True):
    """
    Get all chunks for a project, optionally merged with chunks from attached
    repositories. Pass include_repos=false to skip repository chunks.
    """
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")

    try:
        from app.chunking_pipeline import load_project_chunks
        project_dir = UPLOAD_ROOT / project_id
        chunks = load_project_chunks(project_dir)
    except Exception:
        chunks = []

    # Merge chunks from all attached repositories
    if include_repos:
        repo_ids = PROJECTS[project_id].get("repositories", [])
        if repo_ids:
            try:
                from app.repository_routes import load_chunks_for_repos
            except ImportError:
                from repository_routes import load_chunks_for_repos
            repo_chunks = load_chunks_for_repos(repo_ids)
            chunks = chunks + repo_chunks

    if file_id:
        chunks = [c for c in chunks if c.get("file_id") == file_id]

    return {
        "project_id": project_id,
        "total_chunks": len(chunks),
        "chunks": chunks,
    }