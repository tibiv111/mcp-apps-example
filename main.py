"""
Entrypoint for uvicorn / Render. Keep this file thin — all wiring lives in
`app.create_app()`.

Run locally:  uvicorn main:app --reload --port 8000
On Render:    startCommand in render.yaml points here.
"""

from app import create_app

app = create_app()
