"""
Frontend bridge: subscribes to the backend's pricing-event SSE stream and
re-publishes events onto the frontend's `/shell/events` channel so live
iframes refresh in real time.

The backend owns the pricing book (single source of truth). When an
approve/submit/reject lands — whether issued from the iframe, from chat,
or anywhere else — the backend emits a `pricing-event` SSE. This bridge
forwards it. Without the bridge, mutations would land in the backend
silently and iframes would only update on the next manual lookup.

In combined-mode deploys (one process), BACKEND_URL points at the same
service and the bridge opens an SSE connection to localhost. In split
deploys it's a real cross-service stream. Either way, same code path.

If the backend is unreachable on startup (cold start on Render), the
bridge backs off and retries. We never abort.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from . import state, trace
from .config import BACKEND_URL

log = logging.getLogger(__name__)

# Tunable: how long to wait before reconnecting after a stream ends or
# the backend was unreachable. Kept short because it's just localhost in
# combined mode and recoverable transient failure in split mode.
_BACKOFF_SECONDS = 3.0


async def run_bridge(stop: asyncio.Event) -> None:
    """
    Long-lived bridge task. Loops until `stop` is set:
    1. Open an SSE connection to backend `/backend/pricing-events`.
    2. For every received `pricing-event`, push onto `state.shell_event_subscribers`
       so the frontend's `/shell/events` SSE delivers it to iframes.
    3. On disconnect or error, log + back off + retry.
    """
    url = f"{BACKEND_URL}/backend/pricing-events"
    trace.record(
        "bridge.start",
        layer="sse",
        summary=f"pricing-event bridge starting → {url}",
    )

    while not stop.is_set():
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"backend SSE returned HTTP {resp.status_code}")
                    trace.record(
                        "bridge.connected",
                        layer="sse",
                        summary="bridge connected to backend pricing-event stream",
                    )
                    await _consume_sse(resp, stop)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            trace.record(
                "bridge.error",
                layer="sse",
                summary=f"bridge disconnected: {e} — retrying in {_BACKOFF_SECONDS}s",
            )
            try:
                await asyncio.wait_for(stop.wait(), timeout=_BACKOFF_SECONDS)
            except asyncio.TimeoutError:
                pass

    trace.record("bridge.stop", layer="sse", summary="bridge stopping")


async def _consume_sse(resp: httpx.Response, stop: asyncio.Event) -> None:
    """
    Parse a stream of SSE events (event:/data:/blank-line separators) and
    forward `pricing-event` payloads to the iframe subscribers.
    """
    event_name: str | None = None
    data_lines: list[str] = []

    async for line in resp.aiter_lines():
        if stop.is_set():
            return

        # Blank line terminates an event.
        if line == "":
            if event_name and data_lines:
                _forward_pricing_event(event_name, "\n".join(data_lines))
            event_name = None
            data_lines = []
            continue

        # SSE comment line (starts with ':'); skip.
        if line.startswith(":"):
            continue

        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
        # other field names (id:, retry:) are ignored


def _forward_pricing_event(event_name: str, data_blob: str) -> None:
    """
    Push a backend pricing-event onto the frontend's iframe SSE subscribers.
    The shape matches what shell.js already listens for: each iframe's
    /shell/events stream emits events with name 'pricing-event' carrying
    a JSON `{type, payload}` body.
    """
    if event_name != "pricing-event":
        # Ignore pings and unrelated channels.
        return
    try:
        parsed: dict[str, Any] = json.loads(data_blob)
    except json.JSONDecodeError:
        return
    sse_payload = {
        "event": "pricing-event",
        "data": json.dumps(parsed),
    }
    subs = list(state.shell_event_subscribers)
    for q in subs:
        try:
            q.put_nowait(sse_payload)
        except asyncio.QueueFull:
            pass
    trace.record(
        "bridge.forward",
        layer="sse",
        summary=f"bridge forwarded {parsed.get('type', '?')} to {len(subs)} iframe(s)",
        correlation_id=(parsed.get("payload") or {}).get("ticket"),
        detail=parsed,
    )
