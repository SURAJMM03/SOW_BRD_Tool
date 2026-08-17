"""
section_titles.py
─────────────────────────────────────────────────────────────────────────────
Single source of truth for section ID -> title mappings.

Reads from screen3_sections.json (edited by the user via Screen 3).
Falls back to the built-in defaults if the file doesn't exist yet.

Usage anywhere in the codebase:
    from app.section_titles import get_titles, get_title

    titles = get_titles()           # { "2.1": "Business Issues", ... }
    title  = get_title("2.1")       # "Business Issues"
─────────────────────────────────────────────────────────────────────────────
"""
from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Dict

logger = logging.getLogger("SectionTitles")

# Path to the live sections file written by screen3_routes.py
_SECTIONS_FILE = Path(__file__).parent / "screen3_sections.json"

# ── Built-in defaults (used when the file doesn't exist yet) ─────────────────
_DEFAULTS: Dict[str, str] = {
    "1":     "Introduction",
    "1.1":   "Company Information",
    "1.2":   "Current State of Business",
    "1.3":   "Purpose",
    "1.4":   "Scope",
    "1.5":   "Definitions & Acronyms",
    "2":     "Benefit Realization",
    "2.1":   "Business Issues",
    "2.2":   "Value Drivers",
    "3":     "Supply Chain Scope",
    "3.1":   "Supply Chain Maps",
    "3.2":   "Sites",
    "3.3":   "Demand Foundation",
    "3.4":   "Supply Foundation",
    "3.5":   "Inventory Management Foundation",
    "3.6":   "Constraints",
    "3.7":   "Scenario Structure",
    "4":     "Demand Planning",
    "4.1":   "Demand Planning Process Overview",
    "4.2":   "Forecast Consumption",
    "4.2.1": "Solution Assumptions",
    "4.2.2": "Resources",
    "5":     "Supply & Inventory Planning",
    "5.1":   "Review and Adjust Planning Parameters",
    "5.2":   "Manage Capacity Constraints",
    "5.3":   "Resolve Supply Plan Exceptions",
    "5.4":   "Resolve Inventory Exceptions",
    "6":     "Data Integration",
    "6.1":   "Architecture",
    "6.2":   "Data Sources",
    "6.3":   "Data Files",
    "6.4":   "Data Frequency",
    "7":     "Project Governance & Implementation Approach",
    "7.1":   "Project Organization & Roles",
    "7.2":   "Governance & Decision-Making",
    "7.3":   "Change Management Process",
    "7.4":   "Acceptance & Defect Management",
}


def get_titles() -> Dict[str, str]:
    """
    Return the current live section ID -> title mapping.
    Reads from screen3_sections.json every call so renames are
    always reflected without restarting the server.
    """
    if not _SECTIONS_FILE.exists():
        return dict(_DEFAULTS)

    try:
        data    = json.loads(_SECTIONS_FILE.read_text(encoding="utf-8"))
        active  = data.get("active", [])
        archived = data.get("archived", [])

        titles = dict(_DEFAULTS)   # start with defaults as base
        for sec in active + archived:
            sid   = sec.get("id", "").strip()
            title = sec.get("title", "").strip()
            if sid and title:
                titles[sid] = title   # user value always wins

        return titles

    except Exception as e:
        logger.warning("Could not read section titles from disk: %s — using defaults", e)
        return dict(_DEFAULTS)


def get_title(section_id: str, fallback: str = "") -> str:
    """Return the title for a single section ID."""
    return get_titles().get(section_id, fallback or section_id)