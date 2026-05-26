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
import time
from typing import Any, Awaitable, Callable

import httpx

from .. import pricing, state  # re-exported for handlers that need shared dicts
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
    # Persist into the shared pricing book — this is what makes the
    # catalog and forecast see the change.
    change = pricing.submit_change(product, new_price)
    delta = change.get("delta_pct")
    delta_str = (f" ({delta:+.2f}% vs previous {change['previous_price']:.2f})"
                 if delta is not None else "")
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    f"Pricing change submitted for {change['product']} at "
                    f"{change['new_price']:.2f}{delta_str}. "
                    f"Ticket: {change['ticket']}. The catalog and any "
                    f"subsequent forecast will reflect this pending change."
                ),
            }
        ],
        "structuredContent": change,
    }


async def start_forecast(args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    region = str(args.get("region", "GLOBAL")).strip().upper() or "GLOBAL"
    # Snapshot the pending pricing changes at job-creation time so the
    # forecast result is reproducible even if more changes land mid-run.
    pending = pricing.all_pending()
    job_id = jobs_runner.create_job(region, pending_pricing=pending)
    # Fire-and-forget — progress streams over SSE on /jobs/{id}/events.
    asyncio.create_task(jobs_runner.run_mock_job(job_id))
    if pending:
        plural = "s" if len(pending) != 1 else ""
        pricing_note = f" Factoring in {len(pending)} pending pricing change{plural}."
    else:
        pricing_note = " No pending pricing changes to factor in."
    return {
        "content": [
            {
                "type": "text",
                "text": f"Forecast job {job_id} started for {region}.{pricing_note}",
            }
        ],
        "structuredContent": {
            "job_id": job_id,
            "region": region,
            "status": "queued",
            "pending_pricing_changes": len(pending),
        },
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


async def list_products(_args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """Catalog overview with current prices and pending counts."""
    entries = pricing.list_entries()
    items = [
        {
            "sku": e["sku"],
            "name": e["name"],
            "current_price": e["current_price"],
            "currency": e.get("currency", "USD"),
            "in_stock": e.get("in_stock", True),
            "pending_changes": len(e.get("pending_changes", [])),
            "last_updated": e.get("last_updated"),
        }
        for e in entries
    ]
    if not items:
        text = "Catalog is empty."
    else:
        lines = [
            f"- {it['sku']} · {it['name']} · {it['current_price']:.2f} {it['currency']}"
            + (f" · {it['pending_changes']} pending" if it['pending_changes'] else "")
            + ("" if it['in_stock'] else " · OUT OF STOCK")
            for it in items
        ]
        text = f"{len(items)} product(s):\n" + "\n".join(lines)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"items": items, "count": len(items)},
    }


async def list_pending_changes(_args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """Every pending pricing ticket awaiting review."""
    now = int(time.time())
    pending = pricing.all_pending()
    items = []
    for c in pending:
        age_s = max(0, now - int(c.get("submitted_at") or now))
        items.append({**c, "age_seconds": age_s})
    if not items:
        text = "No pending pricing changes."
    else:
        lines = [
            f"- {c['ticket']} · {c['product']} · "
            f"{c['previous_price']:.2f} → {c['new_price']:.2f} "
            f"({c['delta_pct']:+.2f}%) · {c['status'].replace('_', ' ')} "
            f"· queued {c['age_seconds']}s ago"
            for c in items
        ]
        text = f"{len(items)} pending pricing change(s):\n" + "\n".join(lines)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"items": items, "count": len(items)},
    }


async def approve_pricing_change(args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """Approve a pending ticket — mutates the pricing book."""
    ticket = str(args.get("ticket", "")).strip().upper()
    if not ticket:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "ticket is required"}],
        }
    change = pricing.approve_change(ticket)
    if not change:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"No pending ticket found: {ticket}"}],
        }
    text = (
        f"Approved {change['ticket']}: {change['product']} → "
        f"{change['new_price']:.2f} (was {change['previous_price']:.2f}, "
        f"{change['delta_pct']:+.2f}%). Catalog updated; any open workspace "
        f"will reflect the new price."
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": change,
    }


async def reject_pricing_change(args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """Reject a pending ticket — mutates the pricing book."""
    ticket = str(args.get("ticket", "")).strip().upper()
    if not ticket:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "ticket is required"}],
        }
    reason = (args.get("reason") or "").strip() or None
    change = pricing.reject_change(ticket, reason)
    if not change:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"No pending ticket found: {ticket}"}],
        }
    suffix = f" Reason: {reason}." if reason else ""
    text = (
        f"Rejected {change['ticket']}: {change['product']} stays at "
        f"{change['previous_price']:.2f}.{suffix}"
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": change,
    }


async def get_job(args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """Fetch a forecast job by id, including the pricing changes it factored."""
    job_id = str(args.get("job_id", "")).strip()
    if not job_id:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "job_id is required"}],
        }
    job = state.jobs.get(job_id)
    if not job:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"No job: {job_id}"}],
        }
    pending_count = len(job.get("pending_pricing") or [])
    if job.get("status") == "done":
        r = job.get("result") or {}
        text = (
            f"{job_id} ({job.get('region')}): done · uplift {r.get('uplift_pct')}% "
            f"· baseline {r.get('baseline_units'):,} u · confidence "
            f"{(r.get('confidence') or 0)*100:.1f}% · drag from {pending_count} "
            f"pricing change(s): {r.get('pricing_drag_pct')}pp"
        )
    else:
        text = (
            f"{job_id} ({job.get('region')}): {job.get('status')} "
            f"({job.get('progress', 0)}% · {job.get('step_label', job.get('step'))}) "
            f"· factoring {pending_count} pricing change(s)"
        )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {
            "id": job_id,
            "region": job.get("region"),
            "status": job.get("status"),
            "progress": job.get("progress"),
            "step": job.get("step"),
            "step_label": job.get("step_label"),
            "started_at": job.get("started_at"),
            "pending_pricing": job.get("pending_pricing") or [],
            "result": job.get("result"),
        },
    }


async def simulate_pricing_impact(args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """What-if: project the marginal drag of a hypothetical pricing change."""
    sku = str(args.get("sku", "")).strip().upper()
    new_price = args.get("new_price")
    try:
        new_price = float(new_price)
    except (TypeError, ValueError):
        return {
            "isError": True,
            "content": [{"type": "text", "text": "new_price must be a number"}],
        }
    sim = pricing.simulate(sku, new_price)
    if not sim:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Unknown SKU: {sku}"}],
        }
    text = (
        f"Simulating {sim['sku']} at {sim['hypothetical_price']:.2f} "
        f"(current {sim['current_price']:.2f}, {sim['delta_pct']:+.2f}%): "
        f"would drag forecast uplift by {sim['uplift_drag_pct']:+.2f}pp on its own. "
        f"Combined with {sim['existing_pending_drag_pct']:+.2f}pp from existing pending changes "
        f"→ total drag {sim['combined_drag_pct']:+.2f}pp. Not submitted."
    )
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": sim,
    }


ToolHandler = Callable[[dict[str, Any], str | None], Awaitable[dict[str, Any]]]

TOOL_HANDLERS: dict[str, ToolHandler] = {
    "launch_nav_ai": launch_nav_ai,
    "submit_pricing_change": submit_pricing_change,
    "start_forecast": start_forecast,
    "lookup_product": lookup_product,
    "list_products": list_products,
    "list_pending_changes": list_pending_changes,
    "approve_pricing_change": approve_pricing_change,
    "reject_pricing_change": reject_pricing_change,
    "get_job": get_job,
    "simulate_pricing_impact": simulate_pricing_impact,
}
