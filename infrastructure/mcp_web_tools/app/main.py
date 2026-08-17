from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import logging

from infrastructure.mcp_web_tools.bridge.mcp_web_tools_bridge_http import (
    search_web,
    fetch_url,
)

app = FastAPI()
logger = logging.getLogger("MCPFastAPI")


class WebSearchRequest(BaseModel):
    query: str
    max_results: int = 5


class OpenURLRequest(BaseModel):
    url: str
    max_chars: int = 12000


@app.get("/")
def root():
    return {"message": "MCP Web Tools API is running"}


@app.post("/web_search")
def web_search_endpoint(request: WebSearchRequest):
    try:
        result = search_web(request.query, request.max_results)
        return {"result": result}
    except Exception as e:
        logger.exception("Error in /web_search")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/open_url")
def open_url_endpoint(request: OpenURLRequest):
    try:
        result = fetch_url(request.url, request.max_chars)
        return {"result": result}
    except Exception as e:
        logger.exception("Error in /open_url")
        raise HTTPException(status_code=500, detail=str(e))
