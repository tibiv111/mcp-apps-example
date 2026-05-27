"""
In-memory process state. Replace with a real store for anything beyond a demo.

Kept in one module so the SSE endpoint, the background job runner, and the
tool handlers all touch the same dicts without circular imports.
"""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

# job_id -> job dict (status, progress, step, step_label, result, ...)
jobs: dict[str, dict[str, Any]] = {}

# job_id -> list of subscriber queues that the SSE endpoint drains
job_subscribers: dict[str, list[asyncio.Queue]] = {}

# Issued OAuth access tokens. We don't actually validate them anywhere; this
# just lets you see what was handed out if you instrument the server.
issued_tokens: set[str] = set()

# Each open GET /mcp listener (the host's server→client SSE channel) parks a
# queue here. The MCP dispatcher drains them when broadcasting JSON-RPC
# notifications like `notifications/resources/updated`.
mcp_subscribers: list[asyncio.Queue] = []

# Each open /shell/events listener (iframe ←direct SSE— server) parks a queue
# here. Used to push shell-level changes (e.g. ops banner) straight into the
# iframe without going through the host. The host gets the same fact via
# resources/updated; the direct channel is just for instant visual feedback.
shell_event_subscribers: list[asyncio.Queue] = []

# topic-id -> list of subscriber queues. The ResultsBus drains these when
# any iframe POSTs /bus/publish; one queue per active /bus/subscribe SSE.
# Topics are opaque strings the caller chooses (typically a delegation id).
bus_subscribers: dict[str, list[asyncio.Queue]] = {}

# Live, mutable shell state. The shell template renders these so server-side
# pokes (POST /admin/*) reach the iframe via resources/updated + a re-read.
shell_state: dict[str, Any] = {
    # Ops banner shown at the top of the workspace. None → hidden.
    "banner": None,
    # Incremented on every server-pushed shell mutation; useful as a visible
    # "build" indicator so demo viewers can see the iframe re-rendering.
    "revision": 0,
}


def new_id(prefix: str) -> str:
    """Short, human-greppable IDs for jobs, clients, etc."""
    return f"{prefix}-{secrets.token_hex(3)}"
