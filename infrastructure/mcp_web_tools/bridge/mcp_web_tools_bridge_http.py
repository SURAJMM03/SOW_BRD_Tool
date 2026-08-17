"""
MCP HTTP bridge tools for BRDGenAgent.

This module exposes LangChain @tool functions that call an external MCP server
running as a FastAPI application via HTTP (default: http://localhost:8000).
It returns clean Markdown for LLM consumption.

Requires in BRDGenAgent venv: pip install requests
"""

import os
import json
import logging
from typing import Any, Optional
from langchain.tools import tool
import requests

logger = logging.getLogger("BRDGenAgentMCPBridge")

def _get_base_url() -> str:
    """Get the base URL for the MCP FastAPI server."""
    return os.getenv("MCP_WEBTOOLS_BASE_URL", "http://localhost:8000")

def _make_request(endpoint: str, payload: dict, timeout: int = 60) -> dict:
    base_url = _get_base_url()
    url = f"{base_url}{endpoint}"

    def _post(u: str) -> requests.Response:
        return requests.post(
            u,
            json=payload,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    try:
        logger.debug("Making request to %s with payload: %s", url, payload)
        response = _post(url)

        # If endpoint mismatch in some environments, retry once for legacy path
        if response.status_code == 404 and endpoint == "/web_search":
            legacy_url = f"{base_url}/search_web"
            logger.warning("Got 404 for /web_search; retrying legacy endpoint %s", legacy_url)
            response = _post(legacy_url)

        response.raise_for_status()
        return response.json()

    except requests.exceptions.HTTPError as e:
        r = e.response
        detail = ""
        if r is not None:
            try:
                j = r.json()
                # FastAPI uses {"detail": "..."} by default
                if isinstance(j, dict) and "detail" in j:
                    detail = j["detail"]
                else:
                    detail = j
            except Exception:
                detail = r.text
            error_msg = f"HTTP {r.status_code} from MCP server at {url}: {detail}"
        else:
            error_msg = f"HTTP error calling MCP server at {url}: {e}"

        logger.error(error_msg)
        raise RuntimeError(error_msg) from e

    except requests.exceptions.Timeout:
        error_msg = f"Request to {url} timed out after {timeout} seconds"
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    except requests.exceptions.ConnectionError as e:
        error_msg = f"Failed to connect to MCP server at {url}. Is the server running? Error: {e}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)

    except Exception as e:
        error_msg = f"Unexpected error calling MCP server at {url}: {e}"
        logger.error(error_msg)
        raise RuntimeError(error_msg)
    
    
def _extract_result(response: dict) -> str:
    """
    Extract the result text from the MCP server response.
    
    Args:
        response: The JSON response from the server
        
    Returns:
        The extracted text result
    """
    if not response:
        return ""
    
    # Handle standard FastAPI response format: {"result": "..."}
    if isinstance(response, dict) and "result" in response:
        result = response["result"]
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False)
    
    # Fallback: return the entire response as JSON
    return json.dumps(response, ensure_ascii=False)

@tool
def search_web(query: str, max_results: Optional[int] = None) -> str:
    """
    Search the web via the MCP FastAPI server.
    
    Args:
        query: The search query string
        max_results: Maximum number of results to return (default from env or 5)
        
    Returns:
        Clean Markdown formatted search results
    """
    mr = max_results or int(os.getenv("MCP_WEBTOOLS_MAX_RESULTS", "5"))
    logger.info(f"MCP web_search via HTTP: query='{query}', max_results={mr}")
    
    try:
        payload = {"query": query, "max_results": mr}
        response = _make_request("/web_search", payload)
        result = _extract_result(response)
        logger.debug(f"web_search returned {len(result)} characters")
        return result
        
    except Exception as e:
        error_msg = f"web_search failed: {e}"
        logger.error(error_msg)
        return f"Error performing web search: {e}"

@tool
def fetch_url(url: str, max_chars: Optional[int] = None) -> str:
    """
    Fetch URL and return clean Markdown via MCP FastAPI server.
    
    Args:
        url: The URL to fetch
        max_chars: Maximum characters to return (default from env or 12000)
        
    Returns:
        Clean Markdown content from the URL
    """
    mc = max_chars or int(os.getenv("MCP_WEBTOOLS_MAX_CHARS", "12000"))
    logger.info(f"MCP open_url via HTTP: url='{url}', max_chars={mc}")
    
    try:
        payload = {"url": url, "max_chars": mc}
        response = _make_request("/open_url", payload)
        result = _extract_result(response)
        logger.debug(f"open_url returned {len(result)} characters")
        return result
        
    except Exception as e:
        error_msg = f"open_url failed: {e}"
        logger.error(error_msg)
        return f"Error fetching URL: {e}"

# Health check utility function (not a tool, just for diagnostics)
def check_mcp_server_health() -> bool:
    """
    Check if the MCP FastAPI server is reachable.
    
    Returns:
        True if the server is healthy, False otherwise
    """
    base_url = _get_base_url()
    try:
        response = requests.get(f"{base_url}/", timeout=5)
        response.raise_for_status()
        logger.info(f"✅ MCP server is healthy at {base_url}")
        return True
    except Exception as e:
        logger.warning(f"⚠️ MCP server not reachable at {base_url}: {e}")
        return False
