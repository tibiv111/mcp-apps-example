"""
MCP JSON-RPC 2.0 dispatcher (Streamable HTTP transport).

This module owns POST /mcp (request/response), GET /mcp (server→client SSE
listener for notifications/*) and DELETE /mcp (session end). All MCP methods
route through `mcp_endpoint`.

The shell HTML is rendered lazily via `ui.render.render_shell_html` so this
module doesn't pull in Jinja just to dispatch a `tools/list`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .. import state, trace
from ..config import (
    PROTOCOL_VERSION,
    SERVER_INSTRUCTIONS,
    SERVER_NAME,
    SERVER_VERSION,
    SHELL_MIME,
    SHELL_URI,
)
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
# Server → client notifications (over the GET /mcp SSE channel)
# ---------------------------------------------------------------------------

async def broadcast_notification(method: str, params: dict[str, Any]) -> int:
    """
    Fan a JSON-RPC notification out to every open GET /mcp listener.

    Returns the number of subscribers it was delivered to. Trace event is
    recorded once, with subscriber count, so the diagnostics page shows the
    broadcast even when nobody is listening.
    """
    payload = {"jsonrpc": "2.0", "method": method, "params": params}
    subs = list(state.mcp_subscribers)
    for q in subs:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
    trace.record(
        "mcp.notification",
        layer="mcp",
        summary=f"broadcast {method} → {len(subs)} subscriber(s)",
        detail={"method": method, "params": params, "subscribers": len(subs)},
    )
    return len(subs)


# ---------------------------------------------------------------------------
# Transport-level routes
# ---------------------------------------------------------------------------

@router.get("/mcp")
async def mcp_listener(request: Request) -> EventSourceResponse:
    """
    Streamable HTTP transport: GET /mcp opens a server-initiated SSE channel.
    The server pushes JSON-RPC notifications (e.g. notifications/resources/
    updated) down this stream. The client picks them up and reacts — re-
    reading the resource, refreshing tool list, etc.

    We accept any caller; in a hardened deploy you'd require the bearer
    token, but the demo intentionally stays permissive on the frontend MCP.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    state.mcp_subscribers.append(queue)
    trace.record(
        "mcp.listener.open",
        layer="mcp",
        summary=f"GET /mcp listener opened (total: {len(state.mcp_subscribers)})",
    )

    async def stream() -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": "message", "data": json.dumps(payload)}
        finally:
            try:
                state.mcp_subscribers.remove(queue)
            except ValueError:
                pass
            trace.record(
                "mcp.listener.close",
                layer="mcp",
                summary=f"GET /mcp listener closed (remaining: {len(state.mcp_subscribers)})",
            )

    return EventSourceResponse(stream(), headers={"X-Accel-Buffering": "no"})


@router.delete("/mcp")
async def mcp_session_end() -> Response:
    """Streamable HTTP transport: session termination."""
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Main dispatcher
# ---------------------------------------------------------------------------

def _extract_bearer(request: Request) -> str | None:
    """Pull the bearer token from the Authorization header, if present."""
    auth = request.headers.get("authorization") or request.headers.get("Authorization")
    if not auth or not auth.lower().startswith("bearer "):
        return None
    return auth.split(" ", 1)[1].strip() or None


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    payload = await _safe_json(request)
    if not isinstance(payload, dict):
        return JSONResponse(_error(None, -32700, "Parse error"))

    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}
    is_notification = "id" not in payload  # notifications get no response body
    bearer = _extract_bearer(request)

    correlation = f"rpc-{req_id}" if req_id is not None else f"note-{method}"
    trace.record(
        "mcp.request",
        layer="mcp",
        summary=f"← {method}" + (f" ({params.get('name')})" if method == "tools/call" else ""),
        correlation_id=correlation,
        detail={
            "method": method,
            "id": req_id,
            "has_token": bool(bearer),
            "params_summary": _summarize_params(method, params),
        },
    )

    # Hold the exception (if any) so we can record the trace event *after*
    # the with-block exits — Timer.ms is only set in __exit__.
    error: Exception | None = None
    with trace.Timer() as t:
        try:
            response = await _dispatch(method, req_id, params, bearer, is_notification, correlation)
        except Exception as e:  # noqa: BLE001
            error = e
            response = JSONResponse(_error(req_id, -32000, f"Server error: {e}"))

    if error is not None:
        trace.record(
            "mcp.response",
            layer="mcp",
            summary=f"→ {method} ERROR {error}",
            correlation_id=correlation,
            duration_ms=t.ms,
            detail={"error": str(error)},
        )
    else:
        trace.record(
            "mcp.response",
            layer="mcp",
            summary=f"→ {method} {response.status_code}",
            correlation_id=correlation,
            duration_ms=t.ms,
            detail={"status": response.status_code},
        )
    return response


def _summarize_params(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Trim noisy params before they hit the diagnostics page."""
    if method == "tools/call":
        return {"name": params.get("name"), "arguments": params.get("arguments")}
    if method == "resources/read":
        return {"uri": params.get("uri")}
    if method in ("resources/subscribe", "resources/unsubscribe"):
        return {"uri": params.get("uri")}
    return {}


async def _dispatch(
    method: str | None,
    req_id: Any,
    params: dict[str, Any],
    bearer: str | None,
    is_notification: bool,
    correlation: str,
) -> Response:
    if method == "initialize":
        response = JSONResponse(
            _result(
                req_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {
                        "tools": {"listChanged": False},
                        # subscribe: true  → we honour resources/subscribe and
                        #                    will push notifications/resources/updated
                        # listChanged: true → we may push resources/list_changed
                        "resources": {"listChanged": True, "subscribe": True},
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
                    "instructions": SERVER_INSTRUCTIONS,
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
        return JSONResponse(_result(req_id, {"tools": TOOLS}))

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return JSONResponse(_error(req_id, -32602, f"Unknown tool: {name}"))

        with trace.Timer() as tool_t:
            result = await handler(args, bearer)
        trace.record(
            "tool.call",
            layer="tool",
            summary=f"⚙ {name}" + (" (error)" if result.get("isError") else ""),
            correlation_id=correlation,
            duration_ms=tool_t.ms,
            detail={
                "tool": name,
                "arguments": args,
                "is_error": bool(result.get("isError")),
            },
        )

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

    if method == "resources/subscribe":
        # We don't track which URI which session asked for — the only resource
        # we publish updates for is the shell, so a global broadcast hits the
        # right place. Still acknowledge per spec.
        uri = params.get("uri")
        trace.record(
            "resource.subscribe",
            layer="resource",
            summary=f"client subscribed to {uri}",
            correlation_id=correlation,
            detail={"uri": uri},
        )
        return JSONResponse(_result(req_id, {}))

    if method == "resources/unsubscribe":
        uri = params.get("uri")
        trace.record(
            "resource.unsubscribe",
            layer="resource",
            summary=f"client unsubscribed from {uri}",
            correlation_id=correlation,
            detail={"uri": uri},
        )
        return JSONResponse(_result(req_id, {}))

    if is_notification:
        return Response(status_code=202)
    return JSONResponse(_error(req_id, -32601, f"Method not found: {method}"))
