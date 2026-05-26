"""
Declarative MCP tool & resource definitions.

These dicts are returned verbatim from `tools/list` and `resources/list`.
Adding a new tool means appending here AND registering a handler in
`app.mcp.tools.TOOL_HANDLERS` — nothing in the dispatcher needs to change.
"""

from __future__ import annotations

from typing import Any

from .config import BASE_URL, SHELL_MIME, SHELL_URI

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
            "Kick off a demand forecast job. Returns a job_id; progress streams via SSE."
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
        "name": "discuss_selection",
        "title": "Discuss with assistant",
        "description": (
            "Invoked from inside the workspace iframe when the user selects "
            "something (a forecast result, a dashboard row, a catalog entry) "
            "and asks the assistant to comment on it. The tool returns a "
            "structured payload plus a natural-language prompt that the host "
            "model is expected to respond to inline in the chat. This is the "
            "iframe → model bidirectional path: the user never typed "
            "anything in chat, but the model still answers them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "What's being discussed: 'forecast', 'pricing', 'catalog', 'dashboard_row'.",
                },
                "context": {
                    "type": "object",
                    "description": "The selected payload (e.g. the forecast result dict).",
                    "additionalProperties": True,
                },
                "question": {
                    "type": "string",
                    "description": "Optional user question. Defaults to a sensible 'comment on this'.",
                },
            },
            "required": ["kind", "context"],
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
                "csp": {
                    "connectDomains": [BASE_URL],
                    "resourceDomains": [BASE_URL],
                },
                "prefersBorder": True,
            }
        },
    }
]
