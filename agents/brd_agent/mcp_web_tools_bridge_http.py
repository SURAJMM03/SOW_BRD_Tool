"""
Re-export MCP bridge tools under the names expected by the agent files.
Infrastructure canonical version lives at:
  infrastructure/mcp_web_tools/mcp_web_tools_bridge_http.py
"""
import sys
from pathlib import Path

# Ensure repo root is on sys.path so the infrastructure import resolves
_repo_root = str(Path(__file__).resolve().parents[2])
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from infrastructure.mcp_web_tools.mcp_web_tools_bridge_http import (
    web_search as search_web,
    open_url as fetch_url,
)

__all__ = ["search_web", "fetch_url"]
