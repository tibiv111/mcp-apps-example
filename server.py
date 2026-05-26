"""
NAV AI mock MCP server with MCP Apps UI.

Single-file FastAPI app that implements:
  * Streamable HTTP MCP transport (JSON-RPC 2.0)
  * Mocked OAuth 2.1 endpoints (any creds work)
  * A shell ui:// resource that lets the user pick one of 3 mock workflows
  * Three workflow tools, each backed by its own ui:// resource:
      - dashboard:   static read-only view
      - form:        submit a form, get a result
      - long_job:    enqueue a job, watch live progress

Designed to be readable in one sitting, not production-grade.

Endpoints
---------
  GET  /                                      health check
  POST /mcp                                   MCP JSON-RPC (Streamable HTTP)
  GET  /.well-known/oauth-authorization-server   OAuth discovery
  POST /oauth/register                        Dynamic Client Registration (mocked)
  GET  /oauth/authorize                       authorization endpoint (auto-approves)
  POST /oauth/token                           token endpoint (any code -> token)
  GET  /ui/{name}                             serves UI HTML for ui:// resources
  GET  /jobs/{job_id}/events                  SSE stream of job progress
  POST /api/jobs                              direct job creation (called from iframe)

Run locally:
    pip install fastapi uvicorn sse-starlette
    uvicorn server:app --host 0.0.0.0 --port 8000

Deploy on Render: see render.yaml.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

# ─────────────────────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="NAV AI Mock MCP Server")

# Allow the Claude iframe origin to call our direct endpoints (SSE, /api/jobs).
# In production you'd lock this down; for a mock, * is fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# In-memory state
# ─────────────────────────────────────────────────────────────────────────────

# Mock OAuth: pretend we issued tokens. Real flow returns these; we don't check.
_issued_tokens: set[str] = set()

# Mock job store. Real version would be Postgres + Redis pub/sub.
_jobs: dict[str, dict[str, Any]] = {}
_job_subscribers: dict[str, list[asyncio.Queue]] = {}


def _emit_job_event(job_id: str, event: dict[str, Any]) -> None:
    """Push an event to all SSE subscribers for this job."""
    for q in _job_subscribers.get(job_id, []):
        q.put_nowait(event)


async def _run_mock_job(job_id: str) -> None:
    """Simulate a long-running job emitting progress events."""
    steps = [
        ("Validating inputs", 10),
        ("Loading reference data", 25),
        ("Running model", 55),
        ("Aggregating results", 80),
        ("Finalizing report", 100),
    ]
    for label, pct in steps:
        await asyncio.sleep(2)
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["progress"] = pct
        _jobs[job_id]["step"] = label
        _emit_job_event(job_id, {"type": "progress", "progress": pct, "step": label})

    _jobs[job_id]["status"] = "done"
    _jobs[job_id]["result"] = {
        "summary": "Analysis complete",
        "metric_a": round(42.7 + (hash(job_id) % 100) / 10, 2),
        "metric_b": round(18.3 + (hash(job_id) % 70) / 10, 2),
    }
    _emit_job_event(job_id, {"type": "done", "result": _jobs[job_id]["result"]})


# ─────────────────────────────────────────────────────────────────────────────
# Health + root
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "NAV AI Mock MCP", "status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# OAuth — fake but shape-correct
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request) -> dict[str, Any]:
    """OAuth 2.1 discovery document. Claude Desktop reads this."""
    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
    }


@app.post("/oauth/register")
async def oauth_register(req: Request) -> dict[str, Any]:
    """Dynamic Client Registration — accept anything, return fake credentials."""
    body = await req.json()
    return {
        "client_id": f"mock-client-{secrets.token_hex(8)}",
        "client_secret": f"mock-secret-{secrets.token_hex(16)}",
        "client_id_issued_at": int(time.time()),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }


@app.get("/oauth/authorize", response_class=HTMLResponse)
async def oauth_authorize(
    response_type: str = "code",
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
    scope: str = "",
) -> HTMLResponse:
    """
    Authorization endpoint. Real Entra would show a login screen here.
    We auto-approve and redirect with a fake code.
    """
    code = f"mock-code-{secrets.token_hex(12)}"
    redirect = f"{redirect_uri}?code={code}&state={state}"
    return HTMLResponse(
        f"""
        <!doctype html><html><head><title>Mock Login</title></head>
        <body style="font-family:system-ui;max-width:500px;margin:50px auto;padding:20px">
          <h2>Mock NAV AI Login</h2>
          <p>This is a fake auth page. In production this would be Entra.</p>
          <p>Pretending you signed in as <code>demo-user@nav-ai.local</code>...</p>
          <p><a href="{redirect}">Continue to Claude →</a></p>
          <script>setTimeout(() => location.href = {json.dumps(redirect)}, 800);</script>
        </body></html>
        """
    )


@app.post("/oauth/token")
async def oauth_token(req: Request) -> dict[str, Any]:
    """Token endpoint. Any code/refresh token mints a fresh access token."""
    token = f"mock-token-{secrets.token_hex(24)}"
    _issued_tokens.add(token)
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": f"mock-refresh-{secrets.token_hex(24)}",
        "scope": "mcp",
    }


# ─────────────────────────────────────────────────────────────────────────────
# MCP protocol — Streamable HTTP, JSON-RPC 2.0
# ─────────────────────────────────────────────────────────────────────────────

PROTOCOL_VERSION = "2025-06-18"


def _ui_resource_uri(name: str) -> str:
    return f"ui://nav-ai/{name}"


def _ui_meta(name: str, base_url: str) -> dict[str, Any]:
    """
    The _meta.ui block hosts annotate to render UI inline.
    The 'domain' field is required by Claude (see MCP Apps SEP-1865).
    """
    import hashlib

    domain_hash = hashlib.sha256(base_url.encode()).hexdigest()[:32]
    return {
        "ui": {
            "resourceUri": _ui_resource_uri(name),
            "domain": f"{domain_hash}.claudemcpcontent.com",
            "csp": {
                "connectDomains": [base_url],
                "resourceDomains": [base_url],
            },
        }
    }


# Tool definitions. Each tool optionally points at a ui:// resource via _meta.
def _tools_list(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "name": "launch_nav_ai",
            "description": "Open the NAV AI launcher in Claude. Shows the three available workflows.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "_meta": _ui_meta("shell", base_url),
        },
        {
            "name": "open_dashboard",
            "description": "Open the read-only Sales Dashboard workflow.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "_meta": _ui_meta("dashboard", base_url),
        },
        {
            "name": "open_pricing_form",
            "description": "Open the Pricing Adjustment workflow (a form that submits a price change).",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "_meta": _ui_meta("form", base_url),
        },
        {
            "name": "open_forecast_job",
            "description": "Open the Forecast Run workflow — kicks off a long-running forecast and streams progress.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "_meta": _ui_meta("long_job", base_url),
        },
        {
            "name": "start_forecast",
            "description": "Start a forecast job. Returns a job ID; the iframe subscribes to live updates over SSE.",
            "inputSchema": {
                "type": "object",
                "properties": {"region": {"type": "string", "default": "EU"}},
                "required": [],
            },
        },
    ]


def _resource_list(base_url: str) -> list[dict[str, Any]]:
    return [
        {
            "uri": _ui_resource_uri(n),
            "name": f"NAV AI {n} UI",
            "mimeType": "text/html",
        }
        for n in ("shell", "dashboard", "form", "long_job")
    ]


async def _handle_initialize(_params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
        },
        "serverInfo": {"name": "nav-ai-mock", "version": "0.1.0"},
    }


async def _handle_tools_list(base_url: str) -> dict[str, Any]:
    return {"tools": _tools_list(base_url)}


async def _handle_resources_list(base_url: str) -> dict[str, Any]:
    return {"resources": _resource_list(base_url)}


async def _handle_resources_read(params: dict[str, Any], base_url: str) -> dict[str, Any]:
    uri = params.get("uri", "")
    # ui://nav-ai/<name>  →  /ui/<name>
    name = uri.rsplit("/", 1)[-1] if uri.startswith("ui://nav-ai/") else None
    if name is None:
        return {"contents": []}
    html = _render_ui_html(name, base_url)
    return {
        "contents": [
            {
                "uri": uri,
                "mimeType": "text/html",
                "text": html,
            }
        ]
    }


async def _handle_tools_call(params: dict[str, Any], base_url: str) -> dict[str, Any]:
    name = params.get("name", "")
    args = params.get("arguments", {}) or {}

    if name == "launch_nav_ai":
        return _tool_result(
            text="NAV AI launcher opened. Pick a workflow.",
            ui_name="shell",
            base_url=base_url,
        )
    if name == "open_dashboard":
        return _tool_result(text="Sales dashboard opened.", ui_name="dashboard", base_url=base_url)
    if name == "open_pricing_form":
        return _tool_result(
            text="Pricing adjustment form opened.", ui_name="form", base_url=base_url
        )
    if name == "open_forecast_job":
        return _tool_result(
            text="Forecast workflow opened. Press Start to run.",
            ui_name="long_job",
            base_url=base_url,
        )
    if name == "start_forecast":
        job_id = f"job-{uuid.uuid4().hex[:8]}"
        _jobs[job_id] = {
            "id": job_id,
            "status": "queued",
            "progress": 0,
            "step": "Queued",
            "region": args.get("region", "EU"),
            "created_at": time.time(),
        }
        asyncio.create_task(_run_mock_job(job_id))
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Forecast job {job_id} started for region {args.get('region', 'EU')}.",
                }
            ],
            "structuredContent": {"job_id": job_id, "status": "queued"},
        }

    return {
        "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
        "isError": True,
    }


def _tool_result(*, text: str, ui_name: str, base_url: str) -> dict[str, Any]:
    """Tool result that points at a ui:// resource for inline UI rendering."""
    return {
        "content": [{"type": "text", "text": text}],
        "_meta": _ui_meta(ui_name, base_url),
    }


@app.post("/mcp")
async def mcp_endpoint(req: Request) -> Response:
    """
    Single JSON-RPC endpoint. Streamable HTTP allows both single responses
    and streaming, but for a mock we keep it to plain JSON responses.
    """
    base_url = str(req.base_url).rstrip("/")
    body = await req.json()

    # JSON-RPC dispatch
    method = body.get("method", "")
    params = body.get("params", {}) or {}
    req_id = body.get("id")

    try:
        if method == "initialize":
            result = await _handle_initialize(params)
        elif method == "tools/list":
            result = await _handle_tools_list(base_url)
        elif method == "tools/call":
            result = await _handle_tools_call(params, base_url)
        elif method == "resources/list":
            result = await _handle_resources_list(base_url)
        elif method == "resources/read":
            result = await _handle_resources_read(params, base_url)
        elif method == "notifications/initialized":
            # Notification — no response.
            return Response(status_code=202)
        elif method == "ping":
            result = {}
        else:
            return JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
            )

        return JSONResponse({"jsonrpc": "2.0", "id": req_id, "result": result})
    except Exception as e:
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32603, "message": f"Internal error: {e}"},
            }
        )


# ─────────────────────────────────────────────────────────────────────────────
# Direct endpoints used by the iframe (SSE + job API)
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> EventSourceResponse:
    """SSE stream of job progress. The iframe opens this directly."""

    async def stream():
        q: asyncio.Queue = asyncio.Queue()
        _job_subscribers.setdefault(job_id, []).append(q)
        # Replay current state immediately so a late subscriber catches up.
        if job_id in _jobs:
            yield {"data": json.dumps({"type": "snapshot", **_jobs[job_id]})}
        try:
            while True:
                event = await q.get()
                yield {"data": json.dumps(event)}
                if event.get("type") == "done":
                    break
        finally:
            _job_subscribers.get(job_id, []).remove(q)

    return EventSourceResponse(stream())


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict[str, Any]:
    return _jobs.get(job_id, {"error": "not found"})


# ─────────────────────────────────────────────────────────────────────────────
# UI: served as HTML, embedded in the MCP resources/read response.
# Each HTML page is self-contained — no external scripts, no localStorage,
# only fetch/SSE back to our own backend (allowed via csp.connectDomains).
# ─────────────────────────────────────────────────────────────────────────────


def _ui_html_shared_head(title: str) -> str:
    return f"""
    <meta charset="utf-8" />
    <title>{title}</title>
    <style>
      body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
              margin: 0; padding: 16px; background: #fafafa; color: #222; }}
      h1, h2 {{ margin: 0 0 12px 0; }}
      .card {{ background: #fff; border: 1px solid #e3e3e3; border-radius: 8px;
               padding: 16px; margin-bottom: 12px; }}
      button {{ background: #5436DA; color: white; border: none; padding: 8px 14px;
                border-radius: 6px; cursor: pointer; font-size: 14px; }}
      button:hover {{ background: #4226c4; }}
      button:disabled {{ background: #999; cursor: not-allowed; }}
      input, select {{ padding: 6px; border: 1px solid #ccc; border-radius: 4px;
                       font-size: 14px; }}
      .grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
      .progress {{ height: 10px; background: #eee; border-radius: 5px; overflow: hidden; }}
      .bar {{ height: 100%; background: #5436DA; width: 0%; transition: width 0.4s; }}
      .muted {{ color: #888; font-size: 13px; }}
      pre {{ background: #f0f0f0; padding: 8px; border-radius: 4px; overflow-x: auto; }}
    </style>
    """


def _ui_shell_html(base_url: str) -> str:
    """The launcher shell — three cards, click to call a tool."""
    return f"""
    <!doctype html><html><head>{_ui_html_shared_head("NAV AI")}</head><body>
      <h1>NAV AI Launcher</h1>
      <p class="muted">Pick a workflow to open. Each card calls a tool back through Claude.</p>
      <div class="grid">
        <div class="card">
          <h2>📊 Sales Dashboard</h2>
          <p class="muted">Read-only view of regional sales.</p>
          <button onclick="callTool('open_dashboard')">Open</button>
        </div>
        <div class="card">
          <h2>💰 Pricing Adjustment</h2>
          <p class="muted">Submit a price change for review.</p>
          <button onclick="callTool('open_pricing_form')">Open</button>
        </div>
        <div class="card">
          <h2>📈 Forecast Run</h2>
          <p class="muted">Run a forecast — streams live progress.</p>
          <button onclick="callTool('open_forecast_job')">Open</button>
        </div>
      </div>
      <script>
        // AppBridge protocol: postMessage to the host (Claude) to call a tool.
        // The host then handles the tool call and may swap our iframe to the
        // new tool's ui:// resource.
        function callTool(name, args = {{}}) {{
          window.parent.postMessage({{
            type: 'tool',
            payload: {{ name, arguments: args }},
          }}, '*');
        }}
      </script>
    </body></html>
    """


def _ui_dashboard_html(base_url: str) -> str:
    """Static read-only dashboard."""
    return f"""
    <!doctype html><html><head>{_ui_html_shared_head("Sales Dashboard")}</head><body>
      <h1>📊 Sales Dashboard</h1>
      <p class="muted">Region-level snapshot, refreshed daily.</p>
      <div class="grid">
        <div class="card"><h2>EU</h2><p style="font-size:28px;margin:0">€1.42M</p>
          <p class="muted">+4.2% vs last month</p></div>
        <div class="card"><h2>NA</h2><p style="font-size:28px;margin:0">$2.08M</p>
          <p class="muted">+1.7% vs last month</p></div>
        <div class="card"><h2>APAC</h2><p style="font-size:28px;margin:0">$0.95M</p>
          <p class="muted">-0.5% vs last month</p></div>
      </div>
      <div class="card">
        <h2>Top products</h2>
        <ol>
          <li>Widget Pro — 12,402 units</li>
          <li>Gadget Lite — 9,118 units</li>
          <li>Tool X — 6,540 units</li>
        </ol>
      </div>
      <p><button onclick="back()">← Back to launcher</button></p>
      <script>
        function back() {{
          window.parent.postMessage({{
            type: 'tool', payload: {{ name: 'launch_nav_ai', arguments: {{}} }}
          }}, '*');
        }}
      </script>
    </body></html>
    """


def _ui_form_html(base_url: str) -> str:
    """A form that, on submit, fetches our backend directly (not via MCP)."""
    return f"""
    <!doctype html><html><head>{_ui_html_shared_head("Pricing Adjustment")}</head><body>
      <h1>💰 Pricing Adjustment</h1>
      <div class="card">
        <p>Submit a price change. (Direct call to backend — no tool round-trip.)</p>
        <label>Product
          <select id="product">
            <option>Widget Pro</option>
            <option>Gadget Lite</option>
            <option>Tool X</option>
          </select>
        </label>
        &nbsp;
        <label>New price
          <input id="price" type="number" value="49.99" step="0.01" />
        </label>
        <p><button id="submit">Submit</button></p>
        <div id="result"></div>
      </div>
      <p><button onclick="back()">← Back to launcher</button></p>
      <script>
        const BASE = {json.dumps(base_url)};
        document.getElementById('submit').onclick = async () => {{
          const product = document.getElementById('product').value;
          const price = document.getElementById('price').value;
          document.getElementById('result').innerHTML = '<p class="muted">Submitting…</p>';
          // Pretend submit — in real life this would POST to /api/pricing
          await new Promise(r => setTimeout(r, 700));
          document.getElementById('result').innerHTML =
            '<div class="card" style="background:#e8f5e8">' +
            '<strong>✓ Submitted</strong><br>' +
            '<span class="muted">' + product + ' → €' + price + ' queued for review.</span>' +
            '</div>';
        }};
        function back() {{
          window.parent.postMessage({{
            type: 'tool', payload: {{ name: 'launch_nav_ai', arguments: {{}} }}
          }}, '*');
        }}
      </script>
    </body></html>
    """


def _ui_long_job_html(base_url: str) -> str:
    """Long-running job: tool kicks it off, SSE streams progress."""
    return f"""
    <!doctype html><html><head>{_ui_html_shared_head("Forecast Run")}</head><body>
      <h1>📈 Forecast Run</h1>
      <div class="card">
        <p>Run a regional forecast. Tool call enqueues a job, then the UI
           subscribes to live progress over SSE.</p>
        <label>Region
          <select id="region">
            <option>EU</option><option>NA</option><option>APAC</option>
          </select>
        </label>
        <p><button id="start">Start forecast</button></p>
        <div id="status"></div>
      </div>
      <p><button onclick="back()">← Back to launcher</button></p>
      <script>
        const BASE = {json.dumps(base_url)};
        const startBtn = document.getElementById('start');
        const statusEl = document.getElementById('status');

        startBtn.onclick = () => {{
          const region = document.getElementById('region').value;
          startBtn.disabled = true;
          statusEl.innerHTML = '<p class="muted">Asking Claude to start the job…</p>';

          // Listen for the tool result coming back from the host.
          const handler = (e) => {{
            const msg = e.data;
            if (msg && msg.type === 'toolResult' && msg.payload
                && msg.payload.structuredContent
                && msg.payload.structuredContent.job_id) {{
              window.removeEventListener('message', handler);
              subscribe(msg.payload.structuredContent.job_id);
            }}
          }};
          window.addEventListener('message', handler);

          // Call the tool via the host.
          window.parent.postMessage({{
            type: 'tool',
            payload: {{ name: 'start_forecast', arguments: {{ region }} }},
          }}, '*');
        }};

        function subscribe(jobId) {{
          statusEl.innerHTML =
            '<p><strong>Job:</strong> <code>' + jobId + '</code></p>' +
            '<div class="progress"><div class="bar" id="bar"></div></div>' +
            '<p class="muted" id="step">Waiting for first update…</p>' +
            '<pre id="result" style="display:none"></pre>';

          const es = new EventSource(BASE + '/jobs/' + jobId + '/events');
          es.onmessage = (e) => {{
            const ev = JSON.parse(e.data);
            if (ev.type === 'progress' || ev.type === 'snapshot') {{
              if (ev.progress != null)
                document.getElementById('bar').style.width = ev.progress + '%';
              if (ev.step)
                document.getElementById('step').innerText = ev.step;
            }} else if (ev.type === 'done') {{
              document.getElementById('bar').style.width = '100%';
              document.getElementById('step').innerText = '✓ Complete';
              const r = document.getElementById('result');
              r.style.display = 'block';
              r.innerText = JSON.stringify(ev.result, null, 2);
              startBtn.disabled = false;
              es.close();
            }}
          }};
          es.onerror = () => {{
            document.getElementById('step').innerText = 'Connection error.';
            startBtn.disabled = false;
            es.close();
          }};
        }}

        function back() {{
          window.parent.postMessage({{
            type: 'tool', payload: {{ name: 'launch_nav_ai', arguments: {{}} }}
          }}, '*');
        }}
      </script>
    </body></html>
    """


def _render_ui_html(name: str, base_url: str) -> str:
    if name == "shell":
        return _ui_shell_html(base_url)
    if name == "dashboard":
        return _ui_dashboard_html(base_url)
    if name == "form":
        return _ui_form_html(base_url)
    if name == "long_job":
        return _ui_long_job_html(base_url)
    return "<p>Unknown UI</p>"


# Also serve the same HTML on a plain /ui/<name> route — handy for opening
# in a browser to preview the UI without going through Claude.
@app.get("/ui/{name}", response_class=HTMLResponse)
async def ui_preview(name: str, request: Request) -> HTMLResponse:
    base_url = str(request.base_url).rstrip("/")
    return HTMLResponse(_render_ui_html(name, base_url))
