"""
core/agents/negotiation_agent.py
──────────────────────────────────────────────────────────────────────────────
Negotiation Agent — the key differentiator.

Called when the preferred slot is unavailable. Finds and presents alternative
scheduling options to the customer:

  1. Closest available time (same staff if possible)
  2. Same staff, later in the day
  3. Faster completion option (different / any staff)
  4. A non-optimised option (earliest global slot, ignores staff preference)

Uses find_alternatives() from the scheduler engine.
"""
from __future__ import annotations

import logging
from datetime import datetime

from config.business import (
    NEGOTIATION_MAX_ALTERNATIVES,
    SLOT_GRANULARITY,
)
from core.scheduler import (
    ScheduleResult,
    build_schedule_request,
    find_alternatives,
)
from data.backends.sqlite import Database
from data.models import BookingData, Staff

log = logging.getLogger(__name__)


class NegotiationAgent:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def negotiate(
        self,
        booking: BookingData,
        original_failure: str = "",
    ) -> tuple[list[ScheduleResult], str]:
        """
        Find up to NEGOTIATION_MAX_ALTERNATIVES alternative slots.

        Returns (alternatives, reply_text).
        """
        if not booking.service or not booking.date or not booking.time:
            return [], "Mình cần biết dịch vụ, ngày và giờ để tìm lịch thay thế ạ."

        svc = _resolve_service(booking.service)
        if svc is None:
            return [], f"Không tìm thấy dịch vụ '{booking.service}'."

        existing    = await self._db.get_resource_assignments_for_date(booking.date)
        all_staff   = await self._db.list_staff()
        all_stations = await self._db.list_stations()

        preferred_staff_id: str | None = None
        if booking.preferred_staff:
            for s in all_staff:
                if s.id == booking.preferred_staff or s.name.lower() == booking.preferred_staff.lower():
                    preferred_staff_id = s.id
                    break

        req = build_schedule_request(
            date=booking.date,
            preferred_time=booking.time,
            service=svc,
            all_staff=all_staff,
            all_stations=all_stations,
            existing_assignments=existing,
            preferred_staff_id=preferred_staff_id,
        )

        # Option set 1: with preferred staff (strict — only slots when they're free)
        preferred_alts: list[ScheduleResult] = []
        if preferred_staff_id:
            preferred_alts = await find_alternatives(
                req,
                count=NEGOTIATION_MAX_ALTERNATIVES,
                granularity=SLOT_GRANULARITY,
            )

        # Option set 2: any available staff at the requested time (or nearby)
        req_any = build_schedule_request(
            date=booking.date,
            preferred_time=booking.time,
            service=svc,
            all_staff=all_staff,
            all_stations=all_stations,
            existing_assignments=existing,
            preferred_staff_id=None,
        )
        any_alts = await find_alternatives(
            req_any,
            count=NEGOTIATION_MAX_ALTERNATIVES,
            granularity=SLOT_GRANULARITY,
        )

        # Merge: preferred-staff slots first, then any-staff slots (no duplicates)
        seen_starts: set[str] = set()
        alternatives: list[ScheduleResult] = []
        for alt in preferred_alts:
            if alt.start_time not in seen_starts:
                alternatives.append(alt)
                seen_starts.add(alt.start_time)
        any_staff_alts: list[ScheduleResult] = []
        for alt in any_alts:
            if alt.start_time not in seen_starts:
                any_staff_alts.append(alt)
                seen_starts.add(alt.start_time)

        if not alternatives and not any_staff_alts:
            return [], (
                f"Rất tiếc, mình không tìm được lịch trống phù hợp cho dịch vụ "
                f"**{booking.service}** trong ngày {booking.date} ạ. "
                "Bạn có muốn thử ngày khác không?"
            )

        reply = _format_alternatives(
            preferred_alts=alternatives,
            any_staff_alts=any_staff_alts,
            date=booking.date,
            service_name=booking.service,
            preferred_staff_name=booking.preferred_staff,
            all_staff=all_staff,
        )
        all_alts = alternatives + any_staff_alts
        return all_alts, reply


def _resolve_service(name: str):
    """Delegate to booking_handler's normalised resolver (diacritic-insensitive)."""
    from core.agents.booking_handler import _resolve_multi_service
    return _resolve_multi_service(name)


def _staff_name(resource_id: str, all_staff: list[Staff]) -> str:
    obj = next((s for s in all_staff if s.id == resource_id), None)
    return obj.name if obj else resource_id


def _format_alternatives(
    preferred_alts: list[ScheduleResult],
    any_staff_alts: list[ScheduleResult],
    date: str,
    service_name: str,
    preferred_staff_name: str | None,
    all_staff: list[Staff],
) -> str:
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        date_vn = dt.strftime("%d/%m/%Y")
    except Exception:
        date_vn = date

    pref_label = f"**{preferred_staff_name}**" if preferred_staff_name else "nhân viên bạn chọn"
    total = len(preferred_alts) + len(any_staff_alts)

    lines = [
        f"😊 {pref_label} đã có lịch vào giờ đó. "
        f"Mình tìm được **{total}** lựa chọn thay thế cho **{service_name}** "
        f"ngày **{date_vn}**:\n"
    ]

    labels = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"]
    idx = 0

    if preferred_alts:
        lines.append(f"⏰ Giờ khác với {pref_label}:")
        for alt in preferred_alts:
            staff_step = next((s for s in alt.steps if s.resource_type == "stylist"), None)
            name = _staff_name(staff_step.resource_id, all_staff) if staff_step else ""
            lines.append(
                f"  {labels[idx]} **{alt.start_time}** – {alt.end_time} "
                f"(~{alt.total_duration} phút) — {name}"
            )
            idx += 1

    if any_staff_alts:
        lines.append(f"\n👥 Giờ bạn muốn với nhân viên khác:")
        for alt in any_staff_alts:
            staff_step = next((s for s in alt.steps if s.resource_type == "stylist"), None)
            name = _staff_name(staff_step.resource_id, all_staff) if staff_step else ""
            lines.append(
                f"  {labels[idx]} **{alt.start_time}** – {alt.end_time} "
                f"(~{alt.total_duration} phút) — {name}"
            )
            idx += 1

    lines.append(
        f"\nBạn muốn chọn lựa chọn nào? Nhắn số **1**–**{idx}** để xác nhận nhé!"
    )
    return "\n".join(lines)
