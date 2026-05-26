"""
Frontend-side pricing client.

The pricing book lives on the BACKEND MCP service ([app/backend/pricing.py]).
Every read and every write goes through this client, which posts JSON-RPC
`tools/call` requests to the backend over HTTP. In split deploys that's a
real network hop; in combined-mode deploys it's a localhost call.

Why the indirection — single source of truth. The frontend MCP (the
service Claude talks to) is a thin proxy: tool handlers that touch
pricing data forward to this client, which forwards to the backend.
The book exists in exactly one process, and any mutation announces
itself via the backend's `/backend/pricing-events` SSE stream — which
the frontend's bridge republishes onto `/shell/events` so live iframes
update without polling.

All functions are async because they do HTTP. Callers are MCP tool
handlers (already async) and the dashboard snapshot endpoint
(also async), so awaiting is natural.

Auth: a bearer token is required by the backend. Callers should pass
the user's MCP token through; for service-to-service calls without a
user (e.g. lifespan bridge bootstrap), pass `service_token()`.
"""

from __future__ import annotations

import secrets
from typing import Any

import httpx

from . import state
from .config import BACKEND_URL

# Single service token the backend will accept for frontend-initiated
# operations that aren't tied to a user request (currently none — every
# call we make is in response to a user MCP request that already has a
# bearer). Kept here for future use and for tests.
_SERVICE_TOKEN = "svc-" + secrets.token_hex(8)


def service_token() -> str:
    """Returns the frontend's service token, registering it on first use."""
    state.issued_tokens.add(_SERVICE_TOKEN)
    return _SERVICE_TOKEN


async def _call(name: str, args: dict[str, Any] | None, token: str | None) -> dict[str, Any]:
    """
    Generic backend MCP tool call. Returns the result dict (with
    `content`, `structuredContent`, optional `isError`). Raises on
    transport failure.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": name, "arguments": args or {}},
    }
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{BACKEND_URL}/backend/mcp",
            json=payload,
            headers=headers,
        )
    if resp.status_code >= 400:
        raise RuntimeError(
            f"backend pricing call '{name}' returned HTTP {resp.status_code}"
        )
    body = resp.json()
    if "error" in body:
        raise RuntimeError(
            f"backend pricing call '{name}' returned error: {body['error'].get('message')}"
        )
    return body.get("result") or {}


def _structured(result: dict[str, Any]) -> dict[str, Any]:
    """Pull structuredContent from a tool result, normalizing to {}."""
    return result.get("structuredContent") or {}


# ── reads ──────────────────────────────────────────────────────────────

async def get_entry(sku: str, token: str | None = None) -> dict[str, Any] | None:
    res = await _call("get_product", {"sku": sku}, token or service_token())
    sc = _structured(res)
    if res.get("isError") or sc.get("found") is False:
        return None
    return sc


async def list_entries(token: str | None = None) -> list[dict[str, Any]]:
    res = await _call("list_products", {}, token or service_token())
    return list(_structured(res).get("items") or [])


async def pending_for_sku(sku: str, token: str | None = None) -> list[dict[str, Any]]:
    entry = await get_entry(sku, token=token)
    return list(entry.get("pending_changes") or []) if entry else []


async def all_pending(token: str | None = None) -> list[dict[str, Any]]:
    res = await _call("list_pending_changes", {}, token or service_token())
    return list(_structured(res).get("items") or [])


async def all_current_drifts(token: str | None = None) -> list[dict[str, Any]]:
    res = await _call("get_current_drifts", {}, token or service_token())
    return list(_structured(res).get("items") or [])


async def snapshot(token: str | None = None) -> dict[str, Any]:
    res = await _call("get_pricing_snapshot", {}, token or service_token())
    return _structured(res)


async def simulate(sku: str, new_price: float, token: str | None = None) -> dict[str, Any] | None:
    res = await _call(
        "simulate_pricing_impact",
        {"sku": sku, "new_price": new_price},
        token or service_token(),
    )
    if res.get("isError"):
        return None
    return _structured(res)


# ── writes ─────────────────────────────────────────────────────────────

async def submit_change(product: str, new_price: float, token: str | None = None) -> dict[str, Any]:
    res = await _call(
        "submit_pricing_change",
        {"product": product, "new_price": new_price},
        token or service_token(),
    )
    return _structured(res)


async def approve_change(ticket: str, token: str | None = None) -> dict[str, Any] | None:
    res = await _call(
        "approve_pricing_change",
        {"ticket": ticket},
        token or service_token(),
    )
    if res.get("isError"):
        return None
    return _structured(res)


async def reject_change(ticket: str, reason: str | None = None, token: str | None = None) -> dict[str, Any] | None:
    args: dict[str, Any] = {"ticket": ticket}
    if reason:
        args["reason"] = reason
    res = await _call(
        "reject_pricing_change",
        args,
        token or service_token(),
    )
    if res.get("isError"):
        return None
    return _structured(res)
