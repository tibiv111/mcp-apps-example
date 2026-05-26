"""
Backend-side event bus for pricing book mutations.

Lives in the backend service so the data layer can announce changes
without the frontend needing to know how it's wired. Subscribers are
plain asyncio queues; one driver is the `/backend/pricing-events` SSE
endpoint, another is the frontend's bridge task (when both run in the
same process).
"""

from __future__ import annotations

import asyncio
from typing import Any

_subscribers: list[asyncio.Queue[dict[str, Any]]] = []


def subscribe() -> asyncio.Queue[dict[str, Any]]:
    q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
    _subscribers.append(q)
    return q


def unsubscribe(q: asyncio.Queue[dict[str, Any]]) -> None:
    try:
        _subscribers.remove(q)
    except ValueError:
        pass


def publish(event_type: str, payload: dict[str, Any]) -> int:
    """Fan an event to every subscriber. Returns subscriber count."""
    event = {"type": event_type, "payload": payload}
    for q in list(_subscribers):
        try:
            q.put_nowait(event)
        except asyncio.QueueFull:
            pass
    return len(_subscribers)
