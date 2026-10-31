"""Cost-forecast endpoints.

A forecast, not observability: what Turbine Lens costs per month at a given number of users.
The arithmetic lives in services/cost_model.py and the prices in services/cost_rates.py, so
the same numbers can later serve a notebook or a report without a second implementation.

No ownership check — this is deployment cost, not anyone's data.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter
from pydantic import BaseModel

from ..services import cost_model, cost_rates

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/rates")
async def get_rates():
    """Prices and measured constants with provenance, plus the editable assumption defaults.

    `live` is attempted every call rather than cached: prices change rarely, but a stale
    cache would quietly outlive the grant that made it live in the first place.
    """
    resolved = await asyncio.to_thread(cost_rates.resolve)
    return {**resolved, "assumption_defaults": cost_model.DEFAULT_ASSUMPTIONS}


class ForecastRequest(BaseModel):
    users: float = 0
    concurrent_users: float = 0
    #: Overrides for anything in cost_model.DEFAULT_ASSUMPTIONS, plus the optional
    #: `genie_on_behalf_of_user` scenario flag.
    assumptions: dict = {}
    #: Lets the page explore a rate the workspace cannot yet supply — always echoed back
    #: in the response's provenance so an overridden figure can never pass as a list price.
    rate_overrides: dict = {}


@router.post("/forecast")
async def post_forecast(body: ForecastRequest):
    resolved = await asyncio.to_thread(cost_rates.resolve)
    rates = cost_rates.values(resolved)

    overridden = []
    for key, value in (body.rate_overrides or {}).items():
        if key in rates and value is not None:
            rates[key] = value
            overridden.append(key)

    result = cost_model.forecast(
        users=body.users,
        concurrent=body.concurrent_users,
        assumptions=body.assumptions or {},
        rates=rates,
    )

    still_missing = [k for k in resolved["missing"] if k not in overridden]
    if still_missing:
        result["notes"].insert(0, (
            "Dollar figures are incomplete: "
            + ", ".join(still_missing)
            + " have no price yet. Unit counts and breakpoints below are unaffected."
        ))
    if overridden:
        result["notes"].insert(0, (
            "Manually overridden rate(s): " + ", ".join(overridden)
            + " — these are your figures, not list prices."
        ))

    return {
        **result,
        "rates": resolved["rates"],
        "rate_overrides": overridden,
        "missing_rates": still_missing,
        "ready": not still_missing,
    }


class CurveRequest(ForecastRequest):
    """The forecast across a range of user counts, for the chart."""
    users_max: float = 250
    points: int = 26
    #: Carry the caller's concurrency ratio across the x axis. Off pins concurrency, which
    #: answers a different question: "what if adoption grows but peak load does not?"
    scale_concurrency: bool = True


@router.post("/curve")
async def post_curve(body: CurveRequest):
    resolved = await asyncio.to_thread(cost_rates.resolve)
    rates = cost_rates.values(resolved)
    for key, value in (body.rate_overrides or {}).items():
        if key in rates and value is not None:
            rates[key] = value

    series = await asyncio.to_thread(
        cost_model.curve,
        body.users_max, body.points, body.concurrent_users, body.users,
        body.scale_concurrency, body.assumptions or {}, rates,
    )
    return {
        "series": series,
        "concurrency_ratio": (body.concurrent_users / body.users) if body.users else 0.0,
        "scale_concurrency": body.scale_concurrency,
        "ready": not resolved["missing"],
    }
