"""
ResultsBus: an in-process pub/sub bus iframes can use to talk to each
other through the server.

Why this exists
---------------
MCP iframes are cross-origin siblings under Claude's host. They cannot
postMessage each other directly. The "main hub delegates to Shiny,
Shiny optionally returns a result" pattern needs a relay.

The bus is that relay. Any iframe can:

  * POST /bus/publish    { "topic": "<id>", "payload": ... }
  * GET  /bus/subscribe?topic=<id>      (SSE stream of payloads)

Topics are opaque caller-chosen strings — typically a delegation id the
hub mints when it kicks off work. The bus does not validate or persist
payloads; subscribers receive every published payload as a JSON-serialized
`message` SSE event.

What it deliberately is NOT
---------------------------
  * Not MCP. Stays out of the model's context — pure server↔iframe.
  * Not durable. Late subscribers do NOT see prior publishes. Topics
    are GC'd when the last subscriber goes away.
  * Not multi-process. One Python process owns the topic table; if the
    deployment ever fans out, swap `state.bus_subscribers` for Redis
    pub/sub and keep the HTTP API the same.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from . import state, trace

router = APIRouter()

# Time between forced keep-alive pings on an idle SSE stream. Without
# these, reverse proxies (Render, Cloudflare) reap the connection after
# their idle timeout and the iframe sees a silent disconnect.
_KEEPALIVE_S = 15.0

# Per-subscriber queue depth. If a slow consumer falls this far behind
# the producer we drop the oldest payload rather than letting memory
# grow. Generous enough for typical delegation patterns (one or two
# payloads per topic), tight enough that a stuck client can't OOM us.
_QUEUE_MAX = 32


class PublishBody(BaseModel):
    topic: str = Field(..., min_length=1, max_length=128)
    payload: Any = None


@router.post("/bus/publish")
async def bus_publish(body: PublishBody) -> dict[str, Any]:
    """Broadcast `payload` to every current subscriber of `topic`.
    Returns the number of subscribers that received it (0 if nobody
    is listening — that's not an error, just unobserved)."""
    queues = state.bus_subscribers.get(body.topic, [])
    delivered = 0
    for q in queues:
        try:
            q.put_nowait(body.payload)
            delivered += 1
        except asyncio.QueueFull:
            # Drop oldest, then enqueue. The "lost" payload is reported
            # to the trace bus so it's visible during demos.
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
            q.put_nowait(body.payload)
            delivered += 1
            trace.record(
                "bus.overflow",
                layer="sse",
                summary=f"bus subscriber lagged on {body.topic}",
                correlation_id=body.topic,
            )

    trace.record(
        "bus.publish",
        layer="sse",
        summary=f"publish → {body.topic} ({delivered} listeners)",
        correlation_id=body.topic,
        detail={"topic": body.topic, "subscribers": delivered},
    )
    return {"topic": body.topic, "delivered": delivered}


@router.get("/bus/subscribe")
async def bus_subscribe(topic: str, request: Request) -> EventSourceResponse:
    """Stream every payload published to `topic` as SSE `message` events.
    The stream stays open until the client disconnects; it never ends
    server-side (no terminal `done` event), so a long-lived hub iframe
    can keep one subscription per delegation forever."""
    queue: asyncio.Queue = asyncio.Queue(maxsize=_QUEUE_MAX)
    state.bus_subscribers.setdefault(topic, []).append(queue)
    trace.record(
        "bus.subscribe",
        layer="sse",
        summary=f"subscribe → {topic}",
        correlation_id=topic,
    )

    async def stream() -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(
                        queue.get(), timeout=_KEEPALIVE_S
                    )
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {"event": "message", "data": json.dumps(payload)}
        finally:
            subs = state.bus_subscribers.get(topic, [])
            try:
                subs.remove(queue)
            except ValueError:
                pass
            if not subs:
                state.bus_subscribers.pop(topic, None)
            trace.record(
                "bus.unsubscribe",
                layer="sse",
                summary=f"unsubscribe ← {topic}",
                correlation_id=topic,
            )

    return EventSourceResponse(stream(), headers={"X-Accel-Buffering": "no"})
