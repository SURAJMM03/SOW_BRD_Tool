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
               section_id: Optional[str] = None) -> List[Dict]:
        """Search images by query with optional filters.

        section_id: hard filter on brd_section_id ("1"–"6"). Records tagged to
        a different section are excluded. Untagged records always pass through.
        section_hint: legacy soft boost (kept for backward compatibility).
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

            # Hard section filter — skip only if positively tagged to a different section
            rec_sid = record.get("brd_section_id", "")
            if section_id and rec_sid and rec_sid != section_id:
                score = 0

            # Legacy soft boost by section_hint substring
            if section_hint and section_hint in record.get("section_hint", ""):
                score *= 1.2

            if score > 0:
                scored_records.append({**record, "score": score})

        scored_records.sort(key=lambda x: x["score"], reverse=True)
        return scored_records[:top_k]
    
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
                 section_id: Optional[str] = None) -> List[Dict]:
    """Module-level search function"""

    if image_index is None:
        return []

    return image_index.search(query, top_k, diagram_type, section_hint, section_id)


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