"""Location worker - USGS HypoInverse (hyp1.40) wrapper.

Ported from:
  legacy/chunks/f_t314_phases_locators_v1.py  (Hyp1.4 phase file writer,
                                                station list, control file)
  legacy/chunks/f_t314_read_output.py          (arc parser)
"""

import logging
import random
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from obspy import UTCDateTime
from obspy.core.event import (
    Amplitude,
    Arrival,
    Catalog,
    Comment,
    ConfidenceEllipsoid,
    CreationInfo,
    Event,
    Magnitude,
    Origin,
    OriginQuality,
    OriginUncertainty,
    Pick,
    QuantityError,
    WaveformStreamID,
)
from obspy.core.event.resourceid import ResourceIdentifier

from tf2asloc.utils.helpers import (
    calculate_distance,
    extract_inventory_subset_Z_from_station,
    num2str_nopoint,
    num2str_nopoint0,
    num2str_nopoint_negmag,
    sanitize_rid_segment,
    smi_base,
    strtofloat,
)

logger = logging.getLogger(__name__)

# Baked into the Docker image (see Dockerfile.tf2asloc) - never user-configurable.
_HYPOINVERSE_BIN = "/usr/local/bin/hypoinverse"

# ---------------------------------------------------------------------------
# compact_float helper (needed for amplitude formatting in phase lines)
# ---------------------------------------------------------------------------


def _compact_float(val: float, total_len: int) -> str:
    for dec in range(total_len - 1, -1, -1):
        candidate = f"{val:.{dec}f}".rstrip("0").rstrip(".")
        if len(candidate) <= total_len and candidate != "0":
            if abs(val) >= 10 and len(candidate.strip()) < len(str(int(val))):
                continue
            return candidate.rjust(total_len)
    int_c = str(int(round(val)))
    if len(int_c) <= total_len:
        return int_c.rjust(total_len)
    sci_c = f"{val:.1e}"
    if len(sci_c) <= total_len:
        return sci_c.rjust(total_len)
    return str(int(val))[-total_len:].rjust(total_len)


# ---------------------------------------------------------------------------
# Phase file writer - Hyp1.4 format
# ---------------------------------------------------------------------------


def _assign_weight(prob: float, thresholds: list[float]) -> tuple[str, str]:
    """Return (weight_char, importance_str) for a pick probability."""
    imp_str = num2str_nopoint(prob, 4, 3)
    for i, thr in enumerate(thresholds):
        if prob >= thr:
            return str(i), imp_str
    return "4", num2str_nopoint(0, 4, 3)


def _write_phase_file(
    phifile: Path,
    inv,
    pick_df: pd.DataFrame,
    assign_df: pd.DataFrame,
    cat_df: pd.DataFrame,
    config: dict,
) -> None:
    """Write a Hyp1.4-format phase file (.ph2000) for all events in cat_df."""
    mc = config["magnitude"]
    TEST7 = float(mc.get("test7", -1.1))
    TEST8 = float(mc.get("test8", 2.35))
    TEST9 = float(mc.get("test9", 0.0012))
    mag_n = float(mc.get("mag_n", 1.2614))
    mag_K = float(mc.get("mag_K", 0.0031))
    mag_c = float(mc.get("mag_c", 0.9043))
    mag_dist = float(mc.get("mag_dist", 100.0))
    use_trial = bool(config.get("locator", {}).get("use_trial", False))

    ac = config["associator"]
    p_thr = [float(ac[f"pwgt{i}"]) for i in range(4)]
    s_thr = [float(ac[f"swgt{i}"]) for i in range(4)]

    # Build station index: "net.sta.loc" -> Station object
    station_index: dict = {}
    for net in inv:
        for sta in net:
            loc_codes = set(ch.location_code for ch in sta) or {""}
            for loc in loc_codes:
                station_index[f"{net.code}.{sta.code}.{loc}"] = sta

    if phifile.exists():
        phifile.unlink()

    lines_out: list[str] = []

    for _, crow in cat_df.iterrows():
        evidx = crow["event_index"]
        OT = UTCDateTime(crow["time"])
        olat = crow["latitude"]
        olon = crow["longitude"]
        odep = float(crow["z(km)"])
        omag = float(crow["magnitude"]) if float(crow["magnitude"]) <= 9 else None
        EVID = str(crow["hinv_id"])
        EVIDstr = EVID.rjust(10)
        omag_str = (
            num2str_nopoint_negmag(omag, 3, 2) if omag is not None else num2str_nopoint(0, 3, 2)
        )

        arows = assign_df[assign_df["event_idx"] == evidx]

        # Collect station arrivals
        station_data: dict = {}
        for _, arow in arows.iterrows():
            pidx = arow["pick_idx"]
            if pidx not in pick_df.index:
                continue
            prow = pick_df.loc[pidx]
            sid = prow["id"]
            atype = prow["type"].upper()
            arr = UTCDateTime(prow["timestamp"])
            prob = float(prow["prob"])
            ampl_val = float(arow.get("amplitude", 0)) if "amplitude" in arow else 0.0

            if atype == "P":
                wgt, imp = _assign_weight(prob, p_thr)
            else:
                wgt, imp = _assign_weight(prob, s_thr)

            ampl = ampl_val if ampl_val and ampl_val > 0 else None

            if sid not in station_data:
                station_data[sid] = {"P": None, "S": None}
            station_data[sid][atype] = {
                "arrival": arr,
                "prob": prob,
                "weight": wgt,
                "importance": imp,
                "amplitude": ampl,
            }

        # --- Event header line ---
        def _lat_str(lat: float) -> str:
            deg = np.floor(abs(lat))
            mins = 60 * (abs(lat) % 1)
            hem = " " if lat >= 0 else "S"
            return num2str_nopoint0(deg, 2, 0) + hem + num2str_nopoint(mins, 4, 2)

        def _lon_str(lon: float) -> str:
            deg = np.floor(abs(lon))
            mins = 60 * (abs(lon) % 1)
            hem = "E" if lon >= 0 else "W"
            return num2str_nopoint0(deg, 3, 0) + hem + num2str_nopoint(mins, 4, 2)

        evtstr = (
            OT.strftime("%Y%m%d%H%M")
            + num2str_nopoint0(OT.second + OT.microsecond / 1_000_000, 4, 2)
            + _lat_str(olat)
            + _lon_str(olon)
            + num2str_nopoint(odep, 5, 2)
            + omag_str
            + num2str_nopoint(0, 3, 0)  # Npswgt
            + num2str_nopoint(0, 3, 0)  # gap
            + num2str_nopoint(0, 3, 0)  # dmin
            + num2str_nopoint(0, 4, 2)  # rms
            + num2str_nopoint(0, 3, 0)  # maxerr_azm
            + num2str_nopoint(0, 2, 0)  # maxerr_dip
            + num2str_nopoint(0, 4, 2)  # maxerr_km
            + num2str_nopoint(0, 3, 0)  # interr_azm
            + num2str_nopoint(0, 2, 0)  # interr_dip
            + num2str_nopoint(0, 4, 2)  # interr_km
            + num2str_nopoint(0, 3, 2)  # durmag
            + "   "  # region remark
            + num2str_nopoint(0, 4, 2)  # minerr_km
            + " "  # aux remark analyst
            + " "  # aux remark program
            + num2str_nopoint(0, 3, 0)  # Ns
            + num2str_nopoint(0, 4, 2)  # erh
            + num2str_nopoint(0, 4, 2)  # erz
            + num2str_nopoint(0, 3, 0)  # npol
            + num2str_nopoint(0, 4, 1)  # nampmag
            + num2str_nopoint(0, 4, 1)  # ndurmag
            + num2str_nopoint(0, 3, 2)  # MAD ampmag
            + num2str_nopoint(0, 3, 2)  # MAD durmag
            + "   "  # crust model code
            + " "  # authority code
            + " "  # data source
            + " "  # dur data source
            + " "  # amp data source
            + " "  # dur mag type
            + num2str_nopoint(0, 3, 0)  # Npsval
            + "X"  # amp mag type
            + "L"  # ext mag label
            + omag_str  # ext magnitude
            + num2str_nopoint(0, 3, 1)  # n ext mags
            + " "  # alt amp label
            + num2str_nopoint(0, 3, 2)  # alt amp mag
            + num2str_nopoint(0, 3, 1)  # n alt amp mags
            + EVIDstr  # event ID
            + "L"  # preferred mag label
            + num2str_nopoint(0, 3, 2)  # pref mag
            + num2str_nopoint(0, 4, 1)  # n pref mags
            + " "  # alt dur label
            + num2str_nopoint(0, 3, 2)  # alt dur mag
            + num2str_nopoint(0, 4, 1)  # n alt dur mags
            + " "  # version
            + " "  # review version
            + "  "  # domain code
            + "  "  # processing version
            + " "  # depth type
            + " "  # crust model type
            + num2str_nopoint(0, 4, 0)  # datum
            + num2str_nopoint(0, 5, 2)  # geoid depth
        )
        lines_out.append(evtstr)

        # --- Station lines ---
        for sid, arrivals in station_data.items():
            if sid not in station_index:
                logger.warning("Station %s not in inventory - skipping.", sid)
                continue

            try:
                tmpinv = extract_inventory_subset_Z_from_station(station_index[sid], sid, OT)
            except ValueError as exc:
                logger.warning("Cannot extract Z-channel for %s: %s", sid, exc)
                continue

            tmpnet = tmpinv.networks[0]
            tmpsta = tmpnet.stations[0]
            tmpcha = tmpsta.channels[0]
            net_code = tmpnet.code
            sta_code = tmpsta.code
            cha_code = tmpcha.code
            loc_code = tmpcha.location_code or "--"

            st5 = sta_code.ljust(5)[:5]
            stlat = tmpcha.latitude
            stlon = tmpcha.longitude

            diststr = calculate_distance(olat, olon, stlat, stlon)
            hypo_dist = np.sqrt(diststr**2 + odep**2)

            # Dummy P if missing
            if arrivals["P"] is None:
                arrivals["P"] = {
                    "arrival": OT,
                    "prob": 0.0,
                    "weight": "4",
                    "importance": num2str_nopoint(0, 4, 3),
                    "amplitude": None,
                }
                pchar = "   4"
            else:
                pchar = " P " + arrivals["P"]["weight"]

            pimp = arrivals["P"]["importance"]
            ampl = arrivals["P"].get("amplitude")

            parr_time = arrivals["P"]["arrival"]
            parr = parr_time.strftime("%Y%m%d%H%M")
            parrsec = num2str_nopoint0(parr_time.second + parr_time.microsecond / 1_000_000, 5, 2)
            if parrsec in ("00000", "    0"):
                parrsec = "00001"
            parr += parrsec
            pres = num2str_nopoint(0, 4, 2)
            pwgt = num2str_nopoint(0, 3, 2)

            if arrivals["S"] is None:
                schar = "   0"
                sarr = "    0"
                simp = num2str_nopoint(0, 4, 3)
            else:
                sarr_time = arrivals["S"]["arrival"]
                parro = UTCDateTime(
                    parr_time.year, parr_time.month, parr_time.day, parr_time.hour, parr_time.minute
                )
                try:
                    sarro = sarr_time - parro
                    sarr = num2str_nopoint0(sarro, 5, 2)
                    schar = " S " + arrivals["S"]["weight"]
                    simp = arrivals["S"]["importance"]
                except ValueError as exc:
                    sarr = "    0"
                    schar = "   0"
                    simp = num2str_nopoint(0, 4, 3)
                    logging.warning(
                        "Invalid S arrival time (%s) for station %s: %s", sarro, sid, exc
                    )
                if not ampl:
                    ampl = arrivals["S"].get("amplitude")

            sres = num2str_nopoint(0, 4, 2)
            swgt = num2str_nopoint(0, 3, 2)

            # Amplitude string
            if not ampl or ampl <= 0:
                amplstr = num2str_nopoint(0, 7, 2)
                ampwgt, amplab, useamp = "0", " ", "X"
            elif ampl < 0.25:
                amplstr = _compact_float(ampl, 7)
                ampwgt, amplab, useamp = "1", "L", " "
            else:
                amplstr = num2str_nopoint(ampl, 7, 2)
                ampwgt, amplab, useamp = "1", "L", " "
            amptype = " 1"
            ampper = num2str_nopoint(0, 3, 2)
            pdel = num2str_nopoint(0, 4, 2)
            sdel = num2str_nopoint(0, 4, 2)

            diststr_f = num2str_nopoint(diststr, 4, 1)
            ainstr = num2str_nopoint(0, 3, 0)

            if ampl and ampl > 0:
                magstat = (
                    np.log10(ampl * 1000)
                    + mag_n * np.log10(hypo_dist / mag_dist)
                    + mag_K * (hypo_dist - mag_dist)
                    + mag_c
                )
            else:
                magstat = 0.0
            magstatstr = num2str_nopoint_negmag(magstat, 3, 2)

            if omag is not None:
                dur = num2str_nopoint(10 ** ((omag - TEST7 - TEST9 * diststr) / TEST8), 4, 0)
            else:
                dur = num2str_nopoint(0, 4, 0)

            azm0 = num2str_nopoint(0, 3, 0)
            durmag_s = omag_str
            durlab = " "

            row = (
                f"{st5}{net_code} V{cha_code} {pchar}{parr}{pres}{pwgt}"
                f"{sarr}{schar}{sres}{amplstr} 1{swgt}{pdel}{sdel}"
                f"{diststr_f}{ainstr}{ampwgt}0{ampper} {dur}{azm0}"
                f"{durmag_s}{magstatstr}{pimp}{simp}J{durlab}{amplab}"
                f"{loc_code}{amptype}{cha_code}{useamp}X"
            )
            if len(row) != 120:
                raise ValueError(f"Station row for {sid} has length {len(row)}, expected 120.")
            lines_out.append(row)

        # Trial / terminator line
        if use_trial and omag is not None:
            evtHHMM = OT.strftime("%H%M")
            evtSSFF = num2str_nopoint0(OT.second + OT.microsecond / 1_000_000, 4, 2)
            lines_out.append(
                f"      {evtHHMM}{evtSSFF}"
                + _lat_str(olat)
                + _lon_str(olon)
                + num2str_nopoint(odep, 5, 2)
                + " " * 28
                + EVIDstr
            )
        else:
            # Event ID must sit in columns 63-72 of the terminator line
            lines_out.append(" " * 62 + EVIDstr)

    phifile.write_text("\n".join(lines_out) + "\n")
    logger.debug("Phase file written: %s (%d event(s))", phifile, len(cat_df))


# ---------------------------------------------------------------------------
# Station list writer - Hyp1.4 format
# ---------------------------------------------------------------------------


def _write_station_list(
    station_path: Path,
    inv,
    pick_df: pd.DataFrame,
    OT: UTCDateTime,
) -> None:
    """Write Hyp1.4-format station list covering all stations in pick_df."""
    station_index: dict = {}
    for net in inv:
        for sta in net:
            loc_codes = set(ch.location_code for ch in sta) or {""}
            for loc in loc_codes:
                station_index[f"{net.code}.{sta.code}.{loc}"] = sta

    seen: set[str] = set()
    lines: list[str] = []

    for _, row in pick_df.iterrows():
        sid: str = row["id"]
        if sid in seen or sid not in station_index:
            continue
        seen.add(sid)

        try:
            tmpinv = extract_inventory_subset_Z_from_station(station_index[sid], sid, OT)
        except ValueError:
            continue

        tmpnet = tmpinv.networks[0]
        tmpsta = tmpnet.stations[0]
        tmpcha = tmpsta.channels[0]
        net_code = tmpnet.code
        sta_code = tmpsta.code
        cha_code = tmpcha.code
        loc_code = tmpcha.location_code or "--"
        lat = tmpcha.latitude
        lon = tmpcha.longitude
        elev = tmpcha.elevation

        st5 = sta_code.ljust(5)[:5]

        def _lat(v: float) -> str:
            deg = np.floor(abs(v))
            mins = 60 * (abs(v) % 1)
            hem = "N" if v >= 0 else "S"
            return f"{num2str_nopoint0(deg, 2, 0)} {mins:7.4f}{hem}"

        def _lon(v: float) -> str:
            deg = np.floor(abs(v))
            mins = 60 * (abs(v) % 1)
            hem = "E" if v >= 0 else "W"
            return f"{num2str_nopoint0(deg, 3, 0)} {mins:7.4f}{hem}"

        staelev = num2str_nopoint(np.floor(abs(elev)), 4, 0)
        line = (
            f"{st5:<5} {net_code:<2} V{cha_code}  "
            f"{_lat(lat)}{_lon(lon)}{staelev}"
            f"0.0     0.00  0.00  0.00  0.00 3  0.00{loc_code}{cha_code} "
        )
        lines.append(line)

    station_path.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Control file writer
# ---------------------------------------------------------------------------


def _write_control_file(
    params_path: str,
    output_path: Path,
    phase_filename: str,
) -> None:
    header = [
        "MIN 4",
        "ZTR 8 F",
        "DI1 100 50 1 3",
        "DIS 3 13 2 8",
        "WET 1.0 0.75 0.5 0.25",
        "DUR -1.10 2.35 0 0.0012 0 0 0 0 0 0 9999 0",
        "PRE 3, 3 0 0 9, 2 4 0 9, 1 6 0 9",
    ]

    with open(params_path) as fh:
        user_params = [ln.strip() for ln in fh if ln.strip()]

    tail = [
        "LET 5 2 3 2 2",
        "REP T F",
        "ERF F",
        "TOP F",
        "APP F F F",
        "LST 0 0 0",
        "KPR 2",
        "H71 3 1 3",
        "MAG 1 F 1 1",
        "MA2 1 T",
        f"PHS '{phase_filename}'",
        "FIL",
        "CRH 1 'velmodel.crh'",
        "STA 'stations.txt'",
        "PRT 'h2000.prt'",
        "SUM 'h2000.sum'",
        "ARC 'h2000.arc'",
        "LOC",
        "STO",
    ]

    output_path.write_text("\n".join(header + user_params + tail) + "\n")


# ---------------------------------------------------------------------------
# Arc parser
# ---------------------------------------------------------------------------


def _parse_arc(
    arc_path: Path,
    pick_df: pd.DataFrame,
    assign_df: pd.DataFrame,
    cat_df: pd.DataFrame,
    config: dict,
) -> Catalog:
    """Parse a HypoInverse .arc file and return an ObsPy Catalog."""
    agency = config["global"]["agency"]
    region = config["global"]["region"]
    rid_base = smi_base(config)
    catalog = Catalog()

    creation_rid = round(random.uniform(1, 99999.99999), 5)

    def _make_orig_rid(ot, lat, lon):
        ts = ot.strftime("%Y%m%d%H%M%S") + f"{int(ot.microsecond / 1000):03d}"
        return (
            f"{rid_base}/HINV/{sanitize_rid_segment(agency)}/{sanitize_rid_segment(region)}/"
            f"{ts}_{lat:.3f}_{lon:.3f}_{creation_rid:.5f}"
        )

    flag = 1  # 1=event line, 2=station lines
    event = None
    orig_idx = None
    crow = None
    arows = None
    prows = None

    with arc_path.open() as fh:
        for line in fh:
            if flag == 1:
                # --- Event line ---
                try:
                    ts_str = line[:12]
                    otsec = strtofloat(line, 13, 4, 2)
                    ot = UTCDateTime(ts_str) + otsec

                    lat = strtofloat(line, 17, 2, 0)
                    latmin = strtofloat(line, 20, 4, 2)
                    if line[18] == "S":
                        latmin = -latmin
                    lat = round(lat + latmin / 60, 5)

                    lon = strtofloat(line, 24, 3, 0)
                    lonmin = strtofloat(line, 28, 4, 2)
                    if line[26] != "E":
                        lonmin = -lonmin
                    lon = round(lon + lonmin / 60, 5)

                    dep = strtofloat(line, 32, 5, 2)
                    ampmag = strtofloat(line, 37, 3, 2)
                    gap = strtofloat(line, 43, 3, 0)
                    dmin = strtofloat(line, 46, 3, 0)
                    rms = strtofloat(line, 49, 4, 2)
                    maxerr_azm = strtofloat(line, 53, 3, 0)
                    maxerr_dip = strtofloat(line, 56, 2, 0)
                    maxerr_km = strtofloat(line, 58, 4, 2)
                    interr_azm = strtofloat(line, 62, 3, 0)
                    interr_dip = strtofloat(line, 65, 2, 0)
                    interr_km = strtofloat(line, 67, 4, 2)
                    durmag = strtofloat(line, 71, 3, 2)
                    erh = strtofloat(line, 86, 4, 2)
                    erz = strtofloat(line, 90, 4, 2)
                    npsval = strtofloat(line, 119, 3, 0)
                    extmag = strtofloat(line, 124, 3, 2)
                    evid = int(strtofloat(line, 137, 10, 0))
                    prefmag = strtofloat(line, 148, 3, 2)
                except Exception as exc:
                    logger.warning("Skipping malformed event line: %s", exc)
                    flag = 1
                    continue

                crow = cat_df[cat_df["hinv_id"] == f"{evid:010d}"]
                if crow.empty:
                    logger.warning("Event ID %010d not found in catalog - skipping.", evid)
                    flag = 2
                    continue

                event_rid = crow["resource_id"].values[0]
                evidx = crow["event_index"].values[0]
                orig_rid = _make_orig_rid(ot, lat, lon)

                creation_info = CreationInfo(creation_time=datetime.utcnow(), author=agency)

                if maxerr_azm == 0 and maxerr_dip == 90:
                    maxerhazm = interr_azm
                else:
                    maxerhazm = maxerr_azm

                quality = OriginQuality(
                    standard_error=rms,
                    azimuthal_gap=gap,
                    minimum_distance=dmin,
                    used_phase_count=int(npsval),
                )
                uncertainty = OriginUncertainty(
                    horizontal_uncertainty=erh * 1000,
                    max_horizontal_uncertainty=erh * 1000,
                    azimuth_max_horizontal_uncertainty=maxerhazm,
                    confidence_ellipsoid=ConfidenceEllipsoid(
                        semi_major_axis_length=maxerr_km * 1000,
                        semi_intermediate_axis_length=interr_km * 1000,
                        major_axis_plunge=maxerr_dip,
                        major_axis_azimuth=maxerr_azm,
                        intermediate_axis_plunge=interr_dip,
                        intermediate_axis_azimuth=interr_azm,
                    ),
                )
                origin = Origin(
                    resource_id=ResourceIdentifier(orig_rid),
                    time=ot,
                    latitude=lat,
                    longitude=lon,
                    depth=dep * 1000,
                    depth_errors=QuantityError(uncertainty=erz * 1000),
                    quality=quality,
                    origin_uncertainty=uncertainty,
                    evaluation_mode="automatic",
                    evaluation_status="preliminary",
                    creation_info=creation_info,
                )

                magnitudes = []
                for mag_val, mag_type in [
                    (extmag, "ML"),
                    (prefmag, "Preferred"),
                    (durmag, "Coda"),
                    (ampmag, "Amplitude"),
                ]:
                    if mag_val is not None and mag_val > -1.0:
                        magnitudes.append(
                            Magnitude(
                                mag=mag_val,
                                magnitude_type=mag_type,
                                resource_id=ResourceIdentifier(f"{orig_rid}/{mag_type}"),
                                creation_info=creation_info,
                            )
                        )

                event = Event(
                    resource_id=ResourceIdentifier(event_rid),
                    origins=[origin],
                    magnitudes=magnitudes,
                )
                event.preferred_origin_id = origin.resource_id
                if magnitudes:
                    event.preferred_magnitude_id = magnitudes[0].resource_id
                event.creation_info = creation_info
                # Add GaMMA info as comments
                for key in ["sigma_time", "gamma_score", "num_picks", "num_p_picks", "num_s_picks"]:
                    if key in crow.columns:
                        event.comments.append(
                            Comment(text=f"Associator info: {key} = {crow[key].values[0]}")
                        )

                catalog.append(event)
                orig_idx = 0  # only one origin per event at this point
                arows = assign_df[assign_df["event_idx"] == evidx]
                prows = pick_df.loc[arows["pick_idx"]] if not arows.empty else pd.DataFrame()
                flag = 2

            elif flag == 2:
                if line[:4].isspace():
                    flag = 1
                    continue
                if event is None:
                    continue

                # --- Station line ---
                try:
                    si = _parse_station_line(line)
                except Exception as exc:
                    logger.warning("Skipping malformed station line: %s", exc)
                    continue

                picks_list, amps_list = _create_pick_and_amplitude(
                    si, crow, arows, prows, agency, region, rid_base
                )
                event.picks.extend(picks_list)
                event.amplitudes.extend(amps_list)

                for pick in picks_list:
                    phase = pick.phase_hint
                    arr = Arrival(
                        # rid tail only: embedding a full smi: URI would be invalid
                        resource_id=ResourceIdentifier(
                            f"{pick.resource_id.id}_{orig_rid.rsplit('/', 1)[-1]}"
                        ),
                        pick_id=pick.resource_id,
                        phase=phase,
                        azimuth=si["azimuth"],
                        takeoff_angle=si["emergence_angle"],
                        distance=si["epicentral_dist"] / 111.2,
                        time_residual=(si["p_residual"] if phase == "P" else si["s_residual"]),
                        time_weight=(
                            si["p_weight_actual"] if phase == "P" else si["s_weight_actual"]
                        ),
                    )
                    event.origins[orig_idx].arrivals.append(arr)

    return catalog


def _parse_station_line(line: str) -> dict:
    return {
        "station_code": line[0:5].strip(),
        "network_code": line[5:7].strip(),
        "component_code_3": line[9:12].strip(),
        "p_remark": line[13:15].strip(),
        "p_first_motion": line[15],
        "p_weight_code": line[16],
        "year": int(line[17:21]),
        "month": int(line[21:23]),
        "day": int(line[23:25]),
        "hour": int(line[25:27]),
        "minute": int(line[27:29]),
        "p_sec": strtofloat(line, 30, 5, 2),
        "p_residual": strtofloat(line, 35, 4, 2),
        "p_weight_actual": strtofloat(line, 39, 3, 2),
        "s_sec": strtofloat(line, 42, 5, 2),
        "s_remark": line[46:48].strip(),
        "s_weight_code": line[49],
        "s_residual": strtofloat(line, 51, 4, 2),
        "amplitude": strtofloat(line, 55, 7, 2),
        "amp_units_code": int(line[61:63]) if line[61:63].strip().isdigit() else 0,
        "s_weight_actual": strtofloat(line, 64, 3, 2),
        "p_delay": strtofloat(line, 67, 4, 2),
        "s_delay": strtofloat(line, 71, 4, 2),
        "epicentral_dist": strtofloat(line, 75, 4, 1),
        "emergence_angle": strtofloat(line, 79, 3, 0),
        "amp_mag_weight": line[81],
        "dur_mag_weight": line[82],
        "period": strtofloat(line, 84, 3, 2),
        "coda_dur": strtofloat(line, 88, 4, 0),
        "azimuth": strtofloat(line, 92, 3, 0),
        "dur_mag_station": strtofloat(line, 95, 3, 2),
        "amp_mag_station": strtofloat(line, 98, 3, 2),
        "p_importance": strtofloat(line, 101, 4, 3),
        "s_importance": strtofloat(line, 105, 4, 3),
        "location_code": line[111:113].strip(),
    }


def _create_pick_and_amplitude(
    si: dict,
    crow: pd.DataFrame,
    arows: pd.DataFrame,
    prows: pd.DataFrame,
    agency: str,
    region: str,
    rid_base: str,
) -> tuple[list, list]:
    picks: list = []
    amps: list = []
    event_rid = crow["resource_id"].values[0]
    creation_info = CreationInfo(creation_time=datetime.utcnow(), author=agency)
    weight_to_unc = {"0": 0.1, "1": 0.2, "2": 0.3, "3": 0.5, "4": 99.9}

    stid = (
        si["network_code"]
        + "."
        + si["station_code"]
        + "."
        + (si["location_code"] if si["location_code"] != "--" else "")
    )

    for phase, sec_key, wgt_key, _res_key, _wgt_act_key in [
        ("P", "p_sec", "p_weight_code", "p_residual", "p_weight_actual"),
        ("S", "s_sec", "s_weight_code", "s_residual", "s_weight_actual"),
    ]:
        if not si[sec_key]:
            continue
        pick_time = (
            UTCDateTime(si["year"], si["month"], si["day"], si["hour"], si["minute"], 0)
            + si[sec_key]
        )
        # Source DB pick: unique per (station, phase) within an event by
        # construction of the phase file
        src = None
        if prows is not None and not prows.empty:
            sel = prows[(prows["id"] == stid) & (prows["type"] == phase.lower())]
            if sel.empty:
                # the phase file carries the inventory's loc code, which may
                # differ from the pick's loc code
                prefix = f"{si['network_code']}.{si['station_code']}."
                sel = prows[prows["id"].str.startswith(prefix) & (prows["type"] == phase.lower())]
            if len(sel) > 1:
                # format="ISO8601" tolerates mixed precision: isoformat()
                # omits microseconds entirely when they are zero
                dt = (
                    pd.to_datetime(sel["timestamp"], format="ISO8601")
                    - pd.Timestamp(pick_time.datetime)
                ).abs()
                sel = sel.loc[[dt.idxmin()]]
            if not sel.empty:
                src = sel.iloc[0]
        if src is None:
            # no DB pick behind this entry (e.g. the dummy P written for an
            # S-only station): skip it so no phantom pick/arrival is created
            continue
        motion = si.get("p_first_motion", " ") if phase == "P" else " "
        polarity = "positive" if motion == "U" else "negative" if motion == "D" else None
        pick = Pick(
            time=pick_time,
            time_errors=weight_to_unc.get(si[wgt_key], 0.5),
            phase_hint=phase,
            waveform_id=WaveformStreamID(
                network_code=si["network_code"],
                station_code=si["station_code"],
                location_code=si["location_code"],
                channel_code=si["component_code_3"],
            ),
            evaluation_mode="automatic",
            polarity=polarity,
            creation_info=creation_info,
        )
        # Carry the DB UUID so persistence can link the arrival exactly
        pick.resource_id = ResourceIdentifier(str(src["resource_id"]))
        pick.comments.append(Comment(text=f"{phase} probability: {src['prob']}"))
        picks.append(pick)

    if si["amplitude"] and si["amplitude"] > 0:
        amp = Amplitude(
            # event rid tail only: embedding a full smi: URI would be invalid
            resource_id=ResourceIdentifier(
                f"{rid_base}/Amp/{sanitize_rid_segment(agency)}/{sanitize_rid_segment(region)}/"
                f"{sanitize_rid_segment(stid)}_{event_rid.rsplit('/', 1)[-1]}"
            ),
            generic_amplitude=si["amplitude"] / 1000.0,
            unit="m",
            magnitude_hint="ML",
            type="amplitude",
            method_id="wood-anderson_simulation_mm",
            evaluation_mode="automatic",
            waveform_id=WaveformStreamID(
                network_code=si["network_code"],
                station_code=si["station_code"],
                location_code=si["location_code"],
                channel_code=si["component_code_3"],
            ),
            creation_info=creation_info,
        )
        amps.append(amp)

    return picks, amps


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_location(
    cat_df: pd.DataFrame,
    assign_df: pd.DataFrame,
    pick_df: pd.DataFrame,
    inv,
    config: dict,
) -> tuple[Catalog, list[dict]]:
    """Run USGS HypoInverse on the events in *cat_df*.

    Returns
    -------
    catalog : obspy.Catalog
        Located events parsed from the .arc output.
    location_rows : list[dict]
        Flat dicts suitable for inserting into the Origin DB table.
    """
    if cat_df.empty:
        return Catalog(), []

    loc_cfg = config["locator"]
    bin_path = _HYPOINVERSE_BIN
    metadata_dir = Path(config["_metadata_dir"])
    model_path = metadata_dir / loc_cfg["model"]
    params_path = metadata_dir / loc_cfg["params"]

    if not Path(bin_path).exists():
        raise FileNotFoundError(f"HypoInverse binary not found: {bin_path}")

    with tempfile.TemporaryDirectory(prefix="tf2asloc_hinv_") as tmpdir:
        tmpdir = Path(tmpdir)

        phase_file = tmpdir / "phases.ph2000"
        station_file = tmpdir / "stations.txt"
        control_file = tmpdir / "h2000.inp"
        arc_file = tmpdir / "h2000.arc"

        # Copy velocity model
        shutil.copy(model_path, tmpdir / "velmodel.crh")

        # Determine representative OT for station list
        first_ot = UTCDateTime(cat_df["time"].iloc[0]) if not cat_df.empty else UTCDateTime()

        _write_phase_file(phase_file, inv, pick_df, assign_df, cat_df, config)
        _write_station_list(station_file, inv, pick_df, first_ot)
        _write_control_file(params_path, control_file, "phases.ph2000")

        logger.info("Running HypoInverse on %d events...", len(cat_df))

        # HypoInverse reads *commands* from stdin; "@file" executes a command
        # file (same as the legacy "@h2000.hyp" driver line).  Passing the
        # bare filename would be treated as an unknown command and HypoInverse
        # would exit silently without producing any output.
        result = subprocess.run(
            bin_path,
            input=f"@{control_file.name}\n",
            cwd=str(tmpdir),
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            logger.error("HypoInverse stderr: %s", result.stderr[:2000])
            raise RuntimeError(f"HypoInverse exited with code {result.returncode}.")

        if not arc_file.exists() or arc_file.stat().st_size == 0:
            logger.warning(
                "HypoInverse produced no usable .arc output. stdout tail:\n%s",
                result.stdout[-3000:],
            )
            prt_file = tmpdir / "h2000.prt"
            if prt_file.exists():
                logger.warning(
                    "HypoInverse .prt tail:\n%s",
                    prt_file.read_text()[-3000:],
                )
            return Catalog(), []

        catalog = _parse_arc(arc_file, pick_df, assign_df, cat_df, config)

    # Build flat location_rows for DB insertion
    location_rows: list[dict] = []
    for event in catalog:
        if not event.origins:
            continue
        orig = event.preferred_origin() or event.origins[0]
        q = orig.quality
        u = orig.origin_uncertainty
        location_rows.append(
            {
                "resource_id": str(orig.resource_id),
                "event_resource_id": str(event.resource_id),
                "time": orig.time.datetime,
                "latitude": orig.latitude,
                "longitude": orig.longitude,
                "depth_km": orig.depth / 1000.0 if orig.depth else 0.0,
                "rms": q.standard_error if q else None,
                "gap": q.azimuthal_gap if q else None,
                "dmin": q.minimum_distance if q else None,
                "nph": q.used_phase_count if q else None,
                "erh": u.horizontal_uncertainty / 1000.0
                if u and u.horizontal_uncertainty
                else None,
                "erz": (
                    orig.depth_errors.uncertainty / 1000.0
                    if orig.depth_errors and orig.depth_errors.uncertainty
                    else None
                ),
            }
        )

    logger.info("Located %d event(s).", len(location_rows))
    return catalog, location_rows
