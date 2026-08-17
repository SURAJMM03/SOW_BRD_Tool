"""Serper.dev API client utilities.

Serper (https://serper.dev) provides a simple Google Search API.
We keep this client minimal, synchronous, and safe for server-side usage.

Environment variables:
- SERPER_API_KEY: required
- SERPER_API_URL: optional, defaults to https://google.serper.dev/search
- SERPER_TIMEOUT_SECS: optional, defaults to 30

Notes:
- Serper uses header X-API-KEY.
- Request body is JSON.
"""

from __future__ import annotations

import os
import requests
from typing import Any, Dict, List, Optional

DEFAULT_URL = "https://google.serper.dev/search"

class SerperError(RuntimeError):
    pass


def _get_key() -> str:
    key = os.getenv("SERPER_API_KEY", "").strip()
    if not key:
        raise SerperError(
            "SERPER_API_KEY is not set. Configure it in your environment (.env) before starting the server."
        )
    return key


def _get_url() -> str:
    return os.getenv("SERPER_API_URL", DEFAULT_URL).strip() or DEFAULT_URL


def serper_search(query: str, num: int = 5, gl: Optional[str] = None, hl: Optional[str] = None) -> Dict[str, Any]:
    """Perform a Serper search.

    Args:
        query: Search query
        num: Desired number of results (Serper supports 'num')
        gl: Geo location, e.g., 'in'
        hl: Host language, e.g., 'en'

    Returns:
        Parsed JSON response.

    Raises:
        SerperError on missing key or HTTP errors.
    """
    key = _get_key()
    url = _get_url()
    timeout = int(os.getenv("SERPER_TIMEOUT_SECS", "30"))

    payload: Dict[str, Any] = {"q": query, "num": max(1, min(int(num), 100))}
    if gl:
        payload["gl"] = gl
    if hl:
        payload["hl"] = hl

    try:
        r = requests.post(
            url,
            headers={
                "X-API-KEY": key,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout,
            verify=False
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.HTTPError as e:
        msg = getattr(e.response, "text", "") if getattr(e, "response", None) is not None else str(e)
        raise SerperError(f"Serper HTTP error: {msg}") from e
    except requests.exceptions.RequestException as e:
        raise SerperError(f"Serper request failed: {e}") from e


def to_markdown_search_results(data: Dict[str, Any], max_results: int = 5) -> str:
    """Convert Serper response to clean Markdown."""
    lines: List[str] = []

    # Prefer organic results
    organic = data.get("organic") or []
    if not isinstance(organic, list):
        organic = []

    count = 0
    for item in organic:
        if count >= max_results:
            break
        if not isinstance(item, dict):
            continue
        title = (item.get("title") or "").strip()
        link = (item.get("link") or "").strip()
        snippet = (item.get("snippet") or "").strip()
        if not (title or link or snippet):
            continue
        count += 1
        if title and link:
            lines.append(f"{count}. **{title}**\n   - {link}")
        elif title:
            lines.append(f"{count}. **{title}**")
        elif link:
            lines.append(f"{count}. {link}")
        if snippet:
            lines.append(f"   - {snippet}")

    if not lines:
        # Fallback: Serper may return 'answerBox' or 'knowledgeGraph'
        ab = data.get("answerBox")
        kg = data.get("knowledgeGraph")
        if ab:
            lines.append("**AnswerBox**")
            lines.append(str(ab))
        if kg:
            lines.append("**KnowledgeGraph**")
            lines.append(str(kg))

    return "\n".join(lines).strip()
