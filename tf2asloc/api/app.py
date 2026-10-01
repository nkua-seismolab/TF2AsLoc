"""FastAPI application factory for TF2AsLoc."""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from tf2asloc.api.routes import events as events_router_module
from tf2asloc.api.routes.events import router as events_router
from tf2asloc.api.routes.picks import router as picks_router
from tf2asloc.api.routes.system import router as system_router
from tf2asloc.config.loader import load_config
from tf2asloc.db.session import get_session, init_db

logger = logging.getLogger(__name__)


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def create_app(config_path: str | None = None) -> FastAPI:
    """Build and return the FastAPI application."""
    _setup_logging()

    cfg_path = config_path or os.environ.get("TF2ASLOC_CONFIG", "config.yaml")
    config = load_config(cfg_path)

    # Share config with the events router (needed for max_events and QuakeML agency)
    events_router_module.set_config(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(config)
        logger.info("TF2AsLoc API started.")
        yield
        logger.info("TF2AsLoc API shutting down.")

    app = FastAPI(
        title="TF2AsLoc API",
        description=(
            "Near real-time seismic association and location service. "
            "POST picks for ingestion; GET events as QuakeML."
        ),
        version="0.1.0a0",
        lifespan=lifespan,
    )

    app.include_router(picks_router)
    app.include_router(events_router)
    app.include_router(system_router)

    # Override the session dependency to use the initialised factory
    app.dependency_overrides[get_session] = get_session

    return app


# Module-level app instance used by uvicorn
app = create_app()
