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


def create_job(
    region: str,
    pending_pricing: list[dict[str, Any]] | None = None,
    current_drifts: list[dict[str, Any]] | None = None,
) -> str:
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
        # Two-layer pricing snapshot the model factors in:
        #   pending → uncertainty about future approvals (uplift drag)
        #   drifts  → approved moves already in effect (baseline shift)
        "pending_pricing": list(pending_pricing or []),
        "current_drifts": list(current_drifts or []),
    }
    state.job_subscribers[job_id] = []
    trace.record(
        "job.create",
        layer="jobs",
        summary=(
            f"created job {job_id} (region {region}, "
            f"{len(pending_pricing or [])} pending, "
            f"{len(current_drifts or [])} drift)"
        ),
        correlation_id=job_id,
        detail={
            "region": region,
            "pending_pricing_count": len(pending_pricing or []),
            "drift_count": len(current_drifts or []),
        },
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

        # Two-layer pricing model:
        #   1. Approved drifts shift the baseline. Demand reacts to the
        #      price level currently in effect. A +16% drift drops
        #      baseline by ~8% (elasticity 0.5).
        #   2. Pending changes drag the uplift forecast. They're future
        #      moves the market hasn't seen yet.
        region = job.get("region", "GLOBAL")
        pending = job.get("pending_pricing", []) or []
        drifts = job.get("current_drifts", []) or []

        base_baseline_units = 18420 + int(secrets.token_bytes(1)[0] * 12.5)
        base_uplift = round(2.6 + (secrets.token_bytes(1)[0] / 255) * 1.4, 2)
        confidence = round(0.78 + (secrets.token_bytes(1)[0] / 255) * 0.18, 3)

        ELASTICITY = 0.5  # demand response per 1% price move

        # Baseline shift from approved (currently effective) price drift.
        # Use the mean drift across SKUs so adding more SKUs doesn't
        # blow the number up linearly.
        if drifts:
            mean_drift_pct = sum(d.get("drift_pct", 0) for d in drifts) / len(drifts)
        else:
            mean_drift_pct = 0.0
        baseline_shift_pct = -round(mean_drift_pct * ELASTICITY, 2)  # +price → -demand
        adjusted_units = int(
            base_baseline_units * (1.0 + baseline_shift_pct / 100.0)
        )

        # Uplift drag from pending (forward-looking) changes — sum, not mean,
        # because each pending change is its own future bet.
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
        # Less certainty when there's a lot of pricing motion in flight.
        motion = len(considered) + len(drifts)
        adjusted_confidence = round(max(0.55, confidence - 0.01 * motion), 3)

        result = {
            "region": region,
            "horizon_weeks": 12,
            "baseline_units": adjusted_units,
            "uplift_pct": adjusted_uplift,
            "confidence": adjusted_confidence,
            # Pending (uplift) layer
            "pricing_drag_pct": round(pricing_drag_pct, 2),
            "considered_pricing_changes": considered,
            # Approved (baseline) layer
            "baseline_shift_pct": baseline_shift_pct,
            "mean_drift_pct": round(mean_drift_pct, 2),
            "considered_price_drifts": drifts,
            # Untouched bases for transparency
            "base_uplift_pct": base_uplift,
            "base_baseline_units": base_baseline_units,
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
