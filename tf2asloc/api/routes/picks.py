"""POST /picks - ingest a batch of DL picks into the database."""

import logging
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy.orm import Session

from tf2asloc.api.auth import verify_api_key
from tf2asloc.db.models import Pick
from tf2asloc.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter()

# Same tolerance the orchestrator uses to link arrivals back to picks
DEDUP_TOL_SEC = 1.0


# ---------------------------------------------------------------------------
# Request schema - matches Table 3.1.4.1 exactly
# ---------------------------------------------------------------------------
class PickIn(BaseModel):
    network: str
    station: str
    location: str = ""
    channel: str
    phase: str
    time: str  # ISO-8601, UTC, microsecond precision
    prob: float
    model: str = ""
    author: str = ""

    @field_validator("phase")
    @classmethod
    def phase_must_be_p_or_s(cls, v: str) -> str:
        if v.upper() not in ("P", "S"):
            raise ValueError("phase must be 'P' or 'S'")
        return v.upper()

    @field_validator("prob")
    @classmethod
    def prob_in_range(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("prob must be between 0 and 1")
        return v


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------
@router.post(
    "/picks",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_api_key)],
)
def ingest_picks(
    picks: list[PickIn],
    session: Session = Depends(get_session),
) -> dict:
    """Accept a batch of picks and store them immediately in the database.

    No processing is performed here - the Orchestrator handles association,
    location, and magnitude asynchronously.
    """
    if not picks:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Payload must contain at least one pick.",
        )

    db_picks = []
    for p in picks:
        try:
            arrival_time = datetime.fromisoformat(p.time.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid time format for pick on {p.network}.{p.station}: {exc}",
            ) from exc
        # UTC-only API: reject non-UTC offsets instead of silently dropping them
        if arrival_time.tzinfo is not None:
            if arrival_time.utcoffset():
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=(
                        f"Pick time for {p.network}.{p.station} must be UTC ('Z' or +00:00 offset)."
                    ),
                )
            arrival_time = arrival_time.replace(tzinfo=None)

        db_picks.append(
            Pick(
                id=uuid.uuid4(),
                network=p.network,
                station=p.station,
                location=p.location,
                channel=p.channel,
                phase=p.phase,
                time=arrival_time,
                prob=p.prob,
                model=p.model,
                author=p.author,
            )
        )

    # --- Dedup within the batch: keep the highest-prob pick per
    # (net, sta, phase) among near-coincident times ---
    db_picks.sort(key=lambda pk: pk.prob, reverse=True)
    kept: list[Pick] = []
    kept_by_key: dict[tuple, list[Pick]] = {}
    n_dropped = 0
    for pk in db_picks:
        key = (pk.network, pk.station, pk.phase)
        dup = any(
            abs((pk.time - other.time).total_seconds()) <= DEDUP_TOL_SEC
            for other in kept_by_key.get(key, [])
        )
        if dup:
            n_dropped += 1
            continue
        kept.append(pk)
        kept_by_key.setdefault(key, []).append(pk)

    # --- Dedup against stored picks: drop the lower-prob duplicate, or
    # update the stored row in place (keeps its UUID and event linkage) ---
    n_updated = 0
    new_picks: list[Pick] = []
    tol = timedelta(seconds=DEDUP_TOL_SEC)
    for pk in kept:
        existing = (
            session.query(Pick)
            .filter(
                Pick.network == pk.network,
                Pick.station == pk.station,
                Pick.phase == pk.phase,
                Pick.time >= pk.time - tol,
                Pick.time <= pk.time + tol,
            )
            .order_by(Pick.prob.desc())
            .first()
        )
        if existing is None:
            new_picks.append(pk)
        elif pk.prob > existing.prob:
            existing.time = pk.time
            existing.prob = pk.prob
            existing.location = pk.location
            existing.channel = pk.channel
            existing.model = pk.model
            existing.author = pk.author
            n_updated += 1
        else:
            n_dropped += 1

    session.add_all(new_picks)
    session.commit()

    assigned_ids = [str(pk.id) for pk in new_picks]
    logger.info(
        "Ingested %d picks (%d duplicates dropped, %d existing updated).",
        len(assigned_ids),
        n_dropped,
        n_updated,
    )
    return {"ids": assigned_ids, "deduplicated": n_dropped, "updated": n_updated}
