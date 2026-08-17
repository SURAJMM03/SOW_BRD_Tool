"""export_import_routes.py — Export / Import for Repositories and Projects

ZIP format — Repository:
  manifest.json          type, version, metadata
  files/                 source documents
  chunks.json            pre-built text chunks

ZIP format — Project (full or partial sections):
  manifest.json          type, version, metadata, sections_exported, repo_dependencies
  files/source/          source documents  (omitted for section-only exports)
  files/prompt/          prompt configs    (omitted for section-only exports)
  chunks.json            text chunks       (omitted for section-only exports)
  approved_sections.json {section_id: content} filtered to exported sections
  section_structure.json active / archived section dicts filtered by section
  keyword_store.json     keywords filtered by section
  sources_store.json     sources filtered by section

Import conflict handling:
  Name already exists → 409 {"conflict": true, "suggestion": "Copy of ..."}
  Re-submit with new_name to proceed.

Repo dependency warning:
  If a project has attached repos, the manifest lists them.
  The import endpoint echoes them as warnings so the frontend can inform the user.

Endpoints:
  GET  /api/repositories/{repo_id}/export
  POST /api/repositories/import
  GET  /api/projects/{project_id}/export-info        (section list + dependency check)
  GET  /api/projects/{project_id}/export?sections=1,2
  POST /api/projects/import
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import threading
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

logger = logging.getLogger("ExportImport")

router = APIRouter(prefix="/api", tags=["export-import"])

# ── Paths resolved at import time ─────────────────────────────────────────────
_APP_DIR = Path(__file__).parent

# Mirrors project_routes.py
try:
    from app.project_routes import UPLOAD_ROOT, PROJECTS, FILES, _save_state as _save_projects
except ImportError:
    from project_routes import UPLOAD_ROOT, PROJECTS, FILES, _save_state as _save_projects

# Mirrors repository_routes.py
try:
    from app.repository_routes import (
        REPOS_ROOT, REPOSITORIES, REPO_FILES,
        _save_state as _save_repos,
        _run_repo_chunking_background,
    )
except ImportError:
    from repository_routes import (
        REPOS_ROOT, REPOSITORIES, REPO_FILES,
        _save_state as _save_repos,
        _run_repo_chunking_background,
    )

# Chunking helpers (shared with project + repo pipeline)
try:
    from app.chunking_pipeline import load_project_chunks
except ImportError:
    from chunking_pipeline import load_project_chunks

EXPORT_VERSION = "1.0"

SECTION_NAMES = {
    "1": "Introduction",
    "2": "Benefit Realization",
    "3": "Supply Chain Scope",
    "4": "Demand Planning",
    "5": "Inventory Planning",
    "6": "Data Integration",
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS — screen3 store paths (mirrors screen3_routes.py)
# ─────────────────────────────────────────────────────────────────────────────

def _approved_path(project_id: str) -> Path:
    return _APP_DIR / f"screen3_approved_{project_id}.json"

def _sections_path(project_id: str) -> Path:
    return _APP_DIR / f"screen3_sections_{project_id}.json"

def _keyword_path(project_id: str) -> Path:
    return _APP_DIR / f"screen3_keyword_store_{project_id}.json"

def _sources_path(project_id: str) -> Path:
    return _APP_DIR / f"screen3_sources_{project_id}.json"


def _load_json(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _filter_by_sections(data: dict, section_ids: list[str]) -> dict:
    """Keep only keys whose top-level section number is in section_ids."""
    return {k: v for k, v in data.items() if k.split(".")[0] in section_ids}


def _filter_section_structure(store: dict, section_ids: list[str]) -> dict:
    """Filter active/archived section lists to the selected top-level sections."""
    def keep(s):
        return str(s.get("id", "")).split(".")[0] in section_ids

    return {
        "active": [s for s in store.get("active", []) if keep(s)],
        "archived": [s for s in store.get("archived", []) if keep(s)],
    }


def _approved_section_ids(project_id: str) -> list[str]:
    """Return list of top-level section IDs that have approved content."""
    store = _load_json(_approved_path(project_id))
    sections = store.get("sections", {})
    return sorted({sid.split(".")[0] for sid in sections}, key=lambda x: int(x) if x.isdigit() else x)


def _repo_dependency_info(project_id: str) -> list[dict]:
    """Return info dicts for repos attached to the project."""
    proj = PROJECTS.get(project_id, {})
    result = []
    for rid in proj.get("repositories", []):
        r = REPOSITORIES.get(rid)
        if r:
            result.append({"id": rid, "name": r["name"], "description": r.get("description", "")})
    return result


# ─────────────────────────────────────────────────────────────────────────────
# REPOSITORY EXPORT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/repositories/{repo_id}/export")
def export_repository(repo_id: str):
    if repo_id not in REPOSITORIES:
        raise HTTPException(404, "Repository not found")

    r = REPOSITORIES[repo_id]
    repo_dir = REPOS_ROOT / repo_id

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # manifest
        repo_files = [f for f in REPO_FILES.values() if f["repo_id"] == repo_id]
        manifest = {
            "type": "repository",
            "version": EXPORT_VERSION,
            "id": repo_id,
            "name": r["name"],
            "description": r.get("description", ""),
            "created": r["created"],
            "updated": r["updated"],
            "exported_at": datetime.utcnow().isoformat(),
            "file_count": len(repo_files),
            "chunk_count": sum(f.get("chunk_count", 0) for f in repo_files),
            "files_meta": repo_files,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        # source files
        source_dir = repo_dir / "source"
        if source_dir.exists():
            for fp in source_dir.iterdir():
                if fp.is_file():
                    zf.write(fp, f"files/{fp.name}")

        # chunks
        chunks_path = repo_dir / "chunks.json"
        if chunks_path.exists():
            zf.write(chunks_path, "chunks.json")

    buf.seek(0)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in r["name"])
    filename = f"repo_export_{safe_name}_{repo_id}.zip"
    logger.info("Exporting repository %s (%s)", repo_id, r["name"])

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# REPOSITORY IMPORT
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/repositories/import")
async def import_repository(
    file: UploadFile = File(...),
    new_name: Optional[str] = Form(None),
):
    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid ZIP file")

    if "manifest.json" not in zf.namelist():
        raise HTTPException(400, "Missing manifest.json in ZIP")

    manifest = json.loads(zf.read("manifest.json"))
    if manifest.get("type") != "repository":
        raise HTTPException(400, f"Expected type 'repository', got '{manifest.get('type')}'")
    if manifest.get("version") != EXPORT_VERSION:
        raise HTTPException(400, f"Unsupported export version '{manifest.get('version')}'")

    import_name = (new_name or "").strip() or manifest["name"]

    # Conflict check — same name
    conflict = next((r for r in REPOSITORIES.values() if r["name"].lower() == import_name.lower()), None)
    if conflict:
        suggestion = f"Copy of {import_name}"
        return {
            "conflict": True,
            "existing_name": conflict["name"],
            "existing_id": conflict["id"],
            "suggestion": suggestion,
        }

    # Create repository
    rid = str(uuid4())[:8]
    now = datetime.utcnow().isoformat()
    repo = {
        "id": rid,
        "name": import_name,
        "description": manifest.get("description", ""),
        "created": now,
        "updated": now,
    }
    REPOSITORIES[rid] = repo
    repo_dir = REPOS_ROOT / rid
    repo_dir.mkdir(exist_ok=True)
    (repo_dir / "source").mkdir(exist_ok=True)

    # Extract source files and register FILE records
    files_meta = {f["name"]: f for f in manifest.get("files_meta", [])}
    imported_files = []
    for name in zf.namelist():
        if name.startswith("files/") and not name.endswith("/"):
            fname = name[len("files/"):]
            dest = repo_dir / "source" / fname
            dest.write_bytes(zf.read(name))
            orig = files_meta.get(fname, {})
            fid = str(uuid4())[:8]
            file_record = {
                "id": fid, "repo_id": rid,
                "name": fname,
                "size": orig.get("size", dest.stat().st_size),
                "ext": Path(fname).suffix.lstrip("."),
                "status": "indexed",
                "progress": 100,
                "chunk_count": orig.get("chunk_count", 0),
                "uploaded_at": now,
                "disk_path": str(dest),
            }
            REPO_FILES[fid] = file_record
            imported_files.append(fname)

    # Restore chunks — avoids re-chunking
    if "chunks.json" in zf.namelist():
        (repo_dir / "chunks.json").write_bytes(zf.read("chunks.json"))
        logger.info("Restored %d chunks for repo %s", len(json.loads(zf.read("chunks.json"))), rid)
    else:
        # No chunks → trigger background chunking for each file
        for fname in imported_files:
            fp = repo_dir / "source" / fname
            fid = next((f["id"] for f in REPO_FILES.values() if f["repo_id"] == rid and f["name"] == fname), None)
            if fid:
                REPO_FILES[fid]["status"] = "uploaded"
                t = threading.Thread(target=_run_repo_chunking_background, args=(fid, rid, fp), daemon=True)
                t.start()

    _save_repos()
    logger.info("Imported repository '%s' as %s (%d files)", import_name, rid, len(imported_files))

    return {
        "imported": True,
        "repo": {"id": rid, "name": import_name, "file_count": len(imported_files)},
        "warnings": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# PROJECT EXPORT — INFO endpoint (for UI: section picker + dependency check)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/export-info")
def project_export_info(project_id: str):
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")

    available_sections = []
    for sid, name in SECTION_NAMES.items():
        approved_ids = _approved_section_ids(project_id)
        available_sections.append({
            "id": sid,
            "name": name,
            "has_content": sid in approved_ids,
        })

    repo_deps = _repo_dependency_info(project_id)

    return {
        "project_id": project_id,
        "project_name": PROJECTS[project_id]["name"],
        "sections": available_sections,
        "repo_dependencies": repo_deps,
        "has_repo_dependencies": len(repo_deps) > 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# PROJECT EXPORT
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/projects/{project_id}/export")
def export_project(project_id: str, sections: str = None):
    """
    Export a project as a ZIP.
    sections: comma-separated top-level section IDs, e.g. "1,2,3".
              Omit to export all sections + source files.
    """
    if project_id not in PROJECTS:
        raise HTTPException(404, "Project not found")

    p = PROJECTS[project_id]
    proj_dir = UPLOAD_ROOT / project_id

    # Determine which sections to export
    all_section_ids = list(SECTION_NAMES.keys())
    if sections:
        section_ids = [s.strip() for s in sections.split(",") if s.strip() in all_section_ids]
    else:
        section_ids = all_section_ids
    section_only = bool(sections)  # True = partial export (no source files)

    # Load section data
    approved_store = _load_json(_approved_path(project_id))
    sections_store = _load_json(_sections_path(project_id))
    keyword_store = _load_json(_keyword_path(project_id))
    sources_store = _load_json(_sources_path(project_id))

    # Filter to selected sections
    approved_filtered = _filter_by_sections(approved_store.get("sections", {}), section_ids)
    sections_filtered = _filter_section_structure(sections_store, section_ids)
    keywords_filtered = _filter_by_sections(keyword_store, section_ids)
    sources_filtered = _filter_by_sections(sources_store, section_ids)

    repo_deps = _repo_dependency_info(project_id)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Manifest
        proj_files = [f for f in FILES.values() if f["project_id"] == project_id]
        manifest = {
            "type": "project",
            "version": EXPORT_VERSION,
            "id": project_id,
            "name": p["name"],
            "client": p.get("client", ""),
            "exported_at": datetime.utcnow().isoformat(),
            "sections_exported": section_ids,
            "sections_partial": section_only,
            "includes_files": not section_only,
            "repo_dependencies": repo_deps,
            "files_meta": proj_files if not section_only else [],
            "company": approved_store.get("company", ""),
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))

        # Section content stores
        zf.writestr("approved_sections.json",
                    json.dumps({"sections": approved_filtered,
                                "company": approved_store.get("company", "")}, indent=2))
        zf.writestr("section_structure.json", json.dumps(sections_filtered, indent=2))
        zf.writestr("keyword_store.json", json.dumps(keywords_filtered, indent=2))
        zf.writestr("sources_store.json", json.dumps(sources_filtered, indent=2))

        if not section_only:
            # Source files
            source_dir = proj_dir / "source"
            if source_dir.exists():
                for fp in source_dir.iterdir():
                    if fp.is_file():
                        zf.write(fp, f"files/source/{fp.name}")

            # Prompt files
            prompt_dir = proj_dir / "prompt"
            if prompt_dir.exists():
                for fp in prompt_dir.iterdir():
                    if fp.is_file():
                        zf.write(fp, f"files/prompt/{fp.name}")

            # Chunks
            chunks_path = proj_dir / "chunks.json"
            if chunks_path.exists():
                zf.write(chunks_path, "chunks.json")

    buf.seek(0)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in p["name"])
    sec_tag = f"_sections_{'_'.join(section_ids)}" if section_only else ""
    filename = f"project_export_{safe_name}{sec_tag}_{project_id}.zip"
    logger.info("Exporting project %s sections=%s", project_id, section_ids)

    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─────────────────────────────────────────────────────────────────────────────
# PROJECT IMPORT
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/projects/import")
async def import_project(
    file: UploadFile = File(...),
    new_name: Optional[str] = Form(None),
):
    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(400, "Invalid ZIP file")

    if "manifest.json" not in zf.namelist():
        raise HTTPException(400, "Missing manifest.json in ZIP")

    manifest = json.loads(zf.read("manifest.json"))
    if manifest.get("type") != "project":
        raise HTTPException(400, f"Expected type 'project', got '{manifest.get('type')}'")
    if manifest.get("version") != EXPORT_VERSION:
        raise HTTPException(400, f"Unsupported export version '{manifest.get('version')}'")

    import_name = (new_name or "").strip() or manifest["name"]

    # Conflict check
    conflict = next((p for p in PROJECTS.values() if p["name"].lower() == import_name.lower()), None)
    if conflict:
        suggestion = f"Copy of {import_name}"
        return {
            "conflict": True,
            "existing_name": conflict["name"],
            "existing_id": conflict["id"],
            "suggestion": suggestion,
        }

    # Create project
    pid = str(uuid4())[:8]
    now = datetime.utcnow().isoformat()
    project = {
        "id": pid,
        "name": import_name,
        "client": manifest.get("client", ""),
        "status": "active",
        "created": now,
        "updated": now,
        "repositories": [],
    }
    PROJECTS[pid] = project
    proj_dir = UPLOAD_ROOT / pid
    proj_dir.mkdir(exist_ok=True)
    (proj_dir / "source").mkdir(exist_ok=True)
    (proj_dir / "prompt").mkdir(exist_ok=True)

    imported_source_files = []
    imported_prompt_files = []

    # Extract source + prompt files (full exports only)
    if manifest.get("includes_files", False):
        files_meta = {f["name"]: f for f in manifest.get("files_meta", [])}
        for name in zf.namelist():
            if name.startswith("files/source/") and not name.endswith("/"):
                fname = name[len("files/source/"):]
                dest = proj_dir / "source" / fname
                dest.write_bytes(zf.read(name))
                orig = files_meta.get(fname, {})
                fid = str(uuid4())[:8]
                file_record = {
                    "id": fid, "project_id": pid,
                    "name": fname,
                    "size": orig.get("size", dest.stat().st_size),
                    "ext": Path(fname).suffix.lstrip("."),
                    "category": "source",
                    "status": "indexed",
                    "progress": 100,
                    "chunk_count": orig.get("chunk_count", 0),
                    "uploaded_at": now,
                    "disk_path": str(dest),
                }
                FILES[fid] = file_record
                imported_source_files.append(fname)

            elif name.startswith("files/prompt/") and not name.endswith("/"):
                fname = name[len("files/prompt/"):]
                dest = proj_dir / "prompt" / fname
                dest.write_bytes(zf.read(name))
                fid = str(uuid4())[:8]
                FILES[fid] = {
                    "id": fid, "project_id": pid,
                    "name": fname,
                    "size": dest.stat().st_size,
                    "ext": Path(fname).suffix.lstrip("."),
                    "category": "prompt",
                    "status": "uploaded",
                    "progress": 100,
                    "chunk_count": 0,
                    "uploaded_at": now,
                    "disk_path": str(dest),
                }
                imported_prompt_files.append(fname)

        # Restore chunks
        if "chunks.json" in zf.namelist():
            (proj_dir / "chunks.json").write_bytes(zf.read("chunks.json"))

    # Restore section stores into the app dir (per-project paths)
    if "approved_sections.json" in zf.namelist():
        _approved_path(pid).write_bytes(zf.read("approved_sections.json"))

    if "section_structure.json" in zf.namelist():
        _sections_path(pid).write_bytes(zf.read("section_structure.json"))

    if "keyword_store.json" in zf.namelist():
        _keyword_path(pid).write_bytes(zf.read("keyword_store.json"))

    if "sources_store.json" in zf.namelist():
        _sources_path(pid).write_bytes(zf.read("sources_store.json"))

    _save_projects()
    logger.info(
        "Imported project '%s' as %s (src=%d, prompt=%d)",
        import_name, pid, len(imported_source_files), len(imported_prompt_files),
    )

    # Collect warnings
    warnings = []
    repo_deps = manifest.get("repo_dependencies", [])
    if repo_deps:
        dep_names = ", ".join(f'"{r["name"]}"' for r in repo_deps)
        warnings.append(
            f"This project used repositor{'y' if len(repo_deps)==1 else 'ies'} {dep_names}. "
            f"Import {'that repository' if len(repo_deps)==1 else 'those repositories'} "
            f"first and re-attach to this project for full functionality."
        )

    sections_exported = manifest.get("sections_exported", [])
    sections_partial = manifest.get("sections_partial", False)
    if sections_partial and sections_exported:
        names = [SECTION_NAMES.get(s, s) for s in sections_exported]
        warnings.append(
            f"This is a partial export containing only: {', '.join(names)}. "
            f"Other sections will need to be generated."
        )

    return {
        "imported": True,
        "project": {
            "id": pid,
            "name": import_name,
            "client": project["client"],
            "source_files": len(imported_source_files),
            "prompt_files": len(imported_prompt_files),
            "sections_imported": sections_exported,
        },
        "repo_dependencies": repo_deps,
        "warnings": warnings,
    }
