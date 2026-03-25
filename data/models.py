"""
models.py — Shared dataclass schemas (no external deps)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional, List


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


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


@dataclass
class Profile:
    id:   str
    name: str
    role: Role

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

