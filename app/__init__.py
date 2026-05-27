"""
FastAPI application factory.

`create_app()` is the single place that knows how every concern (MCP, OAuth,
jobs, UI, static assets) gets wired together. The entrypoint (`main.py`)
just calls it.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import bridge
from .admin.router import router as admin_router
from .backend.router import router as backend_router
from .config import BASE_URL, SERVER_NAME, SERVER_VERSION, STATIC_DIR
from .diagnostics.router import router as diagnostics_router
from .jobs.sse import router as jobs_router
from .mcp.router import router as mcp_router
from .oauth.router import router as oauth_router
from .shiny_proxy import router as shiny_proxy_router
from .ui.router import router as ui_router


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """
    Spin the pricing-event bridge for the duration of the app's lifecycle.

    The bridge connects to the backend MCP's `/backend/pricing-events`
    SSE stream and republishes events to every iframe via the frontend's
    `/shell/events`. Without it, a chat-side approve would mutate the
    backend's book but the open workspace UI wouldn't notice until the
    user clicked something.
    """
    stop = asyncio.Event()
    task = asyncio.create_task(bridge.run_bridge(stop), name="pricing-bridge")
    try:
        yield
    finally:
        stop.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task


def create_app() -> FastAPI:
    app = FastAPI(
        title="NAV AI Mock MCP",
        version=SERVER_VERSION,
        lifespan=_lifespan,
    )

    # Permissive CORS so the iframe (hosted on *.claudemcpcontent.com) can
    # talk to /jobs/{id}/events and /static/*. This is a mock; tighten for
    # production deployments.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    # Serve shell.css and shell.js from /static/<file>.
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # Routers.
    app.include_router(mcp_router)
    app.include_router(oauth_router)
    app.include_router(jobs_router)
    app.include_router(ui_router)
    # Backend MCP — conceptually a separate service, called by the frontend
    # MCP's `lookup_product` tool. Mounted in the same process for demo
    # simplicity.
    app.include_router(backend_router)
    # Diagnostics console + live SSE feed of the trace bus.
    app.include_router(diagnostics_router)
    # Admin endpoints that exercise server-pushed resource updates.
    app.include_router(admin_router)
    # Same-origin reverse proxy in front of the standalone R Shiny service.
    # Demonstrated as one of the integration options in the Shiny launcher tab.
    app.include_router(shiny_proxy_router)

    @app.get("/")
    async def root() -> dict[str, object]:
        return {
            "service": SERVER_NAME,
            "version": SERVER_VERSION,
            "mcp_endpoint": f"{BASE_URL}/mcp",
            "preview_ui": f"{BASE_URL}/ui/shell",
        }

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
