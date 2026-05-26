"""
FastAPI application factory.

`create_app()` is the single place that knows how every concern (MCP, OAuth,
jobs, UI, static assets) gets wired together. The entrypoint (`main.py`)
just calls it.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .backend.router import router as backend_router
from .config import BASE_URL, SERVER_NAME, SERVER_VERSION, STATIC_DIR
from .jobs.sse import router as jobs_router
from .mcp.router import router as mcp_router
from .oauth.router import router as oauth_router
from .ui.router import router as ui_router


def create_app() -> FastAPI:
    app = FastAPI(title="NAV AI Mock MCP", version=SERVER_VERSION)

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
