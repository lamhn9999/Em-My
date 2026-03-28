"""
core/agents/availability_agent.py
──────────────────────────────────────────────────────────────────────────────
Availability Agent — handles Type 2 (AVAILABILITY) queries.

Answers questions like:
  • "Is Brad free tomorrow at 9am?"
  • "What slots are open on Friday?"
  • "Do you have anything available next week?"

Uses the scheduler to enumerate free slots given existing bookings.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from config.business import BUSINESS_HOURS, SLOT_GRANULARITY, SERVICES
from core.scheduler import _hhmm_to_min, _min_to_hhmm, _day_of_week
from data.backends.sqlite import Database


class AvailabilityAgent:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def handle(self, date: str, service_name: str | None = None,
                     preferred_staff_id: str | None = None) -> str:
        """
        Return a human-readable availability reply for the given date.
        If service_name provided, slots are filtered to fit the service duration.
        """
        day = _day_of_week(date)
        biz = BUSINESS_HOURS.get(day)
        if not biz:
            return f"Salon không mở cửa vào {_fmt_date(date)} ({_day_vn(day)}) ạ."

        open_min  = _hhmm_to_min(biz[0])
        close_min = _hhmm_to_min(biz[1])

        # Figure out minimum duration
        duration = 60  # default
        svc_obj = None
        if service_name:
            for s in SERVICES:
                if s.name.lower() == service_name.lower():
                    duration = s.total_duration
                    svc_obj = s
                    break

        # Load existing confirmed bookings for that date
        existing = await self._db.get_resource_assignments_for_date(date)
        booked_slots = await self._db.get_confirmed_bookings_by_date(date)

        # Build simple busy map per minute for a global view
        busy_minutes: set[int] = set()
        for slot in booked_slots:
            if slot.get("time"):
                s = _hhmm_to_min(slot["time"])
                e = s + slot["duration_minutes"]
                for m in range(s, e):
                    busy_minutes.add(m)

        # Preferred staff busy times
        staff_busy: set[int] = set()
        if preferred_staff_id:
            for a in existing:
                if a["resource_id"] == preferred_staff_id:
                    s = _hhmm_to_min(a["start_time"])
                    e = _hhmm_to_min(a["end_time"])
                    for m in range(s, e):
                        staff_busy.add(m)

        # Enumerate free slots
        free_slots: list[str] = []
        t = open_min
        while t + duration <= close_min:
            slot_range = set(range(t, t + duration))
            if not slot_range.intersection(busy_minutes):
                if not preferred_staff_id or not slot_range.intersection(staff_busy):
                    free_slots.append(_min_to_hhmm(t))
            t += SLOT_GRANULARITY

        if not free_slots:
            reply = f"Rất tiếc, {_fmt_date(date)} ({_day_vn(day)}) salon đã kín lịch rồi ạ."
            if service_name:
                reply += f"\nBạn muốn mình kiểm tra ngày khác cho dịch vụ **{service_name}** không?"
            return reply

        slots_str = "  •  ".join(free_slots[:10])
        header = f"📅 Lịch trống {_fmt_date(date)} ({_day_vn(day)})"
        if service_name:
            header += f" — **{service_name}** (~{duration} phút)"
        if preferred_staff_id:
            staff_list = await self._db.list_staff()
            staff_name = next(
                (s.name for s in staff_list if s.id == preferred_staff_id), preferred_staff_id
            )
            header += f" — nhân viên **{staff_name}**"

        return (
            f"{header}:\n"
            f"{slots_str}\n\n"
            "Bạn muốn đặt lịch vào khung giờ nào ạ?"
        )

    async def handle_upcoming_slots(self, days_ahead: int = 3) -> str:
        """Give a quick overview of available days in the next N days."""
        vn_tz = timezone(timedelta(hours=7))
        today = datetime.now(vn_tz).date()
        lines = []
        for i in range(1, days_ahead + 1):
            d = today + timedelta(days=i)
            date_str = d.strftime("%Y-%m-%d")
            day = _day_of_week(date_str)
            if day not in BUSINESS_HOURS:
                continue
            biz = BUSINESS_HOURS[day]
            booked = await self._db.get_confirmed_bookings_by_date(date_str)
            lines.append(
                f"• {_fmt_date(date_str)} ({_day_vn(day)}): "
                f"{len(booked)} lịch đã đặt — {biz[0]}–{biz[1]}"
            )
        if not lines:
            return "Không có ngày làm việc nào trong khoảng thời gian tới."
        return "📅 **Tổng quan lịch sắp tới:**\n" + "\n".join(lines)


def _day_vn(day: str) -> str:
    return {
        "mon": "Thứ 2", "tue": "Thứ 3", "wed": "Thứ 4",
        "thu": "Thứ 5", "fri": "Thứ 6", "sat": "Thứ 7", "sun": "Chủ nhật",
    }.get(day, day)


def _fmt_date(date_str: str) -> str:
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.strftime("%d/%m/%Y")
    except Exception:
        return date_str
