"""
Shell HTML rendering.

The template lives at `app/ui/templates/shell.html` and references CSS and
JS from `/static/`. `BASE_URL` is interpolated as a JSON literal so the JS
side gets it as a real string.
"""

from __future__ import annotations

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .. import state
from ..config import BASE_URL, TEMPLATES_DIR

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html"]),
)


def render_shell_html() -> str:
    """Render the shell SPA. Used by both /ui/shell and resources/read."""
    template = _env.get_template("shell.html")
    return template.render(
        base_url=BASE_URL,
        shell_state=state.shell_state,
    )
