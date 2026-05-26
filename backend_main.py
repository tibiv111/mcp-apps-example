"""
Entrypoint for the standalone backend MCP. Mirrors `main.py`, but builds the
backend-only FastAPI app.

Run locally:  uvicorn backend_main:app --reload --port 8001
On Render:    startCommand in render.yaml points here for the backend
              service.
"""

from app.backend import create_backend_app

app = create_backend_app()
