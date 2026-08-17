"""
image_repo_routes.py
─────────────────────────────────────────────────────────────────────────────
Backend for the "Image Library" page (static/image_repo.html) and the
`load_chunks_for_image_repos` helper used by the Template-Editor image browser.

Model (multi-library):
  • The page lists MULTIPLE image libraries. "+ New" creates a new, EMPTY library.
  • You import a DOCUMENT (DOCX/PDF/PPTX/XLSX) into a specific library; its images
    are extracted (with AI vision tagging) and tagged to that library, so they
    show up only in that library.
  • Images are physically stored once in extracted_images/ and indexed in
    image_chunks.json (shared with the editor's Browse Images). Library
    membership is a logical tag (`image_repo_id`) on each record — no duplication.

Imported optionally by main.py; if it raises on import the app still boots.
"""
from __future__ import annotations

import json
import uuid
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile, File
from fastapi.responses import FileResponse, JSONResponse

# S3-backed build has auth_middleware/storage/data_store; this local build
# falls back to plain filesystem storage and relies on main.py's session
# middleware for auth (REQUIRE_API_AUTH).
try:
    from app.auth_middleware import require_authenticated  # type: ignore
    from app.storage import storage  # type: ignore
    from app import data_store  # type: ignore
    _DEPS = [Depends(require_authenticated)]
except ImportError:
    class _LocalStorage:
        _root = Path(__file__).parent
        def read(self, key: str):
            p = self._root / key
            return p.read_bytes() if p.exists() else None
        def write(self, key: str, data: bytes) -> None:
            p = self._root / key
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
    storage = _LocalStorage()
    class _NoopDataStore:
        @staticmethod
        def purge(_path) -> None:
            pass
    data_store = _NoopDataStore()
    _DEPS = []

logger = logging.getLogger("ImageRepo")

router = APIRouter(dependencies=_DEPS)

# ── Paths / constants ───────────────────────────────────────────────────────
_APP_DIR        = Path(__file__).parent
_IMAGE_CHUNKS   = _APP_DIR / "image_chunks.json"
_EXTRACTED_DIR  = _APP_DIR / "extracted_images"
_REPOS_FILE     = _APP_DIR / "image_repos.json"   # registry of image libraries

_THIS_DIR        = Path(__file__).parent
IMAGE_REPOS_ROOT = _THIS_DIR / "image_repos"
REPOS_INDEX_PATH = _THIS_DIR / "image_repos.json"   # registry: [{repo_id, name, ...}]

IMAGE_REPOS_ROOT.mkdir(exist_ok=True)

ALLOWED_EXTS = {"png", "jpg", "jpeg", "gif", "bmp", "webp"}

BRD_SECTIONS: Dict[str, str] = {
    "1": "Introduction",
    "2": "Benefit Realization",
    "3": "Supply Chain Scope",
    "4": "Demand Planning",
    "5": "Supply & Inventory Planning",
    "6": "Data Integration",
}

BRD_SUB_SECTIONS: Dict[str, Dict[str, str]] = {
    "1": {"1.1": "Company Profile", "1.2": "Current State",
          "1.3": "Purpose & Objectives", "1.4": "Project Scope", "1.5": "Glossary"},
    "2": {"2.1": "Quantitative Benefits", "2.2": "Qualitative Benefits",
          "2.3": "KPIs", "2.4": "Benefit Realization Plan"},
    "3": {"3.1": "Network Map", "3.2": "Sites", "3.3": "Demand Foundation",
          "3.4": "Supply Foundation", "3.5": "Inventory Foundation",
          "3.6": "Planning Constraints", "3.7": "Scenario Structure"},
    "4": {"4.1": "Demand Review Process", "4.2": "Statistical Forecast",
          "4.3": "Forecast Consumption", "4.4": "Demand Management",
          "4.5": "Demand Test Cases"},
    "5": {"5.1": "Supply Review Process", "5.2": "Inventory Policy",
          "5.3": "Replenishment", "5.4": "Supply Exceptions", "5.5": "Supply Test Cases"},
    "6": {"6.1": "Data Sources", "6.2": "Inbound Interfaces",
          "6.3": "Outbound Interfaces", "6.4": "Data Frequency", "6.5": "Architecture"},
}

DIAGRAM_TYPES = [
    ("process_flow", "Process Flow"),
    ("screenshot",   "Screenshot"),
    ("architecture", "Architecture Diagram"),
    ("network_map",  "Network Map"),
    ("chart",        "Chart / Graph"),
    ("table",        "Table / Data"),
    ("other",        "Other"),
]


# ── Registry helpers ──────────────────────────────────────────────────────────

def _load_registry() -> List[dict]:
    try:
        data_bytes = storage.read("state/image_repos.json")
        if data_bytes:
            return json.loads(data_bytes.decode("utf-8"))
    except Exception:
        pass
    if REPOS_INDEX_PATH.exists():
        try:
            return json.loads(REPOS_INDEX_PATH.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []

_SUPPORTED_DOC = {".docx", ".doc", ".pdf", ".pptx", ".ppt", ".xlsx", ".xlsm"}


# ── Registry (multiple libraries) ─────────────────────────────────────────────
def _load_repos() -> List[Dict]:
    if _REPOS_FILE.exists():
        try:
            data = json.loads(_REPOS_FILE.read_text(encoding="utf-8"))
            return data.get("repos", []) if isinstance(data, dict) else (data or [])
        except Exception as e:
            logger.error("Failed to read image_repos.json: %s", e)
    return []

def _save_registry(repos: List[dict]) -> None:
    storage.write(
        "state/image_repos.json",
        json.dumps(repos, indent=2, ensure_ascii=False).encode("utf-8"),
    )


def _save_repos(repos: List[Dict]) -> None:
    _REPOS_FILE.write_text(json.dumps({"repos": repos}, indent=2), encoding="utf-8")


def _find_repo(repos: List[Dict], repo_id: str) -> Optional[Dict]:
    return next((r for r in repos if r.get("repo_id") == repo_id), None)


# ── Image store (shared image_chunks.json) ────────────────────────────────────
def _load_chunks() -> List[Dict]:
    if _IMAGE_CHUNKS.exists():
        try:
            return json.loads(_IMAGE_CHUNKS.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error("Failed to read image_chunks.json: %s", e)
    return []


def _save_chunks(records: List[Dict]) -> None:
    _IMAGE_CHUNKS.write_text(json.dumps(records, indent=2), encoding="utf-8")
    try:
        try:
            from app.image_index import reload_image_index
        except ImportError:
            from image_index import reload_image_index
        reload_image_index()
    except Exception as e:
        logger.debug("reload_image_index skipped: %s", e)


def _resolve_file(rec: Dict) -> Optional[Path]:
    """Resolve an image record to an on-disk file."""
    image_id = rec.get("image_id", "")
    for ext in ("png", "jpeg", "jpg", "gif", "webp", "bmp"):
        cand = _EXTRACTED_DIR / f"{image_id}.{ext}"
        if cand.exists():
            return cand
    stored = Path(rec.get("file_path", ""))
    if stored.is_absolute() and stored.exists():
        return stored
    cand = _EXTRACTED_DIR / stored.name
    if cand.exists():
        return cand
    cand = _APP_DIR / stored
    return cand if cand.exists() else None


def _records_for_repo(repo_id: str, records: Optional[List[Dict]] = None) -> List[Dict]:
    """Records that belong to a library AND have a file on disk."""
    records = records if records is not None else _load_chunks()
    out = []
    for r in records:
        if not r.get("image_id"):
            continue
        if r.get("image_repo_id") != repo_id:
            continue
        if _resolve_file(r) is None:
            continue
        out.append(r)
    return out


def _enrich(rec: Dict) -> Dict:
    out = dict(rec)
    out.setdefault("keywords", [])
    out["source_file"] = rec.get("source_doc", "")
    out["section"]     = rec.get("brd_section_id", "")
    out["sub_section"] = rec.get("section_hint", "")
    out["tags"]        = rec.get("keywords", [])
    return out


# ── Meta: sections / sub-sections / diagram types for the tagging dropdowns ──
@router.get("/api/image-repos/meta")
def image_repos_meta():
    try:
        from app.section_titles import get_titles
    except ImportError:
        from section_titles import get_titles
    titles = get_titles()

    sections: Dict[str, str] = {}
    sub_sections: Dict[str, Dict[str, str]] = {}
    for sid, title in titles.items():
        if "." not in sid:
            sections[sid] = title
        else:
            top = sid.split(".")[0]
            sub_sections.setdefault(top, {})[sid] = title

    diagram_types = [
        ["flowchart", "Flowchart"],
        ["table", "Table"],
        ["architecture_diagram", "Architecture Diagram"],
        ["timeline", "Timeline"],
        ["screenshot", "Screenshot"],
        ["chart", "Chart"],
        ["photo", "Photo"],
        ["other", "Other"],
    ]
    return {"sections": sections, "sub_sections": sub_sections, "diagram_types": diagram_types}


# ── Library list / create / edit / delete ─────────────────────────────────────
def _repo_out(repo: Dict, chunks: Optional[List[Dict]] = None) -> Dict:
    return {
        "repo_id": repo["repo_id"],
        "name": repo.get("name", ""),
        "description": repo.get("description", ""),
        "image_count": len(_records_for_repo(repo["repo_id"], chunks)),
    }


@router.get("/api/image-repos")
def list_image_repos():
    chunks = _load_chunks()
    repos = _load_repos()
    return {"repos": [_repo_out(r, chunks) for r in repos]}


@router.post("/api/image-repos")
def create_image_repo(name: str = Form(...), description: str = Form("")):
    repos = _load_repos()
    repo = {
        "repo_id": uuid.uuid4().hex[:8],
        "name": name.strip() or "Untitled Library",
        "description": description.strip(),
        "created": datetime.utcnow().isoformat(),
    }
    repos.append(repo)
    _save_repos(repos)
    # A brand-new library starts empty.
    return {"repo": {**_repo_out(repo, []), "image_count": 0}}


@router.put("/api/image-repos/{repo_id}")
def edit_image_repo(repo_id: str, name: str = Form(...), description: str = Form("")):
    repos = _load_repos()
    repo = _find_repo(repos, repo_id)
    if repo is None:
        raise HTTPException(404, "Library not found")
    repo["name"] = name.strip() or repo.get("name", "")
    repo["description"] = description.strip()
    _save_repos(repos)
    return {"repo": _repo_out(repo)}


@router.delete("/api/image-repos/{repo_id}")
def delete_image_repo(repo_id: str):
    repos = _load_repos()
    if _find_repo(repos, repo_id) is None:
        raise HTTPException(404, "Library not found")
    repos = [r for r in repos if r.get("repo_id") != repo_id]
    _save_repos(repos)
    # Remove image records that belonged to this library.
    chunks = _load_chunks()
    kept = [r for r in chunks if r.get("image_repo_id") != repo_id]
    if len(kept) != len(chunks):
        _save_chunks(kept)

def delete_repo(repo_id: str):
    repos = _load_registry()
    if not any(r["repo_id"] == repo_id for r in repos):
        raise HTTPException(404, "Repo not found")

    # Delete all files
    rd = _repo_dir(repo_id)
    if rd.exists():
        import shutil
        shutil.rmtree(rd)
    data_store.purge(rd)

    updated = [r for r in repos if r["repo_id"] != repo_id]
    _save_registry(updated)
    logger.info("ImageRepo: deleted repo %s", repo_id)
    return {"ok": True}


# ── Images: list / serve / extract-from-doc / edit / delete ──────────────────
@router.get("/api/image-repos/{repo_id}/images")
def list_repo_images(repo_id: str):
    return {"images": [_enrich(r) for r in _records_for_repo(repo_id)]}


@router.get("/api/image-repos/{repo_id}/image/{image_id}")
def serve_repo_image(repo_id: str, image_id: str):
    # Fast path: extracted files are stored as {image_id}.{ext}; resolve by name.
    for ext in ("png", "jpeg", "jpg", "gif", "webp", "bmp"):
        cand = _EXTRACTED_DIR / f"{image_id}.{ext}"
        if cand.exists():
            return FileResponse(path=str(cand), media_type=f"image/{ext}")
    for rec in _load_chunks():
        if rec.get("image_id") == image_id:
            fp = _resolve_file(rec)
            if fp:
                ext = rec.get("ext") or fp.suffix.lstrip(".") or "png"
                return FileResponse(path=str(fp), media_type=f"image/{ext}")
            break
    return JSONResponse({"error": "Image not found"}, status_code=404)


@router.post("/api/image-repos/{repo_id}/extract-doc")
async def extract_images_from_document(repo_id: str, file: UploadFile = File(...)):
    """Import a document INTO this library: extract its images (vision-tagged)
    and tag them to this library so they appear only here."""
    repos = _load_repos()
    if _find_repo(repos, repo_id) is None:
        raise HTTPException(404, "Library not found")

    doc_ext = Path(file.filename).suffix.lower()
    if doc_ext not in _SUPPORTED_DOC:
        raise HTTPException(400, f"Unsupported document '{doc_ext}'. Upload DOCX, PDF, PPTX or XLSX.")

    raw = await file.read()
    src_dir = _APP_DIR / "uploads" / "imagelib_src" / repo_id
    src_dir.mkdir(parents=True, exist_ok=True)
    src_path = src_dir / file.filename
    src_path.write_bytes(raw)

    try:
        from app.image_extractor import extract_and_index_single_file
    except ImportError:
        from image_extractor import extract_and_index_single_file

    try:
        extract_and_index_single_file(
            file_path=str(src_path),
            output_dir=str(_EXTRACTED_DIR),
            index_file=str(_IMAGE_CHUNKS),
            document_id=file.filename,
        )
    except Exception as e:
        logger.error("extract-doc failed for %s: %s", file.filename, e)
        raise HTTPException(500, f"Extraction failed: {e}")

    # Tag this document's extracted images to THIS library.
    chunks = _load_chunks()
    tagged = 0
    for r in chunks:
        if (r.get("source_doc") or "").lower() == (file.filename or "").lower():
            r["image_repo_id"] = repo_id
            tagged += 1
    _save_chunks(chunks)

    viewable = len(_records_for_repo(repo_id, chunks))
    return {
        "images_found": tagged,
        "images_added": tagged,
        "total": viewable,
    }


@router.put("/api/image-repos/{repo_id}/images/{image_id}")
async def edit_repo_image(
    repo_id: str,
    image_id: str,
    caption: str = Form(""),
    description: str = Form(""),
    section_id: str = Form(""),
    sub_section_id: str = Form(""),
    keywords: str = Form(""),
    diagram_type: str = Form(""),
):
    records = _load_chunks()
    found = False
    for rec in records:
        if rec.get("image_id") == image_id and rec.get("image_repo_id") == repo_id:
            rec["caption"]        = caption or rec.get("caption", "")
            rec["description"]    = description
            rec["brd_section_id"] = section_id
            rec["section_hint"]   = sub_section_id or rec.get("section_hint", "")
            rec["keywords"]       = [k.strip() for k in keywords.split(",") if k.strip()]
            if diagram_type:
                rec["diagram_type"] = diagram_type
            found = True
            break
    if not found:
        raise HTTPException(404, "Image not found")
    _save_chunks(records)
    return {"ok": True}


@router.delete("/api/image-repos/{repo_id}/images/{image_id}")
def delete_repo_image(repo_id: str, image_id: str):
    records = _load_chunks()
    kept = [r for r in records
            if not (r.get("image_id") == image_id and r.get("image_repo_id") == repo_id)]
    if len(kept) == len(records):
        raise HTTPException(404, "Image not found")
    _save_chunks(kept)


    fp = Path(target.get("file_path", ""))
    if fp.exists():
        try:
            fp.unlink()
        except Exception as e:
            logger.warning("Could not delete file %s: %s", fp, e)
    data_store.purge(fp)

    updated = [r for r in records if r.get("image_id") != image_id]
    _save_repo_chunks(repo_id, updated)
    return {"ok": True}


# ── Project ↔ Library linking ────────────────────────────────────────────────
def _projects_module():
    try:
        from app import project_routes as pr
    except ImportError:
        import project_routes as pr
    return pr


@router.get("/api/projects/{project_id}/image-repositories")
def get_project_image_repos(project_id: str):
    pr = _projects_module()
    proj = pr.PROJECTS.get(project_id)
    if proj is None:
        raise HTTPException(404, "Project not found")
    linked_ids = set(proj.get("image_repositories", []))
    chunks = _load_chunks()
    repos = [_repo_out(r, chunks) for r in _load_repos() if r["repo_id"] in linked_ids]
    return {"image_repositories": repos}


@router.post("/api/projects/{project_id}/image-repositories/{repo_id}")
def link_project_image_repo(project_id: str, repo_id: str):
    pr = _projects_module()
    proj = pr.PROJECTS.get(project_id)
    if proj is None:
        raise HTTPException(404, "Project not found")
    linked = proj.setdefault("image_repositories", [])
    if repo_id not in linked:
        linked.append(repo_id)
    pr._save_state()
    return {"ok": True, "image_repositories": linked}


@router.delete("/api/projects/{project_id}/image-repositories/{repo_id}")
def unlink_project_image_repo(project_id: str, repo_id: str):
    pr = _projects_module()
    proj = pr.PROJECTS.get(project_id)
    if proj is None:
        raise HTTPException(404, "Project not found")
    linked = proj.setdefault("image_repositories", [])
    if repo_id in linked:
        linked.remove(repo_id)
    pr._save_state()
    return {"ok": True, "image_repositories": linked}


# ── Helper used by the Template-Editor image browser (screen3_routes) ─────────
def load_chunks_for_image_repos(repo_ids: List[str]) -> List[Dict]:
    """Return image records tagged to any of the given libraries."""
    if not repo_ids:
        return []
    wanted = set(repo_ids)
    return [dict(r) for r in _load_chunks()
            if r.get("image_id") and r.get("image_repo_id") in wanted]
