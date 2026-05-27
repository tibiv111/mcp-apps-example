"""
Declarative MCP tool & resource definitions.

These dicts are returned verbatim from `tools/list` and `resources/list`.
Adding a new tool means appending here AND registering a handler in
`app.mcp.tools.TOOL_HANDLERS` — nothing in the dispatcher needs to change.
"""

from __future__ import annotations

from typing import Any

from .config import (
    BASE_URL,
    SHELL_MIME,
    SHELL_URI,
    SHINY_EMBED_URI,
    SHINY_RESOURCE_MIME,
    SHINY_URI,
    SHINY_URL,
    to_ws_url,
)

TOOLS: list[dict[str, Any]] = [
    {
        "name": "launch_nav_ai",
        "title": "Launch NAV AI",
        "description": (
            "Open the NAV AI workspace inline. Shows the launcher for pricing "
            "actions and demand forecasts."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        # Only the launching tool carries a resourceUri; the host uses this to
        # mount the iframe alongside the tool result.
        "_meta": {"ui": {"resourceUri": SHELL_URI}},
    },
    {
        "name": "launch_shiny",
        "title": "Open Shiny dashboard",
        "description": (
            "Open the standalone R Shiny dashboard. Returns a URL-form MCP "
            "resource pointing at the Shiny service; a compliant host opens "
            "its own iframe at that URL, sidestepping the shell's CSP. Use "
            "this to demonstrate Card D in the Shiny launcher tab."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        # `resourceUri` tells the host which resource to fetch + render
        # alongside the tool result. Mirrors the launch_nav_ai pattern, but
        # the referenced resource is URL-form rather than inline HTML.
        "_meta": {"ui": {"resourceUri": SHINY_URI}},
    },
    {
        "name": "launch_shiny_embedded",
        "title": "Open Shiny (server-side embed)",
        "description": (
            "Server-side embed of the Shiny dashboard as an inline-HTML MCP "
            "resource. The server fetches Shiny's HTML, rewrites every "
            "asset path through our reverse proxy, and patches the "
            "WebSocket constructor so it points back at the proxy. Same "
            "rendering path as the existing NAV AI shell — Card E in the "
            "Shiny launcher tab."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        "_meta": {"ui": {"resourceUri": SHINY_EMBED_URI}},
    },
    {
        "name": "submit_pricing_change",
        "title": "Submit pricing change",
        "description": "Submit a pricing change for review. Returns a ticket ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "product": {"type": "string", "description": "Product SKU or name."},
                "new_price": {"type": "number", "description": "Proposed new price."},
            },
            "required": ["product", "new_price"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_product",
        "title": "Look up product",
        "description": (
            "Look up a product in the catalog. Delegates to the backend MCP "
            "server using the caller's OAuth token."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {"type": "string", "description": "Product SKU (e.g. SKU-X12)."},
            },
            "required": ["sku"],
            "additionalProperties": False,
        },
    },
    {
        "name": "start_forecast",
        "title": "Start demand forecast",
        "description": (
            "Kick off a demand forecast job. Returns a job_id; progress streams via SSE. "
            "Automatically factors in every currently-pending pricing change "
            "as a price-elasticity drag on uplift."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "region": {
                    "type": "string",
                    "description": "Region code (e.g. EU, US, APAC). Defaults to GLOBAL.",
                },
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "list_products",
        "title": "List products",
        "description": (
            "List every product in the catalog with current price, pending change "
            "count, and stock status. Use when the user asks 'what do we sell' or "
            "wants an overview before drilling into a specific SKU."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "list_pending_changes",
        "title": "List pending pricing changes",
        "description": (
            "Return every pricing change currently waiting for review, with "
            "ticket id, SKU, previous and new price, percent delta, and how "
            "long it has been queued. Empty list if nothing is pending."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "approve_pricing_change",
        "title": "Approve a pricing change",
        "description": (
            "Approve a pending pricing change by ticket id. Sets the SKU's "
            "current price to the new price, removes the ticket from the "
            "pending queue, and broadcasts a live update so any open "
            "workspace iframe refreshes its catalog/dashboard views. "
            "Returns the approved change record."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Ticket id from submit_pricing_change, e.g. 'PR-A4'.",
                },
            },
            "required": ["ticket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "reject_pricing_change",
        "title": "Reject a pricing change",
        "description": (
            "Reject a pending pricing change by ticket id. Removes the ticket "
            "from the pending queue without changing the current price. "
            "Broadcasts a live update so open workspaces refresh."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ticket": {
                    "type": "string",
                    "description": "Ticket id from submit_pricing_change.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason for rejection (recorded on the change).",
                },
            },
            "required": ["ticket"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_job",
        "title": "Get forecast job",
        "description": (
            "Fetch a forecast job by id, including status, progress, the "
            "final result if complete, and which pricing changes the model "
            "factored into the uplift drag."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "Job id from start_forecast, e.g. 'job-eb0feb'.",
                },
            },
            "required": ["job_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "simulate_pricing_impact",
        "title": "Simulate pricing impact",
        "description": (
            "What-if: project the marginal uplift drag if a SKU were re-priced "
            "at a hypothetical new price. Uses the same price-elasticity model "
            "the forecast runner applies but does NOT submit or persist "
            "anything. Useful for answering 'what would happen if I raised X to $Y?'"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "sku": {
                    "type": "string",
                    "description": "Product SKU to simulate.",
                },
                "new_price": {
                    "type": "number",
                    "description": "Hypothetical new price (USD).",
                },
            },
            "required": ["sku", "new_price"],
            "additionalProperties": False,
        },
    },
]


RESOURCES: list[dict[str, Any]] = [
    {
        "uri": SHELL_URI,
        "name": "NAV AI shell",
        "description": "Interactive workspace for NAV AI pricing and forecasting.",
        "mimeType": SHELL_MIME,
        "_meta": {
            "ui": {
                # connectDomains  → CSP connect-src (SSE/fetch back to our server)
                # resourceDomains → CSP script-src/style-src (shell.css, shell.js)
                # Without resourceDomains the host CSP blocks our /static assets.
                #
                # No frameDomains: the Claude host currently hard-codes
                # `frame-src 'self' blob: data:` regardless of what we send.
                # If that ever changes, Cards A and C in the Shiny tab would
                # need it added back here pointing at BASE_URL and SHINY_URL.
                "csp": {
                    "connectDomains": [BASE_URL],
                    "resourceDomains": [BASE_URL],
                },
                "prefersBorder": True,
            }
        },
    },
    {
        # Card D: URL-form MCP resource. The host is expected to open its own
        # iframe at `externalUrl` rather than embedding inline HTML — that's
        # how URL-form resources sidestep the shell's CSP. Whether the host
        # actually does that is up to it; older Claude builds simply render
        # the resource as text.
        "uri": SHINY_URI,
        "name": "NAV AI — R Shiny dashboard",
        "description": "External R Shiny dashboard for pricing telemetry.",
        "mimeType": SHINY_RESOURCE_MIME,
        "_meta": {
            "ui": {
                "externalUrl": SHINY_URL,
                "prefersBorder": True,
            }
        },
    },
    {
        # Card E: inline-HTML MCP resource whose body is Shiny's own HTML,
        # fetched + rewritten server-side. Same rendering path as the NAV
        # AI shell, so today's Claude actually renders it. CSP hints must
        # allow connect/script/style back to BASE_URL because every
        # rewritten asset URL and the patched WebSocket point there.
        #
        # `connectDomains` lists BOTH the https and wss forms of BASE_URL
        # because browsers don't auto-extend a `https://host` source to
        # `wss://host` — the wss form must be enumerated or Shiny's
        # WebSocket gets blocked by `connect-src`.
        "uri": SHINY_EMBED_URI,
        "name": "NAV AI — R Shiny (server-side embed)",
        "description": "R Shiny dashboard embedded via server-side HTML rewrite + WS shim.",
        "mimeType": SHELL_MIME,
        "_meta": {
            "ui": {
                "csp": {
                    "connectDomains": [BASE_URL, to_ws_url(BASE_URL)],
                    "resourceDomains": [BASE_URL],
                },
                "prefersBorder": True,
            }
        },
    },
]
