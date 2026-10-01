"""API tests for GET /events (QuakeML output)."""

import uuid
from datetime import datetime, timedelta
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

TEST_API_KEY = "test-api-key"

CFG = {
    "api": {"max_events": 3},
    "global": {"agency": "test", "region": "test", "smi_authority": "test.org"},
}

T0 = datetime(2026, 1, 1, 12, 0, 0)


@pytest.fixture()
def api():
    """Yield (TestClient, sessionmaker) backed by an in-memory SQLite database."""
    import os

    os.environ["TF2ASLOC_API_KEY"] = TEST_API_KEY

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from tf2asloc.db.models import Base, SystemState

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, expire_on_commit=False)

    with TestingSession() as s:
        s.add(SystemState(id=1, last_inventory_reload=datetime(1970, 1, 1)))
        s.commit()

    def override_get_session():
        return TestingSession()

    from contextlib import asynccontextmanager

    from fastapi import FastAPI

    import tf2asloc.api.routes.events as ev_mod
    import tf2asloc.api.routes.picks as picks_mod

    ev_mod.set_config(CFG)

    @asynccontextmanager
    async def _lifespan(app):
        yield

    test_app = FastAPI(lifespan=_lifespan)
    test_app.include_router(picks_mod.router)
    test_app.include_router(ev_mod.router)
    # Key on the get_session objects the routes actually captured at import time
    test_app.dependency_overrides[ev_mod.get_session] = override_get_session
    test_app.dependency_overrides[picks_mod.get_session] = override_get_session

    client = TestClient(test_app)
    client.headers["X-API-Key"] = TEST_API_KEY
    yield client, TestingSession


def _seed_event(SessionLocal, origin_time=T0, rid_suffix="1", link_arrival=True):
    """Insert a minimal located event; returns (event_id, origin_id, pick_id)."""
    from tf2asloc.db.models import Arrival, Event, Origin, Pick

    with SessionLocal() as s:
        ev = Event(
            id=uuid.uuid4(),
            resource_id=f"smi:test.org/Event/test/test/{rid_suffix}",
            origin_time=origin_time,
            latitude=38.3,
            longitude=22.0,
            depth_km=7.5,
        )
        orig = Origin(
            id=uuid.uuid4(),
            event_id=ev.id,
            resource_id=f"smi:test.org/HINV/test/test/{rid_suffix}",
            time=origin_time,
            latitude=38.3,
            longitude=22.0,
            depth_km=7.5,
            nph=8,
            rms=0.1,
            erh=0.5,
            created_at=origin_time,
        )
        pk = Pick(
            id=uuid.uuid4(),
            event_id=ev.id,
            network="HP",
            station="AAA",
            location="",
            channel="HHZ",
            phase="P",
            time=origin_time + timedelta(seconds=3),
            prob=0.9,
            model="PhaseNet",
            author="tester",
        )
        s.add_all([ev, orig, pk])
        s.flush()
        if link_arrival:
            # only picks referenced by a used arrival are emitted in QuakeML
            s.add(
                Arrival(
                    id=uuid.uuid4(),
                    origin_id=orig.id,
                    pick_id=pk.id,
                    phase="P",
                    time_weight=1.0,
                )
            )
        s.commit()
        return ev.id, orig.id, pk.id


def _read_catalog(resp):
    from obspy import read_events

    return read_events(BytesIO(resp.content))


def _get(client, start="2026-01-01T00:00:00", end="2026-01-02T00:00:00", **extra):
    params = {
        "start": start,
        "end": end,
        "minlatitude": 30.0,
        "maxlatitude": 45.0,
        "minlongitude": 15.0,
        "maxlongitude": 30.0,
    }
    params.update(extra)
    return client.get("/events", params=params)


def test_reject_non_utc_offset(api):
    client, _ = api
    resp = _get(client, start="2026-01-01T00:00:00+02:00")
    assert resp.status_code == 422
    assert "UTC" in resp.json()["detail"]
    # naive, Z and +00:00 all accepted
    assert _get(client).status_code == 200
    assert _get(client, start="2026-01-01T00:00:00Z", end="2026-01-02T00:00:00Z").status_code == 200
    assert _get(client, start="2026-01-01T00:00:00+00:00").status_code == 200


def test_post_pick_rejects_non_utc_offset(api):
    client, _ = api
    pick = {
        "network": "HP",
        "station": "AAA",
        "channel": "HHZ",
        "phase": "P",
        "time": "2026-01-01T12:00:00+02:00",
        "prob": 0.9,
    }
    resp = client.post("/picks", json=[pick])
    assert resp.status_code == 422
    assert "UTC" in resp.json()["detail"]
    pick["time"] = "2026-01-01T12:00:00Z"
    assert client.post("/picks", json=[pick]).status_code == 201


def test_half_open_window(api):
    client, SessionLocal = api
    _seed_event(SessionLocal, origin_time=T0, rid_suffix="a")
    # event exactly at `end` must be excluded, exactly at `start` included
    resp = _get(client, start="2026-01-01T00:00:00", end="2026-01-01T12:00:00")
    assert len(_read_catalog(resp)) == 0
    resp = _get(client, start="2026-01-01T12:00:00", end="2026-01-01T13:00:00")
    assert len(_read_catalog(resp)) == 1


def test_max_events_413(api):
    client, SessionLocal = api
    for i in range(4):  # max_events=3 in CFG
        _seed_event(SessionLocal, origin_time=T0 + timedelta(minutes=i), rid_suffix=str(i))
    resp = _get(client)
    assert resp.status_code == 413
    assert "split" in resp.json()["detail"]


def test_rid_scheme_and_sanitized_method_id(api):
    client, SessionLocal = api
    from tf2asloc.db.models import Arrival, Pick

    ev_id, orig_id, _ = _seed_event(SessionLocal)
    with SessionLocal() as s:
        pk2 = Pick(
            id=uuid.uuid4(),
            event_id=ev_id,
            network="HP",
            station="BBB",
            location="",
            channel="HHZ",
            phase="S",
            time=T0 + timedelta(seconds=5),
            prob=0.8,
            model="my model:v2",  # URI-hostile characters
            author="tester",
        )
        s.add(pk2)
        s.flush()
        s.add(
            Arrival(id=uuid.uuid4(), origin_id=orig_id, pick_id=pk2.id, phase="S", time_weight=0.5)
        )
        s.commit()

    resp = _get(client)
    assert resp.status_code == 200
    cat = _read_catalog(resp)  # obspy parses it => document is valid
    oev = cat[0]
    assert str(oev.resource_id).startswith("smi:test.org/Event/")
    pick_rids = [str(p.resource_id) for p in oev.picks]
    assert all(r.startswith("smi:test.org/pick/") for r in pick_rids)
    methods = [str(p.method_id) for p in oev.picks if p.method_id is not None]
    assert "smi:test.org/method/my_model_v2" in methods
    assert oev.event_type == "earthquake"
    assert oev.event_type_certainty is None


def test_origin_without_stored_arrivals_has_no_arrivals(api):
    client, SessionLocal = api
    _seed_event(SessionLocal, link_arrival=False)  # origin seeded with no Arrival rows
    oev = _read_catalog(_get(client))[0]
    assert oev.origins[0].arrivals == []


def test_backazimuth_from_preferred_origin_only(api):
    client, SessionLocal = api
    from tf2asloc.db.models import Arrival, Origin

    ev_id, orig_id, pick_id = _seed_event(SessionLocal)
    with SessionLocal() as s:
        # worse (non-preferred) origin with a conflicting azimuth
        worse = Origin(
            id=uuid.uuid4(),
            event_id=ev_id,
            resource_id="smi:test.org/HINV/test/test/worse",
            time=T0,
            latitude=38.31,
            longitude=22.01,
            depth_km=8.0,
            nph=4,
            rms=0.9,
            erh=3.0,
            created_at=T0 + timedelta(seconds=60),
        )
        s.add(worse)
        s.flush()
        s.add_all(
            [
                Arrival(
                    id=uuid.uuid4(), origin_id=orig_id, pick_id=pick_id, phase="P", azimuth=90.0
                ),
                Arrival(
                    id=uuid.uuid4(), origin_id=worse.id, pick_id=pick_id, phase="P", azimuth=10.0
                ),
            ]
        )
        s.commit()

    oev = _read_catalog(_get(client))[0]
    # preferred origin's azimuth (90) -> backazimuth 270, not (180+10)%360
    assert oev.picks[0].backazimuth == pytest.approx(270.0)


def test_legacy_magnitude_has_no_origin_reference(api):
    client, SessionLocal = api
    from tf2asloc.db.models import Magnitude

    ev_id, _, _ = _seed_event(SessionLocal)
    with SessionLocal() as s:
        s.add(
            Magnitude(
                id=uuid.uuid4(),
                event_id=ev_id,
                origin_id=None,  # legacy row: computing origin unknown
                resource_id="smi:test.org/Event/test/test/1/ML",
                mag_type="ML",
                value=2.1,
                created_at=T0,
            )
        )
        s.commit()

    oev = _read_catalog(_get(client))[0]
    assert oev.magnitudes[0].origin_id is None
    assert str(oev.preferred_magnitude_id) == "smi:test.org/Event/test/test/1/ML"


def test_amplitude_without_linked_pick_skipped(api):
    client, SessionLocal = api
    from tf2asloc.db.models import Amplitude

    ev_id, _, pick_id = _seed_event(SessionLocal)
    with SessionLocal() as s:
        s.add_all(
            [
                Amplitude(id=uuid.uuid4(), event_id=ev_id, pick_id=pick_id, value=0.001, unit="m"),
                Amplitude(id=uuid.uuid4(), event_id=ev_id, pick_id=None, value=0.002, unit="m"),
            ]
        )
        s.commit()

    oev = _read_catalog(_get(client))[0]
    assert len(oev.amplitudes) == 1
    assert oev.amplitudes[0].generic_amplitude == pytest.approx(0.001)
    assert oev.amplitudes[0].waveform_id.station_code == "AAA"


def _add_pick(s, ev_id, station, phase="P", offset_s=4.0):
    from tf2asloc.db.models import Pick

    pk = Pick(
        id=uuid.uuid4(),
        event_id=ev_id,
        network="HP",
        station=station,
        location="",
        channel="HHZ",
        phase=phase,
        time=T0 + timedelta(seconds=offset_s),
        prob=0.5,
        model="PhaseNet",
        author="tester",
    )
    s.add(pk)
    s.flush()
    return pk


def test_unused_and_zero_weight_picks_excluded(api):
    """Only picks referenced by a positively weighted arrival are emitted."""
    client, SessionLocal = api
    from tf2asloc.db.models import Arrival

    ev_id, orig_id, _ = _seed_event(SessionLocal)
    with SessionLocal() as s:
        _add_pick(s, ev_id, "CCC")  # associated only, no arrival
        zpk = _add_pick(s, ev_id, "DDD", offset_s=5.0)  # weighted out by the locator
        s.add(
            Arrival(id=uuid.uuid4(), origin_id=orig_id, pick_id=zpk.id, phase="P", time_weight=0.0)
        )
        s.commit()

    oev = _read_catalog(_get(client))[0]
    assert {p.waveform_id.station_code for p in oev.picks} == {"AAA"}
    # zero-weight arrival dropped too -> picks == arrivals
    assert len(oev.picks) == len(oev.origins[0].arrivals) == 1


def test_none_weight_arrival_keeps_pick(api):
    """Legacy arrivals without a stored weight still emit their pick."""
    client, SessionLocal = api
    from tf2asloc.db.models import Arrival

    ev_id, orig_id, _ = _seed_event(SessionLocal, link_arrival=False)
    with SessionLocal() as s:
        pk = _add_pick(s, ev_id, "AAA")
        s.add(
            Arrival(id=uuid.uuid4(), origin_id=orig_id, pick_id=pk.id, phase="P", time_weight=None)
        )
        s.commit()

    oev = _read_catalog(_get(client))[0]
    assert len(oev.picks) == len(oev.origins[0].arrivals) == 1


def test_quality_counts_reflect_emitted_content(api):
    client, SessionLocal = api
    from tf2asloc.db.models import Arrival

    ev_id, orig_id, _ = _seed_event(SessionLocal)  # P arrival on AAA, weight 1.0
    with SessionLocal() as s:
        spk = _add_pick(s, ev_id, "AAA", phase="S", offset_s=6.0)
        s.add(
            Arrival(id=uuid.uuid4(), origin_id=orig_id, pick_id=spk.id, phase="S", time_weight=0.5)
        )
        zpk = _add_pick(s, ev_id, "EEE", offset_s=5.0)  # must not count
        s.add(
            Arrival(id=uuid.uuid4(), origin_id=orig_id, pick_id=zpk.id, phase="P", time_weight=0.0)
        )
        s.commit()

    q = _read_catalog(_get(client))[0].origins[0].quality
    assert q.used_phase_count == 2  # emitted arrivals, not the seeded nph=8
    assert q.used_station_count == 1  # AAA only


def test_nph_fallback_without_stored_arrivals(api):
    client, SessionLocal = api
    _seed_event(SessionLocal, link_arrival=False)
    q = _read_catalog(_get(client))[0].origins[0].quality
    assert q.used_phase_count == 8  # locator-reported nph
    assert q.used_station_count is None


def test_only_preferred_origin_emitted(api):
    client, SessionLocal = api
    from tf2asloc.db.models import Origin

    ev_id, _, _ = _seed_event(SessionLocal)
    with SessionLocal() as s:
        s.add(
            Origin(
                id=uuid.uuid4(),
                event_id=ev_id,
                resource_id="smi:test.org/HINV/test/test/worse",
                time=T0,
                latitude=38.31,
                longitude=22.01,
                depth_km=8.0,
                nph=4,  # fewer phases -> not preferred
                rms=0.9,
                created_at=T0 + timedelta(seconds=60),
            )
        )
        s.commit()

    oev = _read_catalog(_get(client))[0]
    assert len(oev.origins) == 1
    assert str(oev.origins[0].resource_id) == "smi:test.org/HINV/test/test/1"
    assert str(oev.preferred_origin_id) == "smi:test.org/HINV/test/test/1"


def test_relinked_pick_still_emitted(api):
    """A pick re-linked to a newer event still appears via the arrival link."""
    client, SessionLocal = api
    from tf2asloc.db.models import Pick

    _, _, pick_id = _seed_event(SessionLocal)
    with SessionLocal() as s:
        s.get(Pick, pick_id).event_id = None  # simulate re-association elsewhere
        s.commit()

    oev = _read_catalog(_get(client))[0]
    assert len(oev.picks) == len(oev.origins[0].arrivals) == 1
    assert oev.picks[0].waveform_id.station_code == "AAA"
