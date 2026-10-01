"""POST /system/reload-inventory - signal the Orchestrator to reload StationXML."""

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from tf2asloc.api.auth import verify_api_key
from tf2asloc.db.models import SystemState
from tf2asloc.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/health", status_code=status.HTTP_200_OK)
def health(session: Session = Depends(get_session)) -> dict:
    """Liveness/readiness probe: verifies the app and its DB connection."""
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@router.post(
    "/system/reload-inventory",
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(verify_api_key)],
)
def reload_inventory(session: Session = Depends(get_session)) -> dict:
    """Update the SystemState timestamp to trigger an inventory reload.

    The Orchestrator polls this timestamp each tick.  When it finds a value
    newer than its own last-loaded timestamp it reloads the StationXML
    inventory from the configured FDSN-WS clients.
    """
    now_utc = datetime.now(UTC).replace(tzinfo=None)
    state: SystemState | None = session.get(SystemState, 1)
    if state is None:
        state = SystemState(id=1, last_inventory_reload=now_utc)
        session.add(state)
    else:
        state.last_inventory_reload = now_utc
    session.commit()
    logger.info("Inventory reload requested at %s UTC.", now_utc.isoformat())
    return {"status": "ok", "requested_at": now_utc.isoformat()}
