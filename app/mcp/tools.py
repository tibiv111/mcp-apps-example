"""
Tool handlers. The dispatcher (`router.py`) looks up the function by tool
name in `TOOL_HANDLERS`, awaits it with the parsed `arguments`, and returns
its dict verbatim as the tool result.

Each handler returns `{content: [...], structuredContent: {...}}`:
  - `content` is text/image blocks shown to the user / model.
  - `structuredContent` is machine-readable data the iframe consumes via
    `res.structuredContent.*` after a `tools/call`.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, Awaitable, Callable

from .. import state  # re-exported for handlers that need shared dicts
from ..jobs import runner as jobs_runner


async def launch_nav_ai(_args: dict[str, Any]) -> dict[str, Any]:
    """
    Per SEP-1865 the host already knows about the UI resource via this tool's
    `_meta.ui.resourceUri` in `tools/list`, so the iframe is mounted from the
    tool definition — not from anything in this return value. The `content`
    array exists only for graceful text-only fallback in hosts without UI
    support.
    """
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "NAV AI workspace opened. Use the launcher to submit a "
                    "pricing change or run a forecast."
                ),
            }
        ]
    }


async def submit_pricing_change(args: dict[str, Any]) -> dict[str, Any]:
    product = str(args.get("product", "")).strip() or "UNKNOWN"
    new_price = args.get("new_price")
    try:
        new_price = float(new_price)
    except (TypeError, ValueError):
        new_price = 0.0
    ticket = "PR-" + secrets.token_hex(2).upper()
    submitted_at = int(time.time())
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Pricing change submitted for {product} at "
                    f"{new_price:.2f}. Ticket: {ticket}."
                ),
            }
        ],
        "structuredContent": {
            "ticket": ticket,
            "product": product,
            "new_price": new_price,
            "status": "queued_for_review",
            "submitted_at": submitted_at,
        },
    }


async def start_forecast(args: dict[str, Any]) -> dict[str, Any]:
    region = str(args.get("region", "GLOBAL")).strip().upper() or "GLOBAL"
    job_id = jobs_runner.create_job(region)
    # Fire-and-forget — progress streams over SSE on /jobs/{id}/events.
    asyncio.create_task(jobs_runner.run_mock_job(job_id))
    return {
        "content": [
            {
                "type": "text",
                "text": f"Forecast job {job_id} started for {region}.",
            }
        ],
        "structuredContent": {"job_id": job_id, "region": region, "status": "queued"},
    }


ToolHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "launch_nav_ai": launch_nav_ai,
    "submit_pricing_change": submit_pricing_change,
    "start_forecast": start_forecast,
}
