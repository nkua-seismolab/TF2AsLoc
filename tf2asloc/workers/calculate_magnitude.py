"""Magnitude worker - Wood-Anderson amplitude + Scordilis ML magnitude.

Ported from legacy/chunks/f_t314_ampmag_calc_v1.py.
"""

import logging
import time
from multiprocessing import Pool

import numpy as np
import pandas as pd
from obspy import UTCDateTime

from tf2asloc.utils.helpers import (
    calculate_distance,
    filter_highest_sampling_rate,
    get_station_coords,
)

logger = logging.getLogger(__name__)

# FDSN Client objects hold SSLContext/socket state and cannot be pickled
# through the Pool task queue.  They are stashed here and inherited by the
# forked worker processes instead (fork start method, Linux).
_CLIENTS: list = []

# ---------------------------------------------------------------------------
# Wood-Anderson instrument simulation poles/zeros
# ---------------------------------------------------------------------------
_PAZ_WA = {
    "poles": [-6.2832 + 4.7124j, -6.2832 - 4.7124j],
    "zeros": [0j],
    "gain": 2800,
    "sensitivity": 2080,
}

# ---------------------------------------------------------------------------
# Per-station amplitude + ML magnitude
# ---------------------------------------------------------------------------


def _calculate_ml_mag(subst, inv, statz: list, dep: float, config: dict) -> float:
    """Measure amplitude on Wood-Anderson-simulated traces and compute median ML.

    *statz* is a list of per-station entries:
        [net, sta, loc, [st1a, st2a, ...], epi_dist, dur(placeholder), amplitude(or -1)]
    """
    mc = config["magnitude"]
    mag_n = float(mc["mag_n"])
    mag_K = float(mc["mag_K"])
    mag_c = float(mc["mag_c"])
    mag_dist = float(mc["mag_dist"])
    prefilter = tuple(mc.get("prefilter", [0.5, 0.8, 10.0, 15.0]))
    water_level = int(mc.get("water_level", 60))

    if subst and len(subst) > 0:
        proc = subst.copy()
        proc = proc.detrend("demean").taper(0.05)
        proc = proc.remove_response(
            inventory=inv,
            output="DISP",
            pre_filt=prefilter,
            water_level=water_level,
        )
        proc = proc.simulate(paz_remove=None, paz_simulate=_PAZ_WA, water_level=water_level)
        proc = proc.detrend("demean").taper(0.05)
    else:
        proc = []

    ml_vals: list[float] = []

    for s in range(len(statz)):
        sta_entry = statz[s]
        st1a = sta_entry[3][0]
        st2a = sta_entry[3][1]
        epi_dist = sta_entry[4]

        # Check if amplitude is pre-existing
        has_existing = len(sta_entry) >= 7 and sta_entry[6] and sta_entry[6] != -1

        if has_existing:
            ampl = sta_entry[6]
        elif proc:
            subst2 = proc.select(station=sta_entry[1])
            try:
                # Decide horizontals per channel group (loc + first two SEED
                # letters) so e.g. HH1/HH2 aren't mixed with HNN/HNE.
                grouped: dict[tuple, dict] = {}
                for tr in subst2:
                    grp = (tr.stats.location, tr.stats.channel[:-1])
                    grouped.setdefault(grp, {})[tr.stats.channel[-1]] = tr
                tr_n, tr_e = None, None
                for grp in sorted(grouped):
                    comps = grouped[grp]
                    if "N" in comps and "E" in comps:
                        h1, h2 = comps["N"], comps["E"]  # ZNE
                    elif "1" in comps and "2" in comps and "3" in comps:
                        h1, h2 = comps["2"], comps["3"]  # 123: 1 is vertical
                    elif "1" in comps and "2" in comps:
                        h1, h2 = comps["1"], comps["2"]  # Z12
                    elif "2" in comps and "3" in comps:
                        h1, h2 = comps["2"], comps["3"]  # Z23
                    else:
                        continue
                    tr_n = h1.slice(st1a, st2a)
                    tr_e = h2.slice(st1a, st2a)
                    break
                if tr_n is None or tr_e is None:
                    continue
                ampl = np.mean([max(abs(tr_n.data)), max(abs(tr_e.data))])
            except Exception as exc:
                logger.debug("Amplitude calc failed at %s: %s", sta_entry[1], exc)
                continue
        else:
            continue

        if ampl == 0:
            continue

        # Store amplitude back
        if len(sta_entry) < 7:
            sta_entry.append(ampl)
        else:
            sta_entry[6] = ampl

        hypo_dist = np.sqrt(epi_dist**2 + dep**2)
        ml = (
            np.log10(ampl * 1000)
            + mag_n * np.log10(hypo_dist / mag_dist)
            + mag_K * (hypo_dist - mag_dist)
            + mag_c
        )
        ml_vals.append(ml)

    return float(np.median(ml_vals)) if ml_vals else 999.0


# ---------------------------------------------------------------------------
# Single-event processing (runs in worker process)
# ---------------------------------------------------------------------------


def _process_single_event(args: dict) -> dict:
    """Worker function for multiprocessing.Pool - process one event."""
    try:
        crow = args["crow"]
        arows = args["arows"]
        prows = args["prows"]
        inv = args["inv"]
        stationlists = args["stationlists"]
        clients_list = args["clients_list"]
        clients = _CLIENTS
        config = args["config"]

        mc = config["magnitude"]
        TEST7 = float(mc.get("test7", -1.1))
        TEST8 = float(mc.get("test8", 2.35))
        TEST9 = float(mc.get("test9", 0.0012))
        tbef = int(mc.get("tbef", 30))
        taft = int(mc.get("taft", 90))
        preP = int(mc.get("preP", 5))
        postP = int(mc.get("postP", 40))
        preS = int(mc.get("preS", 5))
        postS = int(mc.get("postS", 10))
        max_wait = int(mc.get("max_waveform_wait_sec", 90))
        max_retries = int(config["global"].get("max_retries", 5))
        retry_delay = int(config["global"].get("retry_delay", 15))

        OT = UTCDateTime(crow["time"])
        olat = crow["latitude"]
        olon = crow["longitude"]
        odep = float(crow["z(km)"])

        t1 = UTCDateTime(prows["timestamp"].min()) - tbef
        t2 = UTCDateTime(prows["timestamp"].max()) + taft

        # Per-station amplitudes stored by earlier ticks (hydrated into arows)
        pre_ampl_by_station: dict = {}
        if "amplitude" in arows.columns:
            ampl_map = arows.set_index("pick_idx")["amplitude"]
            for station_id in prows["id"].unique():
                for pidx in prows[prows["id"] == station_id].index:
                    v = ampl_map.get(pidx, -1)
                    if v and v > 0:
                        pre_ampl_by_station[station_id] = float(v)

        # Only fetch waveforms for picked stations still needing a measurement
        needed_stas = {
            sid.split(".")[1] for sid in prows["id"].unique() if sid not in pre_ampl_by_station
        }

        # Download waveforms (horizontal components only)
        stream = None
        for idx, stationz in enumerate(stationlists):
            bulk = []
            for stat in stationz:
                net, sta = stat[0], stat[1]
                if sta not in needed_stas:
                    continue
                net_obj = inv.select(network=net)
                if not net_obj:
                    continue
                sta_obj = net_obj.select(station=sta)
                if not sta_obj:
                    continue
                for ch in sta_obj[0][0].channels:
                    if not ch.code.endswith("Z"):
                        bulk.append((net, sta, ch.location_code, ch.code, t1, t2))

            if not bulk:
                continue

            for attempt in range(max_retries):
                try:
                    tmp = clients[idx].get_waveforms_bulk(bulk, timeout=max_wait)
                    tmp = filter_highest_sampling_rate(tmp)
                    tmp.merge(method=1, fill_value="latest")
                    stream = tmp if stream is None else stream + tmp
                    break
                except Exception as exc:
                    if hasattr(exc, "status_code") and exc.status_code == 204:
                        break
                    if attempt == max_retries - 1:
                        logger.warning(
                            "Waveform download failed for client %s after %d tries: %s",
                            clients_list[idx],
                            max_retries,
                            exc,
                        )
                    time.sleep(retry_delay)

        if stream is None:
            stream = []

        # Build statz list
        statz_ids = list(prows["id"].unique())
        statz: list = []
        wst1 = wst2 = None

        for station_id in statz_ids:
            parts = station_id.split(".")
            net, sta, loc = parts[0], parts[1], parts[2] if len(parts) > 2 else ""
            parr = sarr = None

            stpickz = prows[prows["id"] == station_id]
            for _, row in stpickz.iterrows():
                if row["type"] == "p":
                    parr = UTCDateTime(row["timestamp"])
                if row["type"] == "s":
                    sarr = UTCDateTime(row["timestamp"])

            if sarr and not parr:
                st1a, st2a = sarr - preS, sarr + postS
            elif parr and not sarr:
                st1a, st2a = parr - preP, parr + postP
            else:
                st1a, st2a = (sarr or parr) - preS, (sarr or parr) + postS

            entry = [net, sta, loc, [st1a, st2a]]

            try:
                coords = get_station_coords(inv, station_id)
                epi_dist = calculate_distance(olat, olon, coords["lat"], coords["lon"])
                entry.append(epi_dist)
            except KeyError:
                continue

            # Check for pre-existing amplitude (from earlier ticks)
            pre_ampl = pre_ampl_by_station.get(station_id, -1)
            entry.append(-1)  # dur placeholder
            entry.append(pre_ampl)

            statz.append(entry)

            wst1 = min(st1a, wst1) if wst1 else st1a
            wst2 = max(st2a, wst2) if wst2 else st2a

        if stream and wst1 and wst2:
            subst = stream.slice(wst1, wst2)
        else:
            subst = []

        ml_avg = _calculate_ml_mag(subst, inv, statz, odep, config)
        logger.info("Event %s OT=%s ML=%.2f", crow.get("hinv_id", "?"), OT, ml_avg)

        # Compute durations per station
        for s in range(len(statz)):
            epi_dist = statz[s][4]
            dur = 10 ** ((ml_avg - TEST7 - TEST9 * epi_dist) / TEST8)
            if len(statz[s]) <= 5:
                statz[s].append(dur)
            else:
                statz[s][5] = dur

        return {
            "event_idx": int(crow["event_index"]),
            "magnitude": ml_avg,
            "statz": statz,
        }

    except Exception as exc:
        logger.exception("Error in _process_single_event: %s", exc)
        return {"event_idx": int(args["crow"]["event_index"]), "magnitude": 999.0, "statz": []}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_magnitude(
    cat_df: pd.DataFrame,
    assign_df: pd.DataFrame,
    pick_df: pd.DataFrame,
    inv,
    stationlists: list,
    clients_list: list,
    clients: list,
    config: dict,
) -> pd.DataFrame:
    """Calculate ML magnitudes for all events in *cat_df*.

    Updates cat_df['magnitude'] in-place and returns the updated DataFrame.
    Also writes amplitude values back into assign_df['amplitude'].
    """
    mc = config["magnitude"]
    n_workers = int(mc.get("workers", 4))

    tasks: list[dict] = []
    for _, crow in cat_df.iterrows():
        evidx = crow["event_index"]
        arows = assign_df[assign_df["event_idx"] == evidx]
        if arows.empty:
            continue
        prows = pick_df.loc[arows["pick_idx"]]

        tasks.append(
            {
                "crow": crow.to_dict(),
                "arows": arows,
                "prows": prows,
                "inv": inv,
                "stationlists": stationlists,
                "clients_list": clients_list,
                "config": config,
            }
        )

    if not tasks:
        return cat_df

    # Make clients visible to forked pool workers without pickling them.
    global _CLIENTS
    _CLIENTS = clients

    if n_workers > 1:
        with Pool(processes=n_workers) as pool:
            results = pool.map(_process_single_event, tasks)
    else:
        results = [_process_single_event(t) for t in tasks]

    # Write magnitudes back to cat_df and measured amplitudes into assign_df
    if "amplitude" not in assign_df.columns:
        assign_df["amplitude"] = -1.0
    for res in results:
        if res["magnitude"] != 999.0:
            cat_df.loc[cat_df["event_index"] == res["event_idx"], "magnitude"] = res["magnitude"]
        _apply_statz_amplitudes(res, assign_df, pick_df)

    return cat_df


def _apply_statz_amplitudes(res: dict, assign_df: pd.DataFrame, pick_df: pd.DataFrame) -> None:
    """Write per-station amplitudes from a statz result into assign_df."""
    for entry in res.get("statz", []):
        if len(entry) < 7 or not entry[6] or entry[6] <= 0:
            continue
        sid = f"{entry[0]}.{entry[1]}.{entry[2]}"
        pidxs = pick_df.index[pick_df["id"] == sid]
        mask = (assign_df["event_idx"] == res["event_idx"]) & assign_df["pick_idx"].isin(pidxs)
        assign_df.loc[mask, "amplitude"] = float(entry[6])
