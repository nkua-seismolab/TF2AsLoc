"""SQLAlchemy ORM models for TF2AsLoc."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Pick(Base):
    __tablename__ = "picks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    network: Mapped[str] = mapped_column(String(8), nullable=False)
    station: Mapped[str] = mapped_column(String(16), nullable=False)
    location: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    phase: Mapped[str] = mapped_column(String(4), nullable=False)
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    prob: Mapped[float] = mapped_column(Float, nullable=False)
    model: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    author: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # Set to the associated Event.id after association + location
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=True, default=None
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    event: Mapped["Event | None"] = relationship("Event", back_populates="picks")


class Event(Base):
    __tablename__ = "events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    origin_time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    depth_km: Mapped[float] = mapped_column(Float, nullable=False)
    # GaMMA score of the latest accepted association (used by the churn guard)
    gamma_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    picks: Mapped[list["Pick"]] = relationship("Pick", back_populates="event")
    origins: Mapped[list["Origin"]] = relationship("Origin", back_populates="event")
    magnitudes: Mapped[list["Magnitude"]] = relationship("Magnitude", back_populates="event")
    amplitudes: Mapped[list["Amplitude"]] = relationship("Amplitude", back_populates="event")


class Origin(Base):
    __tablename__ = "origins"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False
    )
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    time: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    depth_km: Mapped[float] = mapped_column(Float, nullable=False)
    rms: Mapped[float | None] = mapped_column(Float, nullable=True)
    gap: Mapped[float | None] = mapped_column(Float, nullable=True)
    erh: Mapped[float | None] = mapped_column(Float, nullable=True)
    erz: Mapped[float | None] = mapped_column(Float, nullable=True)
    dmin: Mapped[float | None] = mapped_column(Float, nullable=True)
    nph: Mapped[int | None] = mapped_column(Integer, nullable=True)
    locator: Mapped[str] = mapped_column(String(32), nullable=False, default="hypoinverse")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    event: Mapped["Event"] = relationship("Event", back_populates="origins")
    arrivals: Mapped[list["Arrival"]] = relationship("Arrival", back_populates="origin")


# An origin with RMS above this is a mis-association (blended picks from
# different quakes saturate residuals); it must never win on phase count.
RMS_SANITY_SEC = 1.5


def origin_is_better(new: "Origin", old: "Origin") -> bool:
    """Legacy preference rule: more used phases, smaller RMS or smaller ERH."""
    new_bad = new.rms is not None and new.rms > RMS_SANITY_SEC
    old_bad = old.rms is not None and old.rms > RMS_SANITY_SEC
    if new_bad != old_bad:
        return old_bad
    if (new.nph or 0) > (old.nph or 0):
        return True
    if new.rms is not None and (old.rms is None or new.rms < old.rms):
        return True
    if new.erh is not None and (old.erh is None or new.erh < old.erh):
        return True
    return False


def preferred_origin_of(origins: list["Origin"]) -> "Origin | None":
    """Fold origins chronologically with origin_is_better (legacy rule)."""
    preferred = None
    for orig in sorted(origins, key=lambda o: o.created_at or datetime.min):
        if preferred is None or origin_is_better(orig, preferred):
            preferred = orig
    return preferred


class Arrival(Base):
    """Per-phase association of a pick to an origin (from the locator)."""

    __tablename__ = "arrivals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    origin_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("origins.id"), nullable=False
    )
    pick_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("picks.id"), nullable=True
    )
    phase: Mapped[str] = mapped_column(String(4), nullable=False)
    azimuth: Mapped[float | None] = mapped_column(Float, nullable=True)
    distance_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    takeoff_angle: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_residual: Mapped[float | None] = mapped_column(Float, nullable=True)
    time_weight: Mapped[float | None] = mapped_column(Float, nullable=True)

    origin: Mapped["Origin"] = relationship("Origin", back_populates="arrivals")
    # direct link: pick.event_id may be re-assigned by later associations
    pick: Mapped["Pick | None"] = relationship("Pick")


class Magnitude(Base):
    __tablename__ = "magnitudes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False
    )
    # Origin this magnitude was computed for (nullable for legacy rows)
    origin_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("origins.id"), nullable=True
    )
    resource_id: Mapped[str] = mapped_column(String(256), nullable=False)
    mag_type: Mapped[str] = mapped_column(String(16), nullable=False)  # ML, Mamp, Mdur
    value: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=_utcnow)

    event: Mapped["Event"] = relationship("Event", back_populates="magnitudes")


def preferred_magnitude_of(
    magnitudes: list["Magnitude"], preferred_origin: "Origin | None"
) -> "Magnitude | None":
    """Newest magnitude of the preferred origin, else newest overall."""
    if not magnitudes:
        return None
    mags = sorted(magnitudes, key=lambda m: m.created_at or datetime.min)
    if preferred_origin is not None:
        pref = [m for m in mags if m.origin_id == preferred_origin.id]
        if pref:
            return pref[-1]
    return mags[-1]


class Amplitude(Base):
    __tablename__ = "amplitudes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    pick_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("picks.id"), nullable=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("events.id"), nullable=False
    )
    value: Mapped[float] = mapped_column(Float, nullable=False)
    period: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(16), nullable=False, default="mm")

    event: Mapped["Event"] = relationship("Event", back_populates="amplitudes")


class SystemState(Base):
    """Single-row table used as a state bridge between the API and Orchestrator.

    The API writes to this table (via POST /system/reload-inventory) and the
    Orchestrator reads it each tick to detect an on-demand inventory reload
    request.  The row with id=1 is seeded by init_db() and must never be
    deleted.
    """

    __tablename__ = "system_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_inventory_reload: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, default=lambda: datetime(1970, 1, 1)
    )
