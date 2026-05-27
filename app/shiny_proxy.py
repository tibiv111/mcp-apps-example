"""
Same-origin reverse proxy for the standalone R Shiny service.

Claude's MCP host wraps every resource in a CSP of roughly
`frame-src 'self' blob: data:`, which blocks the shell from iframing
`https://nav-ai-mock-shiny.onrender.com` directly. Proxying Shiny under
this service's own origin sidesteps that — the iframe `src` becomes
`/shiny-proxy/`, which is same-origin to the shell and therefore passes
`frame-src 'self'`.

Three concerns matter for Shiny specifically:

  1. HTTP request forwarding. `httpx.AsyncClient` covers GET/POST/etc.;
     hop-by-hop response headers (transfer-encoding, content-encoding,
     content-length) are dropped so Starlette can re-compute them.

  2. WebSocket upgrade. Shiny's JS computes the websocket URL relative
     to the iframe's pathname, so `/shiny-proxy/` produces
     `/shiny-proxy/websocket/`. We accept that route and pipe bytes
     bidirectionally to upstream `ws(s)://{SHINY_URL}/websocket/`.

  3. Absolute paths in Shiny's initial HTML. `shiny::runApp` emits
     `<script src="/shared/shiny.min.js">` etc. Without rewriting,
     those would resolve against our origin's root, not the proxy
     prefix, and 404. We do a byte-level substitution on text/html
     responses to prefix them with `/shiny-proxy/`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import httpx
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import BASE_URL, SHINY_URL

log = logging.getLogger(__name__)

router = APIRouter()

PROXY_PREFIX = "/shiny-proxy"

# Hop-by-hop / framing-related headers that must not be passed through
# verbatim — Starlette recomputes them based on the response body.
_STRIP_RESPONSE_HEADERS = {
    "content-encoding",
    "transfer-encoding",
    "content-length",
    "connection",
}
_STRIP_REQUEST_HEADERS = {"host", "content-length", "connection"}


def _ws_upstream(path: str) -> str:
    """Return ws(s)://… form of SHINY_URL plus a path."""
    base = SHINY_URL
    if base.startswith("https://"):
        ws_base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws_base = "ws://" + base[len("http://") :]
    else:
        ws_base = base
    return ws_base.rstrip("/") + "/" + path.lstrip("/")


def _rewrite_html_paths(body: bytes) -> bytes:
    """
    Prefix Shiny's absolute asset paths with /shiny-proxy/ so the iframe
    resolves them back through this same-origin proxy. Only touches
    text/html bodies; other content types pass through untouched.
    """
    prefix = PROXY_PREFIX.encode()
    return (
        body.replace(b'href="/', b'href="' + prefix + b"/")
        .replace(b'src="/', b'src="' + prefix + b"/")
        .replace(b'action="/', b'action="' + prefix + b"/")
        .replace(b"url(/", b"url(" + prefix + b"/")
    )


# ---------------------------------------------------------------------------
# Card E: Server-side embed of Shiny as inline-HTML MCP resource.
# ---------------------------------------------------------------------------

def _absolute_proxy_base() -> str:
    """The absolute URL form of the /shiny-proxy/ root, with trailing slash."""
    return BASE_URL.rstrip("/") + PROXY_PREFIX + "/"


def _absolute_proxy_ws() -> str:
    """Absolute ws(s):// URL of the /shiny-proxy/websocket/ endpoint."""
    base = BASE_URL.rstrip("/")
    if base.startswith("https://"):
        ws = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        ws = "ws://" + base[len("http://") :]
    else:
        ws = base
    return ws + PROXY_PREFIX + "/websocket/"


def _rewrite_for_inline_embed(body: bytes, proxy_base_abs: str) -> bytes:
    """
    Like `_rewrite_html_paths` but rewrites Shiny's absolute paths to
    *absolute* URLs through BASE_URL. Required for inline-HTML MCP
    resources because the iframe's `location` is opaque, so a path-only
    prefix like `/shiny-proxy/...` doesn't resolve to anything sensible.
    """
    pb = proxy_base_abs.rstrip("/").encode()
    return (
        body.replace(b'href="/', b'href="' + pb + b"/")
        .replace(b'src="/', b'src="' + pb + b"/")
        .replace(b'action="/', b'action="' + pb + b"/")
        .replace(b"url(/", b"url(" + pb + b"/")
    )


def _build_inline_shim(proxy_base_abs: str, proxy_ws_abs: str) -> str:
    """
    The JS+HTML head insertion that adapts Shiny for an inline-HTML MCP
    iframe whose `location.host` is empty:
      - <base href="..."> anchors relative URLs against the proxy.
      - WebSocket constructor is monkey-patched to force the WS proxy URL,
        because Shiny's own URL builder uses `location.host`.
      - The shim runs synchronously before Shiny's own scripts execute.
    """
    return (
        f'<base href="{proxy_base_abs}">'
        "<script>(function(){"
        "var OriginalWS=window.WebSocket;"
        "function PatchedWS(_url, protocols){"
        f'return new OriginalWS("{proxy_ws_abs}", protocols);'
        "}"
        "PatchedWS.CONNECTING=OriginalWS.CONNECTING;"
        "PatchedWS.OPEN=OriginalWS.OPEN;"
        "PatchedWS.CLOSING=OriginalWS.CLOSING;"
        "PatchedWS.CLOSED=OriginalWS.CLOSED;"
        "PatchedWS.prototype=OriginalWS.prototype;"
        "window.WebSocket=PatchedWS;"
        "})();</script>"
    )


_HEAD_OPEN = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)


async def fetch_embedded_html() -> str:
    """
    Fetch Shiny's root HTML and adapt it for embedding inside an inline-
    HTML MCP resource. Returns the adapted HTML as a string.

    Failure mode: if Shiny is unreachable (cold-start, network), returns
    a small fallback HTML page that explains the situation rather than
    raising. The fallback still renders inside Claude — it's just not
    Shiny.
    """
    proxy_base_abs = _absolute_proxy_base()
    proxy_ws_abs = _absolute_proxy_ws()
    upstream_url = SHINY_URL.rstrip("/") + "/"

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(upstream_url)
            resp.raise_for_status()
            body = resp.content
    except Exception as e:  # noqa: BLE001
        log.warning("shiny embed fetch failed: %s", e)
        return _fallback_embed_html(str(e))

    body = _rewrite_for_inline_embed(body, proxy_base_abs)
    shim = _build_inline_shim(proxy_base_abs, proxy_ws_abs).encode()
    body = _HEAD_OPEN.sub(lambda m: m.group(0) + shim, body, count=1)

    return body.decode("utf-8", errors="replace")


def _fallback_embed_html(error: str) -> str:
    return (
        "<!doctype html>"
        '<html><head><meta charset="utf-8"><title>Shiny unavailable</title>'
        '<style>body{background:#0d0d10;color:#e6e6ea;font-family:system-ui,sans-serif;'
        "padding:24px;margin:0}h1{font-size:14px;letter-spacing:.16em;color:#7a8087;"
        "text-transform:uppercase;margin:0 0 12px 0}p{font-size:13px;color:#9aa0aa;margin:0 0 8px 0}"
        "code{font-family:JetBrains Mono,monospace;font-size:12px;color:#e8746e}</style></head>"
        "<body><h1>Shiny embed unavailable</h1>"
        f"<p>Server tried to fetch Shiny but the upstream failed: <code>{error}</code></p>"
        '<p>On Render free tier this is usually a cold-start; retry in 30–60s. If it persists, '
        "check the Shiny service logs and the <code>SHINY_URL</code> env var.</p>"
        "</body></html>"
    )


@router.get("/shiny-embed.html", response_class=HTMLResponse)
async def shiny_embed_html() -> HTMLResponse:
    """
    Browser-side fetch target for Card E's preview iframe. Returns the
    same HTML the MCP resource serves, so a developer can visit
    `/shiny-embed.html` directly in a browser to debug the rewrite/shim
    independently of the MCP plumbing.
    """
    html = await fetch_embedded_html()
    return HTMLResponse(html)


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

@router.websocket(PROXY_PREFIX + "/websocket")
@router.websocket(PROXY_PREFIX + "/websocket/")
async def shiny_websocket_proxy(client_ws: WebSocket) -> None:
    """
    Bidirectional WebSocket pump between the iframe and the upstream
    Shiny server. Shiny's protocol is text-based (JSON-RPC-ish), but we
    forward bytes too for completeness — binary plot transfers, for
    instance.
    """
    await client_ws.accept()
    upstream_url = _ws_upstream("/websocket/")

    try:
        async with websockets.connect(upstream_url, open_timeout=10) as upstream:
            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await client_ws.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        text = msg.get("text")
                        if text is not None:
                            await upstream.send(text)
                            continue
                        data = msg.get("bytes")
                        if data is not None:
                            await upstream.send(data)
                except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                    pass

            async def upstream_to_client() -> None:
                try:
                    async for msg in upstream:
                        if isinstance(msg, (bytes, bytearray)):
                            await client_ws.send_bytes(bytes(msg))
                        else:
                            await client_ws.send_text(msg)
                except websockets.exceptions.ConnectionClosed:
                    pass

            tasks = [
                asyncio.create_task(client_to_upstream()),
                asyncio.create_task(upstream_to_client()),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
    except Exception as e:  # noqa: BLE001
        log.warning("shiny websocket proxy failed: %s", e)
    finally:
        try:
            await client_ws.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@router.get(PROXY_PREFIX)
async def shiny_root_redirect() -> RedirectResponse:
    """Force a trailing slash so the iframe's base URL is well-defined."""
    return RedirectResponse(url=PROXY_PREFIX + "/", status_code=307)


@router.api_route(
    PROXY_PREFIX + "/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
)
async def shiny_http_proxy(request: Request, path: str = "") -> Response:
    upstream_url = f"{SHINY_URL.rstrip('/')}/{path}"
    headers = {
        k: v for k, v in request.headers.items() if k.lower() not in _STRIP_REQUEST_HEADERS
    }
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream_resp: httpx.Response = await client.request(
                request.method,
                upstream_url,
                content=body if body else None,
                params=request.query_params,
                headers=headers,
            )
    except httpx.HTTPError as e:
        return Response(
            content=f"shiny upstream unreachable: {e}".encode(),
            status_code=502,
            media_type="text/plain",
        )

    content_type = upstream_resp.headers.get("content-type", "")
    content = upstream_resp.content
    if "text/html" in content_type.lower():
        content = _rewrite_html_paths(content)

    resp_headers: dict[str, Any] = {
        k: v
        for k, v in upstream_resp.headers.items()
        if k.lower() not in _STRIP_RESPONSE_HEADERS
    }

    return Response(
        content=content,
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=content_type or None,
    )
