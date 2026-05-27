"""
Tool handlers (frontend MCP — the service Claude talks to).

The frontend MCP is a thin proxy: every pricing-touching handler forwards
through the `pricing` client to the backend MCP, where the actual book
lives. That's how we get a single source of truth across both deploy
modes (combined and split).

Each handler returns `{content: [...], structuredContent: {...}}`:
  - `content` is text/image blocks shown to the user / model.
  - `structuredContent` is machine-readable data the iframe consumes via
    `res.structuredContent.*` after a `tools/call`.

The bearer token is threaded through so handlers that delegate to other
services (the backend MCP) can forward the caller's identity.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from .. import pricing, state
from ..jobs import runner as jobs_runner


async def launch_shiny_embedded(_args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """
    Card E. The host learns the resource URI from `_meta.ui.resourceUri`
    on this tool and follows up with a `resources/read` for
    `ui://nav-ai/shiny-embedded`, which returns Shiny's HTML inline.
    """
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "Opening Shiny via server-side embed (inline-HTML MCP "
                    "resource). The host should render it the same way as "
                    "the existing NAV AI workspace shell."
                ),
            }
        ]
    }


async def launch_shiny(_args: dict[str, Any], _token: str | None) -> dict[str, Any]:
    """
    Card D in the Shiny launcher tab. The host already learned the resource
    URI from this tool's `_meta.ui.resourceUri` in `tools/list`, so it'll
    fetch `ui://nav-ai/shiny` and (if it honours URL-form resources) open
    its own iframe at the URL the resource carries. The text content here
    is fallback for hosts without UI support.
    """
    return {
        "content": [
            {
                "type": "text",
                "text": (
                    "Opening the R Shiny dashboard in a host-owned iframe "
                    "(URL-form MCP resource). If your host doesn't honour "
                    "URL resources, the tool returns the Shiny URL as text."
                ),
            }
        ]
    }


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


async def submit_pricing_change(args: dict[str, Any], token: str | None) -> dict[str, Any]:
    product = str(args.get("product", "")).strip() or "UNKNOWN"
    try:
        new_price = float(args.get("new_price"))
    except (TypeError, ValueError):
        new_price = 0.0
    try:
        change = await pricing.submit_change(product, new_price, token=token)
    except Exception as e:  # noqa: BLE001
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"backend unreachable: {e}"}],
        }
    delta = change.get("delta_pct")
    delta_str = (
        f" ({delta:+.2f}% vs previous {change['previous_price']:.2f})"
        if delta is not None
        else ""
    )
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


async def start_forecast(args: dict[str, Any], token: str | None) -> dict[str, Any]:
    region = str(args.get("region", "GLOBAL")).strip().upper() or "GLOBAL"
    # Snapshot both layers of pricing state from the backend at job-creation
    # time so the forecast result is reproducible.
    try:
        pending = await pricing.all_pending(token=token)
        drifts = await pricing.all_current_drifts(token=token)
    except Exception as e:  # noqa: BLE001
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"backend unreachable: {e}"}],
        }
    job_id = jobs_runner.create_job(region, pending_pricing=pending, current_drifts=drifts)
    asyncio.create_task(jobs_runner.run_mock_job(job_id))

    parts: list[str] = []
    if drifts:
        parts.append(
            f"{len(drifts)} approved price move{'s' if len(drifts) != 1 else ''} "
            f"(baseline shift)"
        )
    if pending:
        parts.append(
            f"{len(pending)} pending change{'s' if len(pending) != 1 else ''} "
            f"(uplift drag)"
        )
    pricing_note = (
        " Factoring in " + " and ".join(parts) + "."
        if parts
        else " No pricing moves to factor in."
    )

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
            "approved_price_drifts": len(drifts),
        },
    }


async def lookup_product(args: dict[str, Any], token: str | None) -> dict[str, Any]:
    """
    Forward to the backend MCP's get_product. The backend now joins
    catalog + pricing book and returns the unified view directly.
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
    try:
        entry = await pricing.get_entry(sku, token=token)
    except Exception as e:  # noqa: BLE001
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"backend unreachable: {e}"}],
        }
    if not entry:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Unknown SKU: {sku}"}],
            "structuredContent": {"sku": sku, "found": False},
        }
    summary = f"{sku} · {entry.get('name', '—')} · {entry.get('current_price')} {entry.get('currency', '')}".strip()
    if entry.get("pending_changes"):
        p = entry["pending_changes"][-1]
        summary += (
            f" · pending {p['new_price']:.2f} "
            f"({p['ticket']}, {p['status'].replace('_', ' ')})"
        )
    entry = {**entry, "source": "nav-ai-backend (single source of truth)"}
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": entry,
    }


async def list_products(_args: dict[str, Any], token: str | None) -> dict[str, Any]:
    """Catalog overview, joined with pending counts. Forwarded to backend."""
    try:
        items = await pricing.list_entries(token=token)
    except Exception as e:  # noqa: BLE001
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"backend unreachable: {e}"}],
        }
    if not items:
        text = "Catalog is empty."
    else:
        lines = [
            f"- {it['sku']} · {it['name']} · {it['current_price']:.2f} {it.get('currency', 'USD')}"
            + (f" · {len(it.get('pending_changes') or [])} pending" if it.get("pending_changes") else "")
            + ("" if it.get("in_stock", True) else " · OUT OF STOCK")
            for it in items
        ]
        text = f"{len(items)} product(s):\n" + "\n".join(lines)
    # Compress to a list-of-pending-counts shape for the structuredContent
    # so the wire size is reasonable.
    compact = [
        {
            "sku": it["sku"],
            "name": it["name"],
            "current_price": it["current_price"],
            "currency": it.get("currency", "USD"),
            "in_stock": it.get("in_stock", True),
            "pending_changes": len(it.get("pending_changes") or []),
            "last_updated": it.get("last_updated"),
        }
        for it in items
    ]
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": {"items": compact, "count": len(compact)},
    }


async def list_pending_changes(_args: dict[str, Any], token: str | None) -> dict[str, Any]:
    """Every pending pricing ticket awaiting review (backend-owned)."""
    now = int(time.time())
    try:
        pending = await pricing.all_pending(token=token)
    except Exception as e:  # noqa: BLE001
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"backend unreachable: {e}"}],
        }
    items = [
        {**c, "age_seconds": max(0, now - int(c.get("submitted_at") or now))}
        for c in pending
    ]
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


async def approve_pricing_change(args: dict[str, Any], token: str | None) -> dict[str, Any]:
    ticket = str(args.get("ticket", "")).strip().upper()
    if not ticket:
        return {"isError": True, "content": [{"type": "text", "text": "ticket is required"}]}
    try:
        change = await pricing.approve_change(ticket, token=token)
    except Exception as e:  # noqa: BLE001
        return {"isError": True, "content": [{"type": "text", "text": f"backend unreachable: {e}"}]}
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


async def reject_pricing_change(args: dict[str, Any], token: str | None) -> dict[str, Any]:
    ticket = str(args.get("ticket", "")).strip().upper()
    if not ticket:
        return {"isError": True, "content": [{"type": "text", "text": "ticket is required"}]}
    reason = (args.get("reason") or "").strip() or None
    try:
        change = await pricing.reject_change(ticket, reason, token=token)
    except Exception as e:  # noqa: BLE001
        return {"isError": True, "content": [{"type": "text", "text": f"backend unreachable: {e}"}]}
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
    """Forecast jobs are still frontend-owned (the runner lives here)."""
    job_id = str(args.get("job_id", "")).strip()
    if not job_id:
        return {"isError": True, "content": [{"type": "text", "text": "job_id is required"}]}
    job = state.jobs.get(job_id)
    if not job:
        return {"isError": True, "content": [{"type": "text", "text": f"No job: {job_id}"}]}
    pending_count = len(job.get("pending_pricing") or [])
    drift_count = len(job.get("current_drifts") or [])
    if job.get("status") == "done":
        r = job.get("result") or {}
        text = (
            f"{job_id} ({job.get('region')}): done · uplift {r.get('uplift_pct')}% "
            f"· baseline {r.get('baseline_units'):,} u · confidence "
            f"{(r.get('confidence') or 0)*100:.1f}% · pending drag "
            f"{r.get('pricing_drag_pct')}pp · baseline shift "
            f"{r.get('baseline_shift_pct')}%"
        )
    else:
        text = (
            f"{job_id} ({job.get('region')}): {job.get('status')} "
            f"({job.get('progress', 0)}% · {job.get('step_label', job.get('step'))}) "
            f"· factoring {pending_count} pending + {drift_count} drifted"
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
            "current_drifts": job.get("current_drifts") or [],
            "result": job.get("result"),
        },
    }


async def simulate_pricing_impact(args: dict[str, Any], token: str | None) -> dict[str, Any]:
    sku = str(args.get("sku", "")).strip().upper()
    try:
        new_price = float(args.get("new_price"))
    except (TypeError, ValueError):
        return {"isError": True, "content": [{"type": "text", "text": "new_price must be a number"}]}
    try:
        sim = await pricing.simulate(sku, new_price, token=token)
    except Exception as e:  # noqa: BLE001
        return {"isError": True, "content": [{"type": "text", "text": f"backend unreachable: {e}"}]}
    if not sim:
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown SKU: {sku}"}]}
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
    "launch_shiny": launch_shiny,
    "launch_shiny_embedded": launch_shiny_embedded,
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
