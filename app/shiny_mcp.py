"""
Standalone MCP server endpoint for the R Shiny dashboard.

Mounted at `/shiny-mcp` on this FastAPI service. The user adds this URL
as a SECOND MCP server in their Claude config; each MCP server gets its
own iframe slot in the host. That's how this bypasses the Card E
limitation (today's Claude won't mount a second iframe for additional
UI resources from one MCP server — but it does mount the first resource
from a separate server).

Intentionally minimal — `initialize`, `tools/list`, `tools/call`,
`resources/list`, `resources/read`. No OAuth, no SSE notifications,
no session bookkeeping beyond an ID echoed in the initialize response.
The protocol is identical to `app.mcp.router` but the surface is so
small that duplicating the dispatcher is clearer than abstracting it.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .config import (
    BASE_URL,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    SHELL_MIME,
)
from .shiny_proxy import fetch_embedded_html

router = APIRouter()

SHINY_MCP_SERVER_NAME = "nav-ai-shiny-mcp"
SHINY_DASHBOARD_URI = "ui://shiny/dashboard"

_CONNECT_DOMAINS = [
    BASE_URL,
    BASE_URL.replace("https://", "wss://").replace("http://", "ws://"),
]


SHINY_LAUNCH_TOOL: dict[str, Any] = {
    "name": "launch_shiny_dashboard",
    "title": "Open Shiny dashboard",
    "description": (
        "Open the R Shiny pricing dashboard. The MCP host renders the "
        "dashboard's HTML in its own iframe, peer to any other MCP server's "
        "UI. Pair with the nav-ai-mock-mcp server (the pricing workspace) "
        "to demonstrate multi-server composition."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "_meta": {"ui": {"resourceUri": SHINY_DASHBOARD_URI}},
}


SHINY_DASHBOARD_RESOURCE: dict[str, Any] = {
    "uri": SHINY_DASHBOARD_URI,
    "name": "Shiny pricing dashboard",
    "description": "R Shiny dashboard embedded via server-side HTML rewrite + WS shim.",
    "mimeType": SHELL_MIME,
    "_meta": {
        "ui": {
            # Same CSP shape as the Card E resource. Both https:// and
            # wss:// forms are needed because connect-src doesn't extend
            # https sources to wss in practice.
            "csp": {
                "connectDomains": _CONNECT_DOMAINS,
                "resourceDomains": [BASE_URL],
            },
            "prefersBorder": True,
        }
    },
}


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------

def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _safe_json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.delete("/shiny-mcp")
async def shiny_mcp_session_end() -> Response:
    """Streamable HTTP transport: session termination."""
    return Response(status_code=204)


@router.post("/shiny-mcp")
async def shiny_mcp_endpoint(request: Request) -> Response:
    payload = await _safe_json(request)
    if not isinstance(payload, dict):
        return JSONResponse(_error(None, -32700, "Parse error"))

    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}
    is_notification = "id" not in payload

    if method == "initialize":
        response = JSONResponse(
            _result(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        "resources": {"listChanged": False, "subscribe": False},
                        "extensions": {
                            "io.modelcontextprotocol/ui": {
                                "mimeTypes": [SHELL_MIME],
                            }
                        },
                    },
                    "serverInfo": {
                        "name": SHINY_MCP_SERVER_NAME,
                        "version": SERVER_VERSION,
                    },
                    "instructions": (
                        "This MCP server exposes a single tool that opens an "
                        "R Shiny pricing dashboard. Add the nav-ai-mock-mcp "
                        "server alongside this one to see multi-server "
                        "composition: pricing workspace + Shiny dashboard, "
                        "each in its own iframe."
                    ),
                },
            )
        )
        response.headers["Mcp-Session-Id"] = uuid.uuid4().hex
        return response

    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "ping":
        return JSONResponse(_result(req_id, {}))

    if method == "tools/list":
        return JSONResponse(_result(req_id, {"tools": [SHINY_LAUNCH_TOOL]}))

    if method == "tools/call":
        name = params.get("name")
        if name != "launch_shiny_dashboard":
            return JSONResponse(_error(req_id, -32602, f"Unknown tool: {name}"))
        return JSONResponse(
            _result(
                req_id,
                {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Opening the R Shiny pricing dashboard. The host "
                                "should mount it in its own iframe."
                            ),
                        }
                    ]
                },
            )
        )

    if method == "resources/list":
        return JSONResponse(_result(req_id, {"resources": [SHINY_DASHBOARD_RESOURCE]}))

    if method == "resources/read":
        uri = params.get("uri")
        if uri != SHINY_DASHBOARD_URI:
            return JSONResponse(_error(req_id, -32602, f"Unknown resource: {uri}"))
        html = await fetch_embedded_html()
        return JSONResponse(
            _result(
                req_id,
                {
                    "contents": [
                        {
                            "uri": SHINY_DASHBOARD_URI,
                            "mimeType": SHELL_MIME,
                            "text": html,
                            "_meta": SHINY_DASHBOARD_RESOURCE["_meta"],
                        }
                    ]
                },
            )
        )

    if is_notification:
        return Response(status_code=202)
    return JSONResponse(_error(req_id, -32601, f"Method not found: {method}"))
