"""API tests for POST /picks."""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    """Create a TestClient backed by an in-memory SQLite database."""
    import os

    os.environ["TF2ASLOC_CONFIG"] = "_nonexistent_"  # prevent file load
    os.environ["TF2ASLOC_API_KEY"] = TEST_API_KEY

    from datetime import datetime

    # Patch init_db and get_session to use SQLite in-memory
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from tf2asloc.db import session as db_session
    from tf2asloc.db.models import Base, SystemState

    # StaticPool: share one connection so all sessions see the same in-memory DB
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    # Seed SystemState
    with TestingSession() as s:
        s.add(SystemState(id=1, last_inventory_reload=datetime(1970, 1, 1)))
        s.commit()

    def override_get_session():
        return TestingSession()

    def override_init_db(config):
        pass  # already done above

    with (
        patch.object(db_session, "init_db", override_init_db),
        patch.object(db_session, "get_session", override_get_session),
    ):
        # Build app directly
        from tf2asloc.api.routes import events as ev_mod

        ev_mod.set_config(
            {"api": {"max_events": 100}, "global": {"agency": "test", "region": "test"}}
        )

        from contextlib import asynccontextmanager

        from fastapi import FastAPI

        @asynccontextmanager
        async def _lifespan(app):
            yield

        test_app = FastAPI(lifespan=_lifespan)
        from tf2asloc.api.routes.events import router as events_router
        from tf2asloc.api.routes.picks import router as picks_router
        from tf2asloc.api.routes.system import router as system_router

        test_app.include_router(picks_router)
        test_app.include_router(events_router)
        test_app.include_router(system_router)
        # Key on the get_session objects the route modules captured at import
        # time (import order across test files must not matter)
        from tf2asloc.api.routes import picks as picks_mod
        from tf2asloc.api.routes import system as sys_mod

        for mod in (ev_mod, picks_mod, sys_mod):
            test_app.dependency_overrides[mod.get_session] = override_get_session
        test_app.dependency_overrides[db_session.get_session] = override_get_session

        test_client = TestClient(test_app)
        # All POSTs carry the key by default; auth-failure tests override it.
        test_client.headers["X-API-Key"] = TEST_API_KEY
        yield test_client


TEST_API_KEY = "test-api-key"


VALID_PICK = {
    "network": "HA",
    "station": "ATHU",
    "location": "",
    "channel": "HHZ",
    "phase": "P",
    "time": "2026-01-01T17:09:49.320Z",
    "prob": 0.93,
    "model": "EQTransformer",
    "author": "SL-NKUA",
}


def test_post_picks_valid(client):
    response = client.post("/picks", json=[VALID_PICK])
    assert response.status_code == 201
    body = response.json()
    assert "ids" in body
    assert len(body["ids"]) == 1


def test_post_picks_batch(client):
    picks = [VALID_PICK, {**VALID_PICK, "phase": "S", "prob": 0.67}]
    response = client.post("/picks", json=picks)
    assert response.status_code == 201
    assert len(response.json()["ids"]) == 2


def test_post_picks_dedup_within_batch(client):
    dup = {**VALID_PICK, "time": "2026-01-01T17:09:49.820Z", "prob": 0.50}
    response = client.post("/picks", json=[VALID_PICK, dup])
    assert response.status_code == 201
    body = response.json()
    assert len(body["ids"]) == 1
    assert body["deduplicated"] == 1


def test_post_picks_dedup_against_db_lower_prob(client):
    client.post("/picks", json=[VALID_PICK])
    dup = {**VALID_PICK, "time": "2026-01-01T17:09:49.820Z", "prob": 0.50}
    response = client.post("/picks", json=[dup])
    body = response.json()
    assert body["ids"] == []
    assert body["deduplicated"] == 1
    assert body["updated"] == 0


def test_post_picks_dedup_against_db_higher_prob_updates(client):
    first = client.post("/picks", json=[VALID_PICK]).json()
    better = {**VALID_PICK, "time": "2026-01-01T17:09:49.820Z", "prob": 0.99}
    response = client.post("/picks", json=[better])
    body = response.json()
    assert body["ids"] == []
    assert body["updated"] == 1
    # Stored row was updated in place: a re-post of the original now loses
    again = client.post("/picks", json=[VALID_PICK]).json()
    assert again["ids"] == []
    assert again["deduplicated"] == 1
    assert first["ids"]  # original UUID was kept


def test_post_picks_no_dedup_outside_tolerance(client):
    client.post("/picks", json=[VALID_PICK])
    far = {**VALID_PICK, "time": "2026-01-01T17:09:52.320Z"}
    response = client.post("/picks", json=[far])
    body = response.json()
    assert len(body["ids"]) == 1
    assert body["deduplicated"] == 0


def test_post_picks_invalid_phase(client):
    bad = {**VALID_PICK, "phase": "X"}
    response = client.post("/picks", json=[bad])
    assert response.status_code == 422


def test_post_picks_invalid_prob(client):
    bad = {**VALID_PICK, "prob": 1.5}
    response = client.post("/picks", json=[bad])
    assert response.status_code == 422


def test_post_picks_empty_body(client):
    response = client.post("/picks", json=[])
    assert response.status_code == 422


def test_post_picks_malformed_time(client):
    bad = {**VALID_PICK, "time": "not-a-date"}
    response = client.post("/picks", json=[bad])
    assert response.status_code == 422


def test_post_picks_missing_api_key(client):
    response = client.post("/picks", json=[VALID_PICK], headers={"X-API-Key": ""})
    assert response.status_code == 401


def test_post_picks_invalid_api_key(client):
    response = client.post("/picks", json=[VALID_PICK], headers={"X-API-Key": "wrong-key"})
    assert response.status_code == 401
