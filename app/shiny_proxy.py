"""
Same-origin reverse proxy for the standalone R Shiny service, plus the
server-side renderer used to embed Shiny as an inline-HTML MCP resource.

Two related responsibilities live here:

  1. **Reverse proxy** at `/shiny-proxy/*` (HTTP + WebSocket). Card C in
     the Shiny launcher uses this directly via an iframe `src`. Cards
     E + F also point asset/WS traffic at it.

  2. **Inline embed renderer** (`fetch_embedded_html`). Cards E and F
     return Shiny's HTML *as* an MCP resource body; this function
     fetches Shiny's root HTML, rewrites every URL so the iframe
     resolves them through the proxy, and inlines two scripts:
     - the MCP App host handshake (so the iframe is made visible)
     - the Shiny embed shim (WebSocket / fetch / XHR redirection)

Both scripts live as static files in `static/` and are loaded once at
module import. They are inlined into the response (not loaded via
`<script src=>`) so the handshake fires immediately, with no extra
network round-trip on cold load.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Literal

import httpx
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import BASE_URL, SHINY_URL, STATIC_DIR, to_ws_url
from .ui.render import render_template

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


# ---------------------------------------------------------------------------
# Static JS assets, loaded once at import time
# ---------------------------------------------------------------------------

def _load_static_js(name: str) -> str:
    path = STATIC_DIR / name
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise FileNotFoundError(
            f"required static asset missing: {path}. "
            f"shiny_proxy needs this file at import time."
        ) from e


HANDSHAKE_JS: str = _load_static_js("mcp-app-handshake.js")
_SHIM_TEMPLATE: str = _load_static_js("shiny-embed-shim.js")


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _proxy_base_abs() -> str:
    """Absolute https URL of the /shiny-proxy/ root, ending with `/`."""
    return BASE_URL.rstrip("/") + PROXY_PREFIX + "/"


def _proxy_ws_abs() -> str:
    """Absolute wss URL of the /shiny-proxy/websocket/ endpoint."""
    return to_ws_url(BASE_URL.rstrip("/")) + PROXY_PREFIX + "/websocket/"


def _ws_upstream(path: str) -> str:
    """Build the ws(s):// URL for an upstream Shiny path."""
    return to_ws_url(SHINY_URL.rstrip("/")) + "/" + path.lstrip("/")


# ---------------------------------------------------------------------------
# HTML URL rewriter
#
# Two modes for the two iframe contexts:
#
#   "proxy" — used by Card C. The iframe is served from `BASE_URL/shiny-proxy/`,
#       so its document base already resolves relative URLs correctly. We
#       only need to prefix absolute-path URLs (`href="/foo"`) so they don't
#       escape the proxy prefix.
#
#   "embed" — used by Cards E + F. The iframe has no base URL we control
#       (the host enforces `base-uri 'self'`), so every URL — absolute-path
#       *and* relative — must be converted to a fully-qualified URL through
#       the proxy.
# ---------------------------------------------------------------------------

_URL_ATTR_RE = re.compile(
    rb'(\s(?:src|href|action))=(["\'])([^"\']*)\2',
    re.IGNORECASE,
)
_CSS_URL_RE = re.compile(rb'url\(([\'"]?)([^\'")\s]+)\1\)', re.IGNORECASE)
_ABSOLUTE_SCHEME = (
    b"http://",
    b"https://",
    b"//",
    b"data:",
    b"javascript:",
    b"mailto:",
    b"#",
    b"about:",
)


def _rewrite_urls(body: bytes, mode: Literal["proxy", "embed"], proxy_base_abs: str = "") -> bytes:
    """Rewrite every URL in `body` according to mode. Single source of truth
    for both proxy-passthrough and inline-embed iframe HTML."""
    if mode == "proxy":
        prefix = PROXY_PREFIX.encode()

        def rewrite(url: bytes) -> bytes:
            if url.startswith(b"/") and not url.startswith(b"//"):
                return prefix + url
            return url
    else:
        pb_slash = proxy_base_abs.rstrip("/").encode() + b"/"
        pb_no_slash = proxy_base_abs.rstrip("/").encode()

        def rewrite(url: bytes) -> bytes:
            if not url or url.startswith(_ABSOLUTE_SCHEME):
                return url
            return (pb_no_slash if url.startswith(b"/") else pb_slash) + url

    def replace_attr(m: "re.Match[bytes]") -> bytes:
        attr, quote, url = m.group(1), m.group(2), m.group(3)
        return attr + b"=" + quote + rewrite(url) + quote

    def replace_css(m: "re.Match[bytes]") -> bytes:
        quote, url = m.group(1), m.group(2)
        return b"url(" + quote + rewrite(url) + quote + b")"

    body = _URL_ATTR_RE.sub(replace_attr, body)
    body = _CSS_URL_RE.sub(replace_css, body)
    return body


# ---------------------------------------------------------------------------
# Inline embed renderer (Cards E + F)
# ---------------------------------------------------------------------------

_HEAD_OPEN = re.compile(rb"<head\b[^>]*>", re.IGNORECASE)


def _render_iframe_bootstrap(proxy_base_abs: str, proxy_ws_abs: str) -> bytes:
    """Two `<script>` blocks injected at the top of `<head>` in the embedded
    Shiny HTML: the MCP App handshake (host visibility), then the Shiny
    runtime shim (WebSocket / fetch / XHR redirection). Inlined so they
    execute before any of Shiny's own scripts.

    Kept as a string `.replace` rather than a Jinja template so the shim
    stays a directly-editable `.js` file with normal JS tooling support.
    """
    shim = _SHIM_TEMPLATE.replace(
        "{{PROXY_BASE}}", proxy_base_abs
    ).replace("{{PROXY_WS}}", proxy_ws_abs)
    return (
        f"<script>{HANDSHAKE_JS}</script><script>{shim}</script>"
    ).encode()


# Network failures we expect when the upstream Shiny service is down or slow
# (free-tier cold starts, container restarts). Anything else — including
# template rendering errors — should propagate so we see real bugs.
_SHINY_FETCH_ERRORS = (httpx.HTTPError, asyncio.TimeoutError)


async def fetch_embedded_html() -> str:
    """Fetch Shiny's root HTML, rewrite URLs, inject the bootstrap scripts."""
    proxy_base_abs = _proxy_base_abs()
    proxy_ws_abs = _proxy_ws_abs()

    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(SHINY_URL.rstrip("/") + "/")
            resp.raise_for_status()
            body = resp.content
    except _SHINY_FETCH_ERRORS as e:
        log.warning("shiny embed fetch failed: %s", e)
        return render_template(
            "iframes/shiny_unavailable.html",
            handshake_js=HANDSHAKE_JS,
            error=str(e),
        )

    body = _rewrite_urls(body, mode="embed", proxy_base_abs=proxy_base_abs)
    body = _HEAD_OPEN.sub(
        lambda m: m.group(0) + _render_iframe_bootstrap(proxy_base_abs, proxy_ws_abs),
        body,
        count=1,
    )
    return body.decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/shiny-embed.html", response_class=HTMLResponse)
async def shiny_embed_html() -> HTMLResponse:
    """Browser-side fetch target for Card E's preview iframe — same content
    the MCP resource serves, exposed at a stable URL for direct testing."""
    return HTMLResponse(await fetch_embedded_html())


@router.websocket(PROXY_PREFIX + "/websocket")
@router.websocket(PROXY_PREFIX + "/websocket/")
async def shiny_websocket_proxy(client_ws: WebSocket) -> None:
    """Bidirectional WebSocket pump between the iframe and upstream Shiny."""
    await client_ws.accept()
    upstream_url = _ws_upstream("/websocket/")

    try:
        async with websockets.connect(upstream_url, open_timeout=10) as upstream:
            await _pump_ws(client_ws, upstream)
    except (OSError, websockets.exceptions.WebSocketException) as e:
        log.warning("shiny websocket proxy failed: %s", e)
    finally:
        # The client may already be closed — Starlette raises RuntimeError
        # in that case, and WebSocketDisconnect can fire while close()
        # is in flight. Either is a benign race; anything else is a bug.
        try:
            await client_ws.close()
        except (RuntimeError, WebSocketDisconnect):
            pass


async def _pump_ws(client_ws: WebSocket, upstream: Any) -> None:
    """Forward messages in both directions until either side disconnects."""
    async def client_to_upstream() -> None:
        try:
            while True:
                msg = await client_ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if (text := msg.get("text")) is not None:
                    await upstream.send(text)
                elif (data := msg.get("bytes")) is not None:
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
    _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()


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
        k: v
        for k, v in request.headers.items()
        if k.lower() not in _STRIP_REQUEST_HEADERS
    }
    body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            upstream_resp = await client.request(
                request.method,
                upstream_url,
                content=body or None,
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
        content = _rewrite_urls(content, mode="proxy")

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
