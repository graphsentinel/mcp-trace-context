"""Minimal MCP server, Python SDK v2 (mcp==2.3.0), one tool. Nothing trace-related here."""
import os

import httpx
from mcp.server import MCPServer

DOWNSTREAM = os.environ.get("DOWNSTREAM", "http://downstream-api.oa-lab:8080")
mcp = MCPServer("oa-py2")


@mcp.tool()
async def fetch_item(id: str) -> str:
    """Fetch one item from the downstream API."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DOWNSTREAM}/items/{id}")
        return r.text


if __name__ == "__main__":
    if os.environ.get("MCP_TRANSPORT") == "stdio":
        mcp.run("stdio")
    else:
        mcp.run("streamable-http", host="0.0.0.0", port=8000, stateless_http=True, json_response=True)
