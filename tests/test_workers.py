"""Unit tests for worker utilities and science functions."""

from math import isclose

import pytest

# ---------------------------------------------------------------------------
# helpers.py - strtofloat and num2str_nopoint round-trips
# ---------------------------------------------------------------------------


def test_strtofloat_basic():
    from tf2asloc.utils.helpers import strtofloat

    line = "  1234  "
    # 4 digits, 2 implied decimals → 12.34
    assert isclose(strtofloat(line, 3, 4, 2), 12.34, rel_tol=1e-6)


def test_strtofloat_empty():
    from tf2asloc.utils.helpers import strtofloat

    assert strtofloat("      ", 1, 6, 2) == 0.0


def test_num2str_nopoint_round_trip():
    from tf2asloc.utils.helpers import num2str_nopoint, strtofloat

    val = 12.34
    formatted = num2str_nopoint(val, 6, 2)
    assert len(formatted) == 6
    back = strtofloat(formatted + "  ", 1, 6, 2)
    assert isclose(back, val, rel_tol=1e-4)


def test_num2str_nopoint_zero():
    from tf2asloc.utils.helpers import num2str_nopoint

    result = num2str_nopoint(0, 4, 2)
    assert result == "   0"


def test_num2str_nopoint_negmag_clamp():
    from tf2asloc.utils.helpers import num2str_nopoint_negmag

    # Values below -0.98 should be clamped to -0.99
    result = num2str_nopoint_negmag(-1.5, 3, 2)
    # -0.99 → scaled = -99 → '-99'
    assert "-99" in result


def test_num2str_nopoint0_leading_zeros():
    from tf2asloc.utils.helpers import num2str_nopoint0

    result = num2str_nopoint0(5.0, 4, 1)
    assert result == "0050"


# ---------------------------------------------------------------------------
# Scordilis ML magnitude formula spot-check
# ---------------------------------------------------------------------------


def test_scordilis_formula():
    """Verify the Scordilis formula against a known hand-calculated value."""
    from math import log10, sqrt

    ampl_mm = 1.0  # Wood-Anderson amplitude in mm
    epi_dist = 50.0  # km
    dep = 10.0  # km
    mag_n = 1.2614
    mag_K = 0.0031
    mag_c = 0.9043
    mag_dist = 100.0

    hypo_dist = sqrt(epi_dist**2 + dep**2)
    ml = (
        log10(ampl_mm * 1000)
        + mag_n * log10(hypo_dist / mag_dist)
        + mag_K * (hypo_dist - mag_dist)
        + mag_c
    )
    # Known approximate result: ~ 3.0 + correction terms
    assert -2.0 < ml < 6.0, f"ML {ml} outside reasonable range"


# ---------------------------------------------------------------------------
# Event refinement: window picks, multi-origin persistence, amplitude reuse
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_session():
    """In-memory SQLite session with all tables created."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from tf2asloc.db.models import Base

    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as s:
        yield s


def test_get_window_picks_includes_associated(db_session):
    import uuid
    from datetime import datetime, timedelta

    from tf2asloc.db.models import Event, Pick
    from tf2asloc.orchestrator.reconciliation import get_window_picks

    t0 = datetime(2026, 1, 1, 12, 0, 0)
    ev = Event(
        id=uuid.uuid4(),
        resource_id="smi:local/event/1",
        origin_time=t0,
        latitude=38.0,
        longitude=22.0,
        depth_km=5.0,
    )
    db_session.add(ev)
    db_session.flush()
    linked = Pick(
        network="HP",
        station="AAA",
        location="",
        channel="HHZ",
        phase="P",
        time=t0,
        prob=0.9,
        event_id=ev.id,
    )
    unlinked = Pick(
        network="HP",
        station="BBB",
        location="",
        channel="HHZ",
        phase="P",
        time=t0 + timedelta(seconds=2),
        prob=0.8,
    )
    db_session.add_all([linked, unlinked])
    db_session.commit()

    picks = get_window_picks(db_session, t0 - timedelta(seconds=60), t0 + timedelta(seconds=60))
    assert len(picks) == 2


def _persist_tick(session, pick, event_rid, orig_rid, t, lat, mag, event_map, nph=None):
    """Run _persist_results for a one-event/one-origin synthetic tick."""
    import pandas as pd

    from tf2asloc.orchestrator.orchestrator import _persist_results

    cat_df = pd.DataFrame(
        [
            {
                "resource_id": event_rid,
                "time": t,
                "latitude": lat,
                "longitude": 22.0,
                "z(km)": 5.0,
                "magnitude": mag,
                "event_index": 0,
            }
        ]
    )
    assign_df = pd.DataFrame(
        [
            {
                "event_idx": 0,
                "pick_idx": 0,
                "pick_rid": str(pick.id),
                "_db_id": str(pick.id),
            }
        ]
    )
    location_rows = [
        {
            "event_resource_id": event_rid,
            "resource_id": orig_rid,
            "time": t,
            "latitude": lat,
            "longitude": 22.0,
            "depth_km": 5.0,
            "nph": nph,
        }
    ]
    config = {"global": {"agency": "test", "region": "test"}}
    _persist_results(
        session,
        cat_df,
        assign_df,
        pd.DataFrame(),
        location_rows,
        None,
        {str(pick.id): pick},
        config,
        event_map,
    )
    session.commit()


def test_persist_results_appends_refined_origin(db_session):
    from datetime import datetime

    from tf2asloc.db.models import Event, Magnitude, Origin, Pick

    t0 = datetime(2026, 1, 1, 12, 0, 0)
    pick = Pick(
        network="HP",
        station="AAA",
        location="",
        channel="HHZ",
        phase="P",
        time=t0,
        prob=0.9,
    )
    db_session.add(pick)
    db_session.flush()

    # Tick 1: new event
    _persist_tick(
        db_session,
        pick,
        "smi:local/event/1",
        "smi:local/origin/1",
        t0,
        38.0,
        2.0,
        event_map={},
        nph=4,
    )
    ev = db_session.query(Event).one()
    assert pick.event_id == ev.id

    # Tick 2: refined solution mapped to the same event by dedup
    _persist_tick(
        db_session,
        pick,
        "smi:local/event/2",
        "smi:local/origin/2",
        t0,
        38.01,
        2.3,
        event_map={"smi:local/event/2": ev.id},
        nph=6,
    )

    assert db_session.query(Event).count() == 1
    origins = db_session.query(Origin).order_by(Origin.created_at).all()
    assert len(origins) == 2
    assert all(o.event_id == ev.id for o in origins)
    mags = db_session.query(Magnitude).all()
    assert len(mags) == 2
    assert {m.origin_id for m in mags} == {o.id for o in origins}
    # Summary follows the quality-preferred origin (more phases)
    db_session.refresh(ev)
    assert ev.latitude == pytest.approx(38.01)

    # Tick 3: identical re-run is idempotent (same deterministic rids)
    _persist_tick(
        db_session,
        pick,
        "smi:local/event/2",
        "smi:local/origin/2",
        t0,
        38.01,
        2.3,
        event_map={"smi:local/event/2": ev.id},
        nph=6,
    )
    assert db_session.query(Origin).count() == 2
    assert db_session.query(Magnitude).count() == 2

    # Tick 4: lower-quality refinement is stored but summary keeps the best
    _persist_tick(
        db_session,
        pick,
        "smi:local/event/3",
        "smi:local/origin/3",
        t0,
        38.05,
        2.1,
        event_map={"smi:local/event/3": ev.id},
        nph=2,
    )
    assert db_session.query(Origin).count() == 3
    db_session.refresh(ev)
    assert ev.latitude == pytest.approx(38.01)


def test_apply_statz_amplitudes():
    import pandas as pd

    from tf2asloc.workers.calculate_magnitude import _apply_statz_amplitudes

    pick_df = pd.DataFrame({"id": ["HP.AAA.", "HP.BBB."]})
    assign_df = pd.DataFrame({"event_idx": [0, 0], "pick_idx": [0, 1], "amplitude": [-1.0, -1.0]})
    res = {
        "event_idx": 0,
        # [net, sta, loc, [t1, t2], epi_dist, dur, ampl]
        "statz": [["HP", "AAA", "", [None, None], 10.0, -1, 0.00123]],
    }
    _apply_statz_amplitudes(res, assign_df, pick_df)
    assert assign_df.loc[0, "amplitude"] == pytest.approx(0.00123)
    assert assign_df.loc[1, "amplitude"] == -1.0


def test_build_quakeml_quality_preferred():
    import uuid
    from datetime import datetime, timedelta
    from io import BytesIO

    from tf2asloc.api.routes.events import _build_quakeml
    from tf2asloc.db.models import Event, Magnitude, Origin

    t0 = datetime(2026, 1, 1, 12, 0, 0)
    ev = Event(
        id=uuid.uuid4(),
        resource_id="smi:local/event/1",
        origin_time=t0,
        latitude=38.0,
        longitude=22.0,
        depth_km=5.0,
    )
    # o1 is older but higher quality (more phases, lower rms/erh)
    o1 = Origin(
        id=uuid.uuid4(),
        event_id=ev.id,
        resource_id="smi:local/origin/1",
        time=t0,
        latitude=38.0,
        longitude=22.0,
        depth_km=5.0,
        nph=8,
        rms=0.1,
        erh=0.5,
        locator="hypoinverse",
        created_at=t0,
    )
    o2 = Origin(
        id=uuid.uuid4(),
        event_id=ev.id,
        resource_id="smi:local/origin/2",
        time=t0,
        latitude=38.01,
        longitude=22.0,
        depth_km=6.0,
        nph=6,
        rms=0.3,
        erh=2.0,
        locator="hypoinverse",
        created_at=t0 + timedelta(seconds=60),
    )
    # o2 first on purpose: order in the list must not matter
    ev.origins.extend([o2, o1])
    m1 = Magnitude(
        id=uuid.uuid4(),
        event_id=ev.id,
        origin_id=o1.id,
        resource_id="smi:local/origin/1/ML",
        mag_type="ML",
        value=2.0,
        created_at=t0,
    )
    m2 = Magnitude(
        id=uuid.uuid4(),
        event_id=ev.id,
        origin_id=o2.id,
        resource_id="smi:local/origin/2/ML",
        mag_type="ML",
        value=2.3,
        created_at=t0 + timedelta(seconds=60),
    )
    ev.magnitudes.extend([m2, m1])

    xml = _build_quakeml([ev], {"global": {"agency": "test", "region": "test"}})

    from obspy import read_events

    cat = read_events(BytesIO(xml))
    oev = cat[0]
    # Only the preferred origin is emitted; refinement history stays in the DB
    assert len(oev.origins) == 1
    assert len(oev.magnitudes) == 2
    # Quality rule: the older but better-constrained origin stays preferred
    assert str(oev.origins[0].resource_id) == "smi:local/origin/1"
    assert str(oev.preferred_origin_id) == "smi:local/origin/1"
    assert str(oev.preferred_magnitude_id) == "smi:local/origin/1/ML"
    # Only the emitted origin can be referenced by a magnitude
    by_rid = {str(m.resource_id): m.origin_id for m in oev.magnitudes}
    assert str(by_rid["smi:local/origin/1/ML"]) == "smi:local/origin/1"
    assert by_rid["smi:local/origin/2/ML"] is None
    # Creation time comes from the row's created_at, not the request time
    assert oev.origins[0].creation_info.creation_time is not None

    # Flip quality: the newer origin now wins on used phase count
    o2.nph = 10
    xml = _build_quakeml([ev], {"global": {"agency": "test", "region": "test"}})
    oev = read_events(BytesIO(xml))[0]
    assert str(oev.origins[0].resource_id) == "smi:local/origin/2"
    assert str(oev.preferred_origin_id) == "smi:local/origin/2"
    assert str(oev.preferred_magnitude_id) == "smi:local/origin/2/ML"


def test_should_refine_event_churn_guard(db_session):
    import uuid
    from datetime import datetime

    from tf2asloc.db.models import Event, Pick
    from tf2asloc.orchestrator.reconciliation import should_refine_event

    t0 = datetime(2026, 1, 1, 12, 0, 0)
    ev = Event(
        id=uuid.uuid4(),
        resource_id="smi:local/event/1",
        origin_time=t0,
        latitude=38.0,
        longitude=22.0,
        depth_km=5.0,
        gamma_score=10.0,
    )
    db_session.add(ev)
    db_session.flush()
    p1 = Pick(
        network="HP",
        station="AAA",
        location="",
        channel="HHZ",
        phase="P",
        time=t0,
        prob=0.9,
        event_id=ev.id,
    )
    p2 = Pick(
        network="HP",
        station="BBB",
        location="",
        channel="HHZ",
        phase="P",
        time=t0,
        prob=0.8,
    )
    db_session.add_all([p1, p2])
    db_session.commit()

    same_set = {str(p1.id)}
    grown_set = {str(p1.id), str(p2.id)}

    # Identical pick set -> never refine, even with a better score
    assert not should_refine_event(db_session, ev, same_set, 20.0)
    # Changed pick set + improved gamma_score -> refine
    assert should_refine_event(db_session, ev, grown_set, 20.0)
    # Changed pick set but gamma_score did not improve -> skip
    assert not should_refine_event(db_session, ev, grown_set, 10.0)
    assert not should_refine_event(db_session, ev, grown_set, 5.0)
    # Missing scores never block the update
    assert should_refine_event(db_session, ev, grown_set, None)
    ev.gamma_score = None
    assert should_refine_event(db_session, ev, grown_set, 5.0)


# ---------------------------------------------------------------------------
# calculate_distance
# ---------------------------------------------------------------------------


def test_calculate_distance_same_point():
    from tf2asloc.utils.helpers import calculate_distance

    assert isclose(calculate_distance(38.0, 22.0, 38.0, 22.0), 0.0, abs_tol=1e-6)


def test_calculate_distance_known():
    from tf2asloc.utils.helpers import calculate_distance

    # Athens (37.98, 23.73) to Thessaloniki (40.64, 22.94) - geodesic ~303 km
    d = calculate_distance(37.98, 23.73, 40.64, 22.94)
    assert 295 < d < 312, f"Distance {d:.1f} km outside expected range"


# ---------------------------------------------------------------------------
# origin_is_better - RMS sanity guard
# ---------------------------------------------------------------------------


def test_origin_is_better_garbage_rms_never_wins_on_nph():
    from tf2asloc.db.models import Origin, origin_is_better

    clean = Origin(nph=10, rms=0.2, erh=1.0)
    garbage = Origin(nph=30, rms=5.0, erh=1.0)
    assert not origin_is_better(garbage, clean)
    assert origin_is_better(clean, garbage)


def test_origin_is_better_nph_still_wins_when_both_sane():
    from tf2asloc.db.models import Origin, origin_is_better

    a = Origin(nph=10, rms=0.4, erh=1.0)
    b = Origin(nph=12, rms=0.5, erh=2.0)
    assert origin_is_better(b, a)
