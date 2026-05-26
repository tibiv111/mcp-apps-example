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


def new_id(prefix: str) -> str:
    """Short, human-greppable IDs for jobs, clients, etc."""
    return f"{prefix}-{secrets.token_hex(3)}"
