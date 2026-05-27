# R Shiny — integration options demo

A standalone R Shiny app plus a comparison page inside the NAV AI mock
MCP shell that demonstrates **four ways** to embed an external Shiny app
into an MCP App resource. The point isn't "this is the right way" — it's
"here are the tradeoffs, see for yourself which one fits your situation."

Open the **R Shiny — integration options** card in the launcher to see
all four side by side. The other four launcher cards (dashboard, pricing,
forecast, catalog) are untouched.

## The four options

| # | Approach | Status in Claude | Status in browser preview | Engineering effort |
| --- | --- | --- | --- | --- |
| A | Direct nested iframe `<iframe src=SHINY_URL>` | Blocked: `Framing '…shiny.onrender.com/' violates frame-src 'self' blob: data:` | Works | Zero |
| B | Open in a new tab via `window.open(SHINY_URL)` | Blocked: `Blocked opening … in a sandboxed frame whose 'allow-popups' permission is not set` | Works | Five lines |
| C | Same-origin reverse proxy at `/shiny-proxy/*` | Still blocked: shell document is loaded under the host's opaque origin, so even our own service URL is cross-origin from inside the shell | Works | ~120 LOC: HTTP forwarding + HTML path rewriting + WebSocket pump. See [app/shiny_proxy.py](../app/shiny_proxy.py). |
| D | URL-form MCP resource via `launch_shiny` tool | Depends on whether the host renders URL-form resources. Today Claude appears to ignore them | n/a | Implemented: new `launch_shiny` tool + `ui://nav-ai/shiny` URL-list resource. See [app/schemas.py](../app/schemas.py), [app/mcp/tools.py](../app/mcp/tools.py), [app/mcp/router.py](../app/mcp/router.py). |

### Why each one exists

- **A** is the obvious wiring. Shows up first because that's where
  everyone starts.
- **B** is the "stop fighting the sandbox" answer.
- **C** is what you reach for when the in-canvas experience is
  non-negotiable. It costs you a bidirectional WebSocket proxy and
  HTML rewriting because `shiny::runApp` emits absolute paths like
  `/shared/shiny.min.js`.
- **D** is the spec-clean answer, deferred to the host.

### Empirical CSP findings

The launcher tab's diagnosis section summarises what we observed. The key
insight is that `frame-src 'self'` in Claude's host CSP does **not** mean
"the MCP server's URL." Claude renders the MCP resource HTML under its
own opaque origin (a blob:/data: URL or an internal `mcp_apps://…`
scheme), so `'self'` is the host's origin — every external URL,
**including our own service**, is cross-origin from the shell's
perspective. That's why Card C (the same-origin reverse proxy) fails
inside Claude: it's only same-origin to *our service*, not to the *shell
document*.

There's a speculative hint at [app/schemas.py](../app/schemas.py)
(`csp.frameDomains`) by analogy with the known `connectDomains` /
`resourceDomains` keys. If a future Claude build honours it, Cards A and
C would unblock without any client-side changes; today it appears to be
silently ignored.

### What Card D actually does

Calling **Call launch_shiny ↗** in the Shiny tab issues a real
`tools/call launch_shiny`, then `resources/read ui://nav-ai/shiny`. The
server responds with:

```json
{
  "contents": [{
    "uri": "ui://nav-ai/shiny",
    "mimeType": "text/uri-list;profile=mcp-app",
    "text": "https://nav-ai-mock-shiny.onrender.com\n",
    "_meta": {
      "ui": {
        "externalUrl": "https://nav-ai-mock-shiny.onrender.com",
        "csp": { "frameDomains": ["https://nav-ai-mock-shiny.onrender.com"] },
        "prefersBorder": true
      }
    }
  }]
}
```

The card's result panel shows exactly this payload so you can verify the
wire format. Whether the host then opens its own iframe at
`externalUrl` is up to the host — watch [/diagnostics](../app/diagnostics)
after clicking the button to see if a follow-up `resources/read` or
iframe load happens.

## What the Shiny app shows

The same one Shiny app backs all of A, B, C — only the *embedding* differs.
It renders:

- Live counts (products, in/out of stock, pending pricing changes, jobs).
- Bar chart of delta-% for the most recent pending pricing changes.
- Tables of recent pending pricing changes and recent forecast jobs.
- Re-pulls every 3 seconds, so an approve/submit in chat or in any of
  the Python views surfaces in Shiny within a tick.

### How it talks to NAV AI

It calls `GET /dashboard/snapshot` on the frontend MCP service — the same
unauthenticated read endpoint the Python dashboard uses. No bearer token,
no MCP handshake. The Shiny **server** process makes the HTTP call (not
the user's browser), so CORS is not in play.

## Run locally

Requires R (4.4+) with the following packages:

```r
install.packages(c("shiny", "httr2", "jsonlite", "ggplot2"))
```

From the repo root, run uvicorn and the Shiny app side by side:

```bash
# Terminal 1
uvicorn main:app --reload --port 8000

# Terminal 2
Rscript -e "shiny::runApp('shiny', port=3838, host='127.0.0.1')"
```

Open `http://localhost:8000/ui/shell` → click **Compare options →**.
Cards A and C should both load Shiny (since localhost lifts the CSP);
card B opens it in a new tab.

Point at a remote NAV AI instance:

```bash
NAV_AI_URL=https://nav-ai-mock-mcp.onrender.com \
  Rscript -e "shiny::runApp('shiny', port=3838, host='127.0.0.1')"
```

## Deploy to Render

The Shiny service is wired into the existing `render.yaml` as a
Docker-based web service. On a fresh deploy:

1. `git push` — Render builds all three services from `render.yaml`.
2. Once the deploys settle, set the cross-references:

   | Service               | Env var       | Value (example)                              |
   |-----------------------|---------------|----------------------------------------------|
   | `nav-ai-mock-mcp`     | `BASE_URL`    | `https://nav-ai-mock-mcp.onrender.com`       |
   | `nav-ai-mock-mcp`     | `BACKEND_URL` | `https://nav-ai-mock-backend.onrender.com`   |
   | `nav-ai-mock-mcp`     | `SHINY_URL`   | `https://nav-ai-mock-shiny.onrender.com`     |
   | `nav-ai-mock-backend` | `FRONTEND_URL`| `https://nav-ai-mock-mcp.onrender.com`       |
   | `nav-ai-mock-shiny`   | `NAV_AI_URL`  | `https://nav-ai-mock-mcp.onrender.com`       |

3. Manually trigger a redeploy on each service after setting env vars.

Build performance: the Docker image pulls R binaries from P3M rather than
compiling from source, so the build runs in ~2–3 minutes on Render's
free tier. Cold-start after idle is 30–60s because R itself takes a few
seconds to boot.

## Caveats

- **Free Render cold-start** dominates first-load latency for cards A
  and C; refresh once to get a warm response.
- **Reverse-proxy fidelity**: the HTML rewriter in
  [app/shiny_proxy.py](../app/shiny_proxy.py) covers Shiny's standard
  `href="/…"` / `src="/…"` / `url(/…)` patterns. If you mount custom
  htmlwidgets that emit other absolute URLs, you may need to extend the
  rewrite rules.
- **WebSocket fallback**: the proxy pumps a real WebSocket upgrade. If
  your network drops WebSockets, Shiny will fall back to SockJS
  long-polling, which the HTTP arm of the proxy already handles.
