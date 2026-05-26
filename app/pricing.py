"""
Shared pricing book — the cross-cutting state that ties together pricing
submissions, catalog lookups, and forecasts.

Why this module exists
----------------------
The original demo had three disconnected flows: a pricing form that echoed
a ticket id, a catalog lookup that always returned the same canned price,
and a forecast that generated independent random numbers. Submitting a
pricing change had no effect on either of the other two views. That made
the demo feel hollow.

`submit_change` writes into a single in-memory book. `get_entry` /
`pending_for_sku` / `all_pending` are read from by both the backend's
catalog tool (so a lookup shows pending price proposals) and the forecast
runner (so a 10% price hike depresses uplift via a simple elasticity).

Every mutation also fans out a live event to open iframes through
`state.shell_event_subscribers` and drops a trace record for /diagnostics.
That's how the catalog view auto-refreshes when you submit a price change
in a different tab.

Demo simplification
-------------------
The book lives in-process. In split-deploy mode (backend on a separate
service), the frontend's submit and the backend's lookup wouldn't see the
same state — for that you'd need a database, a Redis pub/sub, or routing
backend lookups through the frontend. The combined-deploy default keeps
everything in one process, which is what most reviewers will try first.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from . import state, trace
from .backend.data import CATALOG

# sku -> { sku, name, current_price, currency, pending_changes: [change, ...] }
# Seeded lazily from the backend catalog on first access.
_book: dict[str, dict[str, Any]] = {}


def _ensure_seeded() -> None:
    if _book:
        return
    for sku, entry in CATALOG.items():
        _book[sku] = {
            "sku": sku,
            "name": entry["name"],
            "current_price": float(entry["price"]),
            "currency": entry.get("currency", "USD"),
            "in_stock": entry.get("in_stock", True),
            "last_updated": entry.get("last_updated"),
            "pending_changes": [],
        }


def get_entry(sku: str) -> dict[str, Any] | None:
    _ensure_seeded()
    return _book.get((sku or "").upper())


def list_entries() -> list[dict[str, Any]]:
    _ensure_seeded()
    return list(_book.values())


def pending_for_sku(sku: str) -> list[dict[str, Any]]:
    entry = get_entry(sku)
    return list(entry["pending_changes"]) if entry else []


def all_pending() -> list[dict[str, Any]]:
    _ensure_seeded()
    out: list[dict[str, Any]] = []
    for entry in _book.values():
        out.extend(entry["pending_changes"])
    return out


def find_change(ticket: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Locate a pending change by ticket id. Returns (entry, change) or None."""
    _ensure_seeded()
    ticket = (ticket or "").strip().upper()
    if not ticket:
        return None
    for entry in _book.values():
        for change in entry.get("pending_changes", []):
            if change.get("ticket", "").upper() == ticket:
                return entry, change
    return None


def approve_change(ticket: str) -> dict[str, Any] | None:
    """
    Approve a pending change. Sets entry.current_price to the new price,
    removes the change from pending, emits a pricing-event so live views
    re-fetch. Returns the approved change record (with status='approved')
    or None if no such ticket.
    """
    found = find_change(ticket)
    if not found:
        return None
    entry, change = found
    entry["current_price"] = float(change["new_price"])
    entry["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    entry["pending_changes"] = [
        c for c in entry["pending_changes"] if c.get("ticket") != change.get("ticket")
    ]
    change = {**change, "status": "approved", "decided_at": int(time.time())}
    _notify("pricing.approved", change)
    return change


def reject_change(ticket: str, reason: str | None = None) -> dict[str, Any] | None:
    """
    Reject a pending change. Removes it from pending without touching the
    current price. Emits a pricing-event so live views re-fetch. Returns
    the rejected change record (with status='rejected') or None.
    """
    found = find_change(ticket)
    if not found:
        return None
    entry, change = found
    entry["pending_changes"] = [
        c for c in entry["pending_changes"] if c.get("ticket") != change.get("ticket")
    ]
    change = {
        **change,
        "status": "rejected",
        "decided_at": int(time.time()),
        "reason": (reason or "").strip() or None,
    }
    _notify("pricing.rejected", change)
    return change


# Mirrors the ELASTICITY constant in jobs/runner.py — kept in sync by hand.
# If you tweak the forecast model, update both.
_FORECAST_ELASTICITY = 0.5


def simulate(sku: str, new_price: float) -> dict[str, Any] | None:
    """
    What-if: project the marginal uplift drag if `sku` were re-priced at
    `new_price`. Doesn't persist anything — just runs the same elasticity
    the forecast runner applies, so chat can answer "what would this do?"
    without polluting the pending queue.
    """
    entry = get_entry(sku)
    if entry is None:
        return None
    previous = float(entry["current_price"])
    new = float(new_price)
    delta_pct = (
        round(((new - previous) / previous) * 100.0, 2) if previous else None
    )
    drag_pct = round((delta_pct or 0) * _FORECAST_ELASTICITY, 2)
    existing_drag = round(
        sum(
            (c.get("delta_pct") or 0) * _FORECAST_ELASTICITY
            for c in all_pending()
        ),
        2,
    )
    return {
        "sku": entry["sku"],
        "name": entry["name"],
        "current_price": previous,
        "hypothetical_price": new,
        "delta_pct": delta_pct,
        "uplift_drag_pct": drag_pct,
        "existing_pending_drag_pct": existing_drag,
        "combined_drag_pct": round(existing_drag + drag_pct, 2),
    }


def submit_change(product: str, new_price: float) -> dict[str, Any]:
    """
    Record a pricing change against the book. Returns the change record.
    Also fans live updates to open iframes and to /diagnostics.
    """
    _ensure_seeded()
    sku = (product or "").strip().upper() or "UNKNOWN"
    entry = _book.get(sku)
    if entry is None:
        # SKU not in catalog — create a stub so the rest of the demo works.
        entry = {
            "sku": sku,
            "name": product,
            "current_price": float(new_price),
            "currency": "USD",
            "in_stock": True,
            "last_updated": None,
            "pending_changes": [],
        }
        _book[sku] = entry

    previous_price = float(entry["current_price"])
    new_price_f = float(new_price)
    delta_pct = (
        round(((new_price_f - previous_price) / previous_price) * 100.0, 2)
        if previous_price
        else None
    )

    change = {
        "ticket": "PR-" + secrets.token_hex(2).upper(),
        "product": sku,
        "name": entry["name"],
        "previous_price": previous_price,
        "new_price": new_price_f,
        "delta_pct": delta_pct,
        "status": "queued_for_review",
        "submitted_at": int(time.time()),
    }
    entry["pending_changes"].append(change)

    _notify("pricing.submitted", change)
    return change


def snapshot() -> dict[str, Any]:
    """Aggregate view for the dashboard."""
    _ensure_seeded()
    entries = list(_book.values())
    pending = all_pending()
    return {
        "products": len(entries),
        "pending_changes": len(pending),
        "recent_pending": pending[-5:][::-1],
        "in_stock": sum(1 for e in entries if e.get("in_stock")),
        "out_of_stock": sum(1 for e in entries if not e.get("in_stock")),
    }


# ── Live fan-out ────────────────────────────────────────────────────────

def _notify(event_type: str, payload: dict[str, Any]) -> None:
    """
    Trace the event and push to every open /shell/events listener so live
    iframes refresh their catalog/dashboard views without a poll.
    """
    trace.record(
        "pricing.event",
        layer="resource",
        summary=f"{event_type}: {payload.get('product', '')} {payload.get('new_price', '')}",
        correlation_id=payload.get("ticket"),
        detail={"type": event_type, "payload": payload},
    )

    sse_payload = {
        "event": "pricing-event",
        "data": json.dumps({"type": event_type, "payload": payload}),
    }
    for q in list(state.shell_event_subscribers):
        try:
            q.put_nowait(sse_payload)
        except asyncio.QueueFull:
            pass
