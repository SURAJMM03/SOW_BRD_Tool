"""
rerun_vision.py
---------------
Re-runs GPT-4o vision analysis on all images in image_chunks.json
that were saved with empty keywords (i.e. vision failed during upload).

Place this script in EITHER:
  - C:\...\backend\          (run as: python rerun_vision.py)
  - C:\...\backend\app\      (run as: python rerun_vision.py)

The script auto-detects its location.

Optional args:
    --source-doc "MyFile.pdf"   only process images from that file
    --force-all                 re-analyse even images that already have keywords
"""

import os
import sys
import json
import base64
import logging
import argparse
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("rerun_vision")

# ── Auto-detect paths regardless of where script is placed ───────────────────
_HERE = Path(__file__).resolve().parent

if (_HERE / "image_chunks.json").exists():
    # Script is inside backend/app/
    APP_DIR = _HERE
elif (_HERE / "app" / "image_chunks.json").exists():
    # Script is inside backend/
    APP_DIR = _HERE / "app"
else:
    # Fallback: assume script is in backend/, app/ is subfolder
    APP_DIR = _HERE / "app"
    logger.warning(
        "image_chunks.json not found in auto-detected path: %s\n"
        "Make sure you placed rerun_vision.py in backend/ or backend/app/",
        APP_DIR
    )

IMAGE_CHUNKS  = APP_DIR / "image_chunks.json"
EXTRACTED_DIR = APP_DIR / "extracted_images"

logger.info("APP_DIR      : %s", APP_DIR)
logger.info("image_chunks : %s", IMAGE_CHUNKS)
logger.info("extracted_dir: %s", EXTRACTED_DIR)

# ── OpenAI with SSL bypass ──────────────────────────────────────────────────
try:
    import httpx
    from openai import OpenAI
except ImportError:
    logger.error("Missing packages. Run: pip install openai httpx")
    sys.exit(1)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logger.error("OPENAI_API_KEY environment variable not set.")
    sys.exit(1)

client = OpenAI(
    api_key=OPENAI_API_KEY,
    http_client=httpx.Client(verify=False, timeout=30.0),  # bypass corporate SSL proxy
    max_retries=1,
    timeout=30.0,
)

# ── Circuit breaker ──────────────────────────────────────────────────────────
# Trip after VISION_STRIKE_LIMIT consecutive failures and short-circuit the
# rest of the run — prevents the OpenAI client from spinning on a degraded
# endpoint for thousands of images.
VISION_STRIKE_LIMIT = 5
_vision_strikes = 0
_vision_disabled = False

# ── Vision prompt ─────────────────────────────────────────────────────────────
VISION_PROMPT = """\
You are a supply chain and ERP domain expert. Analyze this image and return ONLY valid JSON (no markdown, no comments).

Source: {source_context}

Respond with this exact JSON structure:
{{
  "description": "<2-4 sentence description of what this diagram/chart shows>",
  "caption": "<single short label, max 12 words, suitable as figure caption>",
  "keywords": ["<8-12 domain-specific terms: process names, entities, diagram type>"],
  "diagram_type": "<one of: flowchart, table, architecture_diagram, timeline, screenshot, chart, photo, other>",
  "section_hint": "<best-guess BRD section like '3 Supply Chain Scope' or 'unknown'>"
}}"""

VALID_DIAGRAM_TYPES = {
    "flowchart", "table", "architecture_diagram", "timeline",
    "screenshot", "chart", "photo", "other"
}

MIME_MAP = {
    ".png":  "image/png",
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif":  "image/gif",
    ".webp": "image/webp",
    ".bmp":  "image/bmp",
}


def analyse_image(image_path: Path, source_context: str) -> dict:
    """Call GPT-4o vision for one image. Returns enriched fields or None on failure."""
    global _vision_strikes, _vision_disabled
    if _vision_disabled:
        return None
    try:
        raw  = image_path.read_bytes()
        b64  = base64.b64encode(raw).decode("utf-8")
        mime = MIME_MAP.get(image_path.suffix.lower(), "image/png")

        response = client.chat.completions.create(
            model=os.getenv("VISION_MODEL", "gpt-4.1"),
            max_tokens=1024,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": VISION_PROMPT.format(source_context=source_context)
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{mime};base64,{b64}"}
                    }
                ]
            }]
        )

        text = response.choices[0].message.content.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        text = text.strip()

        data = json.loads(text)

        diagram_type = data.get("diagram_type", "other")
        if diagram_type not in VALID_DIAGRAM_TYPES:
            diagram_type = "other"

        _vision_strikes = 0
        return {
            "description":  data.get("description", ""),
            "caption":      data.get("caption", image_path.stem),
            "keywords":     data.get("keywords", [])[:12],
            "diagram_type": diagram_type,
            "section_hint": data.get("section_hint", "unknown"),
        }

    except json.JSONDecodeError as e:
        logger.warning("JSON parse error for %s: %s", image_path.name, e)
        return None
    except Exception as e:
        logger.error("Vision call failed for %s: %s", image_path.name, e)
        _vision_strikes += 1
        if _vision_strikes >= VISION_STRIKE_LIMIT:
            _vision_disabled = True
            logger.error(
                "Vision circuit breaker tripped after %d consecutive failures — "
                "skipping vision for remaining images in this run.",
                _vision_strikes,
            )
        return None


def resolve_image_path(record: dict) -> Path:
    """Find the actual file on disk for a record."""
    image_id = record.get("image_id", "")
    ext      = record.get("ext", "png")

    # Try absolute path stored in record
    stored = Path(record.get("file_path", ""))
    if stored.is_absolute() and stored.exists():
        return stored

    # Try extracted_images/<image_id>.<ext>
    candidate = EXTRACTED_DIR / f"{image_id}.{ext}"
    if candidate.exists():
        return candidate

    # Try all common extensions
    for e in ("png", "jpg", "jpeg", "gif", "webp"):
        candidate = EXTRACTED_DIR / f"{image_id}.{e}"
        if candidate.exists():
            return candidate

    # Try by filename from stored path
    if stored.name:
        candidate = EXTRACTED_DIR / stored.name
        if candidate.exists():
            return candidate

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Re-run GPT-4o vision on images with empty keywords."
    )
    parser.add_argument(
        "--source-doc", default=None,
        help="Only process images from this source document (e.g. 'MyFile.pdf')"
    )
    parser.add_argument(
        "--force-all", action="store_true",
        help="Re-analyse ALL images, even those that already have keywords"
    )
    args = parser.parse_args()

    # ── Load index ────────────────────────────────────────────────────────────
    if not IMAGE_CHUNKS.exists():
        logger.error("image_chunks.json not found at: %s", IMAGE_CHUNKS)
        sys.exit(1)

    records = json.loads(IMAGE_CHUNKS.read_text(encoding="utf-8"))
    logger.info("Loaded %d total records from image_chunks.json", len(records))

    # ── Filter: only records needing vision ───────────────────────────────────
    targets = []
    for r in records:
        has_keywords = bool(r.get("keywords"))
        if args.force_all or not has_keywords:
            if args.source_doc:
                if r.get("source_doc", "").lower() != args.source_doc.lower():
                    continue
            targets.append(r)

    if not targets:
        logger.info("Nothing to do — all records already have keywords.")
        return

    logger.info(
        "%d image(s) need vision analysis%s.",
        len(targets),
        f" (filtered to '{args.source_doc}')" if args.source_doc else ""
    )

    # ── Re-analyse ────────────────────────────────────────────────────────────
    updated = 0
    skipped = 0

    for i, record in enumerate(targets, 1):
        image_id = record.get("image_id", "?")
        source   = record.get("source_doc", "unknown")
        page     = record.get("page_number", "?")

        img_path = resolve_image_path(record)
        if not img_path:
            logger.warning(
                "[%d/%d] File not found on disk for image_id=%s — skipping",
                i, len(targets), image_id
            )
            skipped += 1
            continue

        logger.info(
            "[%d/%d] Analysing %s  (from %s, page %s) ...",
            i, len(targets), image_id, source, page
        )

        vision_data = analyse_image(img_path, f"{source}, page {page}")

        if vision_data is None:
            logger.warning("  → Vision failed, skipping.")
            skipped += 1
            continue

        # Patch the matching record in the full list
        for r in records:
            if r.get("image_id") == image_id:
                r.update(vision_data)
                break

        logger.info(
            "  → ✅ caption: %s | keywords: %s",
            vision_data["caption"],
            ", ".join(vision_data["keywords"][:4])
        )
        updated += 1

    # ── Save patched index ────────────────────────────────────────────────────
    IMAGE_CHUNKS.write_text(json.dumps(records, indent=2), encoding="utf-8")
    logger.info(
        "\nDone. %d updated, %d skipped. Saved → %s",
        updated, skipped, IMAGE_CHUNKS
    )
    if updated:
        logger.info("Restart uvicorn so the image index reloads from the updated file.")


if __name__ == "__main__":
    main()