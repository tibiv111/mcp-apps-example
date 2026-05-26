"""
Tool handlers. The dispatcher (`router.py`) looks up the function by tool
name in `TOOL_HANDLERS`, awaits it with the parsed `arguments` and the
caller's bearer token (or None), and returns its dict verbatim as the tool
result.

The bearer token is threaded through so handlers that delegate to other
services (see `lookup_product`) can forward the caller's identity instead
of inventing their own.

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

import httpx

from .. import state  # re-exported for handlers that need shared dicts
from ..config import BACKEND_URL
from ..jobs import runner as jobs_runner


async def launch_nav_ai(_args: dict[str, Any], _token: str | None) -> dict[str, Any]:
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


async def submit_pricing_change(args: dict[str, Any], _token: str | None) -> dict[str, Any]:
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


async def start_forecast(args: dict[str, Any], _token: str | None) -> dict[str, Any]:
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


async def lookup_product(args: dict[str, Any], token: str | None) -> dict[str, Any]:
    """
    Bridge to the backend MCP server. Forwards the caller's bearer token so
    the backend can authenticate the request against the same OAuth-issued
    tokens.

    Calls the backend's `get_product` tool over HTTP. The backend lives at
    `/backend/mcp` on this same host but is treated as if it were remote —
    this is what the swap to a real separate service would look like.
    """
    sku = str(args.get("sku", "")).strip().upper()
    if not sku:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "sku is required"}],
        }
    if not token:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "no bearer token to forward to backend"}],
        }

    backend_url = f"{BACKEND_URL}/backend/mcp"
    rpc_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "get_product", "arguments": {"sku": sku}},
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                backend_url,
                json=rpc_payload,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as e:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"backend unreachable: {e}"}],
        }

    if resp.status_code == 401:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "backend rejected the bearer token"}],
        }
    if resp.status_code >= 400:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"backend error: HTTP {resp.status_code}"}],
        }

    body = resp.json()
    if "error" in body:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"backend error: {body['error'].get('message')}"}],
        }

    inner = body.get("result") or {}
    # Pass through the backend's content + structuredContent. Tag the result
    # with provenance so the UI can show that it came from the backend.
    structured = dict(inner.get("structuredContent") or {})
    structured.setdefault("source", "nav-ai-backend")
    return {
        "content": inner.get("content")
        or [{"type": "text", "text": f"Looked up {sku} via backend."}],
        "structuredContent": structured,
        "isError": inner.get("isError", False),
    }


async def discuss_selection(args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """
    Bidirectional path: iframe asks the host model to comment on something
    the user just selected. The tool call goes through Claude (the user never
    typed in chat), and the tool result is text that's plainly addressed *to*
    the model, asking it to respond — so Claude turns around and answers in
    the chat thread unprompted from the user's typing perspective.

    The 'content' string deliberately reads as a directive. The model sees
    it as part of the tool result and responds naturally.
    """
    kind = str(args.get("kind", "selection")).strip().lower() or "selection"
    context = args.get("context") or {}
    if not isinstance(context, dict):
        context = {"value": context}
    question = (args.get("question") or "").strip()

    if kind == "forecast":
        default_q = (
            "Briefly interpret this forecast for the user: explain what the "
            "uplift and confidence numbers imply, and one risk to watch."
        )
        headline = f"Forecast result for {context.get('region', '—')}"
    elif kind == "pricing":
        default_q = (
            "Comment on this pricing change: is the magnitude reasonable, "
            "and what should the user expect from the review queue?"
        )
        headline = f"Pricing submission {context.get('ticket', '')}"
    elif kind == "catalog":
        default_q = (
            "Summarize this product for the user in one sentence and flag "
            "anything unusual about the price or stock state."
        )
        headline = f"Catalog entry {context.get('sku', '')}"
    elif kind == "dashboard_row":
        default_q = (
            "Briefly explain what this dashboard line item means, in plain "
            "English. One paragraph max."
        )
        headline = f"Dashboard item: {context.get('label', '')}"
    else:
        default_q = "Briefly comment on the selection below for the user."
        headline = f"Workspace selection ({kind})"

    prompt = question or default_q
    context_blob = "\n".join(f"  {k}: {v}" for k, v in context.items())

    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"[Iframe-initiated discussion · {headline}]\n\n"
                    f"The user clicked 'Discuss' inside the NAV AI workspace. "
                    f"They have not typed anything in chat — this is a "
                    f"bidirectional event from the UI surface. Please respond "
                    f"to them directly.\n\n"
                    f"Selected context:\n{context_blob}\n\n"
                    f"Request: {prompt}"
                ),
            }
        ],
        "structuredContent": {
            "kind": kind,
            "headline": headline,
            "prompt": prompt,
            "selection": context,
        },
    }


ToolHandler = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "launch_nav_ai": launch_nav_ai,
    "submit_pricing_change": submit_pricing_change,
    "start_forecast": start_forecast,
    "lookup_product": lookup_product,
    "discuss_selection": discuss_selection,
}
