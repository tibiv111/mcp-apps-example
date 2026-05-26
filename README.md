# NAV AI Mock MCP

A minimal MCP server with MCP Apps UI, conforming to SEP-1865.

## What's in it

- **Streamable HTTP MCP server** at `/mcp` (JSON-RPC 2.0).
- **Mock OAuth** — discovery + DCR + authorize + token endpoints, any creds accepted.
- **One `ui://` shell resource** with all three demo workflows inside, navigated
  client-side in JS. (Multiple `ui://` resources with cross-resource navigation
  is harder than it sounds — see below.)
- **Three tools:**
  - `launch_nav_ai` — opens the shell UI in Claude.
  - `submit_pricing_change` — synchronous tool called from the form view.
  - `start_forecast` — kicks off a long job, returns an ID; the iframe then
    subscribes to live progress over SSE directly to our backend.

Single file (`server.py`), ~450 lines, FastAPI.

## Why a single shell resource

The MCP Apps spec is clear that tool results return to the *same View* that
made the call, via `ui/notifications/tool-result`. The host does **not** tear
down and re-render the iframe with a different `ui://` resource just because
the tool's `_meta.ui.resourceUri` points elsewhere.

So the natural pattern is:

- One launcher tool with `_meta.ui.resourceUri` pointing at the shell.
- The shell is a small SPA with internal routing.
- All "navigation between apps" happens in JS inside the iframe.
- The shell calls tools (`tools/call` via JSON-RPC postMessage) for actual
  server-side actions and reads structured results back.

## Protocol details (per SEP-1865)

- `mimeType` must be `text/html;profile=mcp-app` exactly.
- View ↔ Host comm is **JSON-RPC 2.0 over `postMessage`**, not a custom shape.
- View must send `ui/initialize`, wait for the result, then
  `ui/notifications/initialized` before doing anything else.
- Resource `_meta.ui.csp.connectDomains` must list any backends the iframe
  fetches/SSEs/WebSockets to. We list our own base URL so the SSE channel works.

## Deploy on Render

1. Push to a Git repo, **New → Blueprint** in Render, point at the repo.
2. `render.yaml` provisions a free Python web service.
3. ~1 minute to live. You get a `https://<name>.onrender.com` URL.
4. Sanity check: `GET https://<your-url>/` returns the health JSON.
   `GET https://<your-url>/ui/shell` renders the HTML in a plain browser —
   note that buttons that call tools **won't work in the browser** because
   there's no MCP host listening for postMessage. Navigation (Open / Back)
   does work since that's pure JS.

## Wire into Claude Desktop

1. Settings → Connectors → **Add custom connector**.
2. URL: `https://<your-url>.onrender.com/mcp`
3. Claude reads OAuth metadata, does DCR, opens a browser tab on
   `/oauth/authorize` (auto-redirects), exchanges the code for a token.
4. In a chat, type "open NAV AI" or "launch nav ai". Claude calls
   `launch_nav_ai`, the host fetches the shell resource, and the iframe
   renders inline.
5. Inside the iframe:
   - **Sales Dashboard** — pure static view.
   - **Pricing Adjustment** — fills a form, submits via `tools/call`, gets
     a structured result back and shows the confirmation.
   - **Forecast Run** — calls `start_forecast` via `tools/call`, gets a job
     ID, opens an SSE stream to `/jobs/<id>/events` and shows live progress.

## Local testing

```bash
pip install -r requirements.txt
uvicorn server:app --reload --port 8000

# In another shell, point the MCP Inspector at it:
npx @modelcontextprotocol/inspector
# Connect to http://localhost:8000/mcp
```

The Inspector lets you exercise tools and `resources/read` without any host
or model — quickest debugging loop.

For end-to-end UI testing including the iframe handshake, the **MCPJam
Inspector** is the only client besides Claude that fully renders MCP Apps:

```bash
npx @mcpjam/inspector
```

## What this isn't

- Production code. No real auth, no persistence, in-memory state.
- A full Streamable HTTP demo — uses plain JSON responses, no streamed tool
  output. The SSE channel covers the streaming case for the demo.
- A solution for Shiny apps in the iframe. R Shiny apps are full SSR
  applications; they don't fit in a `ui://` HTML resource. For real NAV
  workflows, the right pattern is: lightweight shell UI in Claude + deep
  links to the existing Shiny apps in the browser.

## Files

- `server.py` — everything.
- `requirements.txt` — pinned deps.
- `render.yaml` — Render Blueprint config.
- `README.md` — this file.
