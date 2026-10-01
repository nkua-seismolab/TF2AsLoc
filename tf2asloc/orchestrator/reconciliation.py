"""Reconciliation helpers for the Orchestrator."""

import logging
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from tf2asloc.db.models import Event, Pick
from tf2asloc.utils.helpers import calculate_distance

logger = logging.getLogger(__name__)


def get_window_picks(
    session: Session,
    t_start: datetime,
    t_end: datetime,
    created_after: datetime | None = None,
) -> list[Pick]:
    """Return all picks in [t_start, t_end], associated or not.

    Already-associated picks are re-fed to the associator so that events
    gaining new picks in later windows are re-located (refined origins are
    appended to the existing event via the dedup mapping).

    If *created_after* is given, only picks inserted after that instant are
    returned (used to ignore stale picks from previous sessions).
    """
    query = session.query(Pick).filter(
        Pick.time >= t_start,
        Pick.time <= t_end,
    )
    if created_after is not None:
        query = query.filter(Pick.created_at >= created_after)
    return query.all()


def find_duplicate_event(
    session: Session,
    origin_time: datetime,
    latitude: float,
    longitude: float,
    time_tol_sec: float,
    dist_tol_km: float,
) -> Event | None:
    """Return an existing Event that matches within the time/space tolerances.

    Overlapping sliding windows can re-associate leftover picks into a
    near-duplicate of an already-persisted event whose origin time differs by
    a fraction of a second (so the resource_id upsert misses it).  A match
    within *time_tol_sec* and *dist_tol_km* is treated as the same event.
    """
    candidates = (
        session.query(Event)
        .filter(
            Event.origin_time >= origin_time - timedelta(seconds=time_tol_sec),
            Event.origin_time <= origin_time + timedelta(seconds=time_tol_sec),
        )
        .all()
    )
    best, best_dist = None, None
    for ev in candidates:
        dist = calculate_distance(latitude, longitude, ev.latitude, ev.longitude)
        if dist <= dist_tol_km and (best_dist is None or dist < best_dist):
            best, best_dist = ev, dist
    return best


def should_refine_event(
    session: Session,
    existing: Event,
    new_pick_ids: set[str],
    new_gamma_score: float | None,
) -> bool:
    """Churn guard (legacy behaviour) for an event matched by dedup.

    A matched event is refined (new origin/magnitude appended) only if the
    associated pick set changed AND the GaMMA score improved.  Missing
    scores (either side) never block the update.
    """
    old_pick_ids = {
        str(pid) for (pid,) in session.query(Pick.id).filter(Pick.event_id == existing.id)
    }
    if new_pick_ids == old_pick_ids:
        return False
    if (
        existing.gamma_score is not None
        and new_gamma_score is not None
        and new_gamma_score <= existing.gamma_score
    ):
        return False
    return True
