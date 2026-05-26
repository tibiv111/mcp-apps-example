"""
Static configuration for the NAV AI mock MCP server.

`BASE_URL` is the only value that should ever change between environments. On
Render, set it in the service env vars to the assigned URL (e.g.
`https://nav-mock-mcp.onrender.com`). Without it the iframe's SSE callbacks
fall back to localhost and break inside Claude.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- Deploy-time -------------------------------------------------------------

BASE_URL: str = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")

# Where the frontend MCP finds the backend MCP. Defaults to BASE_URL so the
# combined (single-process) deploy keeps working out of the box. Set this on
# the frontend service when deploying split.
BACKEND_URL: str = os.environ.get("BACKEND_URL", BASE_URL).rstrip("/")

# Where the backend MCP finds the frontend's OAuth server for token
# introspection. Unset → backend uses in-process state.issued_tokens (combined
# mode). Set → backend validates via HTTP POST to FRONTEND_URL/oauth/introspect.
FRONTEND_URL: str | None = (os.environ.get("FRONTEND_URL") or "").rstrip("/") or None

# --- Identity ----------------------------------------------------------------

SERVER_NAME: str = "nav-ai-mock"
SERVER_VERSION: str = "0.1.0"

# --- MCP protocol ------------------------------------------------------------

PROTOCOL_VERSION: str = "2025-06-18"
SHELL_URI: str = "ui://nav-ai/shell"
SHELL_MIME: str = "text/html;profile=mcp-app"  # exact, no space after ';'

# --- Demo values -------------------------------------------------------------

DEMO_USER: str = "demo-user@nav-ai.local"

# --- Paths -------------------------------------------------------------------

# app/config.py → app/ → project root
PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
STATIC_DIR: Path = PROJECT_ROOT / "static"
TEMPLATES_DIR: Path = Path(__file__).resolve().parent / "ui" / "templates"
