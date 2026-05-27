"""
Standalone MCP server endpoint for the R Shiny dashboard.

Mounted at `/shiny-mcp` on this FastAPI service. The user adds this URL
as a SECOND MCP server in their Claude config; Claude mounts a fresh
iframe per MCP App tool call, so the Shiny dashboard appears as a peer
of any other server's UI surface in the conversation.

Intentionally minimal — `initialize`, `tools/list`, `tools/call`,
`resources/list`, `resources/read`. No OAuth, no SSE notifications, no
session bookkeeping beyond an ID echoed in `initialize`. The Shiny HTML
itself (with URL rewriting and the MCP App handshake) comes from
`shiny_proxy.fetch_embedded_html`; this module just packages it as a
fresh MCP server endpoint.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from . import trace
from .config import (
    BASE_URL,
    PROTOCOL_VERSION,
    SERVER_VERSION,
    SHELL_MIME,
    SHINY_MCP_DASHBOARD_URI,
    SHINY_MCP_HELLO_URI,
    SHINY_MCP_SERVER_NAME,
    to_ws_url,
)
from .shiny_proxy import HANDSHAKE_JS, fetch_embedded_html
from .ui.render import render_template

router = APIRouter()


# Standard CSP for any iframe this server mounts: scripts/styles from our
# origin (via the reverse proxy) and WebSocket to the same. Both `https`
# and `wss` forms must be listed explicitly — browsers don't extend the
# https source to wss in connect-src.
_IFRAME_CSP = {
    "connectDomains": [BASE_URL, to_ws_url(BASE_URL)],
    "resourceDomains": [BASE_URL],
}


SHINY_DASHBOARD_TOOL: dict[str, Any] = {
    "name": "launch_shiny_dashboard",
    "title": "Open Shiny dashboard",
    "description": (
        "Open the R Shiny pricing dashboard. The MCP host mounts the "
        "dashboard's HTML in its own iframe, peer to any other MCP "
        "server's UI."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "_meta": {"ui": {"resourceUri": SHINY_MCP_DASHBOARD_URI}},
}


SHINY_DASHBOARD_RESOURCE: dict[str, Any] = {
    "uri": SHINY_MCP_DASHBOARD_URI,
    "name": "Shiny pricing dashboard",
    "description": "R Shiny dashboard embedded via server-side HTML rewrite + WS shim.",
    "mimeType": SHELL_MIME,
    "_meta": {"ui": {"csp": _IFRAME_CSP, "prefersBorder": True}},
}


# Kept as a diagnostic for future regressions: a trivial inline-HTML
# resource that lets us A/B test the dispatcher independently of the
# Shiny embed content.
SHINY_HELLO_TOOL: dict[str, Any] = {
    "name": "launch_shiny_hello",
    "title": "Open Shiny hello-world (diagnostic)",
    "description": (
        "Trivial inline-HTML MCP resource. Use when troubleshooting the "
        "shiny-mcp dispatcher: if hello mounts but dashboard doesn't, "
        "the bug is in the embedded Shiny content."
    ),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    "_meta": {"ui": {"resourceUri": SHINY_MCP_HELLO_URI}},
}


SHINY_HELLO_RESOURCE: dict[str, Any] = {
    "uri": SHINY_MCP_HELLO_URI,
    "name": "Shiny hello (diagnostic)",
    "description": "Trivial inline HTML used to verify shiny-mcp can mount an iframe.",
    "mimeType": SHELL_MIME,
    "_meta": {"ui": {"prefersBorder": True}},
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
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _tool_text(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}]}


def _resource_contents(uri: str, text: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "contents": [
            {"uri": uri, "mimeType": SHELL_MIME, "text": text, "_meta": meta},
        ]
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

async def _handle_initialize(req_id: Any) -> Response:
    response = JSONResponse(
        _result(
            req_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False, "subscribe": False},
                    "extensions": {
                        "io.modelcontextprotocol/ui": {"mimeTypes": [SHELL_MIME]},
                    },
                },
                "serverInfo": {
                    "name": SHINY_MCP_SERVER_NAME,
                    "version": SERVER_VERSION,
                },
                "instructions": (
                    "This MCP server exposes a single tool that opens an R "
                    "Shiny pricing dashboard. Add the nav-ai-mock-mcp server "
                    "alongside this one to see multi-server composition: "
                    "pricing workspace + Shiny dashboard, each in its own iframe."
                ),
            },
        )
    )
    response.headers["Mcp-Session-Id"] = uuid.uuid4().hex
    return response


async def _handle_tools_call(req_id: Any, name: str | None) -> Response:
    if name == "launch_shiny_dashboard":
        return JSONResponse(
            _result(
                req_id,
                _tool_text(
                    "Opening the R Shiny pricing dashboard. The host should "
                    "mount it in its own iframe."
                ),
            )
        )
    if name == "launch_shiny_hello":
        return JSONResponse(
            _result(
                req_id,
                _tool_text(
                    "Opening shiny-mcp hello-world iframe (diagnostic)."
                ),
            )
        )
    return JSONResponse(_error(req_id, -32602, f"Unknown tool: {name}"))


async def _handle_resources_read(req_id: Any, uri: str | None) -> Response:
    if uri == SHINY_MCP_DASHBOARD_URI:
        html = await fetch_embedded_html()
        return JSONResponse(
            _result(
                req_id,
                _resource_contents(
                    SHINY_MCP_DASHBOARD_URI, html, SHINY_DASHBOARD_RESOURCE["_meta"]
                ),
            )
        )
    if uri == SHINY_MCP_HELLO_URI:
        html = render_template(
            "iframes/shiny_hello.html",
            base_url=BASE_URL,
            handshake_js=HANDSHAKE_JS,
        )
        return JSONResponse(
            _result(
                req_id,
                _resource_contents(
                    SHINY_MCP_HELLO_URI, html, SHINY_HELLO_RESOURCE["_meta"]
                ),
            )
        )
    return JSONResponse(_error(req_id, -32602, f"Unknown resource: {uri}"))


async def _dispatch(
    method: str | None,
    req_id: Any,
    params: dict[str, Any],
    is_notification: bool,
) -> Response:
    if method == "initialize":
        return await _handle_initialize(req_id)
    if method == "notifications/initialized":
        return Response(status_code=202)
    if method == "ping":
        return JSONResponse(_result(req_id, {}))
    if method == "tools/list":
        return JSONResponse(
            _result(req_id, {"tools": [SHINY_DASHBOARD_TOOL, SHINY_HELLO_TOOL]})
        )
    if method == "tools/call":
        return await _handle_tools_call(req_id, params.get("name"))
    if method == "resources/list":
        return JSONResponse(
            _result(
                req_id,
                {"resources": [SHINY_DASHBOARD_RESOURCE, SHINY_HELLO_RESOURCE]},
            )
        )
    if method == "resources/read":
        return await _handle_resources_read(req_id, params.get("uri"))
    if is_notification:
        return Response(status_code=202)
    return JSONResponse(_error(req_id, -32601, f"Method not found: {method}"))


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

    correlation = (
        f"shiny-rpc-{req_id}" if req_id is not None else f"shiny-note-{method}"
    )
    trace.record(
        "shiny_mcp.request",
        layer="mcp",
        summary=f"shiny-mcp ← {method}",
        correlation_id=correlation,
        detail={"method": method, "id": req_id, "params": params},
    )

    response = await _dispatch(method, req_id, params, is_notification)

    trace.record(
        "shiny_mcp.response",
        layer="mcp",
        summary=f"shiny-mcp → {method} {getattr(response, 'status_code', 200)}",
        correlation_id=correlation,
        detail={"status": getattr(response, "status_code", 200)},
    )
    return response
