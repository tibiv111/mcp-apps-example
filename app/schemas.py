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
