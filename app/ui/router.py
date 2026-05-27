"""
Browser-side preview of the shell SPA — useful for visual development without
spinning up an MCP host. Tool buttons will no-op because there's no parent
window listening for postMessage, but navigation works.

This module also serves `/dashboard/snapshot`, the live aggregate the
dashboard view pulls from. It composes counts across the pricing book and
the running job state so the dashboard reflects what's actually happening
rather than the hardcoded numbers we used to render.
"""

from __future__ import annotations

import time

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from .. import pricing, state
from ..config import BASE_URL, SHELL_MIME
from .render import render_shell_html, render_template

router = APIRouter()


@router.get("/ui/shell", response_class=HTMLResponse)
async def ui_shell_preview() -> HTMLResponse:
    return HTMLResponse(render_shell_html(), media_type=SHELL_MIME)


@router.get("/ui/peer", response_class=HTMLResponse)
async def ui_bus_peer() -> HTMLResponse:
    """Standalone bus peer iframe — same role the Shiny iframe plays when
    mounted by Claude, but with no Shiny dependency. Open in a separate
    browser tab to demo the two-iframe pattern locally."""
    return HTMLResponse(render_template("peer.html", base_url=BASE_URL))


@router.get("/dashboard/snapshot")
async def dashboard_snapshot() -> JSONResponse:
    """
    Aggregate of pricing-book and job state for the dashboard view. Cheap
    enough to call on every dashboard mount + every pricing-event push.

    pricing.snapshot() is an async HTTP call to the backend — the book
    is no longer in this process.
    """
    try:
        book = await pricing.snapshot()
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": f"backend unreachable: {e}"}, status_code=503)
    jobs = list(state.jobs.values())
    running_jobs = [j for j in jobs if j.get("status") == "running"]
    done_jobs = [j for j in jobs if j.get("status") == "done"]
    recent_jobs = sorted(jobs, key=lambda j: j.get("started_at", 0), reverse=True)[:5]
    return JSONResponse(
        {
            "now": int(time.time()),
            "products": book["products"],
            "in_stock": book["in_stock"],
            "out_of_stock": book["out_of_stock"],
            "pending_pricing_changes": book["pending_changes"],
            "recent_pending": book["recent_pending"],
            "jobs_total": len(jobs),
            "jobs_running": len(running_jobs),
            "jobs_done": len(done_jobs),
            "recent_jobs": [
                {
                    "id": j["id"],
                    "region": j.get("region"),
                    "status": j.get("status"),
                    "progress": j.get("progress"),
                    "started_at": j.get("started_at"),
                    "pending_pricing_count": len(j.get("pending_pricing") or []),
                }
                for j in recent_jobs
            ],
        }
    )
