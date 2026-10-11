"""Minimal MCP server, Python SDK v1 (mcp==1.30.0, FastMCP), one tool. Nothing trace-related here."""
import os

import httpx
from mcp.server.fastmcp import FastMCP

DOWNSTREAM = os.environ.get("DOWNSTREAM", "http://downstream-api.oa-lab:8080")
mcp = FastMCP("oa-py1", host="0.0.0.0", port=8000, stateless_http=True, json_response=True)


@mcp.tool()
async def fetch_item(id: str) -> str:
    """Fetch one item from the downstream API."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{DOWNSTREAM}/items/{id}")
        return r.text


if __name__ == "__main__":
    mcp.run(transport="stdio" if os.environ.get("MCP_TRANSPORT") == "stdio" else "streamable-http")
