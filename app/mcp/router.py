"""
MCP JSON-RPC 2.0 dispatcher (Streamable HTTP transport).

This module owns POST /mcp (request/response), GET /mcp (would-be SSE
listener; we 405) and DELETE /mcp (session end). All MCP methods route
through `mcp_endpoint`.

The shell HTML is rendered lazily via `ui.render.render_shell_html` so this
module doesn't pull in Jinja just to dispatch a `tools/list`.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..config import PROTOCOL_VERSION, SERVER_NAME, SERVER_VERSION, SHELL_MIME, SHELL_URI
from ..schemas import RESOURCES, TOOLS
from ..ui.render import render_shell_html
from .tools import TOOL_HANDLERS

router = APIRouter()


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
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Transport-level routes
# ---------------------------------------------------------------------------

@router.get("/mcp")
async def mcp_listener() -> Response:
    """
    Streamable HTTP transport: a GET to /mcp would open a server-initiated
    SSE listener. We don't push anything (no listChanged, no subscriptions),
    so the spec lets us return 405 to signal "no SSE listener here". Claude's
    client treats this as a clean signal and proceeds with normal POST.
    """
    return Response(status_code=405, headers={"Allow": "POST, DELETE"})


@router.delete("/mcp")
async def mcp_session_end() -> Response:
    """Streamable HTTP transport: session termination."""
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    payload = await _safe_json(request)
    if not isinstance(payload, dict):
        return JSONResponse(_error(None, -32700, "Parse error"))

    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}
    is_notification = "id" not in payload  # notifications get no response body

    try:
        if method == "initialize":
            response = JSONResponse(
                _result(
                    req_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {
                            "tools": {"listChanged": False},
                            "resources": {"listChanged": False, "subscribe": False},
                            # Advertise MCP Apps extension support so the host
                            # activates the UI mount path. SEP-1865 uses the
                            # extension identifier 'io.modelcontextprotocol/ui'.
                            "extensions": {
                                "io.modelcontextprotocol/ui": {
                                    "mimeTypes": [SHELL_MIME],
                                }
                            },
                        },
                        "serverInfo": {
                            "name": SERVER_NAME,
                            "version": SERVER_VERSION,
                        },
                    },
                )
            )
            # Streamable HTTP transport requires a session id so the client
            # can correlate subsequent requests. Without this, Claude's MCP
            # proxy ('/v1/toolbox/shttp/...') fails the handshake.
            response.headers["Mcp-Session-Id"] = uuid.uuid4().hex
            return response

        if method == "notifications/initialized":
            return Response(status_code=202)

        if method == "ping":
            return JSONResponse(_result(req_id, {}))

        if method == "tools/list":
            return JSONResponse(_result(req_id, {"tools": TOOLS}))

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            handler = TOOL_HANDLERS.get(name)
            if not handler:
                return JSONResponse(_error(req_id, -32602, f"Unknown tool: {name}"))
            result = await handler(args)

            # Echo only resourceUri onto the tool result so the host renders
            # the iframe. Other _meta.ui fields (csp, permissions) belong on
            # the resource definition and are ignored if set on a tool —
            # see warning in mcp-ext-apps-host.
            tool_def = next((t for t in TOOLS if t["name"] == name), None)
            if tool_def:
                tool_ui = (tool_def.get("_meta") or {}).get("ui") or {}
                resource_uri = tool_ui.get("resourceUri")
                if resource_uri:
                    result.setdefault("_meta", {}).setdefault("ui", {})[
                        "resourceUri"
                    ] = resource_uri
            return JSONResponse(_result(req_id, result))

        if method == "resources/list":
            return JSONResponse(_result(req_id, {"resources": RESOURCES}))

        if method == "resources/read":
            uri = params.get("uri")
            if uri != SHELL_URI:
                return JSONResponse(_error(req_id, -32602, f"Unknown resource: {uri}"))
            return JSONResponse(
                _result(
                    req_id,
                    {
                        "contents": [
                            {
                                "uri": SHELL_URI,
                                "mimeType": SHELL_MIME,
                                "text": render_shell_html(),
                                "_meta": RESOURCES[0]["_meta"],
                            }
                        ]
                    },
                )
            )

        if is_notification:
            return Response(status_code=202)
        return JSONResponse(_error(req_id, -32601, f"Method not found: {method}"))
    except Exception as e:  # noqa: BLE001 — surface any handler bug to caller
        return JSONResponse(_error(req_id, -32000, f"Server error: {e}"))
