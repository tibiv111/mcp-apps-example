"""
NAV AI — Mock MCP Apps server (SEP-1865)

Single-file FastAPI app that:
  1. Speaks MCP JSON-RPC over POST /mcp.
  2. Serves an MCP Apps `ui://` resource (HTML iframe) for the shell SPA.
  3. Exposes a direct SSE endpoint at /jobs/{id}/events for live progress.
  4. Mocks OAuth 2.1 + Dynamic Client Registration for Claude Desktop's connector flow.

No persistence. In-memory state only.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sse_starlette.sse import EventSourceResponse


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

# When deployed, BASE_URL should be set to e.g. https://nav-mock-mcp.onrender.com
# Falls back to localhost for dev.
BASE_URL: str = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")

SERVER_NAME = "nav-ai-mock"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2025-06-18"
SHELL_URI = "ui://nav-ai/shell"
SHELL_MIME = "text/html;profile=mcp-app"  # exact, no space after ';'
DEMO_USER = "demo-user@nav-ai.local"


# -----------------------------------------------------------------------------
# In-memory state
# -----------------------------------------------------------------------------

_jobs: dict[str, dict[str, Any]] = {}
_job_subscribers: dict[str, list[asyncio.Queue]] = {}
_issued_tokens: set[str] = set()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(3)}"


# -----------------------------------------------------------------------------
# FastAPI app
# -----------------------------------------------------------------------------

app = FastAPI(title="NAV AI Mock MCP", version=SERVER_VERSION)


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
        "mcp_endpoint": f"{BASE_URL}/mcp",
        "preview_ui": f"{BASE_URL}/ui/shell",
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# -----------------------------------------------------------------------------
# OAuth 2.1 mock (DCR + authorize + token)
# -----------------------------------------------------------------------------

@app.get("/.well-known/oauth-authorization-server")
async def oauth_discovery() -> dict[str, Any]:
    return {
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "scopes_supported": ["mcp"],
    }


@app.post("/oauth/register")
async def oauth_register(request: Request) -> dict[str, Any]:
    body = await _safe_json(request)
    client_id = _new_id("client")
    client_secret = secrets.token_urlsafe(24)
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_id_issued_at": int(time.time()),
        "client_secret_expires_at": 0,
        "redirect_uris": body.get("redirect_uris", []) if isinstance(body, dict) else [],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_post",
    }


@app.get("/oauth/authorize")
async def oauth_authorize(
    response_type: str = "code",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",
) -> HTMLResponse:
    # Issue a fake auth code and redirect via HTML (Claude opens this in a browser).
    auth_code = secrets.token_urlsafe(16)
    safe_redirect = redirect_uri or "/"
    sep = "&" if "?" in safe_redirect else "?"
    final_url = f"{safe_redirect}{sep}code={auth_code}&state={state}"
    page = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>NAV AI · Sign in</title>
  <meta http-equiv="refresh" content="1;url={final_url}" />
  <style>
    body {{ background:#0a0d12; color:#e8e6e0; font:14px/1.6 ui-sans-serif,system-ui,-apple-system,sans-serif;
            display:grid; place-items:center; height:100vh; margin:0; }}
    .card {{ border:1px solid #1f2630; padding:32px 40px; text-align:center; max-width:380px; }}
    h1 {{ font:italic 28px/1.1 ui-serif,Georgia,serif; margin:0 0 8px; letter-spacing:.02em; }}
    .sub {{ color:#7a8090; font-size:13px; margin-bottom:24px; }}
    .dot {{ display:inline-block; width:6px; height:6px; background:#d4a85a; margin-right:6px;
            vertical-align:middle; animation:p 1.2s ease-in-out infinite; }}
    @keyframes p {{ 0%,100%{{opacity:.3}} 50%{{opacity:1}} }}
  </style>
</head>
<body>
  <div class="card">
    <h1>NAV AI</h1>
    <div class="sub">Authenticating as {DEMO_USER}</div>
    <div><span class="dot"></span>completing sign-in…</div>
  </div>
  <script>setTimeout(function(){{ window.location = {json.dumps(final_url)}; }}, 1000);</script>
</body>
</html>"""
    return HTMLResponse(page)


@app.post("/oauth/token")
async def oauth_token(request: Request) -> dict[str, Any]:
    # Accept anything; return a fake token.
    token = secrets.token_urlsafe(32)
    _issued_tokens.add(token)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": secrets.token_urlsafe(32),
        "scope": "mcp",
    }


# -----------------------------------------------------------------------------
# Tool definitions
# -----------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "launch_nav_ai",
        "title": "Launch NAV AI",
        "description": "Open the NAV AI workspace inline. Shows the launcher for pricing actions and demand forecasts.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "_meta": {"ui": {"resourceUri": SHELL_URI}},
    },
    {
        "name": "submit_pricing_change",
        "title": "Submit pricing change",
        "description": "Submit a pricing change for review. Returns a ticket ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Product SKU or name."},
                "new_price": {"type": "number", "description": "Proposed new price."},
            },
            "required": ["product", "new_price"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_forecast",
        "title": "Start demand forecast",
        "description": "Kick off a demand forecast job. Returns a job_id; progress streams via SSE.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "Region code (e.g. EU, US, APAC). Defaults to GLOBAL.",
                },
            },
            "additionalProperties": False,
        },
    },
]

RESOURCES: list[dict[str, Any]] = [
    {
        "uri": SHELL_URI,
        "name": "NAV AI shell",
        "description": "Interactive workspace for NAV AI pricing and forecasting.",
        "mimeType": SHELL_MIME,
        "_meta": {
            "ui": {
                "csp": {"connectDomains": [BASE_URL]},
                "prefersBorder": True,
            }
        },
    }
]


# -----------------------------------------------------------------------------
# Tool handlers
# -----------------------------------------------------------------------------

async def _tool_launch_nav_ai(_args: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {"type": "text", "text": "NAV AI workspace opened. Use the launcher to submit a pricing change or run a forecast."}
        ]
    }


async def _tool_submit_pricing_change(args: dict[str, Any]) -> dict[str, Any]:
    product = str(args.get("product", "")).strip() or "UNKNOWN"
    new_price = args.get("new_price")
    try:
        new_price = float(new_price)
    except (TypeError, ValueError):
        new_price = 0.0
    ticket = "PR-" + secrets.token_hex(2).upper()
    submitted_at = int(time.time())
    return {
        "content": [
            {
                "type": "text",
                "text": f"Pricing change submitted for {product} at {new_price:.2f}. Ticket: {ticket}.",
            }
        ],
        "structuredContent": {
            "ticket": ticket,
            "product": product,
            "new_price": new_price,
            "status": "queued_for_review",
            "submitted_at": submitted_at,
        },
    }


async def _tool_start_forecast(args: dict[str, Any]) -> dict[str, Any]:
    region = str(args.get("region", "GLOBAL")).strip().upper() or "GLOBAL"
    job_id = _new_id("job")
    _jobs[job_id] = {
        "id": job_id,
        "region": region,
        "status": "queued",
        "progress": 0,
        "step": "queued",
        "started_at": time.time(),
        "result": None,
    }
    _job_subscribers[job_id] = []
    asyncio.create_task(_run_mock_job(job_id))
    return {
        "content": [{"type": "text", "text": f"Forecast job {job_id} started for {region}."}],
        "structuredContent": {"job_id": job_id, "region": region, "status": "queued"},
    }


TOOL_HANDLERS = {
    "launch_nav_ai": _tool_launch_nav_ai,
    "submit_pricing_change": _tool_submit_pricing_change,
    "start_forecast": _tool_start_forecast,
}


# -----------------------------------------------------------------------------
# Background job runner
# -----------------------------------------------------------------------------

_STEPS = [
    ("collecting", "Collecting demand signals"),
    ("modeling", "Fitting seasonal model"),
    ("simulating", "Running Monte Carlo simulations"),
    ("aggregating", "Aggregating scenarios"),
    ("finalizing", "Finalizing forecast"),
]


async def _emit(job_id: str, event: dict[str, Any]) -> None:
    for q in list(_job_subscribers.get(job_id, [])):
        try:
            await q.put(event)
        except Exception:
            pass


async def _run_mock_job(job_id: str) -> None:
    try:
        job = _jobs[job_id]
        for i, (key, label) in enumerate(_STEPS):
            await asyncio.sleep(2)
            job["status"] = "running"
            job["step"] = key
            job["step_label"] = label
            job["progress"] = int(((i + 1) / len(_STEPS)) * 100)
            await _emit(job_id, {"event": "progress", "data": json.dumps({
                "job_id": job_id,
                "status": "running",
                "step": key,
                "step_label": label,
                "progress": job["progress"],
            })})
        # Final result
        region = job.get("region", "GLOBAL")
        result = {
            "region": region,
            "horizon_weeks": 12,
            "baseline_units": 18420 + int(secrets.token_bytes(1)[0] * 12.5),
            "uplift_pct": round(2.6 + (secrets.token_bytes(1)[0] / 255) * 1.4, 2),
            "confidence": round(0.78 + (secrets.token_bytes(1)[0] / 255) * 0.18, 3),
        }
        job["status"] = "done"
        job["progress"] = 100
        job["step"] = "done"
        job["step_label"] = "Complete"
        job["result"] = result
        await _emit(job_id, {"event": "done", "data": json.dumps({
            "job_id": job_id,
            "status": "done",
            "progress": 100,
            "result": result,
        })})
    except Exception as e:
        await _emit(job_id, {"event": "error", "data": json.dumps({"job_id": job_id, "error": str(e)})})


# -----------------------------------------------------------------------------
# SSE endpoint
# -----------------------------------------------------------------------------

@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> EventSourceResponse:
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="job not found")

    queue: asyncio.Queue = asyncio.Queue()
    _job_subscribers.setdefault(job_id, []).append(queue)

    async def stream() -> AsyncIterator[dict[str, Any]]:
        # Initial snapshot
        snap = _jobs[job_id]
        yield {
            "event": "snapshot",
            "data": json.dumps({
                "job_id": job_id,
                "status": snap.get("status"),
                "step": snap.get("step"),
                "step_label": snap.get("step_label"),
                "progress": snap.get("progress", 0),
                "result": snap.get("result"),
            }),
        }
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    # Heartbeat to keep the connection alive through any proxies.
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield event
                if event.get("event") in ("done", "error"):
                    break
        finally:
            try:
                _job_subscribers.get(job_id, []).remove(queue)
            except ValueError:
                pass

    return EventSourceResponse(stream())


# -----------------------------------------------------------------------------
# MCP JSON-RPC dispatcher
# -----------------------------------------------------------------------------

def _jsonrpc_result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _jsonrpc_error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _safe_json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return None


@app.post("/mcp")
async def mcp_endpoint(request: Request) -> Response:
    payload = await _safe_json(request)
    if not isinstance(payload, dict):
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error"))

    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}

    # Notifications: no id, no response body.
    is_notification = "id" not in payload

    try:
        if method == "initialize":
            return JSONResponse(_jsonrpc_result(req_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"listChanged": False, "subscribe": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            }))

        if method == "notifications/initialized":
            return Response(status_code=202)

        if method == "ping":
            return JSONResponse(_jsonrpc_result(req_id, {}))

        if method == "tools/list":
            return JSONResponse(_jsonrpc_result(req_id, {"tools": TOOLS}))

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            handler = TOOL_HANDLERS.get(name)
            if not handler:
                return JSONResponse(_jsonrpc_error(req_id, -32602, f"Unknown tool: {name}"))
            result = await handler(args)
            # Echo only resourceUri on the tool result so the host renders the iframe.
            # Other _meta.ui fields (csp, permissions) belong on the resource and are
            # ignored when set on a tool — see warning in mcp-ext-apps-host.
            tool_def = next((t for t in TOOLS if t["name"] == name), None)
            if tool_def:
                tool_ui = (tool_def.get("_meta") or {}).get("ui") or {}
                resource_uri = tool_ui.get("resourceUri")
                if resource_uri:
                    result.setdefault("_meta", {}).setdefault("ui", {})["resourceUri"] = resource_uri
            return JSONResponse(_jsonrpc_result(req_id, result))

        if method == "resources/list":
            return JSONResponse(_jsonrpc_result(req_id, {"resources": RESOURCES}))

        if method == "resources/read":
            uri = params.get("uri")
            if uri != SHELL_URI:
                return JSONResponse(_jsonrpc_error(req_id, -32602, f"Unknown resource: {uri}"))
            html = _render_shell_html()
            return JSONResponse(_jsonrpc_result(req_id, {
                "contents": [{
                    "uri": SHELL_URI,
                    "mimeType": SHELL_MIME,
                    "text": html,
                    "_meta": RESOURCES[0]["_meta"],
                }],
            }))

        if is_notification:
            return Response(status_code=202)
        return JSONResponse(_jsonrpc_error(req_id, -32601, f"Method not found: {method}"))
    except Exception as e:
        return JSONResponse(_jsonrpc_error(req_id, -32000, f"Server error: {e}"))


# -----------------------------------------------------------------------------
# Browser preview
# -----------------------------------------------------------------------------

@app.get("/ui/shell", response_class=HTMLResponse)
async def ui_shell_preview() -> HTMLResponse:
    return HTMLResponse(_render_shell_html(), media_type=SHELL_MIME)


# -----------------------------------------------------------------------------
# Shell HTML — the SPA served as the ui:// resource
# -----------------------------------------------------------------------------

def _render_shell_html() -> str:
    base_url_json = json.dumps(BASE_URL)
    return _SHELL_HTML.replace("__BASE_URL_JSON__", base_url_json)


_SHELL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>NAV AI</title>
<style>
  :root {
    --bg:        var(--color-background-primary, #0a0d12);
    --bg-elev:   var(--color-background-secondary, #11151c);
    --bg-soft:   var(--color-background-tertiary, #0d1117);
    --border:   var(--color-border-primary, #1f2630);
    --border-2: var(--color-border-secondary, #2a3340);
    --text:     var(--color-text-primary, #e8e6e0);
    --text-2:   var(--color-text-secondary, #8a8f9c);
    --text-3:   var(--color-text-tertiary, #4d535f);
    --accent:   var(--color-text-warning, #d4a85a);
    --accent-d: #8a7547;
    --ok:       var(--color-text-success, #7a9b6e);
    --warn:     #c89c63;
    --danger:   var(--color-text-danger, #b86a5a);
    --info:     var(--color-text-info, #6a8aa8);

    --font-display: ui-serif, 'Iowan Old Style', 'Apple Garamond', 'Hoefler Text', 'Baskerville', 'Palatino Linotype', Georgia, 'Times New Roman', serif;
    --font-sans: ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, sans-serif;
    --font-mono: ui-monospace, 'SF Mono', 'JetBrains Mono', 'Cascadia Mono', Menlo, Consolas, 'Courier New', monospace;

    --pad: 18px;
    --pad-lg: 28px;
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
               font-family: var(--font-sans); font-size: 13px; line-height: 1.55;
               -webkit-font-smoothing: antialiased; }
  body {
    min-height: 100vh;
    background-image:
      radial-gradient(ellipse 80% 50% at 50% -10%, rgba(212,168,90,0.06), transparent 60%),
      url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='160' height='160'><filter id='n'><feTurbulence baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/><feColorMatrix values='0 0 0 0 0.05  0 0 0 0 0.05  0 0 0 0 0.07  0 0 0 0.5 0'/></filter><rect width='100%' height='100%' filter='url(%23n)' opacity='0.5'/></svg>");
  }

  .hidden { display: none !important; }

  /* ── Header ─────────────────────────────────────────────────────── */
  header.app {
    display: flex; align-items: center; justify-content: space-between;
    padding: 14px var(--pad-lg); border-bottom: 1px solid var(--border);
    background: rgba(10,13,18,0.7); backdrop-filter: blur(8px);
    position: sticky; top: 0; z-index: 10;
  }
  .brand { display: flex; align-items: baseline; gap: 10px; }
  .brand .mark {
    font-family: var(--font-display); font-style: italic; font-size: 26px;
    letter-spacing: 0.01em; line-height: 1; color: var(--text);
  }
  .brand .mark::after {
    content: "·"; color: var(--accent); margin: 0 6px; font-style: normal;
  }
  .brand .sub {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.18em;
    color: var(--text-3); text-transform: uppercase;
  }
  .session {
    display: flex; align-items: center; gap: 14px;
    font-family: var(--font-mono); font-size: 11px; color: var(--text-2);
  }
  .session .dot {
    width: 6px; height: 6px; background: var(--ok); display: inline-block;
    box-shadow: 0 0 6px rgba(122,155,110,0.6);
    animation: pulse 2.4s ease-in-out infinite;
  }
  @keyframes pulse { 0%,100%{opacity:0.5} 50%{opacity:1} }

  /* ── Layout ─────────────────────────────────────────────────────── */
  main { padding: var(--pad-lg); max-width: 1100px; margin: 0 auto; }
  .view { animation: rise 320ms cubic-bezier(.2,.7,.2,1) both; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

  .crumbs {
    display: flex; align-items: center; gap: 10px; margin-bottom: 22px;
    font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.1em;
    color: var(--text-3); text-transform: uppercase;
  }
  .crumbs button {
    background: none; border: none; color: var(--text-2); cursor: pointer;
    font: inherit; padding: 4px 6px; margin-left: -6px;
    border-bottom: 1px dashed transparent; transition: color 120ms, border-color 120ms;
  }
  .crumbs button:hover { color: var(--accent); border-bottom-color: var(--accent-d); }
  .crumbs .sep { color: var(--text-3); }

  h1.view-title {
    font-family: var(--font-display); font-style: italic; font-weight: 400;
    font-size: 38px; line-height: 1.05; margin: 0 0 8px; letter-spacing: 0.005em;
  }
  .view-lede { color: var(--text-2); max-width: 60ch; margin: 0 0 28px; }

  /* ── Cards ──────────────────────────────────────────────────────── */
  .grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
    gap: 16px; margin-top: 8px;
  }
  .card {
    background: var(--bg-elev); border: 1px solid var(--border);
    padding: var(--pad-lg); position: relative; overflow: hidden;
    transition: border-color 180ms ease, transform 180ms ease;
  }
  .card::before {
    content: ""; position: absolute; top: 0; left: 0; right: 0; height: 1px;
    background: linear-gradient(90deg, transparent, var(--accent-d), transparent);
    opacity: 0; transition: opacity 240ms ease;
  }
  .card:hover { border-color: var(--border-2); }
  .card:hover::before { opacity: 0.7; }
  .card .tag {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.18em;
    color: var(--text-3); text-transform: uppercase; margin-bottom: 18px;
  }
  .card h3 {
    font-family: var(--font-display); font-style: italic; font-weight: 400;
    font-size: 24px; line-height: 1.15; margin: 0 0 10px;
  }
  .card p { color: var(--text-2); margin: 0 0 22px; font-size: 13px; }

  /* ── Buttons ────────────────────────────────────────────────────── */
  button.primary, button.ghost {
    font-family: var(--font-mono); font-size: 11px; letter-spacing: 0.16em;
    text-transform: uppercase; padding: 10px 16px; cursor: pointer;
    border: 1px solid var(--border-2); background: transparent; color: var(--text);
    transition: all 160ms ease;
  }
  button.primary {
    border-color: var(--accent-d); color: var(--accent);
  }
  button.primary:hover {
    background: var(--accent); color: #0a0d12; border-color: var(--accent);
    box-shadow: 0 0 0 1px var(--accent), 0 0 24px -8px rgba(212,168,90,0.5);
  }
  button.ghost:hover { border-color: var(--text-2); color: var(--text); }
  button:disabled { opacity: 0.4; cursor: not-allowed; }
  button.primary:disabled:hover { background: transparent; color: var(--accent); box-shadow: none; }

  /* ── Dashboard ─────────────────────────────────────────────────── */
  .kpis {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 0; border: 1px solid var(--border); background: var(--bg-elev);
  }
  .kpi { padding: 22px 24px; border-right: 1px solid var(--border); }
  .kpi:last-child { border-right: none; }
  .kpi .label {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.18em;
    color: var(--text-3); text-transform: uppercase; margin-bottom: 10px;
  }
  .kpi .value {
    font-family: var(--font-mono); font-size: 26px; font-weight: 400;
    color: var(--text); letter-spacing: -0.01em; font-variant-numeric: tabular-nums;
  }
  .kpi .delta { font-family: var(--font-mono); font-size: 11px; margin-top: 6px; }
  .kpi .delta.up { color: var(--ok); }
  .kpi .delta.down { color: var(--danger); }

  .panel {
    background: var(--bg-elev); border: 1px solid var(--border);
    padding: var(--pad-lg); margin-top: 18px;
  }
  .panel h4 {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.18em;
    color: var(--text-3); text-transform: uppercase; margin: 0 0 18px;
    display: flex; justify-content: space-between;
  }
  .panel h4 .meta { color: var(--text-3); font-size: 10px; }

  table.feed { width: 100%; border-collapse: collapse; font-family: var(--font-mono); font-size: 12px; }
  table.feed td { padding: 8px 0; border-bottom: 1px solid var(--border); color: var(--text-2); }
  table.feed td:first-child { color: var(--text-3); width: 80px; }
  table.feed td:last-child { color: var(--text); text-align: right; font-variant-numeric: tabular-nums; }
  table.feed tr:last-child td { border-bottom: none; }

  /* ── Form ──────────────────────────────────────────────────────── */
  form.pricing { display: grid; gap: 18px; max-width: 480px; }
  .field { display: flex; flex-direction: column; gap: 6px; }
  .field label {
    font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.18em;
    color: var(--text-3); text-transform: uppercase;
  }
  .field input, .field select {
    background: var(--bg-soft); border: 1px solid var(--border); color: var(--text);
    padding: 12px 14px; font-family: var(--font-mono); font-size: 14px;
    transition: border-color 140ms;
  }
  .field input:focus, .field select:focus {
    outline: none; border-color: var(--accent-d);
  }
  .actions { display: flex; gap: 12px; margin-top: 8px; }
  .receipt {
    border: 1px solid var(--border-2); padding: 22px; margin-top: 20px;
    background: var(--bg-elev); font-family: var(--font-mono); font-size: 12px;
  }
  .receipt .row { display: flex; justify-content: space-between; padding: 6px 0; }
  .receipt .row .k { color: var(--text-3); letter-spacing: 0.1em; text-transform: uppercase; font-size: 10px; }
  .receipt .row .v { color: var(--text); font-variant-numeric: tabular-nums; }
  .receipt .ticket { color: var(--accent); font-size: 16px; letter-spacing: 0.02em; }

  /* ── Forecast ──────────────────────────────────────────────────── */
  .progress-shell {
    margin-top: 22px; padding: 24px; border: 1px solid var(--border); background: var(--bg-elev);
  }
  .progress-head {
    display: flex; justify-content: space-between; align-items: baseline;
    font-family: var(--font-mono); font-size: 11px; color: var(--text-2);
    letter-spacing: 0.08em; margin-bottom: 14px;
  }
  .progress-head .pct { color: var(--accent); font-size: 14px; font-variant-numeric: tabular-nums; }
  .bar {
    position: relative; height: 4px; background: var(--bg-soft); overflow: hidden;
  }
  .bar > i {
    position: absolute; top: 0; left: 0; bottom: 0; width: 0%;
    background: linear-gradient(90deg, var(--accent-d), var(--accent));
    transition: width 420ms cubic-bezier(.2,.7,.2,1);
  }
  .bar::after {
    content: ""; position: absolute; top: 0; bottom: 0; right: 0; width: 60px;
    background: linear-gradient(90deg, transparent, rgba(212,168,90,0.25));
    opacity: 0; transition: opacity 200ms;
  }
  .running .bar::after { opacity: 1; animation: sweep 1.6s ease-in-out infinite; }
  @keyframes sweep { 0%{transform:translateX(60px)} 100%{transform:translateX(-100%)} }

  .steps { display: grid; gap: 6px; margin-top: 18px; }
  .step-row {
    display: grid; grid-template-columns: 18px 1fr auto; align-items: center; gap: 12px;
    font-family: var(--font-mono); font-size: 12px; color: var(--text-3);
    padding: 6px 0;
  }
  .step-row.active { color: var(--text); }
  .step-row.done { color: var(--text-2); }
  .step-row .mark {
    width: 8px; height: 8px; border: 1px solid var(--border-2);
    display: inline-block; margin-left: 4px;
  }
  .step-row.active .mark { background: var(--accent); border-color: var(--accent); animation: pulse 1.4s ease-in-out infinite; }
  .step-row.done .mark { background: var(--ok); border-color: var(--ok); }
  .step-row .t { font-size: 10px; letter-spacing: 0.1em; color: var(--text-3); }

  .result {
    margin-top: 22px; border: 1px solid var(--accent-d); padding: 24px; background: var(--bg-elev);
  }
  .result h5 {
    font-family: var(--font-display); font-style: italic; font-weight: 400;
    font-size: 22px; margin: 0 0 14px;
  }
  .result-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px 28px; font-family: var(--font-mono); font-size: 12px; }
  .result-grid .k { color: var(--text-3); letter-spacing: 0.08em; text-transform: uppercase; font-size: 10px; }
  .result-grid .v { color: var(--text); font-size: 18px; font-variant-numeric: tabular-nums; }

  /* ── Footer signature ───────────────────────────────────────────── */
  footer.app {
    padding: 18px var(--pad-lg) 28px; color: var(--text-3);
    font-family: var(--font-mono); font-size: 10px; letter-spacing: 0.12em;
    text-transform: uppercase; display: flex; justify-content: space-between;
    border-top: 1px solid var(--border); margin-top: 40px;
  }
  footer.app .caret::after {
    content: "▌"; color: var(--accent); margin-left: 4px;
    animation: blink 1.1s steps(2) infinite;
  }
  @keyframes blink { 50% { opacity: 0; } }

  /* small screens */
  @media (max-width: 640px) {
    main { padding: 18px; }
    h1.view-title { font-size: 30px; }
    .kpis { grid-template-columns: 1fr 1fr; }
    .kpi:nth-child(2n) { border-right: none; }
  }
</style>
</head>
<body>
<header class="app">
  <div class="brand">
    <span class="mark">NAV<i style="font-style:normal">AI</i></span>
    <span class="sub">PRICING · FORECAST · ANALYSIS</span>
  </div>
  <div class="session">
    <span><span class="dot"></span>connected</span>
    <span id="session-user">demo-user@nav-ai.local</span>
  </div>
</header>

<main>

<!-- ───── Launcher ───── -->
<section id="view-launcher" class="view">
  <div class="crumbs"><span>workspace</span><span class="sep">/</span><span>launcher</span></div>
  <h1 class="view-title">Good afternoon.</h1>
  <p class="view-lede">Pricing actions, forecasts and exposure analysis — inline. Choose where to begin.</p>
  <div class="grid">
    <div class="card">
      <div class="tag">01 · OVERVIEW</div>
      <h3>Live dashboard</h3>
      <p>Top-line NAV, AUM, P&amp;L and recent activity across the desk.</p>
      <button class="primary" onclick="show('dashboard')">Open dashboard →</button>
    </div>
    <div class="card">
      <div class="tag">02 · ACTION</div>
      <h3>Pricing change</h3>
      <p>Submit a price update for review. Routed to the desk for sign-off.</p>
      <button class="primary" onclick="show('form')">Open pricing →</button>
    </div>
    <div class="card">
      <div class="tag">03 · COMPUTE</div>
      <h3>Demand forecast</h3>
      <p>Run a 12-week forecast across a region with Monte-Carlo scenarios.</p>
      <button class="primary" onclick="show('forecast')">Open forecast →</button>
    </div>
  </div>
</section>

<!-- ───── Dashboard ───── -->
<section id="view-dashboard" class="view hidden">
  <div class="crumbs">
    <button onclick="show('launcher')">← workspace</button>
    <span class="sep">/</span><span>dashboard</span>
  </div>
  <h1 class="view-title">Desk overview</h1>
  <p class="view-lede">Snapshot of fund NAV, AUM and notable activity. All figures EOD, USD.</p>
  <div class="kpis">
    <div class="kpi"><div class="label">NAV / SHARE</div><div class="value">$104.82</div><div class="delta up">+0.34%</div></div>
    <div class="kpi"><div class="label">AUM</div><div class="value">$2.41 B</div><div class="delta up">+1.2%</div></div>
    <div class="kpi"><div class="label">DAY P&amp;L</div><div class="value">+$3.18 M</div><div class="delta up">+0.13%</div></div>
    <div class="kpi"><div class="label">EXPOSURE</div><div class="value">72.4%</div><div class="delta down">−2.1%</div></div>
  </div>

  <div class="panel">
    <h4>Recent activity<span class="meta">last 8h</span></h4>
    <table class="feed">
      <tr><td>14:32</td><td>Pricing change · SKU-X12 → $129.00</td><td>PR-9A · queued</td></tr>
      <tr><td>13:08</td><td>Forecast job · region APAC</td><td>JOB-72 · done</td></tr>
      <tr><td>11:54</td><td>Rebalance · vector_4 +1.8 bp</td><td>committed</td></tr>
      <tr><td>09:21</td><td>Risk check · scenario_macro_v3</td><td>passed</td></tr>
    </table>
  </div>
</section>

<!-- ───── Pricing form ───── -->
<section id="view-form" class="view hidden">
  <div class="crumbs">
    <button onclick="show('launcher')">← workspace</button>
    <span class="sep">/</span><span>pricing</span>
  </div>
  <h1 class="view-title">Submit pricing change</h1>
  <p class="view-lede">Routes the request to the desk via <code style="font-family:var(--font-mono);color:var(--accent)">submit_pricing_change</code>. Confirmation appears below.</p>

  <form class="pricing" onsubmit="event.preventDefault(); submitPricing();">
    <div class="field">
      <label for="prod">Product</label>
      <select id="prod">
        <option>SKU-X12 · Atlas Hedge</option>
        <option>SKU-A04 · Cobalt Growth</option>
        <option>SKU-R21 · Reserve Income</option>
        <option>SKU-V07 · Vector Macro</option>
      </select>
    </div>
    <div class="field">
      <label for="price">New price (USD)</label>
      <input id="price" type="number" min="0" step="0.01" value="129.00" />
    </div>
    <div class="actions">
      <button class="primary" id="submit-btn" type="submit">Submit for review</button>
      <button class="ghost" type="button" onclick="show('launcher')">Cancel</button>
    </div>
  </form>

  <div id="receipt" class="receipt hidden">
    <div class="row"><span class="k">Ticket</span><span class="v ticket" id="r-ticket">—</span></div>
    <div class="row"><span class="k">Product</span><span class="v" id="r-product">—</span></div>
    <div class="row"><span class="k">Price</span><span class="v" id="r-price">—</span></div>
    <div class="row"><span class="k">Status</span><span class="v" id="r-status">—</span></div>
    <div class="row"><span class="k">Submitted</span><span class="v" id="r-time">—</span></div>
  </div>
</section>

<!-- ───── Forecast ───── -->
<section id="view-forecast" class="view hidden">
  <div class="crumbs">
    <button onclick="show('launcher')">← workspace</button>
    <span class="sep">/</span><span>forecast</span>
  </div>
  <h1 class="view-title">Demand forecast</h1>
  <p class="view-lede">12-week horizon, Monte-Carlo scenarios. Progress streams over SSE direct from this workspace.</p>

  <form class="pricing" onsubmit="event.preventDefault(); startForecast();">
    <div class="field">
      <label for="region">Region</label>
      <select id="region">
        <option>GLOBAL</option>
        <option>EU</option>
        <option>US</option>
        <option>APAC</option>
        <option>LATAM</option>
      </select>
    </div>
    <div class="actions">
      <button class="primary" id="start-btn" type="submit">Start forecast</button>
      <button class="ghost" type="button" onclick="show('launcher')">Cancel</button>
    </div>
  </form>

  <div id="progress-shell" class="progress-shell hidden">
    <div class="progress-head">
      <span id="step-label">queued</span>
      <span class="pct" id="pct">0%</span>
    </div>
    <div class="bar" id="bar-wrap"><i id="bar"></i></div>
    <div class="steps" id="steps">
      <div class="step-row" data-step="collecting"><span class="mark"></span><span>Collecting demand signals</span><span class="t">01</span></div>
      <div class="step-row" data-step="modeling"><span class="mark"></span><span>Fitting seasonal model</span><span class="t">02</span></div>
      <div class="step-row" data-step="simulating"><span class="mark"></span><span>Running Monte Carlo simulations</span><span class="t">03</span></div>
      <div class="step-row" data-step="aggregating"><span class="mark"></span><span>Aggregating scenarios</span><span class="t">04</span></div>
      <div class="step-row" data-step="finalizing"><span class="mark"></span><span>Finalizing forecast</span><span class="t">05</span></div>
    </div>
  </div>

  <div id="forecast-result" class="result hidden">
    <h5>Forecast complete</h5>
    <div class="result-grid">
      <div><div class="k">Region</div><div class="v" id="fr-region">—</div></div>
      <div><div class="k">Horizon</div><div class="v" id="fr-horizon">—</div></div>
      <div><div class="k">Baseline units</div><div class="v" id="fr-baseline">—</div></div>
      <div><div class="k">Uplift</div><div class="v" id="fr-uplift">—</div></div>
      <div><div class="k">Confidence</div><div class="v" id="fr-confidence">—</div></div>
      <div><div class="k">Job</div><div class="v" id="fr-job" style="font-size:13px">—</div></div>
    </div>
  </div>
</section>

</main>

<footer class="app">
  <span>NAV·AI MOCK · SEP-1865 · ui://nav-ai/shell</span>
  <span class="caret">demo-user</span>
</footer>

<script>
(function(){
  const BASE_URL = __BASE_URL_JSON__;
  const VIEWS = ['launcher','dashboard','form','forecast'];

  // ── view router ──
  window.show = function(name){
    VIEWS.forEach(v => {
      const el = document.getElementById('view-'+v);
      if (!el) return;
      if (v === name) { el.classList.remove('hidden'); el.style.animation='none'; void el.offsetWidth; el.style.animation=''; }
      else el.classList.add('hidden');
    });
  };

  // ── JSON-RPC postMessage client (with direct-HTTP fallback for /ui/shell preview) ──
  let nextId = 1;
  const pending = new Map();
  // If no parent responds within HOST_PROBE_MS, we assume we're in the preview
  // (a regular browser tab, no MCP host listening) and fall back to direct HTTP.
  const HOST_PROBE_MS = 800;
  let hostMode = 'unknown'; // 'host' | 'direct' | 'unknown'

  function postRpc(payload){
    try { window.parent.postMessage(payload, '*'); } catch(e) {}
  }

  async function directCall(method, params){
    const r = await fetch(BASE_URL + '/mcp', {
      method: 'POST',
      headers: {'Content-Type':'application/json', 'Accept':'application/json'},
      body: JSON.stringify({jsonrpc:'2.0', id: nextId++, method, params: params||{}})
    });
    const json = await r.json();
    if (json.error) throw new Error(json.error.message || 'rpc error');
    return json.result;
  }

  function sendRequest(method, params){
    if (hostMode === 'direct') return directCall(method, params);
    return new Promise((resolve, reject) => {
      const id = nextId++;
      let settled = false;
      pending.set(id, {
        resolve: (v) => { settled = true; resolve(v); },
        reject:  (e) => { settled = true; reject(e); }
      });
      postRpc({jsonrpc:'2.0', id, method, params: params||{}});
      if (hostMode === 'unknown') {
        setTimeout(() => {
          if (!settled && pending.has(id)) {
            pending.delete(id);
            hostMode = 'direct';
            console.debug('[NAV AI] no MCP host detected, falling back to direct HTTP /mcp');
            directCall(method, params).then(resolve, reject);
          }
        }, HOST_PROBE_MS);
      }
    });
  }
  function sendNotification(method, params){
    if (hostMode === 'direct') return; // host-only notifications
    postRpc({jsonrpc:'2.0', method, params: params||{}});
  }
  window.addEventListener('message', (ev) => {
    const msg = ev.data;
    if (!msg || msg.jsonrpc !== '2.0') return;
    hostMode = 'host';
    if (msg.id != null && pending.has(msg.id)) {
      const {resolve, reject} = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(msg.error); else resolve(msg.result);
    }
  });

  // ── ui/initialize handshake ──
  (async () => {
    try {
      const result = await sendRequest('ui/initialize', {
        protocolVersion: '2025-06-18',
        capabilities: {},
        appInfo: { name: 'nav-ai-shell', version: '0.1.0' },
      });
      // Apply theme variables, if any.
      const theme = result && result.theme && result.theme.cssVariables;
      if (theme && typeof theme === 'object') {
        Object.entries(theme).forEach(([k,v]) => {
          document.documentElement.style.setProperty(k, v);
        });
      }
      sendNotification('ui/notifications/initialized', {});
    } catch (e) {
      // No host present (browser preview) — fine, we keep our defaults.
      console.debug('ui/initialize skipped:', e);
    }
  })();

  // ── Pricing form ──
  window.submitPricing = async function(){
    const btn = document.getElementById('submit-btn');
    const prodLabel = document.getElementById('prod').value;
    const product = prodLabel.split(' · ')[0];
    const new_price = parseFloat(document.getElementById('price').value);
    btn.disabled = true; btn.textContent = 'Submitting…';
    try {
      const res = await sendRequest('tools/call', {
        name: 'submit_pricing_change',
        arguments: { product, new_price }
      });
      const data = (res && res.structuredContent) || {};
      const box = document.getElementById('receipt');
      document.getElementById('r-ticket').textContent  = data.ticket || '—';
      document.getElementById('r-product').textContent = data.product || product;
      document.getElementById('r-price').textContent   = '$' + Number(data.new_price || new_price).toFixed(2);
      document.getElementById('r-status').textContent  = (data.status || 'submitted').replace(/_/g,' ');
      document.getElementById('r-time').textContent    = new Date((data.submitted_at||Date.now()/1000)*1000).toISOString().slice(11,19) + ' UTC';
      box.classList.remove('hidden');
    } catch (e) {
      alert('Submit failed: ' + (e && e.message || e));
    } finally {
      btn.disabled = false; btn.textContent = 'Submit for review';
    }
  };

  // ── Forecast + SSE ──
  let currentSource = null;
  function setProgress(pct, label){
    document.getElementById('bar').style.width = (pct||0) + '%';
    document.getElementById('pct').textContent = (pct||0) + '%';
    if (label) document.getElementById('step-label').textContent = label;
  }
  function markStep(active){
    const rows = document.querySelectorAll('.step-row');
    let passed = true;
    rows.forEach(r => {
      r.classList.remove('active','done');
      if (r.dataset.step === active) { r.classList.add('active'); passed = false; }
      else if (passed) { r.classList.add('done'); }
    });
  }
  window.startForecast = async function(){
    if (currentSource) { try { currentSource.close(); } catch(e){} currentSource = null; }
    const btn = document.getElementById('start-btn');
    const region = document.getElementById('region').value;
    btn.disabled = true; btn.textContent = 'Starting…';
    document.getElementById('forecast-result').classList.add('hidden');
    const shell = document.getElementById('progress-shell');
    shell.classList.remove('hidden');
    shell.classList.add('running');
    setProgress(0, 'queued');
    document.querySelectorAll('.step-row').forEach(r => r.classList.remove('active','done'));

    try {
      const res = await sendRequest('tools/call', {
        name: 'start_forecast',
        arguments: { region }
      });
      const job_id = res && res.structuredContent && res.structuredContent.job_id;
      if (!job_id) throw new Error('no job_id returned');
      document.getElementById('fr-job').textContent = job_id;

      const url = BASE_URL + '/jobs/' + encodeURIComponent(job_id) + '/events';
      const src = new EventSource(url);
      currentSource = src;
      const handle = (ev, type) => {
        let payload = {};
        try { payload = JSON.parse(ev.data); } catch(e){}
        if (type === 'progress' || type === 'snapshot') {
          setProgress(payload.progress, payload.step_label || payload.step);
          if (payload.step) markStep(payload.step);
        } else if (type === 'done') {
          setProgress(100, 'complete');
          markStep('finalizing');
          document.querySelectorAll('.step-row').forEach(r => { r.classList.remove('active'); r.classList.add('done'); });
          shell.classList.remove('running');
          const r = payload.result || {};
          document.getElementById('fr-region').textContent     = r.region || region;
          document.getElementById('fr-horizon').textContent    = (r.horizon_weeks || 12) + ' wk';
          document.getElementById('fr-baseline').textContent   = (r.baseline_units || 0).toLocaleString() + ' u';
          document.getElementById('fr-uplift').textContent     = (r.uplift_pct != null ? '+'+r.uplift_pct+'%' : '—');
          document.getElementById('fr-confidence').textContent = r.confidence != null ? (r.confidence*100).toFixed(1)+'%' : '—';
          document.getElementById('forecast-result').classList.remove('hidden');
          src.close(); currentSource = null;
          btn.disabled = false; btn.textContent = 'Start forecast';
        } else if (type === 'error') {
          shell.classList.remove('running');
          alert('Forecast failed: ' + (payload.error || 'unknown'));
          src.close(); currentSource = null;
          btn.disabled = false; btn.textContent = 'Start forecast';
        }
      };
      src.addEventListener('snapshot', e => handle(e, 'snapshot'));
      src.addEventListener('progress', e => handle(e, 'progress'));
      src.addEventListener('done',     e => handle(e, 'done'));
      src.addEventListener('error',    e => { /* network blip; EventSource auto-retries */ });
    } catch (e) {
      shell.classList.remove('running');
      alert('Start failed: ' + (e && e.message || e));
      btn.disabled = false; btn.textContent = 'Start forecast';
    }
  };
})();
</script>
</body>
</html>
"""