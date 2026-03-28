"""
models.py — Shared dataclass schemas (no external deps)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, List, Dict, Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Message type classification (8 types) ─────────────────────────────────────
class MessageType(int, Enum):
    ABUSE        = 0  # Abuse / prompt injection
    INFO         = 1  # Information query
    AVAILABILITY = 2  # Availability query
    BOOKING      = 3  # Booking request
    UPDATE       = 4  # Update existing booking
    CANCELLATION = 5  # Cancellation request
    GREETING     = 6  # Greeting
    OTHER        = 7  # Anything else


class BookingIntent(str, Enum):
    BOOKING = "booking"
    CANCEL  = "cancel"
    QUERY   = "query"
    UNKNOWN = "unknown"


class BookingStatus(str, Enum):
    PENDING   = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class Role(str, Enum):
    CLIENT  = "client"
    MEMBER  = "member"
    OWNER   = "owner"
    OA      = "oa"


# ── Resource & service definitions ────────────────────────────────────────────

@dataclass
class ServiceStep:
    """One operation within a multi-step service (e.g. wash → color → wait)."""
    step_index:           int
    step_type:            str             # "wash", "color", "wait", "rinse", "cut", etc.
    duration:             int             # minutes
    resource_type:        Optional[str]   # "stylist", "washing_bed", "chair", etc. None for wait
    skill_required:       Optional[str] = None  # skill for stylist-type steps (e.g. "color")
    staff_skill_required: Optional[str] = None  # for station-type steps that also need a human operator (e.g. "wash")


@dataclass
class ServiceDefinition:
    """A named service offered by the salon with its ordered steps."""
    name:           str
    steps:          List[ServiceStep]

    @property
    def total_duration(self) -> int:
        return sum(s.duration for s in self.steps)


@dataclass
class Staff:
    """A staff member with skills and availability."""
    id:            str
    name:          str
    skills:        List[str]             # e.g. ["cut", "fade", "color"]
    working_hours: Dict[str, List[str]]  # {"mon": ["09:00","18:00"], ...}


@dataclass
class Station:
    """A physical resource (chair, washing bed, etc.)."""
    id:   str
    name: str
    type: str   # "chair", "washing_bed", "color_station", etc.


@dataclass
class ResourceAssignment:
    """Scheduled assignment of one service step to a resource."""
    step_index:    int
    step_type:     str
    resource_id:   str
    resource_type: str
    start_time:    str   # HH:MM
    end_time:      str   # HH:MM
    duration:      int   # minutes


# ── Waitlist & Blacklist ───────────────────────────────────────────────────────

@dataclass
class WaitlistEntry:
    id:              str
    client_id:       str
    client_name:     str
    service:         str
    preferred_date:  str            # YYYY-MM-DD
    preferred_time:  Optional[str]  # HH:MM or None (any time)
    preferred_staff: Optional[str]  # staff id or None
    created_at:      str
    notified:        bool = False


@dataclass
class BlacklistEntry:
    client_id:  str
    reason:     str
    blocked:    bool
    created_at: str


# ── Core domain entities ───────────────────────────────────────────────────────

@dataclass
class Profile:
    id:             str
    name:           str
    role:           Role
    favorite_staff: Optional[str] = None   # staff id


@dataclass
class Message:
    msg_id:             str
    sender_id:          str
    recipient_id:       str
    sender_role:        str
    recipient_role:     str
    text:               str
    timestamp:          str
    synced_from_api:    bool = False


@dataclass
class BookingData:
    """Persisted booking record — written to the bookings table."""
    intent:             BookingIntent
    booking_id:         str = ""
    client_id:          str = ""
    # ── booking fields ────────────────────────────────
    name:             Optional[str] = None
    phone:            Optional[str] = None
    service:          Optional[str] = None
    date:             Optional[str] = None   # YYYY-MM-DD
    time:             Optional[str] = None   # HH:MM 24h
    duration_minutes: int           = 60
    notes:            Optional[str] = None
    confidence:       float         = field(default=0.0)
    query_type:       Optional[str] = None
    denial_reason:    Optional[str] = None
    # ── multi-step scheduling ─────────────────────────
    preferred_staff:      Optional[str]             = None   # requested staff id/name
    assigned_resources:   List[str]                 = field(default_factory=list)
    steps_schedule:       List[ResourceAssignment]  = field(default_factory=list)
    message_type:         int                       = -1     # 0-7 MessageType
    # ── lifecycle ─────────────────────────────────────
    status:     BookingStatus = BookingStatus.PENDING
    created_at: str           = field(default_factory=_utcnow)

    def is_complete(self) -> bool:
        return all([self.name, self.phone, self.service, self.date, self.time])

    def is_upcoming(self) -> bool:
        if not self.date or not self.time:
            return False

        vn_tz = timezone(timedelta(hours=7))
        now = datetime.now(vn_tz)
        today_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")

        if self.date > today_str:
            return True
        if self.date == today_str and self.time >= time_str:
            return True

        return False

