"""Main orchestrator loop for TF2AsLoc.

Drives the sliding-window pick association -> location -> magnitude pipeline.
"""

import logging
import os
import signal
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from obspy import read_inventory
from pyproj import Transformer
from sqlalchemy import func as sqlfunc

from tf2asloc.db.models import (
    Amplitude,
    Arrival,
    Event,
    Magnitude,
    Origin,
    Pick,
    SystemState,
    preferred_origin_of,
)
from tf2asloc.db.session import init_db, session_scope
from tf2asloc.orchestrator.reconciliation import (
    find_duplicate_event,
    get_window_picks,
    should_refine_event,
)
from tf2asloc.workers import associate, calculate_magnitude, locate

logger = logging.getLogger(__name__)

_SHUTDOWN = False
# PID of the orchestrator main process.  GaMMA's multiprocessing.Pool workers
# are forked from us and inherit the SIGTERM handler below; the pool
# terminates its workers with SIGTERM, so the handler must NOT swallow the
# signal in children or the pool's join() hangs forever.
_MAIN_PID = os.getpid()


def _handle_signal(signum, frame):
    if os.getpid() != _MAIN_PID:
        # Forked child (e.g. GaMMA pool worker): restore the default action
        # and re-deliver so the process actually terminates.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
        return
    global _SHUTDOWN
    logger.info("Shutdown signal received (%s) - finishing current tick.", signum)
    _SHUTDOWN = True


def _load_inventory(config: dict):
    """Load StationXML inventory from the metadata directory."""
    metadata_dir = Path(config["_metadata_dir"])
    inv_path = metadata_dir / config["global"]["inventory"]
    logger.info("Loading inventory from %s", inv_path)
    return read_inventory(str(inv_path))


def _picks_to_df(picks: list[Pick]) -> pd.DataFrame:
    """Convert a list of Pick ORM rows to a GaMMA-compatible DataFrame."""
    rows = []
    for p in picks:
        rows.append(
            {
                # GaMMA expects: id (net.sta.loc), timestamp, type (p/s), prob, resource_id
                "id": f"{p.network}.{p.station}.{str(p.location)}",
                "timestamp": p.time.isoformat(),
                "type": p.phase.lower(),
                "prob": p.prob,
                "resource_id": str(p.id),  # DB UUID used as resource ID
                # Keep DB primary key for back-linking
                "_db_id": str(p.id),
                "_db_pk": p.id,
            }
        )
    return pd.DataFrame(rows)


def _match_db_pick(opick, picks_by_key: dict, tol_sec: float = 1.0):
    """Match an ObsPy pick to a DB Pick by net/sta/phase and closest time."""
    key = (
        opick.waveform_id.network_code,
        opick.waveform_id.station_code,
        (opick.phase_hint or "").upper(),
    )
    best, best_dt = None, tol_sec
    for t, obj in picks_by_key.get(key, []):
        dt = abs((t - opick.time.datetime).total_seconds())
        if dt < best_dt:
            best_dt, best = dt, obj
    return best


def _persist_results(
    session,
    cat_df: pd.DataFrame,
    assign_df: pd.DataFrame,
    pick_df: pd.DataFrame,
    location_rows: list[dict],
    catalog,  # ObsPy Catalog (for Magnitude/Amplitude details)
    picks_map: dict,  # resource_id -> Pick ORM object
    config: dict,
    event_map: dict | None = None,  # event resource_id -> existing Event.id
) -> None:
    """Persist all results to the database in a single transaction."""
    # (net, sta, phase) -> [(time, Pick ORM), ...] for arrival back-linking
    picks_by_key: dict = {}
    for pick_obj in picks_map.values():
        key = (pick_obj.network, pick_obj.station, pick_obj.phase.upper())
        picks_by_key.setdefault(key, []).append((pick_obj.time, pick_obj))

    # Events persisted this run, and events that lost picks to re-linking
    touched_event_ids: set = set()
    stripped_event_ids: set = set()

    for _, crow in cat_df.iterrows():
        event_rid = crow.get("resource_id", "")
        if not event_rid:
            continue

        # Resolve Event - dedup mapping first (a refined solution has a new
        # time-derived resource_id that misses the rid lookup)
        event_obj = None
        if event_map and event_rid in event_map:
            event_obj = session.get(Event, event_map[event_rid])
        if event_obj is None:
            event_obj = session.query(Event).filter_by(resource_id=event_rid).first()
        gscore = crow.get("gamma_score")
        gscore = float(gscore) if gscore is not None and pd.notna(gscore) else None
        if event_obj is None:
            event_obj = Event(
                id=uuid.uuid4(),
                resource_id=event_rid,
                origin_time=pd.to_datetime(crow["time"]).to_pydatetime(),
                latitude=crow["latitude"],
                longitude=crow["longitude"],
                depth_km=float(crow["z(km)"]),
                gamma_score=gscore,
            )
            session.add(event_obj)
            session.flush()
        elif gscore is not None:
            # Accepted refinement: its score is the new baseline for the
            # churn guard (the summary is updated below, quality permitting)
            event_obj.gamma_score = gscore

        touched_event_ids.add(event_obj.id)

        # Origins for this event
        matched_locs = [lr for lr in location_rows if lr.get("event_resource_id") == event_rid]
        tick_origin = None  # origin of this tick, for magnitude linking
        for lr in matched_locs:
            existing_orig = session.query(Origin).filter_by(resource_id=lr["resource_id"]).first()
            if existing_orig is not None:
                tick_origin = existing_orig
            else:
                orig_obj = Origin(
                    id=uuid.uuid4(),
                    event_id=event_obj.id,
                    resource_id=lr["resource_id"],
                    time=lr["time"],
                    latitude=lr["latitude"],
                    longitude=lr["longitude"],
                    depth_km=lr["depth_km"],
                    rms=lr.get("rms"),
                    gap=lr.get("gap"),
                    erh=lr.get("erh"),
                    erz=lr.get("erz"),
                    dmin=lr.get("dmin"),
                    nph=lr.get("nph"),
                    locator="hypoinverse",
                )
                session.add(orig_obj)
                session.flush()
                tick_origin = orig_obj

                # Arrivals - pull residuals/azimuths etc. from the ObsPy catalog
                for oevent in catalog or []:
                    for oorigin in oevent.origins:
                        if str(oorigin.resource_id) != lr["resource_id"]:
                            continue
                        opicks = {str(p.resource_id): p for p in oevent.picks}
                        for oarr in oorigin.arrivals:
                            opick = opicks.get(str(oarr.pick_id))
                            db_pick = None
                            if opick is not None:
                                # locate sets the pick rid to the DB UUID;
                                # fuzzy match kept as legacy fallback
                                db_pick = picks_map.get(str(opick.resource_id))
                                if db_pick is None:
                                    db_pick = _match_db_pick(opick, picks_by_key)
                            session.add(
                                Arrival(
                                    id=uuid.uuid4(),
                                    origin_id=orig_obj.id,
                                    pick_id=db_pick.id if db_pick else None,
                                    phase=oarr.phase,
                                    azimuth=oarr.azimuth,
                                    distance_deg=oarr.distance,
                                    takeoff_angle=oarr.takeoff_angle,
                                    time_residual=oarr.time_residual,
                                    time_weight=oarr.time_weight,
                                )
                            )

        # Event summary follows the quality-preferred origin so API time/
        # space filters stay consistent with the QuakeML preferred origin
        if tick_origin is not None:
            ev_origins = session.query(Origin).filter_by(event_id=event_obj.id).all()
            preferred = preferred_origin_of(ev_origins)
            if preferred is not None:
                event_obj.origin_time = preferred.time
                event_obj.latitude = preferred.latitude
                event_obj.longitude = preferred.longitude
                event_obj.depth_km = preferred.depth_km

        # Magnitude - use the computed ML from cat_df (the magnitudes inside
        # the ObsPy catalog are placeholders parsed back from the arc file,
        # where HypoInverse had no magnitude information yet).
        ml_val = crow.get("magnitude")
        if ml_val is not None and float(ml_val) < 999:
            # One magnitude per origin; newest (created_at) is preferred
            if tick_origin is not None:
                ml_rid = f"{tick_origin.resource_id}/ML"
                origin_uuid = tick_origin.id
            else:
                ml_rid = f"{event_rid}/ML"  # no located origin this tick
                origin_uuid = None
            existing_mag = session.query(Magnitude).filter_by(resource_id=ml_rid).first()
            if existing_mag is None:
                session.add(
                    Magnitude(
                        id=uuid.uuid4(),
                        event_id=event_obj.id,
                        origin_id=origin_uuid,
                        resource_id=ml_rid,
                        mag_type="ML",
                        value=float(ml_val),
                    )
                )
            else:
                existing_mag.value = float(ml_val)

        # Link picks to event and upsert per-pick amplitudes
        evidx = crow["event_index"]
        arows = assign_df[assign_df["event_idx"] == evidx]
        for _, arow in arows.iterrows():
            pick_resource_id = str(arow.get("pick_rid", arow.get("_db_id", "")))
            if pick_resource_id not in picks_map:
                continue
            pick_obj = picks_map[pick_resource_id]
            if pick_obj.event_id is not None and pick_obj.event_id != event_obj.id:
                stripped_event_ids.add(pick_obj.event_id)
            pick_obj.event_id = event_obj.id

            ampl = arow.get("amplitude")
            if ampl is None or pd.isna(ampl) or float(ampl) <= 0:
                continue
            existing_amp = (
                session.query(Amplitude)
                .filter_by(event_id=event_obj.id, pick_id=pick_obj.id)
                .first()
            )
            if existing_amp is None:
                session.add(
                    Amplitude(
                        id=uuid.uuid4(),
                        pick_id=pick_obj.id,
                        event_id=event_obj.id,
                        value=float(ampl),
                        unit="m",
                    )
                )
            else:
                existing_amp.value = float(ampl)

    session.flush()
    _cleanup_husk_events(session, stripped_event_ids - touched_event_ids, config)


def _cleanup_husk_events(session, candidate_ids: set, config: dict) -> None:
    """Delete events stripped below the per-phase pick minimums.

    Overlapping re-association runs re-link each pick to the newest solution
    that claims it.  When clustering jitter puts that solution outside the
    dedup tolerances, it is persisted as a new event and *steals* the picks,
    leaving the old event as a husk: origins and magnitudes intact but no
    longer supported by data.  Such events are removed here.  Picks are never
    deleted - any leftover picks still linked to a husk are returned to the
    unassociated pool (event_id = NULL).
    """
    if not candidate_ids:
        return
    gc = config["gamma"]
    min_p = int(gc.get("min_p_picks_per_eq", 3))
    min_s = int(gc.get("min_s_picks_per_eq", 2))
    n_deleted = 0
    for ev_id in candidate_ids:
        event_obj = session.get(Event, ev_id)
        if event_obj is None:
            continue
        phases = [(p.phase or "").lower() for p in event_obj.picks]
        n_p = sum(1 for ph in phases if ph.startswith("p"))
        n_s = sum(1 for ph in phases if ph.startswith("s"))
        if n_p >= min_p and n_s >= min_s:
            continue
        for orig in event_obj.origins:
            for arr in orig.arrivals:
                session.delete(arr)
            session.delete(orig)
        for mag in event_obj.magnitudes:
            session.delete(mag)
        for amp in event_obj.amplitudes:
            session.delete(amp)
        for pick_obj in event_obj.picks:
            pick_obj.event_id = None
        session.delete(event_obj)
        n_deleted += 1
    if n_deleted:
        session.flush()
        logger.info(
            "Husk cleanup: deleted %d event(s) stripped below phase minimums "
            "(picks reassigned to newer solutions).",
            n_deleted,
        )


def run(config: dict) -> None:
    """Run the orchestrator loop indefinitely."""
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    init_db(config)

    oc = config["orchestrator"]

    # Only picks inserted after this instant are processed in this session;
    # stale unassociated picks from previous sessions are ignored.  A
    # backfill run may override the epoch via orchestrator.session_epoch
    # (ISO-8601, UTC) to consume picks already stored in the DB.
    epoch_cfg = oc.get("session_epoch")
    if epoch_cfg:
        if not isinstance(epoch_cfg, datetime):
            epoch_cfg = datetime.fromisoformat(str(epoch_cfg).replace("Z", "+00:00"))
        session_epoch = epoch_cfg.replace(tzinfo=None)
        logger.warning(
            "Session epoch overridden from config: %s - stored picks after "
            "this instant will be (re)processed.",
            session_epoch.isoformat(),
        )
    else:
        session_epoch = datetime.utcnow()

    inv = _load_inventory(config)
    last_loaded_ts: datetime = datetime.utcnow()

    window_sec = int(oc.get("window_sec", 30))
    overlap_sec = int(oc.get("overlap_sec", 5))
    interval_sec = int(oc.get("interval_sec", 30))
    refresh_hours = float(oc.get("inventory_refresh_hours", 24))

    # Build pyproj transformers
    epsg = int(config["gamma"].get("projection", 2100))
    transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=False)
    rev_transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=False)

    # Build FDSN clients and station lists
    from obspy.clients.fdsn import Client

    client_urls = [u.strip() for u in config["global"]["fdsn_ws_clients"].split(";")]
    clients = []
    clients_list = []
    for url in client_urls:
        try:
            clients.append(Client(url))
            clients_list.append(url)
        except Exception as exc:
            logger.warning("Could not initialise FDSN client %s: %s", url, exc)

    # Build stationlists from CSV files (filenames resolved against metadata_dir)
    import csv

    metadata_dir = Path(config["_metadata_dir"])
    stationlists: list[list] = []
    csvfiles = [f.strip() for f in config["global"]["csvfiles"].split(";")]
    for csvname in csvfiles:
        csvfile = metadata_dir / csvname
        stlist = []
        try:
            # Station CSVs are headerless: net,sta,chan_prefix (legacy format).
            with open(csvfile, newline="") as fh:
                for row in csv.reader(fh):
                    if row:
                        stlist.append(list(row))
        except Exception as exc:
            logger.warning("Could not read station CSV %s: %s", csvfile, exc)
        stationlists.append(stlist)

    # A session_epoch in the past implies a backlog to work through: start
    # in replay mode regardless of the flag; the catch-up graduation in the
    # main loop switches to real-time operation automatically.
    replay = bool(oc.get("replay", False)) or epoch_cfg is not None

    # --- Replay mode: replay_cursor is the *end* of the current window in
    # data-time.  It starts unset and is initialised - once the first pick
    # arrives - to (first_pick_time + window_sec).  The main loop below polls
    # for it instead of exiting if the DB is still empty at startup.
    replay_cursor: datetime | None = None

    logger.info(
        "Orchestrator started. window=%ds overlap=%ds interval=%ds replay=%s",
        window_sec,
        overlap_sec,
        interval_sec,
        replay,
    )

    while not _SHUTDOWN:
        # Heartbeat for the Docker healthcheck: a stale mtime means the loop
        # is hung (e.g. a fork deadlock), which never shows up as an exit.
        Path("/tmp/orchestrator_heartbeat").touch()
        tick_start = time.perf_counter()
        now_utc = datetime.utcnow()

        if replay:
            # --- Initialise the cursor from the earliest unassociated pick,
            # polling if none has arrived yet (instead of exiting). ---
            if replay_cursor is None:
                with session_scope() as session:
                    first_pick_time = (
                        session.query(sqlfunc.min(Pick.time))
                        .filter(
                            Pick.event_id.is_(None),
                            Pick.created_at >= session_epoch,
                        )
                        .scalar()
                    )
                if first_pick_time is None:
                    logger.info(
                        "[replay] Waiting for the first pick to arrive (retry in %ds)...",
                        interval_sec,
                    )
                    time.sleep(interval_sec)
                    continue
                replay_cursor = first_pick_time + timedelta(seconds=window_sec)
                logger.info(
                    "[replay] First pick arrived at %s - first window ends at %s.",
                    first_pick_time.isoformat(),
                    replay_cursor.isoformat(),
                )

            # --- Graduate to real-time operation once the cursor is within
            # one window of the wall clock: the live lookback of
            # (window_sec + overlap_sec) then covers everything from the last
            # replay window onward, so the hand-off has no gap.  Waiting for
            # the cursor to strictly overtake now_utc would never end - the
            # gate below caps it at the (always trailing) latest pick. ---
            if replay_cursor >= now_utc - timedelta(seconds=window_sec):
                replay = False
                logger.info(
                    "[replay] Cursor %s caught up with wall clock - "
                    "switching to real-time operation.",
                    replay_cursor.isoformat(),
                )
                virtual_now = now_utc
            else:
                # --- Gate this tick on data having caught up to the window
                # end.  Sliding window with heavy overlap (interval_sec <<
                # window_sec) means a late-posted pick is still captured by a
                # later overlapping tick, so we only need *some* data to have
                # reached the window end, not a strict watermark. ---
                with session_scope() as session:
                    latest_pick_time = (
                        session.query(sqlfunc.max(Pick.time))
                        .filter(Pick.created_at >= session_epoch)
                        .scalar()
                    )
                if latest_pick_time is None or latest_pick_time < replay_cursor:
                    logger.info(
                        "[replay] Waiting for data to reach window end %s (retry in %ds)...",
                        replay_cursor.isoformat(),
                        interval_sec,
                    )
                    time.sleep(interval_sec)
                    continue

                virtual_now = replay_cursor
        else:
            virtual_now = now_utc

        t_end = virtual_now
        t_start = virtual_now - timedelta(seconds=window_sec + overlap_sec)

        # --- Inventory reload check (skipped in replay: data-clock != wall-clock) ---
        if not replay:
            with session_scope() as session:
                state = session.get(SystemState, 1)
                db_ts = state.last_inventory_reload if state else None

            if db_ts and db_ts > last_loaded_ts:
                logger.info("Inventory reload triggered by API request.")
                inv = _load_inventory(config)
                last_loaded_ts = db_ts
            elif (now_utc - last_loaded_ts).total_seconds() > refresh_hours * 3600:
                logger.info("Inventory reload triggered by schedule.")
                inv = _load_inventory(config)
                last_loaded_ts = now_utc

        # --- Query picks (associated ones are re-fed for refinement) ---
        with session_scope() as session:
            picks = get_window_picks(session, t_start, t_end, created_after=session_epoch)

        n_picks = len(picks)
        n_associated = n_located = n_magnitude = 0

        if n_picks == 0:
            logger.info(
                "[tick] No picks in [%s, %s].",
                t_start.isoformat(),
                t_end.isoformat(),
            )
            if not replay:
                time.sleep(max(0, interval_sec - (time.perf_counter() - tick_start)))
                continue
            # replay: fall through to cursor advance

        else:
            pick_df = _picks_to_df(picks)
            picks_map = {str(p.id): p for p in picks}
            for p in picks:
                picks_map[str(p.id)] = p

            # --- Association ---
            cat_df, assign_df = associate.run_association(
                pick_df, inv, config, transformer, rev_transformer
            )
            n_associated = len(cat_df)

            if not cat_df.empty:
                # Enrich cat_df with resource IDs and hinv_id
                from tf2asloc.utils.helpers import generate_event_id2, make_event_resource_id

                glo = config["global"]
                cat_df["resource_id"] = cat_df.apply(
                    lambda row, glo=glo: make_event_resource_id(
                        row, glo["agency"], glo["region"], glo.get("smi_authority", "local")
                    ),
                    axis=1,
                )
                cat_df["hinv_id"] = cat_df.apply(
                    lambda row: generate_event_id2(
                        pd.to_datetime(row["time"]).to_pydatetime(),
                        row["latitude"],
                        row["longitude"],
                        float(row["z(km)"]),
                    ),
                    axis=1,
                )
                # Sync assign_df resource IDs
                assign_df["pick_rid"] = assign_df["pick_idx"].map(pick_df["resource_id"])
                assign_df["event_rid"] = assign_df["event_idx"].map(
                    cat_df.set_index("event_index")["resource_id"]
                )
                # Sync _db_id into assign_df for pick back-linking
                assign_df["_db_id"] = assign_df["pick_idx"].map(pick_df["_db_id"])

                # --- Match re-associated events to persisted ones ---
                # Overlapping windows re-associate an event's picks (old +
                # new) into a solution whose time-derived resource_id can
                # differ slightly.  Map it to the existing event so the
                # refined origin/magnitude are appended instead of creating
                # a duplicate event.
                dedup_time_sec = float(oc.get("dedup_time_sec", 5.0))
                dedup_dist_km = float(oc.get("dedup_dist_km", 10.0))
                event_map: dict = {}
                skip_rids: list = []
                with session_scope() as session:
                    for _, crow in cat_df.iterrows():
                        existing = find_duplicate_event(
                            session,
                            pd.to_datetime(crow["time"]).to_pydatetime(),
                            crow["latitude"],
                            crow["longitude"],
                            dedup_time_sec,
                            dedup_dist_km,
                        )
                        if existing is None:
                            continue
                        rid = crow["resource_id"]
                        new_pick_ids = set(
                            assign_df.loc[assign_df["event_rid"] == rid, "_db_id"].astype(str)
                        )
                        gscore = crow.get("gamma_score")
                        gscore = float(gscore) if gscore is not None and pd.notna(gscore) else None
                        if should_refine_event(session, existing, new_pick_ids, gscore):
                            event_map[rid] = existing.id
                        else:
                            skip_rids.append(rid)
                if skip_rids:
                    cat_df = cat_df[~cat_df["resource_id"].isin(skip_rids)].copy()
                    assign_df = assign_df[~assign_df["event_rid"].isin(skip_rids)].copy()
                    logger.info(
                        "Churn guard: %d matched event(s) skipped "
                        "(unchanged pick set or no gamma_score improvement).",
                        len(skip_rids),
                    )
                if event_map:
                    logger.info(
                        "Dedup: %d event(s) matched already-persisted "
                        "events - refining with the current pick set.",
                        len(event_map),
                    )

                if cat_df.empty:
                    logger.info("Churn guard: all matched events skipped - nothing to relocate.")
                else:
                    # --- Location ---
                    catalog, location_rows = locate.run_location(
                        cat_df, assign_df, pick_df, inv, config
                    )
                    n_located = len(location_rows)

                    # --- Magnitude ---
                    if n_located > 0:
                        # Hydrate stored per-pick amplitudes so covered stations
                        # are not re-measured (waveforms not re-downloaded)
                        with session_scope() as session:
                            amp_rows = (
                                session.query(Amplitude)
                                .filter(Amplitude.pick_id.in_([p.id for p in picks]))
                                .all()
                            )
                            amp_by_pick = {str(a.pick_id): a.value for a in amp_rows}
                        if amp_by_pick:
                            assign_df["amplitude"] = assign_df["_db_id"].map(amp_by_pick)
                        cat_df = calculate_magnitude.run_magnitude(
                            cat_df,
                            assign_df,
                            pick_df,
                            inv,
                            stationlists,
                            clients_list,
                            clients,
                            config,
                        )

                    # Count events with non-999 magnitude
                    n_magnitude = int((cat_df["magnitude"] < 999).sum() if not cat_df.empty else 0)

                    # --- Persist ---
                    with session_scope() as session:
                        session_picks = {str(p.id): session.merge(p) for p in picks}
                        _persist_results(
                            session,
                            cat_df,
                            assign_df,
                            pick_df,
                            location_rows,
                            catalog,
                            session_picks,
                            config,
                            event_map,
                        )

            elapsed_ms = (time.perf_counter() - tick_start) * 1000
            logger.info(
                "[tick] picks=%d associated=%d located=%d magnitude=%d elapsed=%.0fms",
                n_picks,
                n_associated,
                n_located,
                n_magnitude,
                elapsed_ms,
            )

            if not replay:
                sleep_secs = max(0.0, interval_sec - (time.perf_counter() - tick_start))
                if sleep_secs > 0:
                    time.sleep(sleep_secs)
                continue
            # replay: fall through to cursor advance

        # --- Replay: slide the window end forward by interval_sec.  No
        # sleep here - the gating check at the top of the loop throttles us
        # automatically once we catch up to the latest posted data, while
        # still fast-forwarding through any already-available backlog. ---
        replay_cursor += timedelta(seconds=interval_sec)

    logger.info("Orchestrator stopped.")
