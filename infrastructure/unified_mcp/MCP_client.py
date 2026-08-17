import os

import requests

MCP_BASE_URL = os.getenv("MCP_WEBTOOLS_BASE_URL", "http://localhost:8000").rstrip("/")

def mcp_web_search(query: str, max_results: int = 5) -> str:
    resp = requests.post(
        f"{MCP_BASE_URL}/web_search",
        json={"query": query, "max_results": max_results},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["result"]

def mcp_doc_search(query: str, top_k: int = 5) -> str:
    resp = requests.post(
        f"{MCP_BASE_URL}/doc_search",
        json={"query": query, "top_k": top_k},
        timeout=30,
    )
    resp.raise_for_status()
    hits = resp.json()["hits"]

    # Convert doc hits into readable text
    return "\n".join(
        f"{h['doc_name']} | {h['heading_path']}:\n{h['snippet']}"
        for h in hits
    )
