"""
backfill_brd_section_ids.py

One-shot script: classifies existing chunks in all chunks.json files and
stamps each with a brd_section_id ("1"–"6") where one is missing.

Run from the repo root:
    python backfill_brd_section_ids.py

Dry-run (no writes):
    python backfill_brd_section_ids.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# ── Section keyword map (same as chunking_pipeline.py) ────────────────────────
_SECTION_KEYWORDS: dict[str, list[str]] = {
    "1": [
        "introduction", "purpose", "scope", "company information",
        "definitions", "acronyms", "background", "overview",
    ],
    "2": [
        "benefit realization", "benefit realisation", "business issues",
        "value drivers", "quantitative benefits", "qualitative benefits",
        "kpi", "key performance", "roi", "return on investment",
    ],
    "3": [
        "supply chain scope", "supply chain map", "sites",
        "demand foundation", "supply foundation",
        "inventory management foundation", "scenario structure",
        "planning constraints", "supply chain design",
    ],
    "4": [
        "demand planning", "demand process", "demand management",
        "statistical forecast", "forecast consumption", "net off",
        "demand review", "unconstrained forecast", "consensus demand",
        "demand test", "demand test cases",
    ],
    "5": [
        "supply planning", "inventory planning", "supply & inventory",
        "supply and inventory", "capacity constraints",
        "supply exceptions", "inventory exceptions",
        "supply review", "replenishment", "supply test", "supply test cases",
    ],
    "6": [
        "data integration", "data integrations", "architecture",
        "data sources", "data files", "data frequency",
        "interface", "inbound", "outbound", "etl", "middleware",
    ],
}


def classify_chunk_section(section_heading: str, chunk_text: str) -> str:
    haystack = f"{section_heading} {chunk_text[:400]}".lower()
    best_sid, best_count = "", 0
    for sid, keywords in _SECTION_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw in haystack)
        if count > best_count:
            best_count, best_sid = count, sid
    return best_sid if best_count >= 1 else ""


def backfill_file(chunks_path: Path, dry_run: bool) -> tuple[int, int]:
    """Return (total_chunks, newly_tagged)."""
    try:
        chunks = json.loads(chunks_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ERROR reading {chunks_path}: {e}")
        return 0, 0

    tagged = 0
    for chunk in chunks:
        if chunk.get("brd_section_id"):
            continue  # already classified
        sid = classify_chunk_section(
            chunk.get("section_heading", ""),
            chunk.get("chunk_text", ""),
        )
        chunk["brd_section_id"] = sid
        if sid:
            tagged += 1

    if not dry_run and tagged > 0:
        chunks_path.write_text(
            json.dumps(chunks, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return len(chunks), tagged


_IMG_SECTION_KEYWORDS: dict[str, list[str]] = {
    "1": ["introduction", "purpose", "scope", "company information", "overview"],
    "2": ["benefit realization", "business issues", "value drivers", "kpi", "roi"],
    "3": ["supply chain scope", "supply chain map", "sites", "scenario", "constraints"],
    "4": ["demand planning", "demand process", "forecast", "demand management"],
    "5": ["supply planning", "inventory planning", "capacity", "replenishment"],
    "6": ["data integration", "architecture", "data sources", "interface", "etl"],
}

import re as _re

def _derive_image_section_id(record: dict) -> str:
    sec_heading = record.get("section_heading", "")
    for src in (sec_heading, record.get("section_hint", "")):
        if src and src.lower() not in ("unknown", ""):
            m = _re.match(r"(\d)", src.strip())
            if m and m.group(1) in _IMG_SECTION_KEYWORDS:
                return m.group(1)
    haystack = f"{sec_heading} {record.get('caption','')} {' '.join(record.get('keywords',[]))} {record.get('description','')[:300]}".lower()
    best_sid, best_count = "", 0
    for sid, kws in _IMG_SECTION_KEYWORDS.items():
        count = sum(1 for kw in kws if kw in haystack)
        if count > best_count:
            best_count, best_sid = count, sid
    return best_sid if best_count >= 1 else ""


def backfill_image_file(image_index_path: Path, dry_run: bool) -> tuple[int, int]:
    """Backfill brd_section_id on image_chunks.json records."""
    try:
        records = json.loads(image_index_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  ERROR reading {image_index_path}: {e}")
        return 0, 0

    tagged = 0
    for rec in records:
        if rec.get("brd_section_id"):
            continue
        sid = _derive_image_section_id(rec)
        rec["brd_section_id"] = sid
        if sid:
            tagged += 1

    if not dry_run and tagged > 0:
        image_index_path.write_text(
            json.dumps(records, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    return len(records), tagged


def find_all_chunk_files(repo_root: Path) -> list[Path]:
    uploads = repo_root / "orchestration" / "brd_convo_app" / "backend" / "app" / "uploads"
    if not uploads.exists():
        return []
    return list(uploads.rglob("chunks.json"))


def find_image_index_files(repo_root: Path) -> list[Path]:
    app_dir = repo_root / "orchestration" / "brd_convo_app" / "backend" / "app"
    found = []
    candidate = app_dir / "image_chunks.json"
    if candidate.exists():
        found.append(candidate)
    return found


def main():
    parser = argparse.ArgumentParser(description="Backfill brd_section_id on existing chunks")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    args = parser.parse_args()

    repo_root = Path(__file__).parent
    chunk_files = find_all_chunk_files(repo_root)
    image_files = find_image_index_files(repo_root)

    if not chunk_files and not image_files:
        print("No chunks.json or image_chunks.json found. Nothing to do.")
        sys.exit(0)

    label = "DRY RUN — " if args.dry_run else ""
    print(f"{label}Found {len(chunk_files)} chunks.json + {len(image_files)} image_chunks.json\n")

    total_chunks = total_tagged = 0

    if chunk_files:
        print("── Text chunks ──")
        for path in chunk_files:
            rel = path.relative_to(repo_root)
            n, t = backfill_file(path, dry_run=args.dry_run)
            total_chunks += n
            total_tagged += t
            status = "would tag" if args.dry_run else "tagged"
            print(f"  {rel}  →  {n} chunks, {t} {status}")

    if image_files:
        print("\n── Image chunks ──")
        for path in image_files:
            rel = path.relative_to(repo_root)
            n, t = backfill_image_file(path, dry_run=args.dry_run)
            total_chunks += n
            total_tagged += t
            status = "would tag" if args.dry_run else "tagged"
            print(f"  {rel}  →  {n} records, {t} {status}")

    print(f"\nDone. {total_tagged}/{total_chunks} items {'would be' if args.dry_run else ''} classified.")
    if args.dry_run:
        print("Re-run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
