"""
Pricing book — the canonical store for pricing state in the demo.

Lives on the BACKEND MCP service. The frontend MCP (the one Claude talks
to) is a thin proxy that calls into this through HTTP. That separation
means:

- Single source of truth: every read and every write lands here, whether
  it came from the iframe (via the frontend's MCP), from chat (also via
  the frontend's MCP), or from a hypothetical third caller.
- Mutations publish onto a backend-side event bus
  (`app.backend.events`), which the `/backend/pricing-events` SSE
  endpoint and the frontend's bridge both consume.

In split deploys, the frontend HTTP-calls the backend for every pricing
op. In combined deploys (single process), the same HTTP calls go to
localhost — works identically, just no network hop.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

from . import events
from .data import CATALOG

# sku → { sku, name, current_price, currency, in_stock, last_updated, pending_changes }
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


def all_current_drifts() -> list[dict[str, Any]]:
    """Per-SKU drift between seed catalog price and current effective price."""
    _ensure_seeded()
    drifts: list[dict[str, Any]] = []
    for sku, entry in _book.items():
        seed = CATALOG.get(sku, {}).get("price")
        cur = entry.get("current_price")
        if seed is None or cur is None or not seed:
            continue
        if abs(cur - seed) < 0.005:
            continue
        drifts.append(
            {
                "sku": sku,
                "name": entry.get("name"),
                "seed_price": float(seed),
                "current_price": float(cur),
                "drift_pct": round((cur - seed) / seed * 100.0, 2),
            }
        )
    return drifts


def find_change(ticket: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    _ensure_seeded()
    ticket = (ticket or "").strip().upper()
    if not ticket:
        return None
    for entry in _book.values():
        for change in entry.get("pending_changes", []):
            if change.get("ticket", "").upper() == ticket:
                return entry, change
    return None


def submit_change(product: str, new_price: float) -> dict[str, Any]:
    _ensure_seeded()
    sku = (product or "").strip().upper() or "UNKNOWN"
    entry = _book.get(sku)
    if entry is None:
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
    events.publish("pricing.submitted", change)
    return change


def approve_change(ticket: str) -> dict[str, Any] | None:
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
    events.publish("pricing.approved", change)
    return change


def reject_change(ticket: str, reason: str | None = None) -> dict[str, Any] | None:
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
    events.publish("pricing.rejected", change)
    return change


# Mirrors the ELASTICITY constant in app/jobs/runner.py — kept in sync by hand.
_FORECAST_ELASTICITY = 0.5


def simulate(sku: str, new_price: float) -> dict[str, Any] | None:
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


def snapshot() -> dict[str, Any]:
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
