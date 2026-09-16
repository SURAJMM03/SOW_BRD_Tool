"""
Phase 1-2: Image Extraction & Vision Analysis
Extracts images from PDFs and DOCX files, then analyzes them with GPT-4o vision.
"""

import os
import json
import base64
import hashlib
import logging
from pathlib import Path
from typing import List, Dict, Optional
import io

# Try to import required libraries
try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None

try:
    from docx import Document as DocxDocument
except ImportError:
    DocxDocument = None

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


# ── BRD section ID derivation for image records ───────────────────────────────
_IMG_SECTION_KEYWORDS: dict[str, list[str]] = {
    "1": ["introduction", "purpose", "scope", "company information", "overview"],
    "2": ["benefit realization", "business issues", "value drivers", "kpi", "roi"],
    "3": ["supply chain scope", "supply chain map", "sites", "scenario", "constraints"],
    "4": ["demand planning", "demand process", "forecast", "demand management"],
    "5": ["supply planning", "inventory planning", "capacity", "replenishment"],
    "6": ["data integration", "architecture", "data sources", "interface", "etl"],
}

import re as _re

def _derive_brd_section_id(section_hint: str, caption: str, keywords: list,
                            description: str, section_heading: str = "") -> str:
    """
    Derive a clean BRD section ID ("1"–"6") from an image record.
    Priority: (1) leading digit in section_heading (doc structure — most reliable),
              (2) leading digit in section_hint (vision guess),
              (3) keyword scoring over all text fields.
    """
    for src in (section_heading, section_hint):
        if src and src.lower() not in ("unknown", ""):
            m = _re.match(r"(\d)", src.strip())
            if m and m.group(1) in _IMG_SECTION_KEYWORDS:
                return m.group(1)

    haystack = f"{section_heading} {caption} {' '.join(keywords)} {description[:300]}".lower()
    best_sid, best_count = "", 0
    for sid, kws in _IMG_SECTION_KEYWORDS.items():
        count = sum(1 for kw in kws if kw in haystack)
        if count > best_count:
            best_count, best_sid = count, sid
    return best_sid if best_count >= 1 else ""


class ImageExtractor:
    """Extracts and analyzes images from reference documents"""

    # Supported image formats
    SUPPORTED_FORMATS = {'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp'}
    MIN_IMAGE_SIZE = 2048  # 2 KB
    MAX_IMAGE_SIZE = 20 * 1024 * 1024  # 20 MB

    # Vision circuit breaker — after this many consecutive failures, stop
    # calling the API for the remainder of this run. Prevents retry-storms
    # when the OpenAI endpoint is degraded.
    VISION_STRIKE_LIMIT = 5

    def __init__(self, output_dir: str = "extracted_images", openai_api_key: Optional[str] = None):
        """Initialize image extractor"""
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.openai_api_key = openai_api_key or os.getenv("OPENAI_API_KEY")
        if self.openai_api_key and OpenAI:
            import httpx as _httpx
            _ssl_verify = os.getenv("OPENAI_SSL_VERIFY", "false").lower() not in ("false", "0", "no")
            self.client = OpenAI(
                api_key=self.openai_api_key,
                http_client=_httpx.Client(verify=_ssl_verify, timeout=30.0),
                max_retries=1,
                timeout=30.0,
            )
        else:
            self.client = None
        # Circuit-breaker state for vision calls.
        self._vision_strikes = 0
        self._vision_disabled = False
    
    # ─── Phase 1: Image Extraction ───────────────────────────────────────

    def _extract_docx_section_map(self, doc: "DocxDocument") -> dict:
        """Walk DOCX body in document order; return {rId: {section_heading, h1, h2, h3}}.

        Traverses every <w:p> element (including those inside tables) so that
        images embedded anywhere in the document get the heading that precedes them.
        """
        section_map: dict = {}
        h1 = h2 = h3 = ""

        W   = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        _P  = f"{{{W}}}p"
        _PP = f"{{{W}}}pPr"
        _PS = f"{{{W}}}pStyle"
        _T  = f"{{{W}}}t"
        _VL = f"{{{W}}}val"

        try:
            from lxml import etree as _ET
        except ImportError:
            return section_map

        for p_elem in doc.element.body.iter(_P):
            pPr = p_elem.find(_PP)
            if pPr is not None:
                ps = pPr.find(_PS)
                if ps is not None:
                    val = (ps.get(_VL) or "").lower().replace(" ", "").replace("-", "")
                    text = "".join(t.text for t in p_elem.iter(_T) if t.text).strip()
                    if "heading1" in val:
                        h1, h2, h3 = text, "", ""
                    elif "heading2" in val:
                        h2, h3 = text, ""
                    elif "heading3" in val:
                        h3 = text

            xml_str = _ET.tostring(p_elem, encoding="unicode")
            for rid in set(_re.findall(r'r:(?:embed|id)="(rId\d+)"', xml_str)):
                section_map[rid] = {
                    "section_heading": h3 or h2 or h1,
                    "h1": h1, "h2": h2, "h3": h3,
                }

        return section_map

    def extract_images_from_pdf(self, pdf_path: str,
                                 page_sections: dict = None) -> List[Dict]:
        """Extract images from PDF using PyMuPDF.

        For each page:
        - Extracts embedded raster images (JPEG, PNG, etc.)
        - Falls back to rendering the full page as PNG when no raster images are
          found but the page appears visual (vector diagrams, supply chain maps, etc.)
        """
        if not fitz:
            logger.warning("PyMuPDF not installed. Skipping PDF image extraction.")
            return []

        extracted = []
        doc = None

        try:
            doc = fitz.open(pdf_path)
            logger.info(f"Extracting images from PDF: {pdf_path}")

            for page_num in range(len(doc)):
                page = doc[page_num]
                images = page.get_images(full=True)
                page_had_raster = False
                # Resolve section context for this page from the text-chunk map
                _pn = page_num + 1
                _sec_ctx = (page_sections or {}).get(_pn, {})
                _sec_heading = _sec_ctx.get("section_heading", "")

                for img in images:
                    try:
                        xref = img[0]
                        image_bytes = doc.extract_image(xref)

                        if not image_bytes or "image" not in image_bytes:
                            continue

                        raw_bytes = image_bytes["image"]
                        ext = image_bytes.get("ext", "png").lower()

                        if ext not in self.SUPPORTED_FORMATS:
                            continue

                        file_size = len(raw_bytes)
                        if file_size < self.MIN_IMAGE_SIZE or file_size > self.MAX_IMAGE_SIZE:
                            continue

                        image_id = hashlib.sha256(raw_bytes).hexdigest()[:12]
                        file_path = self.output_dir / f"{image_id}.{ext}"

                        if not file_path.exists():
                            with open(file_path, "wb") as f:
                                f.write(raw_bytes)
                            logger.info(f"Extracted raster image {image_id} from {Path(pdf_path).name} page {_pn}")

                        extracted.append({
                            "image_id":        image_id,
                            "file_path":       str(file_path),
                            "source_doc":      Path(pdf_path).name,
                            "page_number":     _pn,
                            "ext":             ext,
                            "size":            file_size,
                            "section_heading": _sec_heading,
                        })
                        page_had_raster = True

                    except Exception as e:
                        logger.warning(f"Failed to extract raster image from PDF page {_pn}: {e}")

                # ── Page-render fallback for vector diagrams ──────────────────
                # If the page had no embedded raster images but contains drawing
                # objects (supply chain maps, flowcharts, architecture diagrams),
                # render the whole page as PNG so it appears in Browse Images.
                if not page_had_raster:
                    try:
                        page_text = page.get_text("text").strip()
                        has_drawings = bool(page.get_drawings())
                        # Render if: mostly-visual page (< 300 chars) OR has vector drawings
                        if len(page_text) < 300 or has_drawings:
                            mat = fitz.Matrix(2.0, 2.0)  # 2× zoom for legibility
                            pix = page.get_pixmap(matrix=mat, alpha=False)
                            raw_bytes = pix.tobytes("png")

                            if self.MIN_IMAGE_SIZE <= len(raw_bytes) <= self.MAX_IMAGE_SIZE:
                                image_id = hashlib.sha256(raw_bytes).hexdigest()[:12]
                                file_path = self.output_dir / f"{image_id}.png"

                                if not file_path.exists():
                                    file_path.write_bytes(raw_bytes)
                                    logger.info(
                                        f"Rendered page {page_num + 1} of {Path(pdf_path).name} "
                                        f"as PNG (vector/diagram content)"
                                    )

                                extracted.append({
                                    "image_id":        image_id,
                                    "file_path":       str(file_path),
                                    "source_doc":      Path(pdf_path).name,
                                    "page_number":     _pn,
                                    "ext":             "png",
                                    "size":            len(raw_bytes),
                                    "section_heading": _sec_heading,
                                })
                    except Exception as e:
                        logger.warning(f"Page render fallback failed for page {_pn}: {e}")

        except Exception as e:
            logger.error(f"Failed to process PDF {pdf_path}: {e}")
        finally:
            # Always release the file handle so Windows doesn't keep it locked
            # (a leaked handle here blocks subsequent DELETEs with WinError 32).
            if doc is not None:
                try:
                    doc.close()
                except Exception:
                    pass

        return extracted
    
    def extract_images_from_docx(self, docx_path: str) -> List[Dict]:
        """Extract images from DOCX using python-docx"""
        if not DocxDocument:
            logger.warning("python-docx not installed. Skipping DOCX image extraction.")
            return []
        
        extracted = []
        
        try:
            doc = DocxDocument(docx_path)
            logger.info(f"Extracting images from DOCX: {docx_path}")

            # Build heading→image map so each image inherits its document section
            section_map = self._extract_docx_section_map(doc)

            for rid, rel in doc.part.rels.items():
                if "image" not in rel.reltype:
                    continue

                try:
                    image_bytes = rel.target_part.blob

                    file_size = len(image_bytes)
                    if file_size < self.MIN_IMAGE_SIZE or file_size > self.MAX_IMAGE_SIZE:
                        logger.debug(f"Skipping image (size: {file_size} bytes)")
                        continue

                    content_type = rel.target_part.content_type
                    ext_map = {
                        "image/png": "png",
                        "image/jpeg": "jpg",
                        "image/gif": "gif",
                        "image/webp": "webp"
                    }
                    ext = ext_map.get(content_type, "png")

                    if ext not in self.SUPPORTED_FORMATS:
                        logger.debug(f"Skipping unsupported format: {ext}")
                        continue

                    image_id = hashlib.sha256(image_bytes).hexdigest()[:12]
                    file_path = self.output_dir / f"{image_id}.{ext}"

                    sec_ctx = section_map.get(rid, {})
                    sec_heading = sec_ctx.get("section_heading", "")

                    if file_path.exists():
                        logger.debug(f"Image {image_id} already extracted")
                        extracted.append({
                            "image_id":        image_id,
                            "file_path":       str(file_path),
                            "source_doc":      Path(docx_path).name,
                            "page_number":     None,
                            "ext":             ext,
                            "size":            file_size,
                            "section_heading": sec_heading,
                            "h1": sec_ctx.get("h1", ""),
                            "h2": sec_ctx.get("h2", ""),
                            "h3": sec_ctx.get("h3", ""),
                        })
                        continue

                    with open(file_path, "wb") as f:
                        f.write(image_bytes)

                    logger.info(f"Extracted image {image_id} from {Path(docx_path).name} [{sec_heading or 'no section'}]")

                    extracted.append({
                        "image_id":        image_id,
                        "file_path":       str(file_path),
                        "source_doc":      Path(docx_path).name,
                        "page_number":     None,
                        "ext":             ext,
                        "size":            file_size,
                        "section_heading": sec_heading,
                        "h1": sec_ctx.get("h1", ""),
                        "h2": sec_ctx.get("h2", ""),
                        "h3": sec_ctx.get("h3", ""),
                    })

                except Exception as e:
                    logger.warning(f"Failed to extract image from DOCX: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to process DOCX {docx_path}: {e}")

        extracted = self._merge_sweep(extracted, docx_path)
        return extracted

    def _save_image_record(self, raw_bytes: bytes, ext: str, source_doc: str,
                            page_number=None) -> Optional[Dict]:
        """Persist a single raw image to disk and return its index record."""
        file_size = len(raw_bytes)
        if file_size < self.MIN_IMAGE_SIZE or file_size > self.MAX_IMAGE_SIZE:
            return None
        ext = (ext or "png").lower().lstrip(".")
        if ext not in self.SUPPORTED_FORMATS:
            return None
        # Single chokepoint for "is there anything actually on this image".
        # Every extraction path funnels through here, so rejecting blanks once
        # keeps empty art out of extracted_images/ and out of the search index
        # — where it used to surface as a candidate for section embedding.
        try:
            from app.metafile_render import is_blank_image
            if is_blank_image(raw_bytes):
                logger.debug("Skipping blank image from %s", source_doc)
                return None
        except Exception:
            pass
        image_id = hashlib.sha256(raw_bytes).hexdigest()[:12]
        file_path = self.output_dir / f"{image_id}.{ext}"
        if not file_path.exists():
            with open(file_path, "wb") as f:
                f.write(raw_bytes)
        return {
            "image_id":   image_id,
            "file_path":  str(file_path),
            "source_doc": source_doc,
            "page_number": page_number,
            "ext":        ext,
            "size":       file_size,
        }

    def _zip_media_sweep(self, file_path: str) -> List[Dict]:
        """Pull EVERY embedded image straight from the OOXML (docx/pptx/xlsx) zip
        package — word/media, ppt/media, xl/media, plus images in headers,
        footers, shape fills and placeholders that the python-docx/pptx object
        models miss. Content-hash IDs dedupe automatically against the
        object-model pass."""
        import zipfile
        out: List[Dict] = []
        src = Path(file_path).name
        try:
            with zipfile.ZipFile(file_path) as z:
                for name in z.namelist():
                    low = name.lower()
                    if "/media/" not in low and not low.startswith("media/"):
                        continue
                    ext = low.rsplit(".", 1)[-1] if "." in low else ""
                    if ext == "jpeg":
                        ext = "jpg"
                    # Metafiles live in ppt/media too and are not a supported
                    # storage format, but they are renderable — rasterise them
                    # here rather than dropping the slide art on the floor.
                    if ext not in self.SUPPORTED_FORMATS and ext not in ("emf", "wmf"):
                        continue
                    try:
                        raw = z.read(name)
                    except Exception:
                        continue
                    if ext in ("emf", "wmf"):
                        from app.metafile_render import metafile_to_png
                        raw = metafile_to_png(raw)
                        if not raw:
                            continue
                        ext = "png"
                    rec = self._save_image_record(raw, ext, src)
                    if rec:
                        out.append(rec)
        except Exception as e:
            logger.warning("zip media sweep failed for %s: %s", file_path, e)
        return out

    def _merge_sweep(self, extracted: List[Dict], file_path: str) -> List[Dict]:
        """Append any zip-media images not already found by the object model."""
        seen = {r.get("image_id") for r in extracted}
        for rec in self._zip_media_sweep(file_path):
            if rec.get("image_id") not in seen:
                extracted.append(rec)
                seen.add(rec.get("image_id"))
        return extracted

    def extract_images_from_pptx(self, pptx_path: str) -> List[Dict]:
        """Extract embedded images from a .pptx, including images inside group shapes."""
        try:
            from pptx import Presentation
            from pptx.enum.shapes import MSO_SHAPE_TYPE
        except ImportError:
            logger.error("python-pptx not installed — pip install python-pptx")
            return []

        extracted: List[Dict] = []
        try:
            prs = Presentation(pptx_path)
        except Exception as e:
            logger.error("Could not open pptx %s: %s", pptx_path, e)
            return []

        source = Path(pptx_path).name

        def _collect_shapes(shapes):
            """Recursively yield all shapes including those inside groups."""
            for shape in shapes:
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    yield from _collect_shapes(shape.shapes)
                else:
                    yield shape

        def _to_png_bytes(blob: bytes, ext: str):
            """Rasterise EMF/WMF vector metafiles to PNG.

            This used to go through Pillow, which *appears* to work — it opens
            the metafile and reports the right dimensions — but renders
            nothing, producing a 100% white bitmap for every metafile in a
            real deck. Those blank images were then saved and indexed as
            legitimate records. metafile_render drives GDI+ instead, which
            actually plays back the EMF records, and returns None when the
            result has nothing drawn on it.
            """
            from app.metafile_render import sniff_format, metafile_to_png
            # Trust the bytes, not the extension: python-pptx labels EMF parts
            # as 'wmf', and some decks mislabel raster parts too.
            if sniff_format(blob) in ("emf", "wmf"):
                png = metafile_to_png(blob)
                return (png, "png") if png else (None, ext)
            return blob, ext

        for slide_idx, slide in enumerate(prs.slides, start=1):
            for shape in _collect_shapes(slide.shapes):
                try:
                    if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                        blob = shape.image.blob
                        ext = (shape.image.ext or "png").lower()
                        # Skip tiny images (icons, bullets) smaller than 5 KB
                        if len(blob) < 5000:
                            continue
                        # Rasterise metafiles so they can be indexed. Called
                        # unconditionally — it sniffs the bytes and passes
                        # ordinary rasters straight through, which is safer
                        # than trusting shape.image.ext (python-pptx reports
                        # EMF parts as 'wmf').
                        blob, ext = _to_png_bytes(blob, ext)
                        if blob is None:
                            logger.debug("Skipping unrenderable metafile on slide %d", slide_idx)
                            continue
                        rec = self._save_image_record(blob, ext, source, page_number=slide_idx)
                        if rec:
                            extracted.append(rec)
                except Exception as e:
                    logger.warning("pptx image extract failed on slide %d shape %s: %s",
                                   slide_idx, getattr(shape, "name", "?"), e)

        extracted = self._merge_sweep(extracted, pptx_path)
        logger.info("Extracted %d images from pptx %s", len(extracted), source)
        return extracted

    def extract_images_from_xlsx(self, xlsx_path: str) -> List[Dict]:
        """Extract embedded images from a .xlsx/.xlsm workbook."""
        try:
            from openpyxl import load_workbook
        except ImportError:
            logger.error("openpyxl not installed — pip install openpyxl")
            return []

        extracted: List[Dict] = []
        try:
            wb = load_workbook(xlsx_path, data_only=True)
        except Exception as e:
            logger.error("Could not open xlsx %s: %s", xlsx_path, e)
            return []

        source = Path(xlsx_path).name
        for sheet_idx, ws in enumerate(wb.worksheets, start=1):
            for img in getattr(ws, "_images", []):
                try:
                    # openpyxl Image has a `_data` callable that returns raw bytes
                    raw = img._data() if callable(getattr(img, "_data", None)) else img._data
                    # Infer extension from PIL format if available
                    ext = "png"
                    try:
                        pil = getattr(img, "ref", None) or getattr(img, "image", None)
                        if pil is not None and hasattr(pil, "format") and pil.format:
                            ext = pil.format.lower()
                    except Exception:
                        pass
                    rec = self._save_image_record(raw, ext, source, page_number=sheet_idx)
                    if rec:
                        extracted.append(rec)
                except Exception as e:
                    logger.warning("xlsx image extract failed on sheet %d: %s", sheet_idx, e)
        extracted = self._merge_sweep(extracted, xlsx_path)
        logger.info("Extracted %d images from xlsx %s", len(extracted), source)
        return extracted

    def extract_images_from_image_file(self, image_path: str) -> List[Dict]:
        """Treat a directly-uploaded image as a one-record extraction."""
        try:
            raw = Path(image_path).read_bytes()
        except Exception as e:
            logger.error("Could not read image %s: %s", image_path, e)
            return []
        ext = Path(image_path).suffix.lstrip(".").lower() or "png"
        rec = self._save_image_record(raw, ext, Path(image_path).name, page_number=None)
        return [rec] if rec else []

    # ─── Phase 2: Vision Analysis ───────────────────────────────────────
    
    def _minimal_vision_record(self, image_path: str, source_context: str = "") -> Dict:
        # Vision is unavailable (no client, circuit breaker tripped, or the
        # call itself failed) — fall back to whatever locator text the caller
        # already has (doc name + page/slide) as the caption, instead of a
        # bare content-hash filename. It's not a real "what's in this image"
        # description, but it's a searchable string instead of nothing, so
        # the image isn't completely invisible to section-relevance search
        # while vision is down.
        return {
            "description": "",
            "caption": source_context.strip() or Path(image_path).stem,
            "keywords": [],
            "diagram_type": "other",
            "section_hint": "unknown",
            "image_path": image_path,
        }

    def describe_and_tag_image(self, image_path: str, source_context: str = "",
                               section_context: str = "") -> Dict:
        """Analyze image with GPT-4o vision and extract metadata"""

        if not self.client:
            logger.warning("OpenAI client not available. Returning minimal metadata.")
            return self._minimal_vision_record(image_path, source_context)

        # Circuit breaker — if too many consecutive failures, stop calling.
        if self._vision_disabled:
            return self._minimal_vision_record(image_path, source_context)
        
        try:
            # Read and encode image
            with open(image_path, "rb") as f:
                image_data = base64.b64encode(f.read()).decode("utf-8")
            
            # Detect MIME type
            ext = Path(image_path).suffix.lower()
            mime_type_map = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".webp": "image/webp",
                ".bmp": "image/bmp",
            }
            mime_type = mime_type_map.get(ext, "image/png")
            
            # Call vision model (override with VISION_MODEL env var)
            response = self.client.chat.completions.create(
                model=os.getenv("VISION_MODEL", "gpt-4.1"),
                max_tokens=1024,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": f"""You are a supply chain and ERP domain expert analyzing images for a Kinaxis RapidResponse Blueprint Document. Return ONLY valid JSON (no markdown, no comments).

Source: {source_context}{f'{chr(10)}Document section: {section_context}' if section_context else ''}

Respond with this exact JSON structure:
{{
  "description": "<2-4 sentence description of what this diagram/chart shows>",
  "caption": "<single short label, max 12 words, suitable as figure caption>",
  "keywords": ["<8-12 domain-specific terms: process names, entities, diagram type, subsection names>"],
  "diagram_type": "<one of: flowchart, table, architecture_diagram, timeline, screenshot, chart, photo, other>",
  "section_hint": "<BRD section — if Document section is provided above, use it verbatim; otherwise best-guess like '4 Demand Planning'>"
}}"""
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{mime_type};base64,{image_data}"
                                }
                            }
                        ]
                    }
                ]
            )

            # Parse response (OpenAI SDK: choices[0].message.content)
            response_text = response.choices[0].message.content.strip()
            
            # Remove markdown code blocks if present
            if response_text.startswith("```"):
                response_text = response_text.split("```")[1]
                if response_text.startswith("json"):
                    response_text = response_text[4:]
            response_text = response_text.strip()
            
            vision_data = json.loads(response_text)
            
            # Validate and merge
            result = {
                "description": vision_data.get("description", ""),
                "caption": vision_data.get("caption", Path(image_path).stem),
                "keywords": vision_data.get("keywords", [])[:12],
                "diagram_type": vision_data.get("diagram_type", "other"),
                "section_hint": vision_data.get("section_hint", "unknown"),
                "image_path": image_path
            }
            
            # Validate diagram_type
            valid_types = {"flowchart", "table", "architecture_diagram", "timeline", "screenshot", "chart", "photo", "other"}
            if result["diagram_type"] not in valid_types:
                result["diagram_type"] = "other"
            
            logger.info(f"Analyzed image {Path(image_path).name}: {result['caption']}")
            # Reset circuit-breaker on success.
            self._vision_strikes = 0
            return result

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse vision response for {image_path}: {e}")
            return self._minimal_vision_record(image_path, source_context)

        except Exception as e:
            logger.error(f"Vision analysis failed for {image_path}: {e}")
            self._vision_strikes += 1
            if self._vision_strikes >= self.VISION_STRIKE_LIMIT:
                self._vision_disabled = True
                logger.error(
                    "Vision circuit breaker tripped after %d consecutive failures — "
                    "skipping vision for remaining images in this run.",
                    self._vision_strikes,
                )
            return self._minimal_vision_record(image_path, source_context)
    
    # ─── Combined Processing ───────────────────────────────────────
    
    def process_documents(self, doc_dir: str) -> List[Dict]:
        """Process all documents in directory and return image records"""
        
        image_records = []
        doc_path = Path(doc_dir)
        
        if not doc_path.exists():
            logger.warning(f"Document directory not found: {doc_dir}")
            return image_records
        
        # Process PDFs
        for pdf_file in doc_path.glob("*.pdf"):
            try:
                raw_images = self.extract_images_from_pdf(str(pdf_file))
                
                for img_meta in raw_images:
                    vision_data = self.describe_and_tag_image(
                        img_meta["file_path"],
                        source_context=f"{img_meta['source_doc']}, page {img_meta['page_number']}"
                    )
                    
                    record = {**img_meta, **vision_data}
                    record.pop("image_path", None)  # Remove temp field
                    record["brd_section_id"] = _derive_brd_section_id(
                        record.get("section_hint", ""),
                        record.get("caption", ""),
                        record.get("keywords", []),
                        record.get("description", ""),
                    )
                    image_records.append(record)
            
            except Exception as e:
                logger.error(f"Error processing {pdf_file}: {e}")
        
        # Process DOCX files
        for docx_file in doc_path.glob("*.docx"):
            try:
                raw_images = self.extract_images_from_docx(str(docx_file))
                
                for img_meta in raw_images:
                    vision_data = self.describe_and_tag_image(
                        img_meta["file_path"],
                        source_context=f"{img_meta['source_doc']}"
                    )
                    
                    record = {**img_meta, **vision_data}
                    record.pop("image_path", None)  # Remove temp field
                    record["brd_section_id"] = _derive_brd_section_id(
                        record.get("section_hint", ""),
                        record.get("caption", ""),
                        record.get("keywords", []),
                        record.get("description", ""),
                    )
                    image_records.append(record)
            
            except Exception as e:
                logger.error(f"Error processing {docx_file}: {e}")
        
        logger.info(f"Extracted {len(image_records)} images from documents in {doc_dir}")
        return image_records
    
    def save_index(self, image_records: List[Dict], output_file: str = "image_chunks.json"):
        """Save image records to JSON index"""
        output_path = Path(output_file)

        with open(output_path, "w") as f:
            json.dump(image_records, f, indent=2)

        logger.info(f"Saved image index to {output_path}")
        return output_path

    def process_single_file(self, file_path: str,
                            page_sections: dict = None) -> List[Dict]:
        """Extract and vision-analyse images from one file (PDF or DOCX).

        page_sections: optional {page_number: {section_heading, brd_section_id}}
        built from the text chunks produced by the chunking pipeline for PDFs.
        """
        file_path = Path(file_path)
        ext = file_path.suffix.lower()
        image_records = []

        try:
            if ext == ".pdf":
                raw_images = self.extract_images_from_pdf(str(file_path),
                                                          page_sections=page_sections)
            elif ext in (".docx", ".doc"):
                raw_images = self.extract_images_from_docx(str(file_path))
            elif ext in (".pptx", ".ppt"):
                raw_images = self.extract_images_from_pptx(str(file_path))
            elif ext in (".xlsx", ".xlsm"):
                raw_images = self.extract_images_from_xlsx(str(file_path))
            elif ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
                raw_images = self.extract_images_from_image_file(str(file_path))
            else:
                logger.info("Skipping image extraction for unsupported type: %s", ext)
                return []

            # Dedupe raw_images by image_id BEFORE running vision so we don't
            # pay for the same hash twice (this single file can yield repeats
            # when the same asset is embedded on multiple pages).
            seen_ids = set()
            unique_raw = []
            for meta in raw_images:
                iid = meta.get("image_id")
                if iid and iid in seen_ids:
                    continue
                seen_ids.add(iid)
                unique_raw.append(meta)
            if len(unique_raw) < len(raw_images):
                logger.info(
                    "Deduped %d → %d images from %s before vision",
                    len(raw_images), len(unique_raw), file_path.name,
                )

            for img_meta in unique_raw:
                sec_heading = img_meta.get("section_heading", "")
                src_ctx = f"{img_meta['source_doc']}, page {img_meta.get('page_number', '?')}"
                if sec_heading:
                    src_ctx += f" — {sec_heading}"
                vision_data = self.describe_and_tag_image(
                    img_meta["file_path"],
                    source_context=src_ctx,
                    section_context=sec_heading,
                )
                record = {**img_meta, **vision_data}
                record.pop("image_path", None)
                record["brd_section_id"] = _derive_brd_section_id(
                    record.get("section_hint", ""),
                    record.get("caption", ""),
                    record.get("keywords", []),
                    record.get("description", ""),
                    section_heading=sec_heading,
                )
                image_records.append(record)

            logger.info("Extracted %d images from %s", len(image_records), file_path.name)
        except Exception as e:
            logger.error("Error extracting images from %s: %s", file_path.name, e)

        return image_records

    def append_to_index(self, new_records: List[Dict], index_file: str = "image_chunks.json"):
        """Append new image records, deduping by (image_id, source_doc).

        Previously dedupe was by `image_id` alone — but `image_id` is a hash
        of the raw bytes, so identical binaries embedded in DIFFERENT source
        documents (e.g. a shared logo or Kinaxis reference diagram) would
        collide and only the first document's record survived. The second
        doc's images then disappeared from Browse Images because the filter
        scopes by `source_doc`. Deduping by the (image_id, source_doc) pair
        keeps one record per binary per source doc, so every uploaded
        document gets surfaced.
        """
        index_path = Path(index_file)
        existing: List[Dict] = []

        if index_path.exists():
            try:
                existing = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                existing = []

        def _key(rec):
            return (rec.get("image_id") or "", (rec.get("source_doc") or "").lower())

        seen: set = set()
        cleaned_existing: List[Dict] = []
        for rec in existing:
            k = _key(rec)
            if not k[0] or k in seen:
                continue
            seen.add(k)
            cleaned_existing.append(rec)
        if len(cleaned_existing) < len(existing):
            logger.info(
                "Self-healed image index: %d → %d records (dropped %d duplicates by image_id+source_doc)",
                len(existing), len(cleaned_existing), len(existing) - len(cleaned_existing),
            )
            existing = cleaned_existing

        added = [r for r in new_records if r.get("image_id") and _key(r) not in seen]
        merged = existing + added

        index_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        logger.info("Appended %d new images to %s (total: %d)", len(added), index_path.name, len(merged))
        return added


# ─── Convenience functions ───────────────────────────────────────

def extract_and_index_single_file(file_path: str,
                                   output_dir: str = "extracted_images",
                                   index_file: str = "image_chunks.json",
                                   openai_api_key: Optional[str] = None,
                                   page_sections: Optional[dict] = None) -> Dict:
    """
    Extract and vision-analyse images from one uploaded file, appending to the shared index.
    Call this immediately after a file finishes text chunking.

    page_sections: optional {page_number: {section_heading, brd_section_id}} from
    the text chunks — used to assign authoritative document-section metadata to PDF images.
    """
    extractor = ImageExtractor(output_dir=output_dir, openai_api_key=openai_api_key)
    new_records = extractor.process_single_file(file_path, page_sections=page_sections)
    added = extractor.append_to_index(new_records, index_file=index_file)
    return {
        "file": str(file_path),
        "images_found": len(new_records),
        "images_added": len(added),
        "index_file": index_file,
    }


def extract_and_index_images(doc_dir: str, output_dir: str = "extracted_images",
                            index_file: str = "image_chunks.json",
                            openai_api_key: Optional[str] = None) -> Dict:
    """One-shot function to extract and index all images"""
    
    extractor = ImageExtractor(output_dir=output_dir, openai_api_key=openai_api_key)
    image_records = extractor.process_documents(doc_dir)
    extractor.save_index(image_records, output_file=index_file)
    
    return {
        "total_images": len(image_records),
        "output_dir": output_dir,
        "index_file": index_file,
        "records": image_records
    }


def auto_extract_for_project(project_id: str, base_upload_path: str = "./uploads") -> dict:
    """
    Automatically extract images for a specific project.
    
    Args:
        project_id: Project ID (e.g., "6c951d0e")
        base_upload_path: Base path to uploads folder
    
    Returns:
        Dictionary with extraction results
    """
    # Construct project-specific path
    project_source_path = os.path.join(base_upload_path, project_id, "source")
    
    # Check if path exists
    if not os.path.exists(project_source_path):
        logger.warning(f"Project source path not found: {project_source_path}")
        return {
            "total_images": 0,
            "output_dir": None,
            "index_file": None,
            "project_id": project_id,
            "error": f"Source path not found: {project_source_path}"
        }
    
    logger.info(f"Auto-extracting images for project {project_id} from {project_source_path}")
    
    # Extract images
    result = extract_and_index_images(
        doc_dir=project_source_path,
        output_dir="extracted_images",
        index_file="image_chunks.json"
    )
    
    # Add project ID to result
    result["project_id"] = project_id
    result["source_path"] = project_source_path
    
    logger.info(f"✅ Auto-extraction complete for project {project_id}: {result['total_images']} images")
    
    return result


if __name__ == "__main__":
    # Example usage
    import sys
    
    doc_dir = sys.argv[1] if len(sys.argv) > 1 else "reference_docs"
    result = extract_and_index_images(doc_dir)
    
    print(f"\n✅ Extracted {result['total_images']} images")
    print(f"   Output directory: {result['output_dir']}")
    print(f"   Index file: {result['index_file']}")