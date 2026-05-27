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
| D | URL-form MCP resource via `launch_shiny` tool | Rejected outright. Calling the tool from chat surfaces: `Unsupported UI resource content format: {…mimeType:"text/uri-list;profile=mcp-app"…}` | n/a | Implemented: new `launch_shiny` tool + `ui://nav-ai/shiny` URL-list resource. See [app/schemas.py](../app/schemas.py), [app/mcp/tools.py](../app/mcp/tools.py), [app/mcp/router.py](../app/mcp/router.py). |
| E | Server-side embed via `launch_shiny_embedded` tool, rendered in-shell via `iframe.srcdoc` | Shell renders (path rewrite + srcdoc trick slip past `frame-src 'self'`), but every reactive output stayed empty. Console: `Connecting to 'wss://…/shiny-proxy/websocket/' violates connect-src 'self' https://nav-ai-mock-mcp.onrender.com`. Fix: add the `wss://` form to `connectDomains` explicitly (CSP3 scheme flex isn't honoured in practice). After redeploy, expected to render with live data. | Works | Implemented: ~80 LOC of HTML rewrite + WebSocket-constructor shim + fetch/XHR interceptors in `fetch_embedded_html`. See [app/shiny_proxy.py](../app/shiny_proxy.py) and the `ui://nav-ai/shiny-embedded` resource in [app/schemas.py](../app/schemas.py). |
| F | Expose Shiny as a separate MCP server at `/shiny-mcp` — peer to NAV AI rather than nested inside its shell | Architecturally cleanest: each MCP server gets its own iframe slot in the host. The path rewriter + WS shim still do their work inside the resource, but the srcdoc workaround isn't needed because the host mounts the second server's resource as a fresh iframe naturally. Requires the user to add `https://…/shiny-mcp` as a separate connector in Claude. | Works (server endpoint live; render depends on Claude config) | Implemented: ~150 LOC standalone MCP dispatcher in [app/shiny_mcp.py](../app/shiny_mcp.py) reusing `fetch_embedded_html`. |

### Why each one exists

- **A** is the obvious wiring. Shows up first because that's where
  everyone starts.
- **B** is the "stop fighting the sandbox" answer.
- **C** is what you reach for when the in-canvas experience is
  non-negotiable but you control where the shell loads from. Costs a
  bidirectional WebSocket proxy and HTML rewriting because
  `shiny::runApp` emits absolute paths like `/shared/shiny.min.js`.
- **D** is the spec-clean answer, deferred to the host.
- **E** is what's left when you need Shiny to render *today* inside
  Claude *and* you only want one MCP server connected: don't iframe
  Shiny — serve Shiny's HTML *as* the MCP resource body. The shell's
  CSP allows the host's iframe to load scripts/styles and open
  WebSockets to our origin (the `connectDomains` / `resourceDomains`
  hints make that explicit, with both `https://` and `wss://` schemes
  listed because browsers don't auto-extend), so once paths are
  rewritten and the WS URL is shimmed, Shiny runs inside the
  inline-HTML iframe just like the existing shell.
- **F** is the architecturally honest answer: Shiny is its own thing,
  not a sub-view of NAV AI. Expose it as its own MCP server endpoint
  and let Claude compose multiple servers. The path rewriting still
  happens (it's inherent to embedding Shiny anywhere it doesn't own
  origin root) but the cross-resource workarounds drop away.

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

### What Card E actually does

Calling **Call launch_shiny_embedded ↗** in the Shiny tab issues a real
`tools/call launch_shiny_embedded`. The host follows up with
`resources/read ui://nav-ai/shiny-embedded`, and the server:

1. Fetches Shiny's root HTML from the standalone Shiny service.
2. **Rewrites every URL** (absolute-path *and* relative) in `src=`,
   `href=`, `action=`, and CSS `url(...)` references to absolute URLs
   through `<BASE_URL>/shiny-proxy/`. Relative paths like
   `jquery-3.6.0/jquery.min.js` are common in Shiny's output and
   would otherwise resolve against the parent shell's origin.
3. Injects a JS shim that runs before any of Shiny's scripts:
   - Monkey-patches `window.WebSocket` to force every connection at
     `<BASE_URL>/shiny-proxy/websocket/`. Shiny's URL builder uses
     `location.host` which is empty in a srcdoc iframe.
   - Wraps `window.fetch` and `XMLHttpRequest.prototype.open` to
     rewrite URLs at request time, catching the session-relative
     URLs Shiny generates at runtime.

The returned resource has `mimeType: text/html;profile=mcp-app` — the
same as the NAV AI shell. **Empirically, today's Claude accepts the
fetch but doesn't mount it as a second iframe** (the shell is already
mounted; there's no host convention for "open this new UI resource
alongside the existing one"). To still make the demo visible, the
shell-side handler assigns the resource body to the existing
`<iframe id="shiny-embed-iframe">`'s `srcdoc` attribute. `srcdoc`
isn't a navigation, so the host's `frame-src 'self'` doesn't gate it
the way `src=` is gated. The iframe renders the embedded HTML in
place, Shiny's JS runs inside it, and the shim plus path rewriting
route every asset, WS, and XHR back through our proxy. The visible
"Shiny inside Claude" demo lives here.

### Empirical CSP findings (Card E iteration)

A first attempt at Card E used `<base href>` to anchor Shiny's relative
URLs. The host's CSP also enforces `base-uri 'self'`, which rejects any
`<base>` outside the parent's origin:

```text
Setting the document's base URI to 'https://.../shiny-proxy/' violates
the following Content Security Policy directive: "base-uri 'self'".
The action has been blocked.
```

Without `<base>`, relative paths in the srcdoc iframe resolve to the
host's content origin (`*.claudemcpcontent.com`), which 404s with
`text/plain` MIME (and Chrome's strict MIME checking then refuses to
execute the 404 body as JS). The fix is to never rely on `<base>` —
rewrite every URL to an absolute form server-side, plus intercept
fetch/XHR at runtime for URLs Shiny generates dynamically.

### What Card F actually does

A standalone MCP server endpoint lives at `BASE_URL/shiny-mcp`,
implemented in [app/shiny_mcp.py](../app/shiny_mcp.py). It exposes:

- One tool: `launch_shiny_dashboard`
- One resource: `ui://shiny/dashboard` (`mimeType: text/html;profile=mcp-app`,
  body = the same rewritten Shiny HTML the Card E resource serves).

To use it, the user adds `https://nav-ai-mock-mcp.onrender.com/shiny-mcp`
as a *second* MCP connector in their Claude config (alongside the
existing `nav-ai-mock-mcp` one). In chat, "open the Shiny dashboard"
prompts Claude to invoke `launch_shiny_dashboard` on the new server.
The host should mount the dashboard in its own iframe, peer to the
NAV AI workspace — no srcdoc workaround needed, because mounting the
first UI resource from a fresh server is the supported path.

The Shiny rewrite + WebSocket shim machinery is reused as-is. The
only new code is the MCP dispatcher boilerplate.

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
