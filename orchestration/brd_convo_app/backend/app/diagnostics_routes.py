"""
diagnostics_routes.py — "why is this environment behaving differently?"

Everything here exists because the app runs in two very different places: a
developer laptop with Office, an `az login` session and a full pip install, and
a hosted server with none of those guaranteed. The failures that gap produces
are quiet ones:

  * A reader library that is not installed makes its extractor return zero
    blocks. No exception is raised, so the upload reports success and the file
    shows as indexed with nothing in it. The user sees "the tool cannot read my
    document" and there is nothing in the UI to explain why.
  * DefaultAzureCredential resolves interactively from `az login` on a laptop,
    and from a managed identity or service principal on a server. A host with
    neither falls back to whatever LLM_PROVIDER names, and a placeholder API
    key produces a 401 at the first generate — long after deployment.

GET /api/diagnostics answers both in one request, so triaging a hosted install
is one URL rather than an SSH session.

NOTHING HERE RETURNS A SECRET. Credentials are reported as present, placeholder
or missing — never echoed.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from pathlib import Path

from fastapi import APIRouter, Query, Request

router = APIRouter(prefix="/api/diagnostics", tags=["diagnostics"])

_PLACEHOLDERS = {
    "your-anthropic-api-key-here",
    "your-openai-api-key-here",
    "your_openai_api_key_here",
    "your-serper-api-key-here",
    "changeme",
}


def _key_state(name: str) -> str:
    """present | placeholder | missing — never the value itself."""
    raw = (os.getenv(name) or "").strip().strip('"').strip("'")
    if not raw:
        return "missing"
    if raw.lower() in _PLACEHOLDERS:
        return "placeholder"
    return "present"


def _version(module: str):
    try:
        mod = __import__(module)
        return getattr(mod, "__version__", "installed")
    except Exception:
        return None


def reader_status() -> dict:
    """Which document formats this environment can actually read.

    Mirrors chunking_pipeline's dispatch: when the library an extractor imports
    is absent, that extractor returns no blocks and the document reads as empty.
    """
    fitz = _version("fitz")
    pdfminer = _version("pdfminer")
    docx = _version("docx")
    openpyxl = _version("openpyxl")
    pptx = _version("pptx")
    xlrd = _version("xlrd")
    soffice = shutil.which("soffice") or shutil.which("libreoffice")

    dash = "-"
    formats = {
        ".pdf": (bool(fitz or pdfminer),
                 "PyMuPDF={0} pdfminer={1}".format(fitz or dash, pdfminer or dash),
                 "pip install pymupdf"),
        ".docx": (bool(docx), "python-docx={0}".format(docx or dash),
                  "pip install python-docx"),
        ".xlsx": (bool(openpyxl), "openpyxl={0}".format(openpyxl or dash),
                  "pip install openpyxl"),
        ".xls": (bool(xlrd), "xlrd={0}".format(xlrd or dash), "pip install xlrd"),
        ".pptx": (bool(pptx), "python-pptx={0}".format(pptx or dash),
                  "pip install python-pptx"),
        # Legacy binary formats are converted by LibreOffice first; without it
        # the extractor has nothing to hand the parser.
        ".ppt": (bool(pptx and soffice),
                 "python-pptx={0} soffice={1}".format(pptx or dash, soffice or "NOT FOUND"),
                 "install libreoffice (provides soffice on PATH)"),
        ".doc": (bool(docx and soffice),
                 "python-docx={0} soffice={1}".format(docx or dash, soffice or "NOT FOUND"),
                 "install libreoffice (provides soffice on PATH)"),
    }

    readable = {}
    for ext, (ok, detail, fix) in formats.items():
        entry = {"readable": ok, "detail": detail}
        if not ok:
            entry["fix"] = fix
        readable[ext] = entry

    return {
        "formats": readable,
        "unreadable": sorted(k for k, v in readable.items() if not v["readable"]),
    }


def llm_status() -> dict:
    """Which provider will serve a generate call, and whether it can authenticate.

    Configuration only — no API call is made unless the caller passes probe=true.
    """
    provider = (os.getenv("LLM_PROVIDER", "azure") or "azure").strip().lower()
    is_azure = provider in ("azure", "azure_openai", "azureopenai", "aoai")

    info = {
        "provider": "azure" if is_azure else provider,
        "provider_source": "LLM_PROVIDER" if os.getenv("LLM_PROVIDER") else "default (azure)",
        "mock_mode": os.getenv("CLAUDE_MOCK", "") == "1",
    }

    if is_azure:
        # DefaultAzureCredential resolves very differently on a server than on a
        # laptop: `az login` is a developer convenience, not a hosting story.
        service_principal = all(
            os.getenv(v) for v in ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"))
        managed_identity = bool(os.getenv("IDENTITY_ENDPOINT") or os.getenv("MSI_ENDPOINT"))
        az_cli = bool(shutil.which("az"))
        info.update({
            "endpoint": os.getenv("AZURE_OPENAI_ENDPOINT", "(module default)"),
            "deployment": os.getenv("AZURE_OPENAI_DEPLOYMENT", "(module default)"),
            "ssl_verify": os.getenv("AZURE_SSL_VERIFY", "true"),
            "credential_sources": {
                "service_principal_env": service_principal,
                "managed_identity": managed_identity,
                "azure_cli_on_path": az_cli,
            },
        })
        if not (service_principal or managed_identity):
            info["warning"] = (
                "No service principal or managed identity is configured. "
                "DefaultAzureCredential will fall back to the Azure CLI, which only "
                "works if `az login` was run on this host and its token is still "
                "valid — not a durable setup for a hosted service. Set "
                "AZURE_CLIENT_ID / AZURE_CLIENT_SECRET / AZURE_TENANT_ID, or attach "
                "a managed identity.")
    else:
        state = _key_state("CLAUDE_API_KEY")
        info["model"] = os.getenv("CLAUDE_MODEL", "(module default)")
        info["CLAUDE_API_KEY"] = state
        if state != "present":
            info["warning"] = (
                "LLM_PROVIDER={0} but CLAUDE_API_KEY is {1}. Every generate and "
                "review call will fail with 401 'API key is invalid'. Set a real "
                "key, or set LLM_PROVIDER=azure and configure Azure credentials."
                .format(provider, state))

    return info


def identity_status(request) -> dict:
    """Whether this instance is actually identifying anyone.

    The app has no login of its own: it trusts an identity header set by the
    host application. Two things make that unsafe, and neither is visible from
    the UI — so report both.
    """
    from app import identity as ident

    header_present = bool(request.headers.get(ident.AUTH_USER_HEADER))
    info = {
        "auth_user_header": ident.AUTH_USER_HEADER,
        "header_on_this_request": header_present,
        "dev_fallback_user": "set" if ident.DEV_FALLBACK_USER else "unset",
    }
    if ident.DEV_FALLBACK_USER:
        info["warning"] = (
            "DEV_FALLBACK_USER is set, so every request is treated as that user "
            "even with no identity header. That is intended for running the app "
            "standalone on a laptop — on a hosted instance it means anyone who "
            "can reach this URL is signed in. Unset it in production.")
    elif not header_present:
        info["note"] = (
            "No identity header on this request. API calls will be refused with "
            "401 until the host application forwards one.")
    return info


@router.get("")
def diagnostics(request: Request,
                probe: bool = Query(False, description="Make one tiny live LLM call")):
    """Everything needed to triage a hosted install, in one response."""
    from app.project_routes import UPLOAD_ROOT

    uploads = Path(UPLOAD_ROOT)
    writable = False
    try:
        uploads.mkdir(parents=True, exist_ok=True)
        marker = uploads / ".write_test"
        marker.write_text("ok", encoding="utf-8")
        marker.unlink()
        writable = True
    except Exception:
        writable = False

    storage = {
        "upload_root": str(uploads),
        "exists": uploads.exists(),
        "writable": writable,
    }
    if not writable:
        storage["warning"] = (
            "The upload directory is not writable, so no uploaded file can be "
            "stored or read back. In a container this usually means the path sits "
            "inside a read-only image layer — mount a writable volume there.")

    out = {
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cwd": os.getcwd(),
        },
        "identity": identity_status(request),
        "storage": storage,
        "readers": reader_status(),
        "llm": llm_status(),
        "search": {
            # Chunk embeddings and the slide-image vision pass both hang off
            # this one key; without it indexing still works, but degraded.
            "OPENAI_API_KEY": _key_state("OPENAI_API_KEY"),
            "note": ("Without it, uploads still index but retrieval falls back to "
                     "keyword matching and slide images get no description."),
        },
    }

    if probe:
        try:
            from app.claude_provider import completion_from_prompt
            reply = completion_from_prompt(
                "Reply with the single word: ok", max_tokens=16, temperature=0.0)
            out["llm"]["probe"] = {"ok": True, "reply": (reply or "").strip()[:40]}
        except Exception as exc:
            out["llm"]["probe"] = {
                "ok": False,
                "error": "{0}: {1}".format(type(exc).__name__, exc)[:400],
            }

    return out
