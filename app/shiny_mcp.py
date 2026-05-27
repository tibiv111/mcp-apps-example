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

from . import trace
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
SHINY_HELLO_URI = "ui://shiny/hello"

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


# Trivial Hello-world resource used to A/B-test whether shiny-mcp can mount
# *anything* in Claude. If launch_shiny_hello mounts an iframe but
# launch_shiny_dashboard doesn't, the bug is in the embedded Shiny content
# (path rewriting, scripts, etc.). If neither mounts, the bug is somewhere
# in the shiny-mcp dispatcher itself.
SHINY_HELLO_TOOL: dict[str, Any] = {
    "name": "launch_shiny_hello",
    "title": "Open Shiny hello-world (A/B test)",
    "description": (
        "Trivial inline-HTML MCP resource. Used to verify the shiny-mcp "
        "server can mount an iframe at all, isolating any issue in the "
        "rewritten Shiny content from issues in the dispatcher."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
    "_meta": {"ui": {"resourceUri": SHINY_HELLO_URI}},
}


SHINY_HELLO_RESOURCE: dict[str, Any] = {
    "uri": SHINY_HELLO_URI,
    "name": "Shiny hello (A/B test)",
    "description": "Trivial inline HTML used to verify shiny-mcp can mount an iframe.",
    "mimeType": SHELL_MIME,
    "_meta": {"ui": {"prefersBorder": True}},
}


# Minimal MCP Apps host handshake. The host gates iframe visibility on
# `ui/notifications/initialized` coming back from the iframe — without it,
# the iframe is mounted but never made visible to the user. The NAV AI
# shell sends this via shell.js; any custom MCP App iframe (hello, or the
# rewritten Shiny embed) has to do it too.
MCP_APP_HANDSHAKE_JS = """(function(){
  function send(m){ try{ window.parent.postMessage(m,'*'); }catch(e){} }
  var initId = Math.floor(Math.random()*1e9);
  send({jsonrpc:'2.0', id: initId, method:'ui/initialize', params:{
    protocolVersion:'2025-06-18',
    appCapabilities:{availableDisplayModes:['inline']},
    clientInfo:{name:'shiny-mcp-iframe',version:'0.1.0'}
  }});
  setTimeout(function(){
    send({jsonrpc:'2.0', method:'ui/notifications/initialized', params:{}});
  }, 0);
  window.addEventListener('message', function(ev){
    var m = ev.data;
    if (!m || m.jsonrpc !== '2.0') return;
    if (m.method === 'ping' && m.id != null) {
      send({jsonrpc:'2.0', id: m.id, result: {}});
    }
    if (m.method === 'ui/resource-teardown' && m.id != null) {
      send({jsonrpc:'2.0', id: m.id, result: {}});
    }
  });
  function reportSize(){
    var h = Math.max(
      document.documentElement.scrollHeight,
      document.body ? document.body.scrollHeight : 0,
      200);
    send({jsonrpc:'2.0', method:'ui/notifications/size-changed', params:{
      height: h,
      width: document.documentElement.clientWidth || window.innerWidth
    }});
  }
  setTimeout(reportSize, 100);
  setTimeout(reportSize, 600);
  setTimeout(reportSize, 1500);
  window.addEventListener('resize', reportSize);
})();"""


SHINY_HELLO_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>shiny-mcp hello</title>
<style>
  html,body{margin:0;padding:0;background:#0d0d10;color:#e6e6ea;
    font-family:system-ui,-apple-system,sans-serif;min-height:100vh}
  .wrap{padding:24px;max-width:680px}
  h1{font-size:14px;letter-spacing:.16em;text-transform:uppercase;
    color:#9aa0aa;margin:0 0 12px 0;font-weight:500}
  p{font-size:13px;color:#9aa0aa;margin:0 0 8px 0;line-height:1.55}
  code{font-family:'JetBrains Mono',monospace;font-size:12px;color:#5fb878}
  .tag{display:inline-block;background:#1a3322;color:#5fb878;
    border:1px solid #2a5a3a;padding:3px 8px;border-radius:4px;
    font-family:'JetBrains Mono',monospace;font-size:10px;
    letter-spacing:.1em;margin-bottom:14px}
</style>
<script>__MCP_APP_HANDSHAKE__</script>
</head>
<body><div class="wrap">
  <div class="tag">SHINY-MCP · IFRAME MOUNTED</div>
  <h1>Hello from the shiny-mcp connector</h1>
  <p>This iframe came from a <em>second</em> MCP server
    (<code>{base}/shiny-mcp</code>), peer to the NAV AI workspace.</p>
  <p>If you can read this, Claude mounted the iframe successfully and
    the issue with <code>launch_shiny_dashboard</code> is in the rewritten
    Shiny content, not the shiny-mcp dispatcher.</p>
  <p style="color:#7a8087">Tool: <code>launch_shiny_hello</code> ·
    Resource: <code>ui://shiny/hello</code></p>
</div></body></html>
""".replace("__MCP_APP_HANDSHAKE__", MCP_APP_HANDSHAKE_JS)


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

    correlation = f"shiny-rpc-{req_id}" if req_id is not None else f"shiny-note-{method}"
    trace.record(
        "shiny_mcp.request",
        layer="mcp",
        summary=f"shiny-mcp ← {method}",
        correlation_id=correlation,
        detail={"method": method, "id": req_id, "params": params},
    )

    response = await _dispatch_shiny_mcp(method, req_id, params, is_notification)

    status = getattr(response, "status_code", 200)
    trace.record(
        "shiny_mcp.response",
        layer="mcp",
        summary=f"shiny-mcp → {method} {status}",
        correlation_id=correlation,
        detail={"status": status},
    )
    return response


async def _dispatch_shiny_mcp(
    method: str | None,
    req_id: Any,
    params: dict[str, Any],
    is_notification: bool,
) -> Response:
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
        return JSONResponse(
            _result(req_id, {"tools": [SHINY_LAUNCH_TOOL, SHINY_HELLO_TOOL]})
        )

    if method == "tools/call":
        name = params.get("name")
        if name == "launch_shiny_dashboard":
            return JSONResponse(
                _result(
                    req_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Opening the R Shiny pricing dashboard. "
                                    "The host should mount it in its own iframe."
                                ),
                            }
                        ]
                    },
                )
            )
        if name == "launch_shiny_hello":
            return JSONResponse(
                _result(
                    req_id,
                    {
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Opening shiny-mcp hello-world iframe "
                                    "(A/B test to verify mounting)."
                                ),
                            }
                        ]
                    },
                )
            )
        return JSONResponse(_error(req_id, -32602, f"Unknown tool: {name}"))

    if method == "resources/list":
        return JSONResponse(
            _result(
                req_id,
                {"resources": [SHINY_DASHBOARD_RESOURCE, SHINY_HELLO_RESOURCE]},
            )
        )

    if method == "resources/read":
        uri = params.get("uri")
        if uri == SHINY_DASHBOARD_URI:
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
        if uri == SHINY_HELLO_URI:
            return JSONResponse(
                _result(
                    req_id,
                    {
                        "contents": [
                            {
                                "uri": SHINY_HELLO_URI,
                                "mimeType": SHELL_MIME,
                                "text": SHINY_HELLO_HTML.replace("{base}", BASE_URL),
                                "_meta": SHINY_HELLO_RESOURCE["_meta"],
                            }
                        ]
                    },
                )
            )
        return JSONResponse(_error(req_id, -32602, f"Unknown resource: {uri}"))

    if is_notification:
        return Response(status_code=202)
    return JSONResponse(_error(req_id, -32601, f"Method not found: {method}"))
