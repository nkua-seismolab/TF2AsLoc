"""GET /events - retrieve located events as QuakeML v1.2."""

import logging
from datetime import datetime
from io import BytesIO

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session, selectinload

from tf2asloc.db.models import (
    Arrival as DBArrival,
)
from tf2asloc.db.models import (
    Event,
    Origin,
    preferred_magnitude_of,
    preferred_origin_of,
)
from tf2asloc.db.session import get_session

logger = logging.getLogger(__name__)
router = APIRouter()

_MAX_EVENTS_DEFAULT = 1000


def _build_quakeml(events: list[Event], config: dict) -> bytes:
    """Convert a list of ORM Event rows into QuakeML v1.2 bytes via ObsPy."""
    from obspy import UTCDateTime
    from obspy.core.event import (
        Amplitude as OAmplitude,
    )
    from obspy.core.event import (
        Arrival,
        Catalog,
        Comment,
        CreationInfo,
        EventDescription,
        OriginQuality,
        QuantityError,
        ResourceIdentifier,
        WaveformStreamID,
    )
    from obspy.core.event import (
        Event as OEvent,
    )
    from obspy.core.event import (
        Magnitude as OMagnitude,
    )
    from obspy.core.event import (
        Origin as OOrigin,
    )
    from obspy.core.event import (
        Pick as OPick,
    )
    from obspy.geodetics.base import kilometer2degrees as k2d
    from obspy.geodetics.flinnengdahl import FlinnEngdahl

    from tf2asloc.utils.helpers import sanitize_rid_segment, smi_base

    agency = config.get("global", {}).get("agency", "unknown")
    base = smi_base(config)

    # Agency-only info for objects without their own stored creation time
    agency_info = CreationInfo(author=agency, agency_id=agency)
    fe = FlinnEngdahl()

    def _arrival_used(a) -> bool:
        # None = legacy rows persisted without weights; treat as used
        return a.time_weight is None or a.time_weight > 0

    catalog = Catalog()
    for ev in events:
        oevent = OEvent(
            resource_id=ResourceIdentifier(ev.resource_id),
            event_type="earthquake",
            creation_info=CreationInfo(
                author=agency,
                agency_id=agency,
                creation_time=(UTCDateTime(ev.created_at) if ev.created_at else None),
            ),
        )
        oevent.event_descriptions.append(
            EventDescription(
                text=fe.get_region(ev.longitude, ev.latitude),
                type="flinn-engdahl region",
            )
        )

        preferred_db_origin = preferred_origin_of(ev.origins)

        # Only the preferred origin is served; refinement history stays in the
        # DB. Emitted picks are resolved from its used arrivals directly - the
        # associator may have re-linked a pick's event_id to a newer event.
        used_arrivals = [
            a
            for a in (preferred_db_origin.arrivals if preferred_db_origin is not None else [])
            if _arrival_used(a)
        ]
        db_picks = []
        seen_pick_ids: set = set()
        for a in used_arrivals:
            if a.pick is not None and a.pick.id not in seen_pick_ids:
                seen_pick_ids.add(a.pick.id)
                db_picks.append(a.pick)

        # Picks (waveform id needed later for amplitudes)
        wfid_by_pick_id: dict = {}
        # azimuth per pick from the preferred origin's arrivals -> pick backazimuth
        az_by_pick_id: dict = {
            a.pick_id: a.azimuth
            for a in used_arrivals
            if a.pick_id is not None and a.azimuth is not None
        }
        for pk in db_picks:
            wfid = WaveformStreamID(
                network_code=pk.network,
                station_code=pk.station,
                location_code=pk.location,
                channel_code=pk.channel,
            )
            wfid_by_pick_id[pk.id] = wfid
            opick = OPick(
                resource_id=ResourceIdentifier(f"{base}/pick/{pk.id}"),
                time=UTCDateTime(pk.time),
                phase_hint=pk.phase,
                waveform_id=wfid,
                method_id=(
                    ResourceIdentifier(f"{base}/method/{sanitize_rid_segment(pk.model)}")
                    if pk.model
                    else None
                ),
                backazimuth=(
                    (180.0 + az_by_pick_id[pk.id]) % 360.0 if pk.id in az_by_pick_id else None
                ),
                evaluation_mode="automatic",
                creation_info=CreationInfo(author=pk.author, agency_id=agency),
            )
            opick.comments.append(Comment(text=f"prob={pk.prob:.4f} model={pk.model}"))
            oevent.picks.append(opick)

        rid_by_origin_uuid: dict = {}
        if preferred_db_origin is not None:
            orig = preferred_db_origin
            oorigin = OOrigin(
                resource_id=ResourceIdentifier(orig.resource_id),
                time=UTCDateTime(orig.time),
                latitude=orig.latitude,
                longitude=orig.longitude,
                depth=orig.depth_km * 1000.0,  # ObsPy expects metres
                depth_type="from location",
                method_id=ResourceIdentifier(f"{base}/method/{sanitize_rid_segment(orig.locator)}"),
                evaluation_mode="automatic",
                creation_info=CreationInfo(
                    author=agency,
                    agency_id=agency,
                    creation_time=(UTCDateTime(orig.created_at) if orig.created_at else None),
                ),
            )
            if orig.erh is not None:
                herr_deg = round(k2d(orig.erh), 6)
                oorigin.latitude_errors = QuantityError(uncertainty=herr_deg)
                oorigin.longitude_errors = QuantityError(uncertainty=herr_deg)
            if orig.erz is not None:
                oorigin.depth_errors = QuantityError(uncertainty=orig.erz * 1000.0)
            used_stations = {
                (a.pick.network, a.pick.station) for a in used_arrivals if a.pick is not None
            }
            oorigin.quality = OriginQuality(
                standard_error=orig.rms,
                azimuthal_gap=orig.gap,
                minimum_distance=(round(k2d(orig.dmin), 6) if orig.dmin is not None else None),
                # counts reflect the emitted document; origins persisted before
                # arrival storage fall back to the locator-reported nph
                used_phase_count=(len(used_arrivals) if orig.arrivals else orig.nph),
                used_station_count=(len(used_stations) if used_stations else None),
            )
            for a in used_arrivals:
                oorigin.arrivals.append(
                    Arrival(
                        pick_id=(
                            ResourceIdentifier(f"{base}/pick/{a.pick_id}") if a.pick_id else None
                        ),
                        phase=a.phase,
                        azimuth=a.azimuth,
                        distance=a.distance_deg,
                        takeoff_angle=a.takeoff_angle,
                        time_residual=a.time_residual,
                        time_weight=a.time_weight,
                        creation_info=agency_info,
                    )
                )
            oevent.origins.append(oorigin)
            rid_by_origin_uuid[orig.id] = oorigin.resource_id

        preferred_origin_rid = (
            rid_by_origin_uuid[preferred_db_origin.id] if preferred_db_origin is not None else None
        )

        mags_sorted = sorted(ev.magnitudes, key=lambda m: m.created_at or datetime.min)
        for mag in mags_sorted:
            omag = OMagnitude(
                resource_id=ResourceIdentifier(mag.resource_id),
                mag=mag.value,
                magnitude_type=mag.mag_type,
                # None when the computing origin is unknown (legacy rows)
                origin_id=rid_by_origin_uuid.get(mag.origin_id),
                creation_info=CreationInfo(
                    author=agency,
                    agency_id=agency,
                    creation_time=(UTCDateTime(mag.created_at) if mag.created_at else None),
                ),
            )
            oevent.magnitudes.append(omag)

        for amp in ev.amplitudes:
            wfid = wfid_by_pick_id.get(amp.pick_id)
            if wfid is None:
                # No linked pick -> no station attribution; skip the amplitude
                continue
            # QuakeML unit enum has no "mm" - convert to metres
            value = amp.value / 1000.0 if amp.unit == "mm" else amp.value
            oamp = OAmplitude(
                resource_id=ResourceIdentifier(f"{base}/amplitude/{amp.id}"),
                generic_amplitude=value,
                unit="m" if amp.unit == "mm" else amp.unit,
                period=amp.period,
                magnitude_hint="ML",
                waveform_id=wfid,
                pick_id=ResourceIdentifier(f"{base}/pick/{amp.pick_id}"),
                creation_info=agency_info,
            )
            oevent.amplitudes.append(oamp)

        if oevent.origins:
            oevent.preferred_origin_id = preferred_origin_rid
        if oevent.magnitudes:
            pref_mag = preferred_magnitude_of(ev.magnitudes, preferred_db_origin)
            oevent.preferred_magnitude_id = ResourceIdentifier(pref_mag.resource_id)

        catalog.append(oevent)

    buf = BytesIO()
    catalog.write(buf, format="QUAKEML")
    return buf.getvalue()


# Config is injected at app startup - see app.py
_config: dict = {}


def set_config(cfg: dict) -> None:
    global _config
    _config = cfg


def _parse_utc(value: str, name: str) -> datetime:
    """Parse an ISO-8601 instant; UTC-only API, so non-UTC offsets are rejected."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid time format for '{name}': {exc}",
        ) from exc
    if dt.tzinfo is not None:
        if dt.utcoffset():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'{name}' must be UTC ('Z' or +00:00 offset).",
            )
        dt = dt.replace(tzinfo=None)
    return dt


@router.get("/events")
def get_events(
    start: str = Query(..., description="Start time ISO-8601 UTC (inclusive)"),
    end: str = Query(..., description="End time ISO-8601 UTC (exclusive)"),
    minlatitude: float = Query(
        ..., ge=-90.0, le=90.0, description="Minimum latitude in degrees (inclusive)"
    ),
    maxlatitude: float = Query(
        ..., ge=-90.0, le=90.0, description="Maximum latitude in degrees (inclusive)"
    ),
    minlongitude: float = Query(
        ..., ge=-180.0, le=180.0, description="Minimum longitude in degrees (inclusive)"
    ),
    maxlongitude: float = Query(
        ..., ge=-180.0, le=180.0, description="Maximum longitude in degrees (inclusive)"
    ),
    mindepth: float | None = Query(None, description="Minimum depth in km (inclusive)"),
    maxdepth: float | None = Query(None, description="Maximum depth in km (inclusive)"),
    minmagnitude: float | None = Query(None, description="Minimum preferred magnitude (inclusive)"),
    maxmagnitude: float | None = Query(None, description="Maximum preferred magnitude (inclusive)"),
    session: Session = Depends(get_session),
) -> Response:
    """Return located events in the half-open window [start, end) as QuakeML v1.2 XML."""
    t_start = _parse_utc(start, "start")
    t_end = _parse_utc(end, "end")

    if t_end <= t_start:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="'end' must be after 'start'.",
        )
    for name, lo, hi in (
        ("latitude", minlatitude, maxlatitude),
        ("longitude", minlongitude, maxlongitude),
        ("depth", mindepth, maxdepth),
        ("magnitude", minmagnitude, maxmagnitude),
    ):
        if lo is not None and hi is not None and hi < lo:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"'max{name}' must not be less than 'min{name}'.",
            )

    max_events = _config.get("api", {}).get("max_events", _MAX_EVENTS_DEFAULT)

    query = (
        session.query(Event)
        .options(
            selectinload(Event.picks),
            selectinload(Event.origins).selectinload(Origin.arrivals).selectinload(DBArrival.pick),
            selectinload(Event.magnitudes),
            selectinload(Event.amplitudes),
        )
        .filter(
            Event.origin_time >= t_start,
            Event.origin_time < t_end,
            Event.latitude >= minlatitude,
            Event.latitude <= maxlatitude,
            Event.longitude >= minlongitude,
            Event.longitude <= maxlongitude,
        )
    )
    if mindepth is not None:
        query = query.filter(Event.depth_km >= mindepth)
    if maxdepth is not None:
        query = query.filter(Event.depth_km <= maxdepth)

    events: list[Event] = query.order_by(Event.origin_time).limit(max_events + 1).all()
    if len(events) > max_events:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=(
                f"Time window matches more than {max_events} events (api.max_events); "
                "split the request into smaller windows."
            ),
        )

    # Magnitude filtering uses the preferred magnitude, which is resolved in
    # Python by the same legacy rule the QuakeML serialization applies.
    if minmagnitude is not None or maxmagnitude is not None:

        def _mag_in_range(ev: Event) -> bool:
            mag = preferred_magnitude_of(ev.magnitudes, preferred_origin_of(ev.origins))
            if mag is None:
                return False
            if minmagnitude is not None and mag.value < minmagnitude:
                return False
            return not (maxmagnitude is not None and mag.value > maxmagnitude)

        events = [ev for ev in events if _mag_in_range(ev)]

    logger.info("GET /events [%s, %s) -> %d events", start, end, len(events))

    qml_bytes = _build_quakeml(events, _config)
    return Response(content=qml_bytes, media_type="application/xml; charset=utf-8")
