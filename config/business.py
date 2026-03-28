"""
config/business.py
──────────────────────────────────────────────────────────────────────────────
Salon business configuration.

Defines the default set of staff, stations, and multi-step services that are
seeded into the database on first startup. Everything here can be changed by
the business owner — this file is the single source of truth for shop config.

Scaling note: Each ServiceDefinition supports multi-step workflows (Flexible
Job Shop model), so this same config format works for any service business:
  • Single-step: football pitch, restaurant → one step, one resource
  • Multi-step:  salon, spa → sequential steps with different resources
"""
from __future__ import annotations

from data.models import ServiceDefinition, ServiceStep, Staff, Station

# ──────────────────────────────────────────────────────────────────────────────
# Working hours template  (mon=0 … sun=6, "HH:MM" open/close pairs)
# ──────────────────────────────────────────────────────────────────────────────
_FULL_WEEK = {
    "mon": ["09:00", "20:00"],
    "tue": ["09:00", "20:00"],
    "wed": ["09:00", "20:00"],
    "thu": ["09:00", "20:00"],
    "fri": ["09:00", "20:00"],
    "sat": ["08:00", "21:00"],
    "sun": ["09:00", "18:00"],
}

_FULL_WEEK_SHORT = {
    "mon": ["09:00", "18:00"],
    "tue": ["09:00", "18:00"],
    "wed": ["09:00", "18:00"],
    "thu": ["09:00", "18:00"],
    "fri": ["09:00", "18:00"],
    "sat": ["09:00", "18:00"],
    "sun": ["09:00", "18:00"],
}

# ──────────────────────────────────────────────────────────────────────────────
# Staff
# ──────────────────────────────────────────────────────────────────────────────
STAFF: list[Staff] = [
    Staff(
        id="stylist_1",
        name="Linh",
        skills=["cut", "fade", "color", "bleach", "highlight"],
        working_hours=_FULL_WEEK,
    ),
    Staff(
        id="stylist_2",
        name="Minh",
        skills=["cut", "fade", "perm", "straighten"],
        working_hours=_FULL_WEEK,
    ),
    Staff(
        id="stylist_3",
        name="Hoa",
        skills=["color", "bleach", "highlight", "treatment"],
        working_hours=_FULL_WEEK_SHORT,
    ),
    Staff(
        id="technician_1",
        name="Tuấn",
        skills=["wash", "rinse", "treatment", "massage"],
        working_hours=_FULL_WEEK,
    ),
]

# ──────────────────────────────────────────────────────────────────────────────
# Stations (physical resources)
# ──────────────────────────────────────────────────────────────────────────────
STATIONS: list[Station] = [
    Station(id="chair_1",         name="Ghế cắt 1",   type="chair"),
    Station(id="chair_2",         name="Ghế cắt 2",   type="chair"),
    Station(id="chair_3",         name="Ghế cắt 3",   type="chair"),
    Station(id="washing_bed_1",   name="Bồn gội 1",   type="washing_bed"),
    Station(id="washing_bed_2",   name="Bồn gội 2",   type="washing_bed"),
    Station(id="color_station_1", name="Bàn nhuộm 1", type="color_station"),
    Station(id="color_station_2", name="Bàn nhuộm 2", type="color_station"),
]
# ──────────────────────────────────────────────────────────────────────────────
# Service definitions  (multi-step)
# ──────────────────────────────────────────────────────────────────────────────
# resource_type must match Station.type or a staff skill keyword:
#   "chair"         → any chair station
#   "washing_bed"   → any washing bed station
#   "color_station" → any color station
#   "stylist"       → any staff member with required skill
#   None            → unattended wait step (no resource needed)

SERVICES: list[ServiceDefinition] = [
    ServiceDefinition(
        name="cắt tóc",
        steps=[
            ServiceStep(0, "cut",   30, "stylist", skill_required="cut"),
        ],
    ),
    ServiceDefinition(
        name="cắt + gội",
        steps=[
            ServiceStep(0, "wash",  10, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "cut",   30, "stylist",     skill_required="cut"),
        ],
    ),
    ServiceDefinition(
        name="uốn tóc",
        steps=[
            ServiceStep(0, "wash",  10, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "perm",  60, "stylist",     skill_required="perm"),
            ServiceStep(2, "wait",  20, None),
            ServiceStep(3, "rinse", 15, "washing_bed", staff_skill_required="rinse"),
        ],
    ),
    ServiceDefinition(
        name="nhuộm tóc",
        steps=[
            ServiceStep(0, "wash",   10, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "color",  60, "stylist",     skill_required="color"),
            ServiceStep(2, "wait",   30, None),
            ServiceStep(3, "rinse",  10, "washing_bed", staff_skill_required="rinse"),
        ],
    ),
    ServiceDefinition(
        name="tẩy + nhuộm",
        steps=[
            ServiceStep(0, "wash",    10, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "bleach",  60, "stylist",     skill_required="bleach"),
            ServiceStep(2, "wait",    30, None),
            ServiceStep(3, "rinse",   10, "washing_bed", staff_skill_required="rinse"),
            ServiceStep(4, "color",   45, "stylist",     skill_required="color"),
            ServiceStep(5, "wait",    30, None),
            ServiceStep(6, "rinse",   10, "washing_bed", staff_skill_required="rinse"),
        ],
    ),
    ServiceDefinition(
        name="highlight",
        steps=[
            ServiceStep(0, "wash",      10, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "highlight", 90, "stylist",     skill_required="highlight"),
            ServiceStep(2, "wait",      30, None),
            ServiceStep(3, "rinse",     10, "washing_bed", staff_skill_required="rinse"),
        ],
    ),
    ServiceDefinition(
        name="duỗi tóc",
        steps=[
            ServiceStep(0, "wash",      10, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "straighten",60, "stylist",     skill_required="straighten"),
            ServiceStep(2, "wait",      20, None),
            ServiceStep(3, "rinse",     10, "washing_bed", staff_skill_required="rinse"),
        ],
    ),
    ServiceDefinition(
        name="hấp dầu",
        steps=[
            ServiceStep(0, "wash",      10, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "treatment", 30, "stylist",     skill_required="treatment"),
            ServiceStep(2, "wait",      15, None),
            ServiceStep(3, "rinse",     10, "washing_bed", staff_skill_required="rinse"),
        ],
    ),
    ServiceDefinition(
        name="gội đầu",
        steps=[
            ServiceStep(0, "wash",  10, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "rinse", 10, "washing_bed", staff_skill_required="rinse"),
        ],
    ),
    ServiceDefinition(
        name="gội đầu massage",
        steps=[
            ServiceStep(0, "wash",    15, "washing_bed", staff_skill_required="wash"),
            ServiceStep(1, "massage", 20, "stylist",     skill_required="massage"),
            ServiceStep(2, "rinse",   10, "washing_bed", staff_skill_required="rinse"),
        ],
    ),
]

# ──────────────────────────────────────────────────────────────────────────────
# Business hours
# ──────────────────────────────────────────────────────────────────────────────
BUSINESS_HOURS: dict[str, tuple[str, str]] = {
    "mon": ("09:00", "20:00"),
    "tue": ("09:00", "20:00"),
    "wed": ("09:00", "20:00"),
    "thu": ("09:00", "20:00"),
    "fri": ("09:00", "20:00"),
    "sat": ("08:00", "21:00"),
    "sun": ("09:00", "18:00"),
}

# Slot granularity for availability suggestions (minutes)
SLOT_GRANULARITY = 30

# Minimum advance booking (minutes from now)
MIN_ADVANCE_MINUTES = 30

# Maximum days ahead a booking can be made
MAX_ADVANCE_DAYS = 60

# How many alternatives the Negotiation Agent should offer
NEGOTIATION_MAX_ALTERNATIVES = 3


async def seed_business_config(db) -> None:
    """
    Idempotently seed staff, stations, and services into the database.
    Called once at startup by bootstrap() in booking_agent.py.
    """
    async with db.transaction():
        for s in STAFF:
            await db.upsert_staff(s)
        for st in STATIONS:
            await db.upsert_station(st)

    for svc in SERVICES:
        await db.upsert_service(svc)
