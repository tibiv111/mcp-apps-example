"""
Browser-side preview of the shell SPA — useful for visual development without
spinning up an MCP host. Tool buttons will no-op because there's no parent
window listening for postMessage, but navigation works.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from ..config import SHELL_MIME
from .render import render_shell_html

router = APIRouter()


@router.get("/ui/shell", response_class=HTMLResponse)
async def ui_shell_preview() -> HTMLResponse:
    return HTMLResponse(render_shell_html(), media_type=SHELL_MIME)
