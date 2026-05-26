"""
NAV AI mock MCP server with MCP Apps UI (SEP-1865 compliant).

Single-file FastAPI app that implements:
  * Streamable HTTP MCP transport (JSON-RPC 2.0)
  * Mocked OAuth 2.1 endpoints (any creds work)
  * One `ui://` shell resource that contains all three demo "apps",
    navigated internally in JS (no iframe swap between views)
  * Proper MCP Apps protocol: text/html;profile=mcp-app mimeType,
    JSON-RPC over postMessage, ui/initialize handshake
  * Direct SSE channel for live job progress, bypassing MCP notifications

Endpoints
---------
  GET  /                                      health check
  POST /mcp                                   MCP JSON-RPC
  GET  /.well-known/oauth-authorization-server   OAuth discovery
  POST /oauth/register                        DCR (mocked)
  GET  /oauth/authorize                       authorization (auto-approves)
  POST /oauth/token                           token (any code -> token)
  GET  /ui/shell                              browser preview of the shell HTML
  GET  /jobs/{job_id}/events                  SSE stream of job progress
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

app = FastAPI(title="NAV AI Mock MCP Server")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

PROTOCOL_VERSION = "2025-06-18"
UI_MIME_TYPE = "text/html;profile=mcp-app"
SHELL_URI = "ui://nav-ai/shell"

# ─────────────────────────────────────────────────────────────────────────────
# In-memory state
# ─────────────────────────────────────────────────────────────────────────────

_issued_tokens: set[str] = set()
_jobs: dict[str, dict[str, Any]] = {}
_job_subscribers: dict[str, list[asyncio.Queue]] = {}


def _emit_job_event(job_id: str, event: dict[str, Any]) -> None:
    for q in _job_subscribers.get(job_id, []):
        q.put_nowait(event)


async def _run_mock_job(job_id: str) -> None:
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
# Health
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/")
async def root() -> dict[str, str]:
    return {"name": "NAV AI Mock MCP", "status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# OAuth — fake but shape-correct
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request) -> dict[str, Any]:
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
# MCP handlers
# ─────────────────────────────────────────────────────────────────────────────


def _tools_list() -> list[dict[str, Any]]:
    """All tools. The launcher tool points at the shell ui:// resource."""
    return [
        {
            "name": "launch_nav_ai",
            "description": "Open the NAV AI launcher in Claude. Shows a UI with three demo workflows.",
            "inputSchema": {"type": "object", "properties": {}, "required": []},
            "_meta": {"ui": {"resourceUri": SHELL_URI}},
        },
        {
            "name": "start_forecast",
            "description": "Start a long-running forecast job. Returns a job ID; live progress is streamed over SSE.",
            "inputSchema": {
                "type": "object",
                "properties": {"region": {"type": "string", "default": "EU"}},
                "required": [],
            },
        },
        {
            "name": "submit_pricing_change",
            "description": "Submit a pricing change for review. Synchronous tool — returns a confirmation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "product": {"type": "string"},
                    "new_price": {"type": "number"},
                },
                "required": ["product", "new_price"],
            },
        },
    ]


def _resource_list(base_url: str) -> list[dict[str, Any]]:
    """The shell resource, with CSP metadata so the iframe can hit our SSE endpoint."""
    return [
        {
            "uri": SHELL_URI,
            "name": "NAV AI Launcher",
            "description": "NAV AI workflow launcher and demo apps.",
            "mimeType": UI_MIME_TYPE,
            "_meta": {
                "ui": {
                    "csp": {
                        "connectDomains": [base_url],
                    },
                    "prefersBorder": True,
                }
            },
        }
    ]


async def _handle_initialize(_params: dict[str, Any]) -> dict[str, Any]:
    return {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
            "resources": {"listChanged": False, "subscribe": False},
        },
        "serverInfo": {"name": "nav-ai-mock", "version": "0.2.0"},
    }


async def _handle_tools_list() -> dict[str, Any]:
    return {"tools": _tools_list()}


async def _handle_resources_list(base_url: str) -> dict[str, Any]:
    return {"resources": _resource_list(base_url)}


async def _handle_resources_read(params: dict[str, Any], base_url: str) -> dict[str, Any]:
    uri = params.get("uri", "")
    if uri != SHELL_URI:
        return {"contents": []}
    return {
        "contents": [
            {
                "uri": SHELL_URI,
                "mimeType": UI_MIME_TYPE,
                "text": _render_shell_html(base_url),
                "_meta": {
                    "ui": {
                        "csp": {"connectDomains": [base_url]},
                        "prefersBorder": True,
                    }
                },
            }
        ]
    }


async def _handle_tools_call(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name", "")
    args = params.get("arguments", {}) or {}

    if name == "launch_nav_ai":
        # This tool's _meta.ui.resourceUri tells the host to render the shell.
        return {
            "content": [{"type": "text", "text": "NAV AI launcher opened."}],
        }

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

    if name == "submit_pricing_change":
        product = args.get("product", "")
        price = args.get("new_price", 0)
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"Pricing change queued: {product} → €{price:.2f}",
                }
            ],
            "structuredContent": {
                "product": product,
                "new_price": price,
                "status": "queued_for_review",
                "ticket": f"PR-{secrets.token_hex(4).upper()}",
            },
        }

    return {
        "content": [{"type": "text", "text": f"Unknown tool: {name}"}],
        "isError": True,
    }


@app.post("/mcp")
async def mcp_endpoint(req: Request) -> Response:
    base_url = str(req.base_url).rstrip("/")
    body = await req.json()
    method = body.get("method", "")
    params = body.get("params", {}) or {}
    req_id = body.get("id")

    try:
        if method == "initialize":
            result = await _handle_initialize(params)
        elif method == "tools/list":
            result = await _handle_tools_list()
        elif method == "tools/call":
            result = await _handle_tools_call(params)
        elif method == "resources/list":
            result = await _handle_resources_list(base_url)
        elif method == "resources/read":
            result = await _handle_resources_read(params, base_url)
        elif method == "notifications/initialized":
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
# Direct iframe endpoints (SSE for live job progress)
# ─────────────────────────────────────────────────────────────────────────────


@app.get("/jobs/{job_id}/events")
async def job_events(job_id: str) -> EventSourceResponse:
    async def stream():
        q: asyncio.Queue = asyncio.Queue()
        _job_subscribers.setdefault(job_id, []).append(q)
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
# UI: single shell HTML with internal routing.
# Uses JSON-RPC over postMessage as MCP Apps spec mandates.
# ─────────────────────────────────────────────────────────────────────────────


def _render_shell_html(base_url: str) -> str:
    """The whole UI lives in one HTML file. All navigation is internal."""
    return r"""<!doctype html><html><head>
<meta charset="utf-8" />
<title>NAV AI</title>
<style>
  :root {
    --bg: var(--color-background-primary, #fafafa);
    --card-bg: var(--color-background-secondary, #fff);
    --text: var(--color-text-primary, #222);
    --muted: var(--color-text-tertiary, #888);
    --border: var(--color-border-primary, #e3e3e3);
    --accent: #5436DA;
  }
  body { font-family: var(--font-sans, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif);
         margin: 0; padding: 16px; background: var(--bg); color: var(--text); }
  h1, h2 { margin: 0 0 12px 0; }
  .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 8px;
          padding: 16px; margin-bottom: 12px; }
  button { background: var(--accent); color: white; border: none; padding: 8px 14px;
           border-radius: 6px; cursor: pointer; font-size: 14px; }
  button:hover { filter: brightness(1.1); }
  button:disabled { background: #999; cursor: not-allowed; }
  input, select { padding: 6px; border: 1px solid var(--border); border-radius: 4px;
                  font-size: 14px; background: var(--card-bg); color: var(--text); }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .progress { height: 10px; background: var(--border); border-radius: 5px; overflow: hidden; }
  .bar { height: 100%; background: var(--accent); width: 0%; transition: width 0.4s; }
  .muted { color: var(--muted); font-size: 13px; }
  pre { background: var(--border); padding: 8px; border-radius: 4px; overflow-x: auto;
        color: var(--text); }
  .nav { margin-bottom: 12px; }
  .nav button { background: none; color: var(--accent); padding: 4px 8px; }
  .hidden { display: none; }
</style>
</head><body>

<div id="view-launcher">
  <h1>NAV AI Launcher</h1>
  <p class="muted">Pick a workflow. Each card opens a view in this same panel.</p>
  <div class="grid">
    <div class="card">
      <h2>📊 Sales Dashboard</h2>
      <p class="muted">Read-only view of regional sales.</p>
      <button onclick="show('dashboard')">Open</button>
    </div>
    <div class="card">
      <h2>💰 Pricing Adjustment</h2>
      <p class="muted">Submit a price change. Calls a tool, result goes to chat.</p>
      <button onclick="show('form')">Open</button>
    </div>
    <div class="card">
      <h2>📈 Forecast Run</h2>
      <p class="muted">Long-running job. Live progress over SSE.</p>
      <button onclick="show('forecast')">Open</button>
    </div>
  </div>
</div>

<div id="view-dashboard" class="hidden">
  <div class="nav"><button onclick="show('launcher')">← Back</button></div>
  <h1>📊 Sales Dashboard</h1>
  <p class="muted">Region-level snapshot.</p>
  <div class="grid">
    <div class="card"><h2>EU</h2><p style="font-size:28px;margin:0">€1.42M</p>
      <p class="muted">+4.2%</p></div>
    <div class="card"><h2>NA</h2><p style="font-size:28px;margin:0">$2.08M</p>
      <p class="muted">+1.7%</p></div>
    <div class="card"><h2>APAC</h2><p style="font-size:28px;margin:0">$0.95M</p>
      <p class="muted">-0.5%</p></div>
  </div>
  <div class="card">
    <h2>Top products</h2>
    <ol><li>Widget Pro — 12,402</li><li>Gadget Lite — 9,118</li><li>Tool X — 6,540</li></ol>
  </div>
</div>

<div id="view-form" class="hidden">
  <div class="nav"><button onclick="show('launcher')">← Back</button></div>
  <h1>💰 Pricing Adjustment</h1>
  <div class="card">
    <p>Submitting calls the <code>submit_pricing_change</code> tool through Claude.</p>
    <label>Product
      <select id="product">
        <option>Widget Pro</option><option>Gadget Lite</option><option>Tool X</option>
      </select>
    </label>
    &nbsp;
    <label>New price (€)
      <input id="price" type="number" value="49.99" step="0.01" />
    </label>
    <p><button id="submit-pricing">Submit</button></p>
    <div id="pricing-result"></div>
  </div>
</div>

<div id="view-forecast" class="hidden">
  <div class="nav"><button onclick="show('launcher')">← Back</button></div>
  <h1>📈 Forecast Run</h1>
  <div class="card">
    <p>Click Start. Tool returns a job ID; iframe subscribes to live SSE updates.</p>
    <label>Region
      <select id="region">
        <option>EU</option><option>NA</option><option>APAC</option>
      </select>
    </label>
    <p><button id="start-forecast">Start forecast</button></p>
    <div id="forecast-status"></div>
  </div>
</div>

<script>
  // ── Configuration injected from the server ─────────────────────────────────
  const BASE_URL = __BASE_URL__;

  // ── View routing (all in one iframe) ───────────────────────────────────────
  function show(name) {
    for (const id of ['launcher', 'dashboard', 'form', 'forecast']) {
      document.getElementById('view-' + id).classList.toggle('hidden', id !== name);
    }
  }

  // ── MCP Apps JSON-RPC over postMessage ─────────────────────────────────────
  // Per SEP-1865, the View is conceptually an MCP client; the Host is the server.
  // Messages flow through window.parent.postMessage and replies come via
  // 'message' events. Requests carry an id; notifications don't.

  let nextId = 1;
  const pending = new Map();

  function sendRequest(method, params) {
    return new Promise((resolve, reject) => {
      const id = nextId++;
      pending.set(id, { resolve, reject });
      window.parent.postMessage({ jsonrpc: '2.0', id, method, params }, '*');
    });
  }

  function sendNotification(method, params) {
    window.parent.postMessage({ jsonrpc: '2.0', method, params }, '*');
  }

  window.addEventListener('message', (event) => {
    const msg = event.data;
    if (!msg || msg.jsonrpc !== '2.0') return;
    if (msg.id != null && pending.has(msg.id)) {
      const { resolve, reject } = pending.get(msg.id);
      pending.delete(msg.id);
      if (msg.error) reject(new Error(msg.error.message || 'RPC error'));
      else resolve(msg.result);
    }
    // We could also handle ui/notifications/* here (tool-input, tool-result,
    // host-context-changed, etc.) but the launcher doesn't need them.
  });

  // ── Lifecycle: ui/initialize handshake ─────────────────────────────────────
  // The View MUST send ui/initialize and wait for the result before
  // making any other requests.

  (async () => {
    try {
      const result = await sendRequest('ui/initialize', {
        protocolVersion: '2025-06-18',
        clientInfo: { name: 'nav-ai-shell', version: '0.2.0' },
        appCapabilities: {
          availableDisplayModes: ['inline', 'fullscreen'],
        },
        capabilities: {},
      });
      // Apply theme variables if the host provided them.
      const vars = result?.hostContext?.styles?.variables;
      if (vars) {
        for (const [k, v] of Object.entries(vars)) {
          if (v) document.documentElement.style.setProperty(k, v);
        }
      }
      sendNotification('ui/notifications/initialized', {});
    } catch (err) {
      console.warn('ui/initialize failed (likely running outside a host):', err);
    }
  })();

  // ── Pricing form: calls a tool via the host ────────────────────────────────
  document.getElementById('submit-pricing').onclick = async () => {
    const product = document.getElementById('product').value;
    const price = parseFloat(document.getElementById('price').value);
    const out = document.getElementById('pricing-result');
    out.innerHTML = '<p class="muted">Calling submit_pricing_change…</p>';
    try {
      const result = await sendRequest('tools/call', {
        name: 'submit_pricing_change',
        arguments: { product, new_price: price },
      });
      const sc = result?.structuredContent || {};
      out.innerHTML =
        '<div class="card" style="background:#e8f5e8;color:#222">' +
        '<strong>✓ Submitted</strong><br>' +
        '<span class="muted">Ticket: ' + (sc.ticket || '?') + '</span>' +
        '</div>';
    } catch (err) {
      out.innerHTML = '<p class="muted">Error: ' + err.message + '</p>';
    }
  };

  // ── Forecast: tool returns job_id, iframe subscribes to SSE directly ───────
  document.getElementById('start-forecast').onclick = async () => {
    const region = document.getElementById('region').value;
    const btn = document.getElementById('start-forecast');
    const out = document.getElementById('forecast-status');
    btn.disabled = true;
    out.innerHTML = '<p class="muted">Starting…</p>';
    try {
      const result = await sendRequest('tools/call', {
        name: 'start_forecast',
        arguments: { region },
      });
      const jobId = result?.structuredContent?.job_id;
      if (!jobId) throw new Error('no job_id returned');
      subscribe(jobId);
    } catch (err) {
      out.innerHTML = '<p class="muted">Error: ' + err.message + '</p>';
      btn.disabled = false;
    }
  };

  function subscribe(jobId) {
    const out = document.getElementById('forecast-status');
    out.innerHTML =
      '<p><strong>Job:</strong> <code>' + jobId + '</code></p>' +
      '<div class="progress"><div class="bar" id="bar"></div></div>' +
      '<p class="muted" id="step">Waiting for first update…</p>' +
      '<pre id="result" style="display:none"></pre>';

    const es = new EventSource(BASE_URL + '/jobs/' + jobId + '/events');
    es.onmessage = (e) => {
      const ev = JSON.parse(e.data);
      if (ev.type === 'progress' || ev.type === 'snapshot') {
        if (ev.progress != null) document.getElementById('bar').style.width = ev.progress + '%';
        if (ev.step) document.getElementById('step').innerText = ev.step;
      } else if (ev.type === 'done') {
        document.getElementById('bar').style.width = '100%';
        document.getElementById('step').innerText = '✓ Complete';
        const r = document.getElementById('result');
        r.style.display = 'block';
        r.innerText = JSON.stringify(ev.result, null, 2);
        document.getElementById('start-forecast').disabled = false;
        es.close();
      }
    };
    es.onerror = () => {
      document.getElementById('step').innerText = 'SSE connection error.';
      document.getElementById('start-forecast').disabled = false;
      es.close();
    };
  }
</script>
</body></html>
""".replace("__BASE_URL__", json.dumps(base_url))


@app.get("/ui/shell", response_class=HTMLResponse)
async def ui_preview(request: Request) -> HTMLResponse:
    """Browser preview of the shell HTML — buttons that call tools won't work
    here (no MCP host listening), but navigation and CSS render correctly."""
    base_url = str(request.base_url).rstrip("/")
    return HTMLResponse(_render_shell_html(base_url))
