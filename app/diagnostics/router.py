"""
/diagnostics — live trace of every layer of the demo in one view.

The HTML page subscribes to /diagnostics/events (SSE) and renders trace
records as they arrive: which MCP method was called, which tool ran, which
SSE events fanned out, which postMessage notifications were broadcast.
Each event carries a layer tag so the UI can colour-code:

  mcp       — JSON-RPC requests/responses, server→client notifications
  tool      — tool handlers firing
  sse       — iframe-direct SSE channels (jobs, shell)
  jobs      — forecast pipeline steps
  resource  — subscribe / unsubscribe / updated
  admin     — operator-initiated mutations (banner broadcasts)
  ui        — postMessage-level events flagged by the iframe

This is the single-screen story of "how MCP Apps actually works at runtime".
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from .. import trace
from ..config import BASE_URL

router = APIRouter()


@router.get("/diagnostics/events")
async def diagnostics_events(request: Request) -> EventSourceResponse:
    """Live event stream. Sends a snapshot first, then live deltas."""
    queue = trace.subscribe()

    async def stream() -> AsyncIterator[dict[str, Any]]:
        try:
            yield {"event": "snapshot", "data": json.dumps(trace.snapshot())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": "trace", "data": json.dumps(event)}
        finally:
            trace.unsubscribe(queue)

    # X-Accel-Buffering: no defeats Render's (and any nginx-fronted) reverse
    # proxy buffering, which is what makes SSE feel laggy on the free tier.
    return EventSourceResponse(stream(), headers={"X-Accel-Buffering": "no"})


@router.post("/diagnostics/note")
async def diagnostics_note(request: Request) -> JSONResponse:
    """
    Lets the iframe drop a marker onto the trace ('ui' layer). The shell
    calls this when it sends a postMessage or receives a noteworthy host
    notification, so the timeline ties UI behaviour to MCP/SSE traffic.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    kind = str(body.get("kind", "ui.event"))
    summary = str(body.get("summary", "")) or "iframe event"
    detail = body.get("detail") if isinstance(body.get("detail"), dict) else {}
    correlation_id = body.get("correlation_id")
    if correlation_id is not None:
        correlation_id = str(correlation_id)
    trace.record(
        kind, layer="ui", summary=summary, correlation_id=correlation_id, detail=detail
    )
    return JSONResponse({"ok": True})


_PAGE = """<!doctype html>
<html><head>
<meta charset="utf-8" />
<title>NAV AI · diagnostics</title>
<link rel="stylesheet" href="__BASE__/static/diagnostics.css" />
</head><body>
<header>
  <div>
    <div class="logo">NAV<span>·</span>AI · diagnostics</div>
    <div class="sub">live trace · MCP ⇄ tool ⇄ SSE ⇄ postMessage</div>
  </div>
  <div class="nav">
    <a href="/ui/shell">→ shell preview</a>
    <a href="/admin">→ admin</a>
    <button id="pause">Pause</button>
    <button id="clear" class="ghost">Clear</button>
  </div>
</header>
<section class="legend">
  <span class="chip mcp">mcp</span>
  <span class="chip tool">tool</span>
  <span class="chip sse">sse</span>
  <span class="chip jobs">jobs</span>
  <span class="chip resource">resource</span>
  <span class="chip admin">admin</span>
  <span class="chip ui">ui</span>
  <span class="filler"></span>
  <span class="stat" id="stat-total">0 events</span>
  <span class="stat" id="stat-rate">0/s</span>
</section>
<main>
  <ol id="timeline"></ol>
  <div id="empty">
    Waiting for traffic. Open <a href="/ui/shell">/ui/shell</a> in another
    tab and click around — or hit <a href="/admin">/admin</a> to broadcast a
    shell update.
  </div>
</main>
<script src="__BASE__/static/diagnostics.js"></script>
</body></html>
"""


@router.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics_page() -> HTMLResponse:
    return HTMLResponse(_PAGE.replace("__BASE__", BASE_URL))
