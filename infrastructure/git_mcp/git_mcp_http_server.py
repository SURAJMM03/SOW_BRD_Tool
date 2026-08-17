"""
Git MCP HTTP bridge for Blueprint Document version snapshots.

Run locally:
    uvicorn infrastructure.git_mcp.git_mcp_http_server:app --host 127.0.0.1 --port 8020

Then set in the backend .env:
    GIT_MCP_BASE_URL=http://127.0.0.1:8020

The editor backend never calls git directly. It posts snapshot/restore events
to this bridge, and the bridge commits the snapshot JSON files to the repo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel


app = FastAPI(title="Blueprint Document Git MCP Bridge")


class VersionEvent(BaseModel):
    action: Optional[str] = None
    project_id: Optional[str] = None
    version_id: Optional[str] = None
    message: Optional[str] = None
    snapshot_path: Optional[str] = None
    restored_snapshot: Optional[str] = None
    source: Optional[str] = None


def _git_executable() -> str:
    configured = (os.getenv("GIT_MCP_GIT_EXE") or "").strip()
    if configured:
        return configured
    return shutil.which("git") or "git"


def _run_git(repo: Path, args: List[str], check: bool = True) -> subprocess.CompletedProcess:
    git_args: List[str] = []
    user_name = (os.getenv("GIT_MCP_USER_NAME") or "").strip()
    user_email = (os.getenv("GIT_MCP_USER_EMAIL") or "").strip()
    if user_name:
        git_args.extend(["-c", f"user.name={user_name}"])
    if user_email:
        git_args.extend(["-c", f"user.email={user_email}"])
    try:
        result = subprocess.run(
            [_git_executable(), *git_args, *args],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=int(os.getenv("GIT_MCP_COMMAND_TIMEOUT_SECONDS", "30")),
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=500, detail="git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(status_code=504, detail="git command timed out") from exc

    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise HTTPException(status_code=500, detail=detail)
    return result


def _find_repo_for(path: Path) -> Path:
    configured = (os.getenv("GIT_MCP_REPO_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    for parent in [path, *path.parents]:
        if (parent / ".git").exists():
            return parent
    return Path.cwd().resolve()


def _ensure_repo(repo: Path):
    repo.mkdir(parents=True, exist_ok=True)
    result = _run_git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
    if result.returncode == 0:
        return
    if os.getenv("GIT_MCP_AUTO_INIT", "").lower() in {"1", "true", "yes"}:
        _run_git(repo, ["init"])
        return
    raise HTTPException(
        status_code=400,
        detail=f"{repo} is not a git repository. Set GIT_MCP_REPO_PATH or enable GIT_MCP_AUTO_INIT=true.",
    )


def _event_paths(event: VersionEvent) -> List[Path]:
    raw_paths = [event.snapshot_path, event.restored_snapshot]
    paths = []
    for raw in raw_paths:
        if not raw:
            continue
        path = Path(raw).expanduser().resolve()
        if path.exists() and path.is_file():
            paths.append(path)
    return paths


def _relative_paths(repo: Path, paths: List[Path]) -> List[str]:
    rels = []
    for path in paths:
        try:
            rels.append(str(path.relative_to(repo)))
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"Snapshot path {path} is outside git repository {repo}",
            ) from exc
    return rels


def _commit_event(event: VersionEvent) -> dict:
    paths = _event_paths(event)
    if not paths:
        raise HTTPException(status_code=400, detail="No existing snapshot file path was supplied.")

    repo = _find_repo_for(paths[0])
    _ensure_repo(repo)
    rels = _relative_paths(repo, paths)

    _run_git(repo, ["add", "--", *rels])
    status = _run_git(repo, ["status", "--porcelain", "--", *rels], check=False)
    if not status.stdout.strip():
        return {
            "ok": True,
            "committed": False,
            "repo": str(repo),
            "paths": rels,
            "message": "No git changes detected for supplied snapshot path.",
        }

    message = event.message or f"Document version {event.version_id or event.action or 'snapshot'}"
    commit = _run_git(repo, ["commit", "-m", message], check=False)
    if commit.returncode != 0:
        detail = (commit.stderr or commit.stdout or "git commit failed").strip()
        raise HTTPException(status_code=500, detail=detail)

    rev = _run_git(repo, ["rev-parse", "--short", "HEAD"])
    return {
        "ok": True,
        "committed": True,
        "repo": str(repo),
        "paths": rels,
        "commit": rev.stdout.strip(),
        "message": message,
    }


@app.get("/health")
def health():
    configured_git = (os.getenv("GIT_MCP_GIT_EXE") or "").strip()
    git_path = configured_git or shutil.which("git")
    git_available = bool(git_path and Path(git_path).exists()) if configured_git else bool(git_path)
    configured_repo = (os.getenv("GIT_MCP_REPO_PATH") or "").strip()
    return {
        "ok": True,
        "service": "git-mcp-http-bridge",
        "git_available": git_available,
        "git_path": git_path,
        "repo_path": configured_repo or None,
        "git_user_name": (os.getenv("GIT_MCP_USER_NAME") or "").strip() or None,
        "git_user_email": (os.getenv("GIT_MCP_USER_EMAIL") or "").strip() or None,
    }


@app.post("/document-version/snapshot")
def document_version_snapshot(event: VersionEvent):
    return _commit_event(event)


@app.post("/document-version/restore")
def document_version_restore(event: VersionEvent):
    return _commit_event(event)


@app.post("/snapshot")
def snapshot_alias(event: VersionEvent):
    return _commit_event(event)


@app.post("/restore")
def restore_alias(event: VersionEvent):
    return _commit_event(event)
