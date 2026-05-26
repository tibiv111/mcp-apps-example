"""
Mock catalog the backend MCP serves. Hardcoded — would normally be a DB or
upstream API in a real deployment.
"""

from __future__ import annotations

from typing import Any

CATALOG: dict[str, dict[str, Any]] = {
    "SKU-X12": {
        "name": "Atlas Hedge",
        "price": 129.00,
        "currency": "USD",
        "in_stock": True,
        "last_updated": "2026-05-26T09:14:00Z",
    },
    "SKU-A04": {
        "name": "Cobalt Growth",
        "price": 84.50,
        "currency": "USD",
        "in_stock": True,
        "last_updated": "2026-05-26T09:14:00Z",
    },
    "SKU-R21": {
        "name": "Reserve Income",
        "price": 212.75,
        "currency": "USD",
        "in_stock": False,
        "last_updated": "2026-05-25T17:02:00Z",
    },
    "SKU-V07": {
        "name": "Vector Macro",
        "price": 451.10,
        "currency": "USD",
        "in_stock": True,
        "last_updated": "2026-05-26T08:48:00Z",
    },
}
