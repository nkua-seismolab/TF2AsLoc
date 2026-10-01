"""Shared utility functions for TF2AsLoc.

Ported from legacy/chunks/f_t314_misc_functions.py.
"""

import logging
import re
import zlib
from datetime import datetime

import numpy as np
import pandas as pd
from obspy import Stream, UTCDateTime
from obspy.core.inventory import Channel, Inventory, Site
from obspy.core.inventory.network import Network
from obspy.core.inventory.station import Station

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Distance
# ---------------------------------------------------------------------------


def calculate_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance in kilometres."""
    R = 6371.0
    lat1_r, lon1_r, lat2_r, lon2_r = (np.radians(x) for x in (lat1, lon1, lat2, lon2))
    dlat = lat2_r - lat1_r
    dlon = lon2_r - lon1_r
    a = np.sin(dlat / 2) ** 2 + np.cos(lat1_r) * np.cos(lat2_r) * np.sin(dlon / 2) ** 2
    return R * 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))


# ---------------------------------------------------------------------------
# Waveform fetching
# ---------------------------------------------------------------------------


def fetch_waveform(client, entry) -> Stream:
    """Fetch a single waveform segment; returns an empty Stream on 204 / no-data."""
    net, sta, loc, cha, start, end = entry
    try:
        return client.get_waveforms(net, sta, loc, cha, start, end)
    except Exception as exc:
        if hasattr(exc, "status_code") and exc.status_code == 204:
            logger.debug("No data (204) for %s.%s.%s.%s", net, sta, loc, cha)
            return Stream()
        logger.warning("Error fetching %s.%s.%s.%s: %s", net, sta, loc, cha, exc)
        raise


# ---------------------------------------------------------------------------
# Resource IDs
# ---------------------------------------------------------------------------

# Complement of the charset ObsPy accepts inside a smi: URI path segment
_RID_UNSAFE = re.compile(r"[^\w\-\.\*\(\)~']")


def sanitize_rid_segment(segment) -> str:
    """Make an arbitrary string safe for use inside a smi: resource identifier."""
    cleaned = _RID_UNSAFE.sub("_", str(segment))
    return cleaned or "unknown"


def smi_base(config: dict) -> str:
    """Return the rid base 'smi:{authority}' from global.smi_authority."""
    authority = config.get("global", {}).get("smi_authority", "local")
    return f"smi:{sanitize_rid_segment(authority)}"


def make_event_resource_id(
    row: pd.Series, agency: str, region: str, authority: str = "local"
) -> str:
    dt = pd.to_datetime(row["time"])
    time_str = dt.strftime("%Y%m%d%H%M%S") + f"{int(dt.microsecond / 1000):03d}"
    # Coordinate suffix (~110 m granularity) disambiguates events sharing the
    # same millisecond origin time - e.g. simultaneous events in different
    # regions - which would otherwise silently merge under one resource_id.
    loc_str = f"{float(row['latitude']):.3f}_{float(row['longitude']):.3f}"
    return (
        f"smi:{sanitize_rid_segment(authority)}/Event/"
        f"{sanitize_rid_segment(agency)}/{sanitize_rid_segment(region)}/"
        f"{time_str}_{sanitize_rid_segment(loc_str)}"
    )


def generate_event_id2(
    origin_time: datetime,
    latitude: float,
    longitude: float,
    depth_km: float = 5.0,
) -> str:
    """Generate a deterministic 10-digit numeric event ID.

    Mixes the full millisecond timestamp and the hypocentre through a stable
    CRC so the ID is reproducible across processes (unlike the salted builtin
    ``hash()``) and does not recycle its time component every 1000 s.
    Capped below 1.5e9 to stay within HypoInverse's event-ID field.
    """
    unix_ms = int(origin_time.timestamp() * 1000)
    payload = f"{unix_ms},{latitude:.5f},{longitude:.5f},{depth_km:.2f}"
    event_id = zlib.crc32(payload.encode()) % 1_500_000_000
    return f"{event_id:010d}"


# ---------------------------------------------------------------------------
# Inventory helpers
# ---------------------------------------------------------------------------


def get_station_coords(inv: Inventory, station_id: str) -> dict:
    """Return {lat, lon, elev_m} for a *net.sta* or *net.sta.loc* seed ID."""
    parts = station_id.split(".")
    net_code, sta_code = parts[0], parts[1]
    for net in inv:
        if net.code != net_code:
            continue
        for sta in net:
            if sta.code != sta_code:
                continue
            return {"lat": sta.latitude, "lon": sta.longitude, "elev_m": sta.elevation}
    raise KeyError(f"Station {station_id} not found in inventory.")


def inv_subset_from_picks(inv: Inventory, prows: pd.DataFrame) -> Inventory:
    """Return an Inventory subset covering only the stations present in *prows*.

    *prows* must have columns ``id`` (net.sta.loc) and ``timestamp``.
    """
    new_networks: list[Network] = []
    seen: set[str] = set()

    for _, row in prows.iterrows():
        seed_id: str = row["id"]
        if seed_id in seen:
            continue
        seen.add(seed_id)

        ot = UTCDateTime(row["timestamp"])
        parts = seed_id.split(".")
        if len(parts) >= 3:
            net_code, sta_code, loc_code = parts[0], parts[1], parts[2]
        else:
            net_code, sta_code, loc_code = parts[0], parts[1], ""

        matched_station: Station | None = None
        active_channels: list[Channel] = []

        for network in inv:
            if network.code != net_code:
                continue
            for station in network:
                if station.code != sta_code:
                    continue
                chans = [
                    ch
                    for ch in station.channels
                    if ch.location_code == loc_code
                    and (not hasattr(ch, "is_active") or ch.is_active(ot))
                ]
                if not chans:
                    chans = [
                        ch
                        for ch in station.channels
                        if (loc_code == "" or ch.location_code == "")
                        and (not hasattr(ch, "is_active") or ch.is_active(ot))
                    ]
                if chans:
                    matched_station = station
                    active_channels = chans
                    break

        if not matched_station or not active_channels:
            logger.debug("No active channels for %s - skipping.", seed_id)
            continue

        new_sta = Station(
            code=matched_station.code,
            latitude=active_channels[0].latitude,
            longitude=active_channels[0].longitude,
            elevation=active_channels[0].elevation,
            creation_date=active_channels[0].start_date,
            site=matched_station.site,
            channels=active_channels,
        )
        existing_net = next((n for n in new_networks if n.code == net_code), None)
        if existing_net:
            existing_net.stations.append(new_sta)
        else:
            new_networks.append(
                Network(
                    code=net_code,
                    stations=[new_sta],
                    description=getattr(network, "description", ""),
                )
            )

    return Inventory(networks=new_networks, source=inv.source)


def extract_inventory_subset_Z_from_station(
    station_obj: Station, network_code_raw: str, OT: UTCDateTime
) -> Inventory:
    """Extract a minimal Inventory containing the first active Z-channel for *station_obj*."""
    network_code = network_code_raw.split(".")[0]
    loc_code = network_code_raw.split(".")[2] if len(network_code_raw.split(".")) > 2 else ""

    def _build(ch: Channel) -> Inventory:
        site = station_obj.site or Site(name=station_obj.code)
        new_channel = Channel(
            code=ch.code,
            location_code=ch.location_code,
            latitude=ch.latitude,
            longitude=ch.longitude,
            elevation=ch.elevation,
            depth=ch.depth,
            azimuth=ch.azimuth,
            dip=ch.dip,
            sample_rate=ch.sample_rate,
            start_date=ch.start_date,
            end_date=ch.end_date,
            response=ch.response,
        )
        new_sta = Station(
            code=station_obj.code,
            latitude=new_channel.latitude,
            longitude=new_channel.longitude,
            elevation=new_channel.elevation,
            site=site,
            channels=[new_channel],
            creation_date=new_channel.start_date,
        )
        return Inventory(
            networks=[Network(code=network_code, stations=[new_sta])],
            source="Subset extracted from Station object",
        )

    for ch in station_obj.channels:
        if ch.code.endswith("Z") and ch.location_code == loc_code and ch.is_active(OT):
            return _build(ch)
    # Fallback: ignore active check
    for ch in station_obj.channels:
        if ch.code.endswith("Z") and ch.location_code == loc_code:
            return _build(ch)

    raise ValueError(
        f"No Z-channel found in station '{station_obj.code}' for loc '{loc_code}' at {OT}."
    )


def filter_highest_sampling_rate(stream: Stream) -> Stream:
    """Keep, per station, only the channel group with the highest sampling rate.

    Traces are grouped per (net, sta) by (location, channel prefix) - e.g.
    ALIK's HH* vs EH* - and only the group with the highest sampling rate
    is kept, so amplitude measurement always uses a single consistent
    channel set per station.  Ties are broken by alphabetically-first group
    key (prefers e.g. HH over HN).  Sampling rate is assumed stable within
    a group; a mismatch is a station-operator metadata error and is logged.
    """
    if not stream:
        return stream

    # (net, sta) -> (loc, chan_prefix) -> [sampling rates]
    groups: dict[tuple, dict[tuple, list[float]]] = {}
    for tr in stream:
        sta_key = (tr.stats.network, tr.stats.station)
        grp_key = (tr.stats.location, tr.stats.channel[:-1])
        groups.setdefault(sta_key, {}).setdefault(grp_key, []).append(tr.stats.sampling_rate)

    best: dict[tuple, tuple] = {}
    for sta_key, grps in groups.items():
        for grp_key, rates in grps.items():
            if len(set(rates)) > 1:
                logger.error(
                    "Mixed sampling rates %s within %s.%s.%s.%s* - station "
                    "metadata error, report to the operator.",
                    sorted(set(rates)),
                    sta_key[0],
                    sta_key[1],
                    *grp_key,
                )
        best[sta_key] = min(grps, key=lambda k: (-max(grps[k]), k))

    return Stream(
        [
            tr
            for tr in stream
            if (tr.stats.location, tr.stats.channel[:-1])
            == best[(tr.stats.network, tr.stats.station)]
        ]
    )


# ---------------------------------------------------------------------------
# Fixed-width number formatters (required for HypoInverse Hyp1.4 format)
# ---------------------------------------------------------------------------


def num2str_nopoint(val: float, dig: int, dec: int) -> str:
    """Right-justify a float scaled by 10^dec as a string of width *dig* (no decimal point)."""
    if val == 0:
        return "0".rjust(dig)
    result = str(round(val * (10**dec)))
    formatted = result.rjust(dig)
    if len(formatted) != dig:
        raise ValueError(f"num2str_nopoint: length {len(formatted)} != {dig} for value {val}")
    return formatted


def num2str_nopoint_negmag(val: float, dig: int, dec: int) -> str:
    """Like num2str_nopoint but handles small negatives (e.g. -0.01 -> '-01').
    Values below -0.98 are clamped to -0.99 (Hyp1.4 manual p.48).
    """
    if val < -0.98:
        val = -0.99
    if -1 < val < 0:
        scaled = round(-val * (10**dec))
        result = "-" + f"{scaled:0{dec}d}"
    else:
        result = str(round(val * (10**dec)))
    formatted = result.rjust(dig)
    if len(formatted) != dig:
        raise ValueError(f"num2str_nopoint_negmag: length {len(formatted)} != {dig}")
    return formatted


def num2str_nopoint0(val: float, dig: int, dec: int) -> str:
    """Like num2str_nopoint but zero-pads on the left instead of space-padding."""
    if val == 0:
        return "0".rjust(dig)
    result = str(round(val * (10**dec)))
    formatted = result.zfill(dig)
    if len(formatted) != dig:
        raise ValueError(f"num2str_nopoint0: length {len(formatted)} != {dig}")
    return formatted


def strtofloat(line: str, start: int, length: int, decimals: int) -> float:
    """Parse a fixed-width implied-decimal float from a Hyp1.4 archive line.

    *start* is a 1-based column number (as in the HypoInverse manual and the
    legacy reader).  Fields that contain an explicit decimal point are taken
    at face value; otherwise the implied decimal is applied.

    Example: strtofloat("  1234", 1, 6, 2) -> 12.34
    """
    raw = line[start - 1 : start - 1 + length].strip()
    if not raw or raw == "0":
        return 0.0
    try:
        if "." in raw:
            return float(raw)
        return int(raw) / (10**decimals)
    except ValueError:
        return 0.0
