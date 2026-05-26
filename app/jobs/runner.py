"""
Mock forecast pipeline. Walks through five labelled steps with a 2 s pause
each, pushing `progress` events to every subscriber queue, then a final
`done` event with the synthetic result payload.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any

from .. import state, trace

STEPS: list[tuple[str, str]] = [
    ("collecting", "Collecting demand signals"),
    ("modeling", "Fitting seasonal model"),
    ("simulating", "Running Monte Carlo simulations"),
    ("aggregating", "Aggregating scenarios"),
    ("finalizing", "Finalizing forecast"),
]


async def emit(job_id: str, event: dict[str, Any]) -> None:
    """Fan an SSE event out to all current subscribers of a job."""
    for q in list(state.job_subscribers.get(job_id, [])):
        try:
            await q.put(event)
        except Exception:
            # A dead queue is fine; the SSE endpoint cleans up its own slot.
            pass


def create_job(region: str) -> str:
    """Register a new job in shared state and return its id."""
    region = (region or "GLOBAL").strip().upper() or "GLOBAL"
    job_id = state.new_id("job")
    state.jobs[job_id] = {
        "id": job_id,
        "region": region,
        "status": "queued",
        "progress": 0,
        "step": "queued",
        "started_at": time.time(),
        "result": None,
    }
    state.job_subscribers[job_id] = []
    trace.record(
        "job.create",
        layer="jobs",
        summary=f"created job {job_id} (region {region})",
        correlation_id=job_id,
        detail={"region": region},
    )
    return job_id


async def run_mock_job(job_id: str) -> None:
    """Walk the fake pipeline, emitting progress and finally a result."""
    try:
        job = state.jobs[job_id]
        for i, (key, label) in enumerate(STEPS):
            await asyncio.sleep(2)
            job["status"] = "running"
            job["step"] = key
            job["step_label"] = label
            job["progress"] = int(((i + 1) / len(STEPS)) * 100)
            await emit(
                job_id,
                {
                    "event": "progress",
                    "data": json.dumps(
                        {
                            "job_id": job_id,
                            "status": "running",
                            "step": key,
                            "step_label": label,
                            "progress": job["progress"],
                        }
                    ),
                },
            )
            trace.record(
                "sse.progress",
                layer="sse",
                summary=f"{job_id} · {label} ({job['progress']}%)",
                correlation_id=job_id,
                detail={"step": key, "progress": job["progress"]},
            )

        # Synthetic but plausible result.
        region = job.get("region", "GLOBAL")
        result = {
            "region": region,
            "horizon_weeks": 12,
            "baseline_units": 18420 + int(secrets.token_bytes(1)[0] * 12.5),
            "uplift_pct": round(2.6 + (secrets.token_bytes(1)[0] / 255) * 1.4, 2),
            "confidence": round(0.78 + (secrets.token_bytes(1)[0] / 255) * 0.18, 3),
        }
        job["status"] = "done"
        job["progress"] = 100
        job["step"] = "done"
        job["step_label"] = "Complete"
        job["result"] = result

        await emit(
            job_id,
            {
                "event": "done",
                "data": json.dumps(
                    {
                        "job_id": job_id,
                        "status": "done",
                        "progress": 100,
                        "result": result,
                    }
                ),
            },
        )
        trace.record(
            "job.done",
            layer="jobs",
            summary=f"{job_id} complete · {region}",
            correlation_id=job_id,
            detail={"result": result},
        )
    except Exception as e:  # noqa: BLE001 — mock server; surface anything
        await emit(
            job_id,
            {"event": "error", "data": json.dumps({"job_id": job_id, "error": str(e)})},
        )
        trace.record(
            "job.error",
            layer="jobs",
            summary=f"{job_id} failed: {e}",
            correlation_id=job_id,
        )
