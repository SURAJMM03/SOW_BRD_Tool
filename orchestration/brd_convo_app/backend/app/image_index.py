"""
Phase 3: Image Index
BM25-based search over image metadata (parallel to doc_retrieval_index.py)
"""

import json
import os
import re
import logging
from pathlib import Path
from typing import List, Dict, Optional

logger = logging.getLogger(__name__)


class ImageIndex:
    """BM25-based search index for images"""
    
    def __init__(self, json_path: str):
        """Load image_chunks.json and build BM25 index"""
        self.json_path = json_path
        self.records = []
        self.corpus = []  # Tokenized documents
        self.token_sets = []          # per-record token set (all fields)
        self.content_token_sets = []  # per-record tokens describing the picture only
        self.bm25_index = None
        self.raw_records = []
        
        if os.path.exists(json_path):
            try:
                with open(json_path, "r") as f:
                    self.raw_records = json.load(f)
                self._build_index()
                logger.info(f"Loaded {len(self.raw_records)} images from {json_path}")
            except Exception as e:
                logger.error(f"Failed to load image index: {e}")
    
    def _tokenize(self, text: str) -> List[str]:
        """Tokenize text like doc_retrieval_index.py"""
        if not text:
            return []
        
        # Convert to lowercase
        text = text.lower()
        
        # Split on non-alphanumeric characters
        tokens = re.findall(r'[a-z0-9_]+', text)
        
        # Remove very short tokens
        tokens = [t for t in tokens if len(t) > 2]
        
        return tokens
    
    def _build_index(self):
        """Build BM25 index from records"""
        
        if not self.raw_records:
            return
        
        # Try to import rank_bm25
        try:
            from rank_bm25 import BM25Okapi
            self.has_bm25 = True
        except ImportError:
            logger.warning("rank_bm25 not available, using simple TF search")
            self.has_bm25 = False
        
        # Build corpus: caption + keywords + description + section_heading for each record
        for record in self.raw_records:
            caption          = record.get("caption", "")
            keywords         = " ".join(record.get("keywords", []))
            description      = record.get("description", "")
            section_heading  = record.get("section_heading", "")
            source_doc       = record.get("source_doc", "")
            image_id         = record.get("image_id", "")

            # section_heading repeated twice to give it more weight in BM25
            combined_text = f"{caption} {keywords} {section_heading} {section_heading} {description} {source_doc} {image_id}"
            
            # Tokenize
            tokens = self._tokenize(combined_text)
            self.corpus.append(tokens)
            # A set per record for O(1) term-coverage checks at query time.
            # BM25 alone cannot answer "how much of the query does this record
            # actually contain" — it happily returns a strong score for one
            # rare term matching once, which is how logos kept winning.
            self.token_sets.append(set(tokens))
            # Vocabulary that describes the *picture*, excluding the source
            # filename and image id. Those two are identical for every image
            # out of the same deck, so letting them satisfy coverage means a
            # query mentioning the client name matches all 118 of that deck's
            # images equally.
            self.content_token_sets.append(
                set(self._tokenize(f"{caption} {keywords} {section_heading} {description}"))
            )
        
        # Build BM25 if available
        if self.has_bm25:
            try:
                from rank_bm25 import BM25Okapi
                self.bm25_index = BM25Okapi(self.corpus)
            except Exception as e:
                logger.warning(f"Failed to build BM25 index: {e}")
                self.has_bm25 = False
    
    def search(self, query: str, top_k: int = 5,
               diagram_type: Optional[str] = None,
               section_hint: Optional[str] = None,
               section_id: Optional[str] = None,
               source_docs: Optional[set] = None,
               prefer_diagrams: bool = False,
               min_term_coverage: float = 0.0,
               min_score_ratio: float = 0.0) -> List[Dict]:
        """Search images by query with optional filters.

        section_id: hard filter on brd_section_id ("1"–"6"). Records tagged to
        a different section are excluded. Untagged records always pass through.
        section_hint: legacy soft boost (kept for backward compatibility).
        source_docs: hard filter on source_doc — only images from these
            filenames are considered. Applied BEFORE the top_k cut, which is
            the whole point: this index is global across every project, so a
            caller that cuts to top_k first and filters afterwards gets
            starved. Measured on a real project whose deck contributed 118 of
            the index's 3490 images, the global top-20 for a section query
            contained ZERO of that project's images — so the section was
            offered nothing and the document came out with no images at all.
        prefer_diagrams: down-weight logos/icons/screenshots so real diagrams
            (architecture, flowchart, process, timeline) win the window. A deck
            is full of vendor logos that match generic query vocabulary; left
            unweighted they crowd out the one flowchart that was wanted. Also
            drops brand marks outright — see _is_brand_mark.
        min_term_coverage: reject records matching less than this fraction of
            the distinct query terms. Previously any record with score > 0
            survived, and BM25 gives a positive score for a *single* matched
            token — so a section query of a dozen words routinely returned
            images that shared one generic word ("platform", "process") with
            it, which the prompt then offered up for embedding.
        min_score_ratio: reject records scoring below this fraction of the
            best match for the same query, so a weak tail is not padded in
            just to fill top_k.
        """
        if not self.raw_records or not self.corpus:
            return []

        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        if self.has_bm25 and self.bm25_index:
            try:
                scores = list(self.bm25_index.get_scores(query_tokens))
            except Exception as e:
                logger.warning(f"BM25 scoring failed: {e}, using simple TF")
                scores = self._simple_tf_score(query_tokens)
        else:
            scores = self._simple_tf_score(query_tokens)

        scored_records = []
        for i, record in enumerate(self.raw_records):
            score = scores[i] if i < len(scores) else 0

            if diagram_type and record.get("diagram_type") != diagram_type:
                score = 0

            if source_docs is not None and record.get("source_doc") not in source_docs:
                score = 0

            # Hard section filter — skip only if positively tagged to a different section
            rec_sid = record.get("brd_section_id", "")
            if section_id and rec_sid and rec_sid != section_id:
                score = 0

            # Legacy soft boost by section_hint substring
            if section_hint and section_hint in record.get("section_hint", ""):
                score *= 1.2

            if prefer_diagrams and score > 0:
                # Brand marks are excluded outright here rather than merely
                # down-weighted. A 0.15 multiplier still lets a logo win when
                # it is the only thing that matched, which is precisely the
                # case where it should not be offered at all.
                if self._is_brand_mark(record):
                    score = 0
                else:
                    score *= self._diagram_weight(record)

            if score > 0:
                coverage = self._term_coverage(i, query_tokens)
                if coverage < min_term_coverage:
                    continue
                scored_records.append({**record, "score": score,
                                       "term_coverage": coverage})

        scored_records.sort(key=lambda x: x["score"], reverse=True)

        # Relative floor. BM25 scores are unnormalised, so there is no
        # meaningful absolute threshold — but "less than half as good as the
        # best match for this query" is a reliable signal that the tail is
        # padding rather than genuine matches. Without this the caller always
        # received exactly top_k rows no matter how weak, and the prompt then
        # invited the model to embed them.
        if scored_records and min_score_ratio > 0:
            cutoff = scored_records[0]["score"] * min_score_ratio
            scored_records = [r for r in scored_records if r["score"] >= cutoff]

        return scored_records[:top_k]

    def _term_coverage(self, idx: int, query_tokens: List[str]) -> float:
        """Fraction of distinct query terms this record actually contains.

        Measured against the picture's own vocabulary (caption/keywords/
        description/heading) — never the filename or image id, which are
        constant across a whole deck and would let every image from a source
        claim credit for matching that source's name.
        """
        if not query_tokens:
            return 0.0
        distinct = set(query_tokens)
        if idx >= len(self.content_token_sets):
            return 0.0
        hits = len(distinct & self.content_token_sets[idx])
        return hits / len(distinct)

    def _is_brand_mark(self, record: Dict) -> bool:
        """True for logos/icons/UI chrome — never what a section wants embedded."""
        caption = record.get("caption", "") or ""
        if self._LOGO_WORDS.search(caption):
            return True
        return (record.get("diagram_type") or "").lower() in ("logo", "icon")

    # Captions the vision tagger gives to brand marks and UI chrome. These are
    # never what a SOW section wants embedded, but they match generic query
    # vocabulary ("platform", "integration", "process") very readily.
    _LOGO_WORDS = re.compile(
        r"\b(logo|icon|branding|brand mark|wordmark|company logo|"
        r"corporate logo|product logo|badge|avatar|bullet)\b", re.I)

    _DIAGRAM_TYPES = {
        "architecture_diagram": 1.6,
        "flowchart": 1.6,
        "process_flow": 1.6,
        "process_diagram": 1.6,
        "timeline": 1.4,
        "gantt": 1.4,
        "chart": 1.2,
        "table": 1.2,
        "screenshot": 0.8,
        "photo": 0.5,
        "other": 0.6,
    }

    def _diagram_weight(self, record: Dict) -> float:
        caption = record.get("caption", "") or ""
        if self._LOGO_WORDS.search(caption):
            return 0.15
        w = self._DIAGRAM_TYPES.get((record.get("diagram_type") or "").lower(), 1.0)
        # A whole rendered slide (slide_renderer.py) is the complete diagram as
        # the author drew it, whereas an embedded picture from that same slide
        # is usually just an icon cropped out of the middle of it. When both
        # match a query, the assembled diagram is virtually always the one the
        # reviewer meant.
        if record.get("render_kind") == "slide":
            w *= 2.0
        return w
    
    def _simple_tf_score(self, query_tokens: List[str]) -> List[float]:
        """Simple TF scoring if BM25 unavailable"""
        scores = []
        
        for corpus_tokens in self.corpus:
            # Count matching tokens
            matches = sum(1 for token in query_tokens if token in corpus_tokens)
            score = matches / len(query_tokens) if query_tokens else 0
            scores.append(score)
        
        return scores


# ─── Module-level singleton ───────────────────────────────────────

_IMAGE_INDEX_PATH = os.path.join(
    os.path.dirname(__file__), "image_chunks.json"
)

# Try to load image index
image_index = None
try:
    if os.path.exists(_IMAGE_INDEX_PATH):
        image_index = ImageIndex(_IMAGE_INDEX_PATH)
        logger.info(f"Image index loaded: {_IMAGE_INDEX_PATH}")
except Exception as e:
    logger.warning(f"Failed to load image index: {e}")


def reload_image_index() -> int:
    """Re-read image_chunks.json from disk and rebuild the in-memory BM25 index.

    Call after a file upload (which appends new records) so they become
    searchable without a server restart. Returns the new record count.
    """
    global image_index
    try:
        if os.path.exists(_IMAGE_INDEX_PATH):
            image_index = ImageIndex(_IMAGE_INDEX_PATH)
            logger.info("Image index reloaded: %d records", len(image_index.raw_records))
            return len(image_index.raw_records)
        image_index = None
        return 0
    except Exception as e:
        logger.error("reload_image_index failed: %s", e)
        return 0


def search_images(query: str, top_k: int = 5,
                 diagram_type: Optional[str] = None,
                 section_hint: Optional[str] = None,
                 section_id: Optional[str] = None,
                 source_docs: Optional[set] = None,
                 prefer_diagrams: bool = False,
                 min_term_coverage: float = 0.0,
                 min_score_ratio: float = 0.0) -> List[Dict]:
    """Module-level search function"""

    if image_index is None:
        return []

    return image_index.search(query, top_k, diagram_type, section_hint,
                              section_id, source_docs=source_docs,
                              prefer_diagrams=prefer_diagrams,
                              min_term_coverage=min_term_coverage,
                              min_score_ratio=min_score_ratio)


def get_image_by_id(image_id: str) -> Optional[Dict]:
    """Get image metadata by ID"""
    
    if image_index is None:
        return None
    
    for record in image_index.raw_records:
        if record.get("image_id") == image_id:
            return record
    
    return None


def get_images_for_section(section_id: str, top_k: int = 3) -> list:
    """
    Get images most relevant to a specific section.
    Intelligently maps sections to image types.

    UNUSED, and not safe to adopt as-is: the SECTION_KEYWORDS map below is
    keyed to one specific BRD template's numbering, so on any other template
    (or any SOW) "3" or "6" means a different section and this returns images
    for the wrong topic. Callers should search on the section's own *title*
    instead, which is the only description that means the same thing in every
    template — see _relevant_images_for_section in sow_section_routes.py.

    Args:
        section_id: Section identifier (e.g., "3.1", "4", "5")
        top_k: Maximum number of images to return

    Returns:
        List of relevant image records for the section
    """
    
    # Section-to-keyword mapping
    # Maps each section to keywords that identify relevant images
    SECTION_KEYWORDS = {
        "3": "supply chain network scope capabilities overview process",
        "3.1": "map location plant production facility geography site distribution network",
        "3.2": "sites table data warehouse facility DC distribution center address location",
        "3.3": "demand planning forecast consumption variability scenario forecast model",
        "3.4": "supply planning lead time safety stock sourcing supplier procurement",
        "3.5": "inventory min max ABC analysis obsolete stock level inventory policy",
        "3.6": "transportation logistics network distribution route shipment",
        "3.7": "scenario simulation plan outcome forecast what-if",
        "4": "demand planning demand forecast process methodology planning approach",
        "5": "supply inventory constraints capacity planning parameters buffer stock",
        "6": "data integration architecture system ETL flow database",
        "7": "implementation governance change management rollout timeline",
    }
    
    # Get keywords for this section
    query = SECTION_KEYWORDS.get(section_id, section_id)
    
    logger.info(f"Searching for images for section {section_id} with keywords: {query}")
    
    # Search with section-specific keywords + hard section filter
    images = search_images(
        query=query,
        top_k=top_k,
        section_hint=section_id,
        section_id=section_id,
    )
    
    logger.info(f"Found {len(images)} images for section {section_id}")
    
    return images


if __name__ == "__main__":
    # Test search
    if image_index:
        results = search_images("distribution network", top_k=3)
        
        print(f"\nSearch results for 'distribution network':")
        for result in results:
            print(f"  - {result['caption']} (score: {result['score']:.2f})")
    else:
        print("No image index available")