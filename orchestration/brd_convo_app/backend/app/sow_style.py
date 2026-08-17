"""
sow_style.py — Read a SOW template's own font/size/color styling so the
per-section HTML preview (sow_section_routes.py's preview endpoint) can
approximate what the exported .docx will actually look like, instead of
rendering the reviewer's draft in a generic, unstyled way.

Deliberately a small, separate module rather than growing sow_workflow.py
further — this is read-only style *introspection*, not document assembly.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger("SOWStyle")

# Matches sow_workflow._BRAND_NAVY / _set_cell_navy_header exactly, so a
# previewed table's header looks identical to the real exported one.
_BRAND_NAVY = "1B3A6B"
_TABLE_HEADER_TEXT = "FFFFFF"

_DEFAULT_PROFILE: Dict = {
    "body_font": "Calibri",
    "body_size_pt": 11,
    "body_color": "#1A1A1A",
    "heading_font": "Calibri",
    "heading_size_pt": 14,
    "heading_color": f"#{_BRAND_NAVY}",
    "table_header_bg": f"#{_BRAND_NAVY}",
    "table_header_color": f"#{_TABLE_HEADER_TEXT}",
}

_CACHE: Dict[str, Dict] = {}


def _rgb_hex(color) -> Optional[str]:
    try:
        if color and color.rgb:
            return f"#{str(color.rgb)}"
    except Exception:
        pass
    return None


def _font_profile(font) -> Dict:
    out = {}
    try:
        if font.name:
            out["font"] = font.name
        if font.size:
            out["size_pt"] = font.size.pt
        c = _rgb_hex(font.color)
        if c:
            out["color"] = c
    except Exception:
        pass
    return out


def get_template_style_profile(template_id: Optional[str], template_docx_path: Optional[Path]) -> Dict:
    """Best-effort read of a template's Normal/Heading styles. Falls back to
    a sane hardcoded profile (matching the Bristlecone brand navy already
    used elsewhere) when there's no real template file — e.g. a project still
    on the flat FALLBACK_SECTIONS structure with no imported .docx."""
    cache_key = template_id or "none"
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    profile = dict(_DEFAULT_PROFILE)
    if template_docx_path and Path(template_docx_path).exists():
        try:
            from docx import Document
            doc = Document(str(template_docx_path))
            styles = doc.styles

            try:
                normal = _font_profile(styles["Normal"].font)
                if normal.get("font"):
                    profile["body_font"] = normal["font"]
                if normal.get("size_pt"):
                    profile["body_size_pt"] = normal["size_pt"]
                if normal.get("color"):
                    profile["body_color"] = normal["color"]
            except KeyError:
                pass

            for style_name in ("Heading 1", "Heading 2", "Heading 3", "Heading 4"):
                try:
                    h = _font_profile(styles[style_name].font)
                except KeyError:
                    continue
                if h.get("font"):
                    profile["heading_font"] = h["font"]
                if h.get("size_pt"):
                    profile["heading_size_pt"] = h["size_pt"]
                if h.get("color"):
                    profile["heading_color"] = h["color"]
                break  # first heading level found is enough for a preview
        except Exception as exc:
            logger.warning("Could not read style profile from %s: %s", template_docx_path, exc)

    _CACHE[cache_key] = profile
    return profile
