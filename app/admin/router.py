"""
Admin-style endpoints that exercise the *server pushing changes* path of
MCP Apps.

The interesting one is POST /admin/broadcast. It mutates shell_state and
then does two things:

  1. Sends `notifications/resources/updated` over every open GET /mcp SSE
     listener — the spec-compliant signal to MCP hosts that the resource
     has changed and they should re-read it.
  2. Pushes the same fact straight into open iframes via /shell/events. This
     bypasses the host so demo viewers see the banner appear *instantly*,
     without depending on whether their MCP host honours
     resources/updated. The MCP notification still fires; it's just no
     longer the only path.

There's no auth on /admin/* — this is a demo. Don't deploy it to anything
public without a token.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse

from .. import state, trace
from ..config import SHELL_URI
from ..mcp.router import broadcast_notification

router = APIRouter()


# ---------------------------------------------------------------------------
# Iframe-direct push channel
# ---------------------------------------------------------------------------

@router.get("/shell/events")
async def shell_events(request: Request) -> EventSourceResponse:
    """
    Per-iframe SSE channel for shell-level updates (banner changes etc.).

    This is the same architectural pattern as /jobs/{id}/events — high-
    frequency or UI-only updates that should not be in the model's context
    window get their own direct channel from server to iframe.
    """
    queue: asyncio.Queue = asyncio.Queue(maxsize=64)
    state.shell_event_subscribers.append(queue)
    trace.record(
        "sse.shell.open",
        layer="sse",
        summary=f"iframe /shell/events listener opened (total: {len(state.shell_event_subscribers)})",
    )

    async def stream() -> AsyncIterator[dict[str, Any]]:
        # Snapshot so a late-joining iframe lands on the current banner.
        yield {
            "event": "snapshot",
            "data": json.dumps(
                {
                    "banner": state.shell_state.get("banner"),
                    "revision": state.shell_state.get("revision", 0),
                }
            ),
        }
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield event
        finally:
            try:
                state.shell_event_subscribers.remove(queue)
            except ValueError:
                pass
            trace.record(
                "sse.shell.close",
                layer="sse",
                summary=f"iframe /shell/events listener closed (remaining: {len(state.shell_event_subscribers)})",
            )

    return EventSourceResponse(stream())


def _push_shell_event(event_name: str, data: dict[str, Any]) -> int:
    payload = {"event": event_name, "data": json.dumps(data)}
    subs = list(state.shell_event_subscribers)
    for q in subs:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            pass
    trace.record(
        "sse.shell.push",
        layer="sse",
        summary=f"push {event_name} → {len(subs)} iframe(s)",
        detail={"event": event_name, "data": data, "subscribers": len(subs)},
    )
    return len(subs)


# ---------------------------------------------------------------------------
# Broadcast
# ---------------------------------------------------------------------------

@router.post("/admin/broadcast")
async def broadcast(request: Request) -> JSONResponse:
    """
    Mutate the shell and push the change everywhere.

    Body: {"banner": "<text>" | null, "tone": "info|warn|alert" (optional)}

    Sets the ops banner. `banner: null` clears it. Returns a small report
    listing what fired so you can paste the result into a slide.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")

    banner = body.get("banner")
    if banner is not None and not isinstance(banner, str):
        raise HTTPException(status_code=400, detail="banner must be a string or null")
    tone = str(body.get("tone", "info")).lower()
    if tone not in {"info", "warn", "alert"}:
        tone = "info"

    state.shell_state["revision"] = int(state.shell_state.get("revision", 0)) + 1
    if banner:
        state.shell_state["banner"] = {
            "text": banner.strip(),
            "tone": tone,
            "set_at": int(time.time()),
        }
    else:
        state.shell_state["banner"] = None

    trace.record(
        "admin.broadcast",
        layer="admin",
        summary=(
            f"banner cleared (rev {state.shell_state['revision']})"
            if not banner
            else f"banner set ({tone}, rev {state.shell_state['revision']}): {banner!r}"
        ),
        detail={"banner": state.shell_state["banner"], "revision": state.shell_state["revision"]},
    )

    # 1) Spec path: tell every MCP client that the resource changed.
    mcp_subs = await broadcast_notification(
        "notifications/resources/updated",
        {"uri": SHELL_URI},
    )

    # 2) Demo-friendly path: tell every open iframe directly.
    iframe_subs = _push_shell_event(
        "shell-update",
        {
            "banner": state.shell_state["banner"],
            "revision": state.shell_state["revision"],
        },
    )

    return JSONResponse(
        {
            "ok": True,
            "revision": state.shell_state["revision"],
            "banner": state.shell_state["banner"],
            "mcp_subscribers_notified": mcp_subs,
            "iframe_subscribers_notified": iframe_subs,
        }
    )


# ---------------------------------------------------------------------------
# Admin console (so reviewers can poke the system without curl)
# ---------------------------------------------------------------------------

_ADMIN_HTML = """<!doctype html>
<html><head>
<meta charset="utf-8" />
<title>NAV AI · admin</title>
<style>
  body { background:#0a0d12; color:#e8e6e0; font: 13px ui-sans-serif, system-ui;
         padding: 28px; max-width: 720px; margin: 0 auto; }
  h1 { font: italic 28px ui-serif, Georgia, serif; margin: 0 0 4px; }
  p.lede { color:#8a8f9c; margin: 0 0 24px; }
  fieldset { border: 1px solid #1f2630; padding: 16px 18px; margin: 0 0 16px; }
  legend { color:#d4a85a; font: 11px ui-monospace; letter-spacing: 0.16em;
           text-transform: uppercase; padding: 0 6px; }
  label { display:block; font: 11px ui-monospace; letter-spacing: 0.14em;
          text-transform: uppercase; color:#8a8f9c; margin: 6px 0 4px; }
  input, select { width: 100%; background:#0d1117; border:1px solid #2a3340;
                  color:#e8e6e0; padding: 8px 10px; font: 13px ui-sans-serif;
                  border-radius: 2px; }
  button { background:#d4a85a; color:#0a0d12; border:none; padding: 10px 18px;
           font: 600 12px ui-sans-serif; letter-spacing: 0.06em; cursor: pointer;
           text-transform: uppercase; }
  button.ghost { background: transparent; color:#8a8f9c; border:1px solid #2a3340;
                 margin-left: 8px; }
  pre { background:#0d1117; border:1px solid #1f2630; padding: 12px;
        font: 12px ui-monospace; color:#7a9b6e; max-height: 200px; overflow: auto; }
  .nav { font: 11px ui-monospace; color:#4d535f; margin-bottom: 24px;
         letter-spacing: 0.16em; text-transform: uppercase; }
  .nav a { color:#8a8f9c; text-decoration: none; border-bottom: 1px dashed #2a3340; }
  .nav a:hover { color:#d4a85a; }
</style>
</head><body>
<div class="nav">
  NAV·AI · admin · <a href="/diagnostics">→ live diagnostics</a> ·
  <a href="/ui/shell">→ shell preview</a>
</div>
<h1>Server-pushed shell updates</h1>
<p class="lede">
  Mutates <code>shell_state.banner</code>, broadcasts
  <code>notifications/resources/updated</code> to every open MCP session, and
  also pushes the change down each iframe's direct <code>/shell/events</code>
  channel. Watch <a href="/diagnostics">/diagnostics</a> and the shell preview
  side-by-side.
</p>
<fieldset>
  <legend>Set banner</legend>
  <label for="text">Text</label>
  <input id="text" placeholder="e.g. Pricing review queue paused for 5 min" />
  <label for="tone">Tone</label>
  <select id="tone">
    <option value="info">info</option>
    <option value="warn">warn</option>
    <option value="alert">alert</option>
  </select>
  <div style="margin-top:14px">
    <button onclick="send()">Broadcast</button>
    <button class="ghost" onclick="clearBanner()">Clear</button>
  </div>
</fieldset>
<fieldset>
  <legend>Last response</legend>
  <pre id="out">—</pre>
</fieldset>
<script>
async function send(){
  const text = document.getElementById('text').value.trim();
  const tone = document.getElementById('tone').value;
  const r = await fetch('/admin/broadcast', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({banner: text || null, tone})
  });
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
async function clearBanner(){
  const r = await fetch('/admin/broadcast', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({banner: null})
  });
  document.getElementById('out').textContent = JSON.stringify(await r.json(), null, 2);
}
</script>
</body></html>
"""


@router.get("/admin", response_class=HTMLResponse)
async def admin_page() -> HTMLResponse:
    return HTMLResponse(_ADMIN_HTML)
