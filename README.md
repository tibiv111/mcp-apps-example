# NAV AI — Mock MCP Apps server

A single-file FastAPI app that implements an [MCP Apps](https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/) (SEP-1865) server with an interactive iframe UI, live SSE progress, and a mocked OAuth flow — everything needed to test the MCP Apps surface in Claude end-to-end without standing up real auth or persistence.

## What you get

- `POST /mcp` — JSON-RPC 2.0 MCP endpoint (initialize, tools/*, resources/*, ping).
- `ui://nav-ai/shell` — a polished single-page workspace served as an MCP App UI resource. Four views (launcher, dashboard, pricing form, forecast) with internal navigation. Adapts to host theme variables.
- `POST /oauth/*` + discovery — mock OAuth 2.1 with Dynamic Client Registration.
- `GET /jobs/{id}/events` — direct SSE channel from the iframe for high-frequency progress, bypassing MCP.
- `GET /ui/shell` — same HTML in a regular browser tab for visual checks.

Three tools:

| Tool | UI? | Purpose |
|---|---|---|
| `launch_nav_ai` | ✓ | Opens the workspace iframe |
| `submit_pricing_change` | – | Called from the iframe's form view |
| `start_forecast` | – | Called from the iframe; spawns a background job |

## Run locally

```bash
pip install -r requirements.txt
uvicorn server:app --reload --port 8000
```

Open http://localhost:8000/ui/shell for the visual preview. Tool buttons will no-op (no MCP host parent listening) but navigation works.

## Deploy to Render

1. Push this folder to a GitHub repo.
2. In Render: **New +** → **Blueprint** → pick the repo. Render reads `render.yaml`.
3. (Optional) Set the `BASE_URL` env var on the service to your Render URL once it's assigned, e.g. `https://nav-mock-mcp.onrender.com`. Without this, the iframe's SSE calls will use `localhost` and break in Claude.
4. Wait for deploy. Your MCP endpoint is `https://<your-app>.onrender.com/mcp`.

> Free tier sleeps after 15 min idle; first request takes ~30 sec to wake.

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
┌─────────────────────────────────────────────────┐
│  FastAPI app (one process)                      │
│                                                 │
│  ┌────────────┐ ┌────────────┐ ┌─────────────┐  │
│  │ MCP        │ │ OAuth      │ │ Direct      │  │
│  │ /mcp       │ │ /oauth/*   │ │ /jobs/*     │  │
│  └─────┬──────┘ └────────────┘ └──────┬──────┘  │
│        │                              │         │
│        ▼                              ▼         │
│   In-memory: jobs dict, SSE subscriber queues   │
│                                                 │
│   Shell HTML (inlined CSS + JS, one function)   │
└─────────────────────────────────────────────────┘
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

## File layout

```
nav-mock-mcp/
├── server.py          # FastAPI app, MCP handlers, OAuth, shell HTML
├── requirements.txt   # fastapi, uvicorn, sse-starlette
├── render.yaml        # Render Blueprint
└── README.md          # this file
```
