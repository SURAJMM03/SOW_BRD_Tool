# -*- coding: utf-8 -*-
"""
doc_retrieval_index.py

Deterministic, section-aware document retrieval for BRD enrichment.

Supported formats (Phase-1+):
- DOCX  : chunk by Word heading structure (Heading 1/2/3/4…)
- PDF   : chunk per page (with cleanup)
- CSV   : chunk per row (rendered as key:value lines)

Top-5 relevance improvements included:
1) Index headings (heading_path is embedded into indexed heading_text)
2) PDF cleanup: de-hyphenation + whitespace normalization
3) Deterministic query expansion using domain synonyms
4) Field boosting: heading score > body score
5) Document-level weighting (templates / blueprints > others)

No embeddings, no vector DB. Everything is deterministic and auditable.
"""

from __future__ import annotations

import csv
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

# Dependencies:
#   pip install python-docx pypdf
from docx import Document
from pypdf import PdfReader


# -----------------------------------------------------------------------------
# Config (tunable, deterministic)
# -----------------------------------------------------------------------------

# Heading vs body weighting in final BM25 score
HEADING_WEIGHT: float = float(os.getenv("DOCRET_HEADING_WEIGHT", "0.45"))
BODY_WEIGHT: float = float(os.getenv("DOCRET_BODY_WEIGHT", "0.55"))

# Max chars used for snippet creation
DEFAULT_SNIPPET_CHARS: int = int(os.getenv("DOCRET_SNIPPET_CHARS", "450"))

# Chunk splitting controls (DOCX): reduce mega-chunks
MAX_PARAS_PER_CHUNK: int = int(os.getenv("DOCRET_MAX_PARAS_PER_CHUNK", "10"))

# Domain synonym expansion (Phase-1 deterministic)
# Keep this small + curated for best quality.
DOMAIN_SYNONYMS: Dict[str, List[str]] = {
    "forecast": ["demand", "demand plan", "unconstrained forecast"],
    "consumption": ["net off", "net-off", "forecast consumption"],
    "interface": ["integration", "inbound", "outbound"],
    "integration": ["interface", "middleware", "etl"],
    "assumption": ["constraint", "dependency"],
    "constraints": ["assumptions", "limitations", "gating factors"],
    "sop": ["s&op", "sales operations planning", "sales and operations planning"],
    "brd": ["business requirements", "business requirement document"],
}

# Document-level priors / weights
# Matches by substring in filename (lowercased).
DOC_WEIGHT_RULES: List[Tuple[str, float]] = [
    ("template", 1.25),
    ("blueprint", 1.15),
    ("solution", 1.10),
    ("best practice", 1.10),
    ("example", 1.00),
    ("draft", 0.95),
    ("wip", 0.90),
]

# PDF cleanup knobs
PDF_DEHYPHENATE: bool = os.getenv("DOCRET_PDF_DEHYPHENATE", "true").lower() == "true"

# PDF vision analysis via OpenAI (GPT-4.1 or any vision-capable model)
# Requires: OPENAI_API_KEY and pymupdf installed
PDF_VISION_ENABLED: bool = os.getenv("PDF_VISION_ENABLED", "false").lower() == "true"
PDF_VISION_MODEL: str = os.getenv("PDF_VISION_MODEL", "gpt-4.1")
PDF_VISION_DPI: int = int(os.getenv("PDF_VISION_DPI", "150"))


# -----------------------------------------------------------------------------
# Tokenization utilities
# -----------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")


def tokenize(text: str) -> List[str]:
    """Lowercase alphanumeric tokens; deterministic."""
    return [m.group(0).lower() for m in _WORD_RE.finditer(text or "")]


def expand_query(query: str) -> str:
    """
    Deterministic query expansion using DOMAIN_SYNONYMS.
    Keeps BM25 but increases recall for known vocabulary.
    """
    base_terms = tokenize(query)
    expanded_terms = set(base_terms)

    for t in base_terms:
        for syn in DOMAIN_SYNONYMS.get(t, []):
            expanded_terms.update(tokenize(syn))

    # Return as a space-joined bag-of-words string
    return " ".join(sorted(expanded_terms))


def calc_doc_weight(doc_name: str) -> float:
    """
    Deterministic document-level weighting based on filename cues.
    """
    n = (doc_name or "").lower()
    for key, w in DOC_WEIGHT_RULES:
        if key in n:
            return float(w)
    return 1.0


# -----------------------------------------------------------------------------
# Chunk model
# -----------------------------------------------------------------------------

@dataclass
class Chunk:
    doc_name: str
    heading_path: str          # e.g., "5 Demand Planning > 5.2 Forecast Consumption"
    heading_text: str          # indexed field (includes heading_path)
    body_text: str             # indexed field (section text, cleaned)
    chunk_id: str              # stable identifier
    brd_section_id: str = ""   # BRD top-level section ("1"–"6"); empty = unclassified


# -----------------------------------------------------------------------------
# BM25 with field boosting (heading vs body) + doc weighting
# -----------------------------------------------------------------------------

class BM25Index:
    """
    Deterministic BM25 ranking over chunks, with:
    - separate TF maps for heading and body
    - field-weighted scoring
    - document-level weighting
    """
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = float(k1)
        self.b = float(b)

        self.chunks: List[Chunk] = []
        self.tf_heading: List[Dict[str, int]] = []
        self.tf_body: List[Dict[str, int]] = []
        self.df: Dict[str, int] = {}
        self.doc_len: List[int] = []
        self.avgdl: float = 0.0
        self.N: int = 0

    def build(self, chunks: Iterable[Chunk]) -> None:
        self.chunks = list(chunks)
        self.N = len(self.chunks)

        self.tf_heading = []
        self.tf_body = []
        self.df = {}
        self.doc_len = []

        for ch in self.chunks:
            htoks = tokenize(ch.heading_text)
            btoks = tokenize(ch.body_text)

            tf_h: Dict[str, int] = {}
            tf_b: Dict[str, int] = {}

            for t in htoks:
                tf_h[t] = tf_h.get(t, 0) + 1
            for t in btoks:
                tf_b[t] = tf_b.get(t, 0) + 1

            # document frequency across union of both fields
            for t in set(tf_h.keys()).union(tf_b.keys()):
                self.df[t] = self.df.get(t, 0) + 1

            self.tf_heading.append(tf_h)
            self.tf_body.append(tf_b)
            self.doc_len.append(len(htoks) + len(btoks))

        self.avgdl = (sum(self.doc_len) / self.N) if self.N else 0.0

    def idf(self, term: str) -> float:
        # smoothed BM25 idf
        n_q = self.df.get(term, 0)
        if self.N == 0:
            return 0.0
        return math.log(1 + (self.N - n_q + 0.5) / (n_q + 0.5))

    def _bm25_field(self, terms: List[str], tf_map: Dict[str, int], dl: int) -> float:
        if not terms:
            return 0.0
        denom = self.k1 * (1 - self.b + self.b * (dl / self.avgdl)) if self.avgdl else 1.0
        score = 0.0
        for t in terms:
            tf = tf_map.get(t, 0)
            if tf == 0:
                continue
            score += self.idf(t) * (tf * (self.k1 + 1)) / (tf + denom)
        return float(score)

    def score(self, expanded_query: str, idx: int) -> float:
        terms = tokenize(expanded_query)
        if not terms:
            return 0.0

        dl = self.doc_len[idx]
        ch = self.chunks[idx]

        s_h = self._bm25_field(terms, self.tf_heading[idx], dl)
        s_b = self._bm25_field(terms, self.tf_body[idx], dl)

        combined = (HEADING_WEIGHT * s_h) + (BODY_WEIGHT * s_b)

        # document-level weighting
        combined *= calc_doc_weight(ch.doc_name)
        return float(combined)

    def search(
        self,
        query: str,
        top_k: int = 5,
        doc_filter: Optional[List[str]] = None,
        section_filter: Optional[str] = None,
    ) -> List[Tuple[Chunk, float]]:
        if self.N == 0:
            return []

        q = expand_query(query)

        allowed = None
        if doc_filter:
            allowed = set([d.lower() for d in doc_filter])

        scored: List[Tuple[int, float]] = []
        for i, ch in enumerate(self.chunks):
            if allowed and ch.doc_name.lower() not in allowed:
                continue
            # Skip only chunks that are positively tagged to a DIFFERENT section.
            # Untagged chunks (empty brd_section_id) are always included so that
            # legacy data and reference-docs-folder files are never excluded.
            if section_filter and ch.brd_section_id and ch.brd_section_id != section_filter:
                continue
            s = self.score(q, i)
            if s > 0:
                scored.append((i, s))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [(self.chunks[i], s) for i, s in scored[: max(1, top_k)]]


# -----------------------------------------------------------------------------
# Extractors
# -----------------------------------------------------------------------------

def _clean_text_basic(text: str) -> str:
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_docx_chunks(docx_path: Path) -> List[Chunk]:
    """
    Chunk DOCX by heading structure.

    Improvements:
    - Heading path included in heading_text for indexing.
    - Split large sections into sub-chunks if paragraph count exceeds MAX_PARAS_PER_CHUNK.
    """
    doc = Document(str(docx_path))

    heading_stack: List[str] = []
    paras_buffer: List[str] = []
    chunks: List[Chunk] = []
    chunk_seq = 0

    def heading_level(style_name: str) -> Optional[int]:
        if not style_name:
            return None
        m = re.match(r"Heading\s+(\d+)", style_name.strip(), re.IGNORECASE)
        if not m:
            return None
        try:
            return int(m.group(1))
        except Exception:
            return None

    def flush_buffer():
        nonlocal chunk_seq, paras_buffer
        if not paras_buffer:
            return
        # split into smaller chunks if too long
        for start in range(0, len(paras_buffer), MAX_PARAS_PER_CHUNK):
            sub = paras_buffer[start:start + MAX_PARAS_PER_CHUNK]
            body = _clean_text_basic("\n".join(sub))
            if not body:
                continue
            heading_path = " > ".join(heading_stack) if heading_stack else "(No Heading)"
            heading_text = f"{heading_path}"
            chunks.append(
                Chunk(
                    doc_name=docx_path.name,
                    heading_path=heading_path,
                    heading_text=heading_text,
                    body_text=body,
                    chunk_id=f"{docx_path.stem}:docx:{chunk_seq}",
                )
            )
            chunk_seq += 1
        paras_buffer = []

    for p in doc.paragraphs:
        txt = (p.text or "").strip()
        if not txt:
            continue
        style = getattr(p.style, "name", "") if getattr(p, "style", None) else ""
        lvl = heading_level(style)

        if lvl is not None:
            flush_buffer()
            while len(heading_stack) >= lvl:
                heading_stack.pop()
            heading_stack.append(txt)
        else:
            paras_buffer.append(txt)

    flush_buffer()
    return chunks


def _vision_describe_page(image_bytes: bytes, page_label: str) -> str:
    """
    Send a rendered PDF page image to OpenAI vision and return a text description.
    Returns empty string on any failure so callers can degrade gracefully.
    """
    import base64
    try:
        import openai
        import httpx
    except ImportError:
        return ""

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return ""

    ssl_verify = os.getenv("OPENAI_SSL_VERIFY", "true").lower() not in ("false", "0", "no")
    client = openai.OpenAI(api_key=api_key, http_client=httpx.Client(verify=ssl_verify))

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    prompt = (
        f"This is {page_label} from a business / supply chain document. "
        "Describe ALL visual content in detail: supply chain maps, process flows, "
        "org charts, diagrams, tables, and charts. "
        "Extract every label, node, connection, arrow, and relationship shown. "
        "Also transcribe any text visible inside images or diagrams. "
        "Be thorough and structured."
    )

    try:
        response = client.chat.completions.create(
            model=PDF_VISION_MODEL,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                ],
            }],
            max_tokens=1500,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception:
        return ""


def extract_pdf_chunks(pdf_path: Path) -> List[Chunk]:
    """
    PDF extraction: page-level chunks + optional vision analysis.

    Text extraction: pypdf (fast, always runs)
    Vision analysis: PyMuPDF renders each page → OpenAI GPT-4.1 vision describes it.
      Enable with PDF_VISION_ENABLED=true + OPENAI_API_KEY set.
    """
    reader = PdfReader(str(pdf_path))
    chunks: List[Chunk] = []
    seq = 0

    # Open with PyMuPDF for page rendering when vision is enabled
    _fitz_doc = None
    if PDF_VISION_ENABLED:
        try:
            import fitz  # PyMuPDF
            _fitz_doc = fitz.open(str(pdf_path))
        except ImportError:
            pass  # vision silently skipped if pymupdf not installed

    total_pages = len(reader.pages)

    for i, page in enumerate(reader.pages):
        # --- Text extraction ---
        raw = page.extract_text() or ""
        if PDF_DEHYPHENATE:
            raw = re.sub(r"-\n", "", raw)
        raw = raw.replace("\r", "\n")
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n{3,}", "\n\n", raw)
        text = raw.strip()

        # --- Vision analysis ---
        vision_text = ""
        if _fitz_doc is not None and i < len(_fitz_doc):
            try:
                import fitz
                mat = fitz.Matrix(PDF_VISION_DPI / 72, PDF_VISION_DPI / 72)
                pix = _fitz_doc[i].get_pixmap(matrix=mat)
                image_bytes = pix.tobytes("png")
                label = f"page {i + 1} of {total_pages} in '{pdf_path.name}'"
                vision_text = _vision_describe_page(image_bytes, label)
            except Exception:
                pass

        # Combine: text first, then vision description clearly labelled
        body_parts = []
        if text:
            body_parts.append(text)
        if vision_text:
            body_parts.append(f"[Visual content — vision analysis]\n{vision_text}")
        combined = "\n\n".join(body_parts).strip()

        if not combined:
            continue

        heading_path = f"Page {i + 1}"
        chunks.append(
            Chunk(
                doc_name=pdf_path.name,
                heading_path=heading_path,
                heading_text=heading_path,
                body_text=combined,
                chunk_id=f"{pdf_path.stem}:pdf:{seq}",
            )
        )
        seq += 1

    if _fitz_doc is not None:
        _fitz_doc.close()

    return chunks


def extract_csv_chunks(csv_path: Path) -> List[Chunk]:
    """
    Phase-1 CSV extraction: row-level chunks.

    Each row becomes:
      col1: value
      col2: value
      ...
    """
    chunks: List[Chunk] = []
    seq = 0

    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []

        for row in reader:
            lines = []
            for h in headers:
                v = (row.get(h) or "").strip()
                if v:
                    lines.append(f"{h}: {v}")
            body = _clean_text_basic("\n".join(lines))
            if not body:
                continue

            heading_path = "CSV Row"
            chunks.append(
                Chunk(
                    doc_name=csv_path.name,
                    heading_path=heading_path,
                    heading_text=heading_path,
                    body_text=body,
                    chunk_id=f"{csv_path.stem}:csv:{seq}",
                )
            )
            seq += 1

    return chunks


# -----------------------------------------------------------------------------
# Index builder + snippet helper
# -----------------------------------------------------------------------------

def build_index_from_folder(folder: Path, glob_pattern: str = "*") -> Tuple[BM25Index, List[Chunk]]:
    """
    Load DOCX/PDF/CSV from folder, chunk them, and build BM25 index.
    """
    all_chunks: List[Chunk] = []

    for p in sorted(folder.glob(glob_pattern)):
        if p.name.startswith("~$"):
            continue  # Word temp file
        ext = p.suffix.lower()

        try:
            if ext == ".docx":
                all_chunks.extend(extract_docx_chunks(p))
            elif ext == ".pdf":
                all_chunks.extend(extract_pdf_chunks(p))
            elif ext == ".csv":
                all_chunks.extend(extract_csv_chunks(p))
        except Exception:
            # Keep index build resilient; skip problematic files
            continue

    idx = BM25Index()
    idx.build(all_chunks)
    return idx, all_chunks

def make_snippet(text: str, query: str, max_chars: int = DEFAULT_SNIPPET_CHARS) -> str:
    """
    Create a deterministic snippet around the first matching token.
    """
    if not text:
        return ""
    q_terms = tokenize(expand_query(query))
    lower = text.lower()

    hit_pos = None
    for t in q_terms:
        pos = lower.find(t)
        if pos != -1:
            hit_pos = pos
            break

    if hit_pos is None:
        snippet = text[:max_chars]
        return snippet + ("…" if len(text) > max_chars else "")

    start = max(0, hit_pos - int(max_chars * 0.35))
    end = min(len(text), start + max_chars)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet
