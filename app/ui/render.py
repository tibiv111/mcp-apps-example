"""
Template rendering.

The shell template lives at `app/ui/templates/shell.html`; the iframe
bodies served to MCP-mounted Shiny iframes live at
`app/ui/templates/iframes/`. All templates share a single Jinja
environment so autoescape / loader settings stay consistent.
"""

from __future__ import annotations

from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import state
from ..config import BASE_URL, SHINY_URL, TEMPLATES_DIR

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render_template(name: str, **context: Any) -> str:
    """Render any Jinja template under TEMPLATES_DIR by relative path."""
    return _env.get_template(name).render(**context)


def render_shell_html() -> str:
    """Render the shell SPA. Used by both /ui/shell and resources/read."""
    return render_template(
        "shell.html",
        base_url=BASE_URL,
        shiny_url=SHINY_URL,
        shell_state=state.shell_state,
    )
