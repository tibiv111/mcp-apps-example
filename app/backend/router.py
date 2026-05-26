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

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .. import pricing as pricing_book
from .. import state as app_state
from ..config import FRONTEND_URL, PROTOCOL_VERSION
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
        "description": "Fetch catalog entry for a single SKU.",
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
        "description": "Return all known products in the catalog.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]


def _tool_get_product(args: dict[str, Any]) -> dict[str, Any]:
    sku = str(args.get("sku", "")).strip().upper()
    if not sku:
        return {
            "isError": True,
            "content": [{"type": "text", "text": "sku is required"}],
        }
    entry = CATALOG.get(sku)
    if not entry:
        return {
            "isError": True,
            "content": [{"type": "text", "text": f"Unknown SKU: {sku}"}],
            "structuredContent": {"sku": sku, "found": False},
        }
    # Overlay pending pricing changes from the shared book so the catalog
    # reflects what was submitted in another view.
    book_entry = pricing_book.get_entry(sku) or {}
    pending = pricing_book.pending_for_sku(sku)
    current_price = book_entry.get("current_price", entry["price"])
    latest_pending = pending[-1] if pending else None

    summary = f"{sku} · {entry['name']} · {current_price} {entry['currency']}"
    if latest_pending:
        summary += (
            f" · pending {latest_pending['new_price']:.2f} "
            f"({latest_pending['ticket']}, {latest_pending['status'].replace('_', ' ')})"
        )
    return {
        "content": [{"type": "text", "text": summary}],
        "structuredContent": {
            "sku": sku,
            "found": True,
            **entry,
            "price": current_price,
            "current_price": current_price,
            "pending_changes": pending,
            "has_pending": bool(pending),
        },
    }


def _tool_list_products(_args: dict[str, Any]) -> dict[str, Any]:
    items = []
    for sku, entry in CATALOG.items():
        pending = pricing_book.pending_for_sku(sku)
        items.append({"sku": sku, **entry, "pending_changes": pending})
    return {
        "content": [{"type": "text", "text": f"{len(items)} products in catalog"}],
        "structuredContent": {"items": items},
    }


BACKEND_TOOL_HANDLERS: dict[str, Any] = {
    "get_product": _tool_get_product,
    "list_products": _tool_list_products,
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
