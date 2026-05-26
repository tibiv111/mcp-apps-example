"""
Backend MCP server — mounted at /backend/mcp.

This is the 'other' MCP server in the example: Claude does NOT talk to it
directly. The frontend MCP's `lookup_product` tool forwards an HTTP call
here, passing along the same `Authorization: Bearer <token>` header it
received from Claude. That demonstrates how a user-facing MCP server can
delegate to a deeper backend MCP server with shared auth.

The bearer token is validated against `state.issued_tokens` — the same set
the frontend OAuth populates — so any token Claude obtained through the
frontend's /oauth/* flow is automatically accepted here. In a real
deployment the two halves would share a JWKS endpoint or token introspection
service rather than an in-process set.
"""

from __future__ import annotations

from typing import Any

import asyncio
import json
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .. import state as app_state
from ..config import FRONTEND_URL, PROTOCOL_VERSION
from . import events as backend_events
from . import pricing as pricing_book
from .data import CATALOG

router = APIRouter()


BACKEND_SERVER_NAME = "nav-ai-backend"
BACKEND_SERVER_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# JSON-RPC + auth helpers
# ---------------------------------------------------------------------------

def _result(req_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


async def _safe_json(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        return None


async def _require_bearer(authorization: str | None) -> str:
    """
    Extract the bearer token and confirm it was issued by our OAuth layer.
    Raises 401 otherwise. Unlike the frontend /mcp (which currently accepts
    anything), the backend enforces — that's the whole point of the demo.

    Two modes:
      - Combined deploy (FRONTEND_URL unset): check the in-process
        `state.issued_tokens` set. Works because the frontend's OAuth handler
        populates that set in the same process.
      - Split deploy (FRONTEND_URL set): POST to the frontend's
        /oauth/introspect endpoint (RFC 7662). The two processes don't share
        memory; introspection is how the resource server validates tokens
        issued by a separate auth server.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="empty bearer token")

    if FRONTEND_URL:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.post(
                    f"{FRONTEND_URL}/oauth/introspect",
                    data={"token": token},
                )
        except httpx.HTTPError as e:
            raise HTTPException(status_code=503, detail=f"auth server unreachable: {e}")
        if resp.status_code != 200 or not (resp.json() or {}).get("active"):
            raise HTTPException(status_code=401, detail="token introspection failed")
        return token

    if token not in app_state.issued_tokens:
        raise HTTPException(status_code=401, detail="unknown bearer token")
    return token


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

BACKEND_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_product",
        "title": "Get product",
        "description": "Fetch catalog entry for a single SKU, joined with pricing state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Product SKU code."},
            },
            "required": ["sku"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_products",
        "title": "List products",
        "description": "Return all products with current price and pending change count.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_pricing_change",
        "title": "Submit pricing change (backend)",
        "description": "Record a new pending pricing change in the book.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product": {"type": "string"},
                "new_price": {"type": "number"},
            },
            "required": ["product", "new_price"],
            "additionalProperties": False,
        },
    },
    {
        "name": "approve_pricing_change",
        "title": "Approve pricing change (backend)",
        "description": "Promote a pending ticket to current price and remove from pending.",
        "inputSchema": {
            "type": "object",
            "properties": {"ticket": {"type": "string"}},
            "required": ["ticket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reject_pricing_change",
        "title": "Reject pricing change (backend)",
        "description": "Remove a pending ticket without applying it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket": {"type": "string"},
                "reason": {"type": "string"},
            },
            "required": ["ticket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "list_pending_changes",
        "title": "List pending pricing changes (backend)",
        "description": "Every queued pricing ticket with delta, age, and status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_pricing_snapshot",
        "title": "Pricing book snapshot (backend)",
        "description": "Aggregate counts for dashboards.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_current_drifts",
        "title": "Current price drifts (backend)",
        "description": "For each SKU whose price has drifted from seed, return the percentage drift.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "simulate_pricing_impact",
        "title": "Simulate pricing impact (backend)",
        "description": "What-if elasticity projection for a hypothetical re-price. Does not persist.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string"},
                "new_price": {"type": "number"},
            },
            "required": ["sku", "new_price"],
            "additionalProperties": False,
        },
    },
]


def _enriched_entry(sku: str) -> dict[str, Any] | None:
    """CATALOG row joined with the book's current_price + pending changes."""
    sku = (sku or "").upper()
    cat = CATALOG.get(sku)
    if not cat:
        return None
    book = pricing_book.get_entry(sku) or {}
    pending = pricing_book.pending_for_sku(sku)
    current_price = book.get("current_price", cat["price"])
    return {
        "sku": sku,
        "name": cat["name"],
        "seed_price": cat["price"],
        "current_price": current_price,
        "price": current_price,  # back-compat
        "currency": cat["currency"],
        "in_stock": cat.get("in_stock", True),
        "last_updated": book.get("last_updated", cat.get("last_updated")),
        "pending_changes": pending,
        "has_pending": bool(pending),
        "found": True,
    }


def _tool_get_product(args: dict[str, Any]) -> dict[str, Any]:
    sku = str(args.get("sku", "")).strip().upper()
    if not sku:
        return {"isError": True, "content": [{"type": "text", "text": "sku is required"}]}
    entry = _enriched_entry(sku)
    if not entry:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Unknown SKU: {sku}"}],
            "structuredContent": {"sku": sku, "found": False},
        }
    summary = (
        f"{sku} · {entry['name']} · {entry['current_price']} {entry['currency']}"
    )
    if entry["pending_changes"]:
        p = entry["pending_changes"][-1]
        summary += (
            f" · pending {p['new_price']:.2f} "
            f"({p['ticket']}, {p['status'].replace('_', ' ')})"
        )
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": entry,
    }


def _tool_list_products(_args: dict[str, Any]) -> dict[str, Any]:
    items = [_enriched_entry(sku) for sku in CATALOG.keys()]
    items = [i for i in items if i]
    return {
        "content": [{"type": "text", "text": f"{len(items)} products in catalog"}],
        "structuredContent": {"items": items},
    }


def _tool_submit(args: dict[str, Any]) -> dict[str, Any]:
    product = str(args.get("product", "")).strip() or "UNKNOWN"
    try:
        new_price = float(args.get("new_price"))
    except (TypeError, ValueError):
        return {"isError": True, "content": [{"type": "text", "text": "new_price must be a number"}]}
    change = pricing_book.submit_change(product, new_price)
    return {
        "content": [{"type": "text", "text": f"Submitted {change['ticket']}"}],
        "structuredContent": change,
    }


def _tool_approve(args: dict[str, Any]) -> dict[str, Any]:
    ticket = str(args.get("ticket", "")).strip().upper()
    if not ticket:
        return {"isError": True, "content": [{"type": "text", "text": "ticket is required"}]}
    change = pricing_book.approve_change(ticket)
    if not change:
        return {"isError": True, "content": [{"type": "text", "text": f"No pending ticket: {ticket}"}]}
    return {
        "content": [{"type": "text", "text": f"Approved {change['ticket']}"}],
        "structuredContent": change,
    }


def _tool_reject(args: dict[str, Any]) -> dict[str, Any]:
    ticket = str(args.get("ticket", "")).strip().upper()
    if not ticket:
        return {"isError": True, "content": [{"type": "text", "text": "ticket is required"}]}
    reason = (args.get("reason") or "").strip() or None
    change = pricing_book.reject_change(ticket, reason)
    if not change:
        return {"isError": True, "content": [{"type": "text", "text": f"No pending ticket: {ticket}"}]}
    return {
        "content": [{"type": "text", "text": f"Rejected {change['ticket']}"}],
        "structuredContent": change,
    }


def _tool_list_pending(_args: dict[str, Any]) -> dict[str, Any]:
    pending = pricing_book.all_pending()
    return {
        "content": [{"type": "text", "text": f"{len(pending)} pending change(s)"}],
        "structuredContent": {"items": pending, "count": len(pending)},
    }


def _tool_snapshot(_args: dict[str, Any]) -> dict[str, Any]:
    snap = pricing_book.snapshot()
    return {
        "content": [{"type": "text", "text": f"{snap['products']} products, {snap['pending_changes']} pending"}],
        "structuredContent": snap,
    }


def _tool_drifts(_args: dict[str, Any]) -> dict[str, Any]:
    drifts = pricing_book.all_current_drifts()
    return {
        "content": [{"type": "text", "text": f"{len(drifts)} drifted SKU(s)"}],
        "structuredContent": {"items": drifts, "count": len(drifts)},
    }


def _tool_simulate(args: dict[str, Any]) -> dict[str, Any]:
    sku = str(args.get("sku", "")).strip().upper()
    try:
        new_price = float(args.get("new_price"))
    except (TypeError, ValueError):
        return {"isError": True, "content": [{"type": "text", "text": "new_price must be a number"}]}
    sim = pricing_book.simulate(sku, new_price)
    if not sim:
        return {"isError": True, "content": [{"type": "text", "text": f"Unknown SKU: {sku}"}]}
    return {
        "content": [{"type": "text", "text": "what-if simulation"}],
        "structuredContent": sim,
    }


BACKEND_TOOL_HANDLERS: dict[str, Any] = {
    "get_product": _tool_get_product,
    "list_products": _tool_list_products,
    "submit_pricing_change": _tool_submit,
    "approve_pricing_change": _tool_approve,
    "reject_pricing_change": _tool_reject,
    "list_pending_changes": _tool_list_pending,
    "get_pricing_snapshot": _tool_snapshot,
    "get_current_drifts": _tool_drifts,
    "simulate_pricing_impact": _tool_simulate,
}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/backend/mcp")
async def backend_mcp_endpoint(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Response:
    await _require_bearer(authorization)

    payload = await _safe_json(request)
    if not isinstance(payload, dict):
        return JSONResponse(_error(None, -32700, "Parse error"))

    method = payload.get("method")
    req_id = payload.get("id")
    params = payload.get("params") or {}

    try:
        if method == "initialize":
            return JSONResponse(
                _result(
                    req_id,
                    {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {
                            "name": BACKEND_SERVER_NAME,
                            "version": BACKEND_SERVER_VERSION,
                        },
                    },
                )
            )

        if method == "ping":
            return JSONResponse(_result(req_id, {}))

        if method == "tools/list":
            return JSONResponse(_result(req_id, {"tools": BACKEND_TOOLS}))

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            handler = BACKEND_TOOL_HANDLERS.get(name)
            if not handler:
                return JSONResponse(_error(req_id, -32602, f"Unknown tool: {name}"))
            return JSONResponse(_result(req_id, handler(args)))

        return JSONResponse(_error(req_id, -32601, f"Method not found: {method}"))
    except Exception as e:  # noqa: BLE001
        return JSONResponse(_error(req_id, -32000, f"Server error: {e}"))


# ---------------------------------------------------------------------------
# Pricing event stream — backend's source of truth for live mutations.
# Public (no auth) because it's a read-only event feed; the frontend's
# bridge connects here and republishes onto its own /shell/events channel
# so iframes get live updates regardless of which service issued the
# mutation.
# ---------------------------------------------------------------------------

@router.get("/backend/pricing-events")
async def backend_pricing_events(request: Request) -> EventSourceResponse:
    queue = backend_events.subscribe()

    async def stream() -> AsyncIterator[dict[str, Any]]:
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": "{}"}
                    continue
                yield {
                    "event": "pricing-event",
                    "data": json.dumps(event),
                }
        finally:
            backend_events.unsubscribe(queue)

    return EventSourceResponse(stream(), headers={"X-Accel-Buffering": "no"})
