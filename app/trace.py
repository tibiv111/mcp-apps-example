"""
In-process trace bus.

Every interesting moment in the demo (an MCP request landing, a tool firing,
an SSE event going out, a resource notification broadcast) calls `record()`.
Events go to a small ring buffer for late subscribers and to every live
queue in `_subscribers`, which the diagnostics SSE endpoint drains.

This is the substrate the /diagnostics page renders. It's also the easiest
way to *see* the three architectural layers (MCP, iframe-direct SSE,
postMessage) cooperating without instrumenting every file by hand.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Any, Deque

# Bounded so a long-running process can't OOM. The diagnostics UI shows the
# tail; older events fall off.
_RING_MAX = 500
_ring: Deque[dict[str, Any]] = deque(maxlen=_RING_MAX)

# Each open /diagnostics/events SSE connection adds a queue here. We fan
# every event out to all of them.
_subscribers: list[asyncio.Queue[dict[str, Any]]] = []

_seq = 0


def _next_seq() -> int:
    global _seq
    _seq += 1
    return _seq


def record(
    kind: str,
    *,
    layer: str,
    summary: str,
    correlation_id: str | None = None,
    duration_ms: float | None = None,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Drop an event onto the bus.

    `layer` is one of: 'mcp', 'tool', 'sse', 'jobs', 'ui', 'resource',
    'admin', 'oauth'. The diagnostics UI colour-codes by layer.

    `correlation_id` groups related events on the timeline — e.g. an MCP
    request id, a job id, an intent id.
    """
    event = {
        "seq": _next_seq(),
        "ts": time.time(),
        "kind": kind,
        "layer": layer,
        "summary": summary,
        "correlation_id": correlation_id,
        "duration_ms": duration_ms,
        "detail": detail or {},
    }
    _ring.append(event)
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass
    return event


def snapshot() -> list[dict[str, Any]]:
    """Return everything currently in the ring buffer (oldest first)."""
    return list(_ring)


def subscribe() -> asyncio.Queue[dict[str, Any]]:
    """Register a subscriber queue. Caller is responsible for `unsubscribe`."""
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue[dict[str, Any]]) -> None:
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


class Timer:
    """`with Timer() as t: ...` then read `t.ms` after the block."""

    def __enter__(self) -> "Timer":
        self._start = time.perf_counter()
        self.ms: float = 0.0
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.ms = (time.perf_counter() - self._start) * 1000.0
