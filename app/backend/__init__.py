"""
Mock 'backend' MCP server.

Conceptually a separate service: the frontend MCP server (the one Claude
talks to) calls into this over real HTTP. In this repo both halves live in
the same package so the example deploys as one unit OR as two — the
combined `app.create_app()` factory mounts the backend router alongside the
frontend, while `app.backend.create_backend_app()` builds a standalone
FastAPI app that exposes ONLY the backend.

When deployed split, the backend validates bearer tokens by calling the
frontend's RFC 7662 introspection endpoint (`FRONTEND_URL/oauth/introspect`)
rather than touching the frontend's in-process token set.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware


def create_backend_app() -> FastAPI:
    """Build a FastAPI app that exposes only the backend MCP."""
    from .router import router as backend_router

    app = FastAPI(title="NAV AI Backend MCP", version="0.1.0")

    # The backend is called server-to-server by the frontend, but we keep
    # CORS permissive for the same reason as the frontend — useful for
    # local browser-based debugging.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )

    app.include_router(backend_router)

    @app.get("/")
    async def root() -> dict[str, object]:
        return {"service": "nav-ai-backend", "mcp_endpoint": "/backend/mcp"}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
