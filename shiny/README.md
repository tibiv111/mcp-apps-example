# R Shiny experimental view

A standalone R Shiny app embedded into the NAV AI mock MCP shell as a
nested iframe, to spike out what's possible when reusing existing Shiny
UIs alongside MCP Apps. Nothing in the rest of the project depends on it —
if the Shiny server isn't running, the "Shiny" launcher tab just shows a
placeholder and the four original views continue to work.

## What it shows

- Live counts (products, in/out of stock, pending pricing changes, jobs).
- Bar chart of delta-% for the most recent pending pricing changes.
- Tables of recent pending pricing changes and recent forecast jobs.
- Re-pulls every 3 seconds, so an approve/submit in chat or in any of the
  Python views surfaces in Shiny within a tick.

## How it talks to NAV AI

It calls `GET /dashboard/snapshot` on the **frontend** MCP service —
the same unauthenticated read endpoint the Python dashboard uses. No
bearer token, no MCP handshake. The Shiny **server** process makes the
HTTP call (not the user's browser), so CORS is not in play.

For a richer demo you could swap the poller for an SSE consumer against
`/backend/pricing-events` using `httr2::req_perform_stream()` and a
`reactiveVal` push pattern; this spike sticks with polling for clarity.

## Run locally

Requires R (4.4+) with the following packages:

```r
install.packages(c("shiny", "httr2", "jsonlite", "ggplot2"))
```

From the repo root:

```bash
Rscript -e "shiny::runApp('shiny', port=3838, host='127.0.0.1')"
```

The MCP shell's `SHINY_URL` defaults to `http://localhost:3838`, so the
"Shiny" launcher tab will iframe it automatically when uvicorn is
running on port 8000.

Point at a remote NAV AI instance:

```bash
NAV_AI_URL=https://nav-ai-mock-mcp.onrender.com \
  Rscript -e "shiny::runApp('shiny', port=3838, host='127.0.0.1')"
```

## Deploy to Render

The Shiny service is wired into the existing `render.yaml` as a
Docker-based web service (Render has no native R runtime). On a fresh
deploy:

1. `git push` — Render builds all three services from `render.yaml`.
2. Once the deploys settle, set the cross-references in each service's
   dashboard:

   | Service               | Env var       | Value (example)                              |
   |-----------------------|---------------|----------------------------------------------|
   | `nav-ai-mock-mcp`     | `BASE_URL`    | `https://nav-ai-mock-mcp.onrender.com`       |
   | `nav-ai-mock-mcp`     | `BACKEND_URL` | `https://nav-ai-mock-backend.onrender.com`   |
   | `nav-ai-mock-mcp`     | `SHINY_URL`   | `https://nav-ai-mock-shiny.onrender.com`     |
   | `nav-ai-mock-backend` | `FRONTEND_URL`| `https://nav-ai-mock-mcp.onrender.com`       |
   | `nav-ai-mock-shiny`   | `NAV_AI_URL`  | `https://nav-ai-mock-mcp.onrender.com`       |

3. Manually trigger a redeploy on each service after setting its env
   vars (Render only rebuilds the affected service).

Build performance: the Docker image pulls R binaries from P3M (Posit's
package manager) rather than compiling from source, so the build runs
in ~2–3 minutes on Render's free tier. Cold-start after idle is 30–60s
because R itself takes a few seconds to boot — fine for a demo, painful
in a live walkthrough.

## Iframe caveats

The shell embeds Shiny inside an iframe whose parent is itself an iframe
when the MCP host renders the resource — that's the "iframe-in-iframe"
pattern. Shiny's websocket runs fine through nested frames, and
`shiny::runApp` doesn't send `X-Frame-Options` by default, so embedding
works without extra config. If you later move to Shiny Server / Posit
Connect you'll need to allow framing in their configs.
