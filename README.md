# NAV AI Mock MCP

A minimal MCP server with MCP Apps UI, for trying out Claude Desktop's
inline UI rendering without any Azure/Entra setup.

## What's in it

- **Streamable HTTP MCP server** at `/mcp` (JSON-RPC 2.0).
- **Mock OAuth** (`/.well-known/oauth-authorization-server`, `/oauth/register`,
  `/oauth/authorize`, `/oauth/token`) — any creds accepted, fake tokens minted.
- **Three demo workflows**, each with its own `ui://` resource:
  - `dashboard` — static read-only view.
  - `form` — submit-and-result form.
  - `long_job` — kicks off a job, streams live progress over SSE.
- **A launcher shell** at `ui://nav-ai/shell` — three cards, click to open.
- **Direct iframe → backend channel** via SSE (`/jobs/{id}/events`) for live
  job progress, bypassing MCP notifications. Same pattern you'd use in the
  real NAV gateway.

Single file (`server.py`), ~450 lines, FastAPI.

## Deploy on Render

1. Push this folder to a Git repo (GitHub/GitLab).
2. In Render, **New → Blueprint**, point at the repo. `render.yaml` does the rest.
3. Wait ~1 minute. You'll get a URL like `https://nav-ai-mock-mcp.onrender.com`.
4. Sanity-check: open `https://<your-url>/ui/shell` in a browser. You should see
   the launcher. Open `/ui/dashboard`, `/ui/form`, `/ui/long_job` to preview each.

## Wire into Claude Desktop

1. In Claude Desktop, open **Settings → Connectors → Add custom connector**.
2. Paste your Render URL with `/mcp` appended:
   `https://<your-url>.onrender.com/mcp`
3. Claude Desktop will:
   - Read OAuth metadata from `/.well-known/oauth-authorization-server`.
   - Do Dynamic Client Registration against `/oauth/register`.
   - Open a browser window to `/oauth/authorize` — the mock auto-redirects.
   - Exchange the code for a token at `/oauth/token`.
4. Once connected, in a chat: type `launch nav ai` (or just `@nav-ai-mock`
   from the `+` menu, depending on how the connector named itself).
5. Claude calls `launch_nav_ai`, the shell UI renders inline, click any card.

## Local testing

```bash
pip install -r requirements.txt
uvicorn server:app --reload --port 8000

# In another shell:
npx @modelcontextprotocol/inspector
# Connect to http://localhost:8000/mcp
```

The Inspector lets you list tools, invoke them, and inspect the raw JSON-RPC
without any model in the loop — much faster than iterating against Claude
Desktop.

## What this *is not*

- Production code. No real auth, no persistence, in-memory state only.
- A full demo of Streamable HTTP — uses plain JSON responses, no streaming.
  Good enough for tool calls + ui:// rendering; not enough to demo MCP
  progress notifications (the iframe SSE channel replaces those anyway).
- Domain signing. The `_meta.ui.domain` is computed correctly, but Claude's
  full domain-signing handshake may require additional setup for custom
  connectors in some Claude Enterprise configurations. If the iframe doesn't
  render but the tool call returns text, that's where to look.

## Files

- `server.py` — everything.
- `requirements.txt` — pinned deps.
- `render.yaml` — Render Blueprint config.
- `README.md` — this file.
