"""
DEPRECATED shim — the maintained implementation lives in
infrastructure/mcp_web_tools/bridge/mcp_web_tools_bridge_http.py.

This file used to be a diverged copy of the bridge. It now simply re-exports
the bridge version under both the old names (web_search / open_url) and the
new names (search_web / fetch_url) so any existing imports keep working.
"""

from infrastructure.mcp_web_tools.bridge.mcp_web_tools_bridge_http import (  # noqa: F401
    search_web,
    fetch_url,
    check_mcp_server_health,
)

# Backwards-compatible aliases (old names exported by this module's previous copy)
web_search = search_web
open_url = fetch_url
