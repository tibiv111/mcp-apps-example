"""
Direct SSE channel from the iframe (`new EventSource(...)`) back to the
server. Bypasses MCP on purpose: high-frequency progress updates should not
land in the model's context window.

CORS is permissive (set in the app factory) because the iframe is served
from a different origin (`*.claudemcpcontent.com`) than the MCP server.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from sse_starlette.sse import EventSourceResponse

from .. import state, trace

router = APIRouter()


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> EventSourceResponse:
    if job_id not in state.jobs:
        raise HTTPException(status_code=404, detail="job not found")

    queue: asyncio.Queue = asyncio.Queue()
    state.job_subscribers.setdefault(job_id, []).append(queue)
    trace.record(
        "sse.subscribe",
        layer="sse",
        summary=f"iframe subscribed to {job_id}",
        correlation_id=job_id,
    )

    async def stream() -> AsyncIterator[dict[str, Any]]:
        # Initial snapshot so a late subscriber lands on the current state.
        snap = state.jobs[job_id]
        yield {
            "event": "snapshot",
            "data": json.dumps(
                {
                    "job_id": job_id,
                    "status": snap.get("status"),
                    "step": snap.get("step"),
                    "step_label": snap.get("step_label"),
                    "progress": snap.get("progress", 0),
                    "result": snap.get("result"),
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
                    # Keep the connection alive through any intermediary proxies.
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield event
                if event.get("event") in ("done", "error"):
                    break
        finally:
            try:
                state.job_subscribers.get(job_id, []).remove(queue)
            except ValueError:
                pass
            trace.record(
                "sse.unsubscribe",
                layer="sse",
                summary=f"iframe disconnected from {job_id}",
                correlation_id=job_id,
            )

    return EventSourceResponse(stream(), headers={"X-Accel-Buffering": "no"})
