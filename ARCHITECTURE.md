# NAV AI Mock MCP — Architectural Overview

A reference / demo implementation of an **MCP Apps** (SEP-1865) server with an interactive iframe UI, live SSE progress streams, mocked OAuth, and a peer R Shiny dashboard. Intended to exercise every surface of the MCP Apps spec end-to-end without standing up real auth or persistence.

> This document is written to be fed to a diagramming tool. It lists every component, every channel between components, and every protocol used on each channel.

---

## 1. Deployment topology (three processes)

The project deploys as **three independent services** (combined-mode collapses #1 and #2 into one process):

| # | Service | Runtime | Entrypoint | Default Port | Role |
|---|---------|---------|------------|--------------|------|
| 1 | `nav-ai-mock-mcp` (Frontend MCP) | Python / FastAPI | `main.py` → `app.create_app()` | 8000 | The MCP server Claude actually connects to. Owns iframe UI, OAuth, jobs, admin, diagnostics, bridge. |
| 2 | `nav-ai-mock-backend` (Backend MCP) | Python / FastAPI | `backend_main.py` → `app.backend.create_backend_app()` | 8001 | Owns the **pricing book** (single source of truth). Called server-to-server by the frontend's `lookup_product` etc. |
| 3 | `nav-ai-mock-shiny` (Shiny app) | R / Docker | `shiny/app.R` | 3838 | Peer dashboard, iframed by the frontend's "Shiny" launcher tab. Polls `/dashboard/snapshot`. |

External actors:

- **Claude (MCP host)** — JSON-RPC over HTTP to `POST /mcp`, plus server→client SSE on `GET /mcp`.
- **MCP App iframe** (rendered by Claude inside the chat) — postMessage RPC with Claude, direct SSE to the frontend.
- **Operator** — uses `/admin` console and `/diagnostics` console in a regular browser tab.

---

## 2. Frontend MCP — internal modules

Wired by `app/__init__.py:create_app()`. Each module is a FastAPI `APIRouter`.

```
app/
├── __init__.py        create_app() — wires routers, CORS, /static, lifespan(bridge)
├── config.py          BASE_URL, BACKEND_URL, FRONTEND_URL, SHINY_URL, URIs, MIME types
├── state.py           In-memory: jobs, subscribers (mcp/shell/bus), issued_tokens, shell_state
├── schemas.py         Declarative TOOLS + RESOURCES metadata for MCP
├── bridge.py          Long-lived task: SSE-subscribes backend pricing-events → republishes to iframe SSE
├── bus.py             ResultsBus: pub/sub relay for cross-iframe communication (peer ↔ peer via server)
├── trace.py           In-process trace bus (every layer publishes here for /diagnostics)
├── pricing.py         Thin async HTTP client → backend (used by frontend tool handlers)
├── shiny_proxy.py     Same-origin reverse proxy in front of standalone Shiny service
├── shiny_mcp.py       Second MCP server endpoint at /shiny-mcp (peer to /mcp)
│
├── mcp/
│   ├── router.py      POST/GET/DELETE /mcp dispatcher (JSON-RPC 2.0)
│   └── tools.py       10 tool handlers + TOOL_HANDLERS map
│
├── oauth/
│   └── router.py      OAuth 2.1 discovery + DCR + /authorize + /token + /introspect (RFC 7662)
│
├── jobs/
│   ├── runner.py      Background forecast pipeline (elasticity model, emits trace events)
│   └── sse.py         GET /jobs/{id}/events — direct iframe SSE for forecast progress
│
├── ui/
│   ├── router.py      GET /ui/shell preview + GET /shell/events (iframe-direct SSE)
│   ├── render.py      Jinja2 renderer for shell HTML
│   └── templates/shell.html
│
├── admin/router.py    POST /admin/banner — pushes shell mutations (banner) to all clients
├── diagnostics/router.py  GET /diagnostics console + SSE feed of the trace bus
└── backend/           Backend MCP — see §3
```

Static assets served at `/static`:

- `shell.css`, `shell.js` — the workspace iframe UI (postMessage RPC + SSE client + view router)
- `mcp-app-handshake.js`, `shiny-embed-shim.js` — injected by `shiny_proxy.py` into proxied Shiny HTML
- `diagnostics.css`, `diagnostics.js` — diagnostics console

---

## 3. Backend MCP — internal modules

Built by `app/backend/__init__.py:create_backend_app()`. Mounted under `/backend/*`.

```
app/backend/
├── __init__.py        create_backend_app() — standalone FastAPI build
├── router.py          POST /backend/mcp dispatcher + GET /backend/pricing-events SSE
├── pricing.py         THE PRICING BOOK — single dict, sole source of truth
├── events.py          In-process event bus that pricing.py publishes mutations onto
└── data.py            Hardcoded CATALOG (SKU → name, current_price, stock, ...)
```

Tools exposed by the backend MCP: `submit_pricing_change`, `approve_pricing_change`, `reject_pricing_change`, `list_pending_changes`, `lookup_product`, plus list helpers. Authenticates incoming bearer tokens by:

- **Combined mode** (no `FRONTEND_URL` env): in-process check against `state.issued_tokens`.
- **Split mode** (`FRONTEND_URL` set): RFC 7662 `POST {FRONTEND_URL}/oauth/introspect`.

---

## 4. The MCP tool surface (10 tools)

Defined in [`app/schemas.py`](app/schemas.py), handled in [`app/mcp/tools.py`](app/mcp/tools.py).

| Tool | Mounts iframe? | Calls backend? | Purpose |
|------|----------------|----------------|---------|
| `launch_nav_ai` | **Yes** — has `_meta.ui.resourceUri` | No | Opens the workspace iframe inline |
| `submit_pricing_change` | No | Yes | Writes a pending pricing ticket |
| `start_forecast` | No | Yes (snapshot) | Kicks off background forecast job |
| `lookup_product` | No | Yes | Forwards to backend with caller's OAuth token |
| `list_products` | No | Yes | Catalog overview |
| `list_pending_changes` | No | Yes | All tickets awaiting review |
| `approve_pricing_change` | No | Yes | Approve ticket → backend emits pricing-event → bridge → iframes refresh |
| `reject_pricing_change` | No | Yes | Reject ticket |
| `get_job` | No | No | Fetch forecast job result |
| `simulate_pricing_impact` | No | Yes (snapshot only) | What-if elasticity without persisting |

---

## 5. Communication channels (the diagram payload)

There are **seven** distinct channels in this system. The whole architecture is about which channel each piece of state travels on.

### Channel A — Claude ↔ Frontend MCP (JSON-RPC)
- **Direction:** Claude → server (`POST /mcp`), Claude ← server (`GET /mcp` SSE).
- **Protocol:** JSON-RPC 2.0 over HTTP, plus server-sent notifications (`notifications/resources/updated`, etc.) over an SSE channel.
- **Carries:** `initialize`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `resources/subscribe`, `ping`.

### Channel B — Claude ↔ Iframe (postMessage)
- **Direction:** Bidirectional, inside the browser.
- **Protocol:** JSON-RPC 2.0 wrapped in `window.postMessage`.
- **Carries:** `ui/initialize` handshake (iframe → host first), `tools/call` from iframe (e.g. Submit Pricing button), `updateModelContext` + `sendMessage` (iframe pushes data and chat triggers into the conversation).

### Channel C — Iframe ↔ Frontend MCP (direct SSE, bypasses model)
Allowed via `_meta.ui.csp.connectDomains` on the UI resource.

- **C1 — `GET /jobs/{id}/events`:** per-job forecast progress (high-frequency, not for the model's context window).
- **C2 — `GET /shell/events`:** server-pushed shell mutations (banner changes, pricing approvals bridged from backend).
- **C3 — `GET /bus/subscribe?topic=…`** and **`POST /bus/publish`:** ResultsBus for peer iframe ↔ peer iframe communication via the server (e.g. NAV AI hub delegating to Shiny iframe).

### Channel D — Frontend MCP → Backend MCP (HTTP / server-to-server)
- **D1 — `POST /backend/mcp`:** the frontend's `pricing` module + tool handlers call the backend's MCP tools, forwarding the caller's `Authorization: Bearer …` header.
- **D2 — `GET /backend/pricing-events` (SSE):** the **bridge** task in `app/bridge.py` opens this for the entire process lifetime and republishes events onto Channel C2.

### Channel E — Backend MCP → Frontend OAuth (split-mode only)
- **Direction:** Backend → frontend.
- **Protocol:** RFC 7662 token introspection — `POST {FRONTEND_URL}/oauth/introspect`.
- **Why:** in split deploys the backend has no shared in-process state with the frontend, so it has to ask whether a bearer token is valid.

### Channel F — Shiny → Frontend MCP (HTTP polling)
- **Direction:** Shiny → frontend.
- **Protocol:** HTTP GET to `/dashboard/snapshot` every 3 seconds (unauthenticated; aggregates backend pricing book + frontend job state).

### Channel G — Shiny iframe paths (three variants, all distinct architecturally)
The "Shiny" launcher tab demos three different ways to embed Shiny. They share Shiny as the target but use different protocol surfaces:

- **G1 — Direct iframe to `SHINY_URL`:** browser loads Shiny directly. Requires permissive host CSP. (Launcher card C/D)
- **G2 — Reverse-proxied via `/shiny/*`:** `app/shiny_proxy.py` proxies HTTP and WebSocket through the frontend so it's same-origin from the iframe's perspective.
- **G3 — Inline-HTML MCP resource (`ui://nav-ai/shiny-embedded`):** server fetches Shiny's HTML, rewrites paths through the reverse proxy, injects a WebSocket constructor shim (`static/shiny-embed-shim.js`), and serves the result with the MCP App MIME. This is the path expected to work inside today's Claude host.
- **G4 — Peer MCP server at `/shiny-mcp`:** an entirely separate MCP server endpoint mounted on the same process. The user adds `/shiny-mcp` as a second connector in their Claude config; Claude mounts it in its own iframe alongside the main `/mcp` workspace. (Launcher card F)

---

## 6. The data flow that ties it together: a pricing approval

This is the canonical end-to-end flow the demo proves out. It exercises Channels A, B, C2, D1, D2 in a single round-trip.

```
1. User in chat: "approve ticket T-123"
2. Claude calls Frontend MCP: POST /mcp { tools/call: approve_pricing_change }       (Channel A)
3. Tool handler → app/pricing.py → POST /backend/mcp { tools/call: approve }         (Channel D1)
4. Backend mutates the pricing book (app/backend/pricing.py) and publishes onto
   app/backend/events.py
5. /backend/pricing-events SSE emits 'pricing-event'                                 (Channel D2)
6. Frontend bridge task (app/bridge.py) consumes that event and pushes it into
   state.shell_event_subscribers
7. Every open iframe's GET /shell/events SSE delivers it                             (Channel C2)
8. shell.js handles 'pricing-event' and re-fetches catalog/dashboard
9. In parallel, the MCP dispatcher broadcasts notifications/resources/updated for
   ui://nav-ai/shell to every GET /mcp listener                                      (Channel A, back)
10. Claude re-reads the resource if its host honours subscriptions
```

---

## 7. State (in `app/state.py`)

All process-local, in-memory. Swap for Redis / Postgres in production.

| Variable | Type | Purpose |
|----------|------|---------|
| `jobs` | `dict[job_id, JobDict]` | Forecast job records (status, progress, result) |
| `job_subscribers` | `dict[job_id, list[Queue]]` | Per-job SSE subscriber queues (Channel C1) |
| `mcp_subscribers` | `list[Queue]` | Open `GET /mcp` SSE listeners (Channel A back) |
| `shell_event_subscribers` | `list[Queue]` | Open `/shell/events` SSE listeners (Channel C2) |
| `bus_subscribers` | `dict[topic, list[Queue]]` | ResultsBus subscribers (Channel C3) |
| `issued_tokens` | `set[str]` | OAuth bearer tokens (combined-mode auth) |
| `shell_state` | `dict` | `banner`, `revision` — mutated by `/admin/*` |

The pricing book itself lives in **`app/backend/pricing.py`**, not in `state.py`, because it belongs to the backend service.

---

## 8. URIs and MIME types (the MCP Apps wire details)

| URI | MIME | Owner | Purpose |
|-----|------|-------|---------|
| `ui://nav-ai/shell` | `text/html;profile=mcp-app` | Frontend `/mcp` | Main workspace iframe — five views |
| `ui://nav-ai/shiny` | `text/uri-list;profile=mcp-app` | Frontend `/mcp` | URL-form resource pointing at `SHINY_URL` (host opens its own iframe) |
| `ui://nav-ai/shiny-embedded` | `text/html;profile=mcp-app` | Frontend `/mcp` | Inline Shiny HTML with proxied paths + WS shim |
| `ui://shiny/dashboard` | `text/html;profile=mcp-app` | Frontend `/shiny-mcp` | Standalone Shiny dashboard exposed as a peer MCP server |
| `ui://shiny/hello` | `text/html;profile=mcp-app` | Frontend `/shiny-mcp` | Hello-world peer MCP example |

OAuth endpoints (mounted by `app/oauth/router.py`): `/oauth/.well-known/oauth-authorization-server`, `/oauth/register` (DCR), `/oauth/authorize`, `/oauth/token`, `/oauth/introspect` (RFC 7662, used by backend in split mode).

---

## 9. Configuration knobs that change topology

From `app/config.py`. These three env vars are what switch combined-mode vs split-mode deploy.

| Env var | Effect when SET | Effect when UNSET |
|---------|-----------------|-------------------|
| `BASE_URL` | Iframe SSE callbacks + asset URLs point at the deployed host | Defaults to `http://localhost:8000` — breaks inside Claude |
| `BACKEND_URL` | Frontend's `pricing` client + bridge target the separate backend service | Defaults to `BASE_URL` — combined-mode (backend mounted in same process) |
| `FRONTEND_URL` | Backend validates bearer tokens via `POST {FRONTEND_URL}/oauth/introspect` | Backend uses in-process `state.issued_tokens` — combined-mode |
| `SHINY_URL` | Shiny launcher tab iframes / proxies this URL | Tab shows a "not configured" placeholder |

---

## 10. Suggested diagram layout

For a single architectural diagram, the most informative layout is **three swim lanes** (External / Frontend Process / Backend Process), with the seven channels drawn between them:

- **Top lane (External):** Claude (MCP host), MCP App iframe, Operator browser tab.
- **Middle lane (Frontend MCP process):** the routers (`/mcp`, `/oauth`, `/shell/events`, `/jobs`, `/bus`, `/admin`, `/diagnostics`, `/shiny-mcp`, `/shiny` proxy), the `bridge` task, in-memory `state`.
- **Bottom lane (Backend MCP process):** `/backend/mcp`, `/backend/pricing-events`, the pricing book.
- **Side panel (third process):** Shiny app, with arrows for G1/G2/G3/G4 and the F polling arrow back to `/dashboard/snapshot`.

Use distinct line styles per channel type:

- **Solid arrow** = synchronous HTTP request/response.
- **Dashed arrow** = SSE stream (label with the event name).
- **Dotted arrow** = `postMessage` (browser-internal).
- **Double arrow** = bidirectional.

Color-code by layer (matches `/diagnostics`):

- `mcp` (Channel A) — blue
- `tool` — purple
- `sse` (Channels C, D2) — green
- `jobs` (Channel C1) — orange
- `resource` — pink
- `admin` — red
- `ui` (Channel B) — gold
