"""
models.py — Shared dataclass schemas (no external deps)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class BookingIntent(str, Enum):
    BOOKING = "booking"
    CANCEL  = "cancel"
    QUERY   = "query"
    UNKNOWN = "unknown"


@dataclass
class BookingData:
    intent:           BookingIntent
    name:             Optional[str]  = None
    phone:            Optional[str]  = None
    service:          Optional[str]  = None
    date:             Optional[str]  = None   # YYYY-MM-DD
    time:             Optional[str]  = None   # HH:MM 24h
    duration_minutes: int            = 60
    notes:            Optional[str]  = None
    confidence:       float          = 0.0
    missing_fields:   list[str]      = field(default_factory=list)
    denial_reason:    Optional[str]  = None