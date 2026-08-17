"""
Phase 6: Embed Images in DOCX Output
 
Processes generated DOCX to replace <IMAGE id="xyz"/> placeholders
with actual embedded images and captions.
 
This should be integrated into brd_workflow.py after document assembly.
"""
 
import re
import io
import logging
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin
 
logger = logging.getLogger(__name__)
 
 
def resolve_image_placeholders(section_text: str, doc, mcp_base_url: str) -> str:
    """
    Replace <IMAGE id="xyz"/> placeholders with actual images in DOCX.
    
    Args:
        section_text: Text content that may contain <IMAGE id="xyz"/> tags
        doc: python-docx Document object (will be modified in-place)
        mcp_base_url: Base URL of MCP server (e.g., "http://localhost:8020")
    
    Returns:
        Cleaned section_text with IMAGE tags removed
    """
    
    image_pattern = r'<IMAGE id="([^"]+)"/>'
    image_tags = re.findall(image_pattern, section_text)
    
    if not image_tags:
        return section_text
    
    image_metadata = {}
    
    for image_id in image_tags:
        try:
            _embed_image_in_docx(
                image_id=image_id,
                doc=doc,
                mcp_base_url=mcp_base_url,
                image_metadata=image_metadata
            )
        except Exception as e:
            logger.warning(f"Failed to embed image {image_id}: {e}")
    
    cleaned_text = re.sub(image_pattern, "", section_text)
    cleaned_text = re.sub(r'\n\n+', '\n\n', cleaned_text).strip()
    
    return cleaned_text
 
 
def _embed_image_in_docx(image_id: str, doc, mcp_base_url: str, 
                         image_metadata: dict) -> None:
    """
    Download and embed a single image in the DOCX document.
    
    Args:
        image_id: Image ID to download
        doc: python-docx Document object
        mcp_base_url: Base URL of MCP server
        image_metadata: Cache for image metadata
    """
    
    try:
        import requests
    except ImportError:
        logger.warning("requests library not available, cannot download images")
        return
    
    try:
        if image_id in image_metadata:
            metadata = image_metadata[image_id]
        else:
            metadata = _get_image_metadata(image_id, mcp_base_url)
            if metadata:
                image_metadata[image_id] = metadata
        
        image_url = urljoin(mcp_base_url, f"/api/images/{image_id}")
        response = requests.get(image_url, timeout=30)
        response.raise_for_status()
        
        image_stream = io.BytesIO(response.content)
        
        try:
            from docx.shared import Inches
            doc.add_picture(image_stream, width=Inches(5.5))
        except ImportError:
            logger.warning("python-docx not available for adding pictures")
            return
        
        if metadata:
            caption_text = f"Figure: {metadata.get('caption', image_id)}"
            caption_para = doc.add_paragraph(caption_text)
            
            try:
                caption_para.style = "Caption"
            except:
                for run in caption_para.runs:
                    run.italic = True
        
        logger.info(f"Embedded image {image_id} in document")
    
    except Exception as e:
        logger.error(f"Error embedding image {image_id}: {e}")
        raise
 
 
def _get_image_metadata(image_id: str, mcp_base_url: str) -> Optional[dict]:
    """
    Fetch image metadata from MCP server.
    
    Args:
        image_id: Image ID
        mcp_base_url: Base URL of MCP server
    
    Returns:
        Image metadata dict or None
    """
    
    try:
        import requests
        
        search_url = urljoin(mcp_base_url, "/api/image_search")
        
        response = requests.post(
            search_url,
            json={"query": image_id, "top_k": 1},
            timeout=10
        )
        response.raise_for_status()
        
        result = response.json()
        
        if result.get("results"):
            return result["results"][0]
    
    except Exception as e:
        logger.warning(f"Failed to fetch image metadata for {image_id}: {e}")
    
    return None
 
 
def extract_image_ids_from_text(text: str) -> list:
    """Extract all image IDs from text containing <IMAGE id="..."/> tags"""
    pattern = r'<IMAGE id="([^"]+)"/>'
    return re.findall(pattern, text)
 
 
def has_image_placeholders(text: str) -> bool:
    """Check if text contains any <IMAGE id=".."/> tags"""
    return '<IMAGE id="' in text
 
 
def strip_image_placeholders(text: str) -> str:
    """Remove all <IMAGE id=".."/> tags from text"""
    pattern = r'<IMAGE id="[^"]+"/>'
    return re.sub(pattern, "", text).strip()
 
 
if __name__ == "__main__":
    from docx import Document
    
    doc = Document()
    doc.add_heading("Test Document with Images", 0)
    
    test_text = """
    The distribution network is complex.
    <IMAGE id="a3f9c12b4d"/>
    This diagram shows the network structure.
    """
    
    cleaned = resolve_image_placeholders(
        section_text=test_text,
        doc=doc,
        mcp_base_url="http://localhost:8020"
    )
    
    doc.add_paragraph(cleaned)
    doc.save("/tmp/test_with_images.docx")
    print("Saved test document to /tmp/test_with_images.docx")