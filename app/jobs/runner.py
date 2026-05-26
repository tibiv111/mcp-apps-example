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


def create_job(region: str, pending_pricing: list[dict[str, Any]] | None = None) -> str:
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
        # Snapshot of pricing changes the model should factor in.
        "pending_pricing": list(pending_pricing or []),
    }
    state.job_subscribers[job_id] = []
    trace.record(
        "job.create",
        layer="jobs",
        summary=f"created job {job_id} (region {region}, {len(pending_pricing or [])} pending pricing change(s))",
        correlation_id=job_id,
        detail={"region": region, "pending_pricing_count": len(pending_pricing or [])},
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

        # Synthetic but plausible result. Pending pricing changes are
        # applied as a simple elasticity drag — a +10% price hike drops
        # uplift by ~5pp, a -5% cut adds ~2.5pp. Crude but enough that
        # the forecast result visibly reacts to pricing submissions.
        region = job.get("region", "GLOBAL")
        pending = job.get("pending_pricing", []) or []
        baseline_units = 18420 + int(secrets.token_bytes(1)[0] * 12.5)
        base_uplift = round(2.6 + (secrets.token_bytes(1)[0] / 255) * 1.4, 2)
        confidence = round(0.78 + (secrets.token_bytes(1)[0] / 255) * 0.18, 3)

        ELASTICITY = 0.5  # demand response per 1% price move
        considered: list[dict[str, Any]] = []
        pricing_drag_pct = 0.0
        for change in pending:
            delta = change.get("delta_pct")
            if delta is None:
                continue
            drag = float(delta) * ELASTICITY
            pricing_drag_pct += drag
            considered.append(
                {
                    "ticket": change.get("ticket"),
                    "product": change.get("product"),
                    "previous_price": change.get("previous_price"),
                    "new_price": change.get("new_price"),
                    "delta_pct": delta,
                    "uplift_drag_pct": round(drag, 2),
                }
            )
        adjusted_uplift = round(base_uplift - pricing_drag_pct, 2)
        # Demand drops as uplift drag rises; small, visible effect.
        adjusted_units = int(baseline_units * (1.0 - (pricing_drag_pct / 100.0) * 0.6))
        # Less certainty when there's a lot of pricing motion in flight.
        adjusted_confidence = round(max(0.55, confidence - 0.01 * len(considered)), 3)

        result = {
            "region": region,
            "horizon_weeks": 12,
            "baseline_units": adjusted_units,
            "uplift_pct": adjusted_uplift,
            "confidence": adjusted_confidence,
            "pricing_drag_pct": round(pricing_drag_pct, 2),
            "considered_pricing_changes": considered,
            "base_uplift_pct": base_uplift,
            "base_baseline_units": baseline_units,
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
