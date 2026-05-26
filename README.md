# NAV AI — Mock MCP Apps server

A FastAPI app that implements an [MCP Apps](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/) (SEP-1865) server with an interactive iframe UI, live SSE progress, and a mocked OAuth flow — everything needed to test the MCP Apps surface in Claude end-to-end without standing up real auth or persistence.

## What you get

- `POST /mcp` — JSON-RPC 2.0 MCP endpoint (initialize, tools/*, resources/*, ping).
- `GET  /mcp` — **server → client SSE channel** for JSON-RPC notifications (`notifications/resources/updated`, etc.). Open by the host's MCP client; the server pushes onto it whenever the shell changes.
- `ui://nav-ai/shell` — a polished single-page workspace served as an MCP App UI resource. Five views (launcher, dashboard, pricing form, forecast, catalog) with internal navigation. Adapts to host theme variables.
- `POST /oauth/*` + discovery — mock OAuth 2.1 with Dynamic Client Registration.
- `GET /jobs/{id}/events` — direct SSE channel from the iframe for high-frequency forecast progress, bypassing MCP.
- `GET /shell/events` — second iframe-direct SSE channel for **server-pushed shell mutations** (banner changes etc.).
- `GET /ui/shell` — same HTML in a regular browser tab for visual checks.
- `GET /diagnostics` — **live trace timeline** of every MCP request, tool call, SSE event and postMessage notification, colour-coded by layer. Open this in a second tab while demoing; everything else lights up in real time.
- `GET /admin` — operator console for pushing shell updates (banner) to every connected client.

Tools — 10 total, grouped by role.

### Mounts the iframe

- `launch_nav_ai` — opens the workspace iframe inline (the only tool with `_meta.ui.resourceUri`).

### Workspace actions (called from inside the iframe)

- `submit_pricing_change` — writes a new pending ticket to the shared pricing book and emits a live SSE event.
- `start_forecast` — kicks off a forecast job that automatically factors every currently-pending pricing change as a price-elasticity drag on uplift.
- `lookup_product` — forwards to the backend MCP using the caller's OAuth token; the response includes any pending price changes overlaid on the catalog entry.

### Chat-side workflow tools (the loop Claude drives)

- `list_products` — catalog overview with current prices, pending counts, stock status.
- `list_pending_changes` — every pricing ticket currently awaiting review, with delta and age.
- `approve_pricing_change` — approve a ticket by id; sets the SKU's current price to the new price, removes the ticket from pending, broadcasts an SSE so the open workspace's catalog and dashboard refresh live.
- `reject_pricing_change` — reject a ticket with an optional reason; removes from pending without touching the current price.
- `get_job` — fetch a forecast job by id, including the pricing changes it factored and base-vs-adjusted numbers.
- `simulate_pricing_impact` — what-if: project the marginal uplift drag of a hypothetical re-pricing without persisting anything.

**The chat workflow loop**: `list_pending_changes` → `simulate_pricing_impact` (optional what-if) → `approve_pricing_change` or `reject_pricing_change` → live SSE pushes refresh the catalog/dashboard in any open iframe → `start_forecast` re-runs with the new pending set.

## Project structure

```
nav-mock-mcp/
├── app/
│   ├── __init__.py        # create_app() — wires routers, CORS, /static mount
│   ├── config.py          # BASE_URL, SHELL_URI, paths, constants
│   ├── state.py           # In-memory jobs/subscribers/tokens
│   ├── schemas.py         # TOOLS and RESOURCES (declarative MCP metadata)
│   │
│   ├── mcp/
│   │   ├── router.py      # POST/GET/DELETE /mcp dispatcher
│   │   └── tools.py       # Tool handlers + TOOL_HANDLERS map
│   │
│   ├── oauth/
│   │   └── router.py      # Discovery + DCR + authorize + token
│   │
│   ├── jobs/
│   │   ├── runner.py      # Background forecast pipeline + event emit
│   │   └── sse.py         # GET /jobs/{id}/events
│   │
│   └── ui/
│       ├── router.py      # GET /ui/shell preview
│       ├── render.py      # Jinja2 shell HTML renderer
│       └── templates/
│           └── shell.html
│
├── static/
│   ├── shell.css          # All shell styles
│   └── shell.js           # postMessage RPC + SSE client + view router
│
├── main.py                # `app = create_app()` — entrypoint
├── requirements.txt
├── render.yaml
└── README.md
```

**Adding a new tool**: append a definition to `app/schemas.py:TOOLS`, write a handler in `app/mcp/tools.py`, register it in `TOOL_HANDLERS`. The dispatcher needs no changes.

## Single source of truth — how the views stay in sync

The pricing book is the demo's authoritative state. It lives in **one place only**: [app/backend/pricing.py](app/backend/pricing.py), inside the backend MCP service. Every read and every write — from the iframe, from chat, from anywhere — goes through it.

```text
┌─ FRONTEND MCP (what Claude talks to) ────────────────────────────────┐
│                                                                       │
│  tool handlers (submit / approve / lookup / start_forecast / …)      │
│      │                                                                │
│      ▼                                                                │
│  app/pricing.py  ──── async HTTP client ────────┐                     │
│                                                  │                    │
│  /shell/events ◀── bridge (app/bridge.py) ──────┼─── SSE             │
│                                                  │                    │
└──────────────────────────────────────────────────┼────────────────────┘
                                                   ▼
┌─ BACKEND MCP (the data service) ─────────────────────────────────────┐
│                                                                       │
│  /backend/mcp  ◀── tools/call (submit, approve, list, ...)            │
│      │                                                                │
│      ▼                                                                │
│  app/backend/pricing.py    ← the actual book (single dict)           │
│      │                                                                │
│      └──▶ app/backend/events.py ──▶ /backend/pricing-events (SSE)   │
└───────────────────────────────────────────────────────────────────────┘
```

**Why it works the way it does:**

- The frontend MCP's `pricing` module is a **thin async HTTP client**. Every tool handler that touches pricing — `submit_pricing_change`, `approve_pricing_change`, `lookup_product`, `list_products`, `start_forecast` (which snapshots `all_pending()` and `all_current_drifts()`), and friends — forwards through it to the backend.
- Mutations on the backend publish onto [app/backend/events.py](app/backend/events.py), which the [/backend/pricing-events](app/backend/router.py) SSE endpoint streams to subscribers.
- The frontend runs a **bridge task** ([app/bridge.py](app/bridge.py)) for its entire lifecycle. It subscribes to backend pricing-events and republishes them on `/shell/events` so every open workspace iframe refreshes its catalog / dashboard the moment chat approves a change — and vice versa.
- In **combined-mode** deploys (single process), the HTTP loopback is just a localhost call; the bridge connects to the same service. In **split-mode** deploys, the same code becomes a real cross-service stream. No deploy-mode branching in handlers.

### The two-layer pricing model the forecast applies

When `start_forecast` snapshots pricing state, it captures **two** lists:

- `pending_pricing` — pricing changes submitted but not yet approved. These are future, uncertain. The forecast model treats them as a drag on **uplift** (`Δuplift = –elasticity × Σ Δprice%`).
- `current_drifts` — for any SKU whose current price has moved from its seed (i.e. an approval already landed), the model treats this as a shift in the **baseline** (`Δbaseline = –elasticity × mean Δprice%`).

So approving a change moves its effect from "uplift drag" to "baseline shift" — the price rise is now in effect, demand is permanently lower at that price level. Both are visible on the forecast result panel and in `structuredContent`. `simulate_pricing_impact` runs the same elasticity without persisting anything.

### `/dashboard/snapshot`

Aggregates the backend pricing book + the frontend's running-job state. Dashboard view fetches on mount and re-fetches on every bridged `pricing-event` push.

## SSE through Render's proxy

Render's free-tier reverse proxy buffers SSE responses by default, which made `/diagnostics` and `/shell/events` feel laggy or batched. Every SSE endpoint now sets `X-Accel-Buffering: no` on the response so events stream through immediately. If you're hosting elsewhere and SSE still feels slow, this is the header to check first.

## Three things the demo proves at runtime

These are the moments that distinguish a working MCP Apps implementation from a regular tool-result app. Open `/diagnostics` in a second tab while doing any of them — every layer lights up on the timeline.

### 1. Iframe → host model (bidirectional)

Run a forecast, then click **Send to chat** on the result panel. The iframe calls the MCP Apps host directly via two methods:

- **`updateModelContext`** — pushes the full structured selection (region, baseline_units, uplift_pct, confidence, etc.) into the model's context without crowding the visible chat.
- **`sendMessage`** — injects a short `user`-role trigger (`analyze this forecast`) into the chat thread. The host treats it as if the user typed it, and Claude responds inline.

Same pattern on the pricing receipt (`review this pricing change`) and catalog entry (`summarize this product`). No `tools/call` round-trip via the server — this is iframe ↔ host direct.

**Graceful fallback**: if the host doesn't implement `sendMessage`/`updateModelContext` (older host or a different wire-name), the iframe reveals a chat-prompt hint the user can paste instead. The wire method name is auto-resolved at runtime by trying a couple of candidates (`sendMessage` and `ui/sendMessage`) — whichever succeeds is cached for the session. Both branches are visible on `/diagnostics` as `ui.sendMessage.ok` or `ui.sendMessage.fail`.

### 2. Server-pushed resource updates

Open `/admin` and broadcast a banner. The server (a) sends `notifications/resources/updated` for `ui://nav-ai/shell` over every open `GET /mcp` SSE listener (the spec path — host re-reads the resource) and (b) pushes the same fact straight into every open iframe via `/shell/events` (the always-works path — the banner appears immediately regardless of host behaviour). The header's `rev N` counter ticks up on every update. Capabilities advertised in `initialize`:

```json
"resources": { "listChanged": true, "subscribe": true }
```

### 3. Live cross-layer diagnostics

`/diagnostics` subscribes to an in-process trace bus that every layer publishes onto:

- `mcp` — `POST /mcp` request/response, `GET /mcp` listener open/close, broadcast notifications
- `tool` — each tool handler firing, with `duration_ms`
- `jobs` — forecast pipeline create / progress / done
- `sse` — iframe `/jobs/{id}/events` and `/shell/events` subscribe / push / unsubscribe
- `resource` — `resources/subscribe` and `resources/unsubscribe`
- `admin` — banner mutations
- `ui` — iframe-side notes via `POST /diagnostics/note`

Click a correlation-id chip to highlight all events from the same request or job.

## Run locally

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

Open <http://localhost:8000/ui/shell> for the visual preview. Tool buttons will no-op (no MCP host parent listening) but navigation works.

## Deploy to Render

`render.yaml` declares **two services** — the frontend (what Claude talks to) and the backend (called by `lookup_product`). They can also run as one process if you don't set the cross-service env vars.

1. Push this folder to a GitHub repo.
2. In Render: **New +** → **Blueprint** → pick the repo. Render reads `render.yaml` and provisions both services.
3. After the first deploy completes and Render assigns URLs, set the cross-service env vars (all `sync: false`, so Render asks you to fill them):

   **On `nav-ai-mock-mcp` (frontend):**
   - `BASE_URL` → this service's URL, e.g. `https://nav-ai-mock-mcp.onrender.com`. Required: without it the iframe's `<link>`/`<script>` and SSE calls all point at `localhost` and break inside Claude.
   - `BACKEND_URL` → the backend service's URL, e.g. `https://nav-ai-mock-backend.onrender.com`. Required for split deploys: without it, `lookup_product` calls `/backend/mcp` on the frontend itself (combined-mode default) instead of the backend service.

   **On `nav-ai-mock-backend`:**
   - `FRONTEND_URL` → the frontend service's URL. Required: the backend uses `{FRONTEND_URL}/oauth/introspect` (RFC 7662) to validate bearer tokens. Without it the backend rejects every request, since it has no shared state with the frontend.

4. Trigger redeploys after setting env vars.
5. Your MCP endpoint is `https://<frontend>.onrender.com/mcp`.

### Combined mode (single service)

To run as one process, deploy only the frontend service (delete the backend section from `render.yaml`, or just don't set `BACKEND_URL` / `FRONTEND_URL`). The frontend's own `/backend/mcp` route serves the backend role, and bearer tokens are validated in-memory against `state.issued_tokens`. The whole demo still works — fewer moving parts, but you lose the architectural realism of two separate services.

> Free tier sleeps after 15 min idle; first request takes ~30 sec to wake. With the split deploy you pay this cold start twice (frontend, then backend) on the first `lookup_product` call after idle.

## Connect from Claude

claude.ai → **Settings** → **Connectors** → **Add custom connector**:

- **Name:** NAV AI Mock
- **URL:** `https://<your-app>.onrender.com/mcp` ← don't forget `/mcp`

Click **Connect**. Claude will run the OAuth flow against the mock (which auto-accepts), then the connector is live.

In a new chat, enable the connector and try:

> Launch NAV AI

The launcher iframe should render inline. From there:
- Click **Open pricing**, fill in a product and price, hit Submit → ticket comes back.
- Click **Open forecast**, pick a region, hit Start → progress bar streams over SSE, final result panel renders.

## Architecture sketch

```
┌──────────────────────────────────────────────────────────┐
│  FastAPI app (one process)                               │
│                                                          │
│  ┌────────────┐ ┌────────────┐ ┌─────────┐ ┌──────────┐  │
│  │ MCP        │ │ OAuth      │ │ Jobs    │ │ UI       │  │
│  │ /mcp       │ │ /oauth/*   │ │ /jobs/* │ │ /ui/...  │  │
│  └─────┬──────┘ └────────────┘ └────┬────┘ └────┬─────┘  │
│        │                            │           │        │
│        ▼                            ▼           ▼        │
│   shared state (app/state.py): jobs, subscribers         │
│                                                          │
│   /static/* → shell.css, shell.js                        │
│   templates/shell.html → rendered with base_url          │
└──────────────────────────────────────────────────────────┘
        ▲                              ▲
        │ JSON-RPC (Claude)            │ SSE (iframe → server)
        │                              │
        └───── Claude Desktop ─────────┘
                    │
              iframe (ui://nav-ai/shell)
              postMessage ↔ Claude (JSON-RPC over postMessage)
```

Two communication channels deliberately:

1. **iframe ⇄ Claude (postMessage):** JSON-RPC 2.0. Form submits and forecast-starts go this way so Claude sees them, can comment on results, and keeps the chat thread coherent.
2. **iframe ⇄ server (SSE direct):** for high-frequency progress updates that should not enter the model's context window. Allowed by `_meta.ui.csp.connectDomains` on the UI resource.

## Constraints honored (these bite if you get them wrong)

1. Mime type is exactly `text/html;profile=mcp-app` (no space).
2. postMessage payloads are full JSON-RPC 2.0 shape — `{jsonrpc, id, method, params}`.
3. The iframe sends `ui/initialize` and waits for the result before calling tools.
4. Only `launch_nav_ai` has `_meta.ui.resourceUri`. The other tools are normal.
5. `connectDomains` includes the server's own `BASE_URL` so the iframe can open SSE back to it.
6. `/oauth/authorize` returns HTML with a redirect, not JSON.
7. Shell HTML references `/static/*` via absolute `BASE_URL` so assets resolve inside the host's iframe sandbox.
