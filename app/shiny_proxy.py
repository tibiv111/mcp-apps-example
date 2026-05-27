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
from typing import Any

import httpx
import websockets
from fastapi import APIRouter, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse

from .config import SHINY_URL

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
