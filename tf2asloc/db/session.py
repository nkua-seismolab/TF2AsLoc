"""Database session management for TF2AsLoc."""

import logging
from contextlib import contextmanager
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from tf2asloc.db.models import Base, SystemState

logger = logging.getLogger(__name__)

_SessionFactory: sessionmaker | None = None


def init_db(config: dict) -> None:
    """Create all tables and seed the SystemState row.

    Safe to call multiple times - create_all() is idempotent.
    """
    global _SessionFactory
    url = config["database"]["url"]
    engine = create_engine(url, pool_pre_ping=True)
    try:
        Base.metadata.create_all(engine)
    except IntegrityError:
        # Race condition: another process ran create_all concurrently.
        # Tables already exist - safe to continue.
        logger.warning("create_all race condition detected; tables already exist, continuing.")
    _SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)

    # Seed the single SystemState row if it does not exist yet
    with _SessionFactory() as session:
        if session.get(SystemState, 1) is None:
            session.add(SystemState(id=1, last_inventory_reload=datetime(1970, 1, 1)))
            session.commit()
            logger.info("SystemState row seeded.")

    logger.info("Database initialised: %s", engine.url.render_as_string(hide_password=True))


def get_session() -> Session:
    """Return a new Session.  Caller is responsible for commit / rollback."""
    if _SessionFactory is None:
        raise RuntimeError("Database has not been initialised - call init_db() first.")
    return _SessionFactory()


@contextmanager
def session_scope():
    """Context manager that yields a Session and handles commit/rollback."""
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
