"""
core/agents/update_agent.py
──────────────────────────────────────────────────────────────────────────────
Update Agent — handles Type 4 (UPDATE) requests.

Responsibilities:
  • Find the customer's active/upcoming confirmed booking
  • Apply requested changes (time, date, service, staff)
  • Re-run the scheduler with the updated parameters, excluding the current
    booking's resource assignments to avoid false self-collision
  • Minimise disruption: only change what was asked
  • Return confirmation or hand off to NegotiationAgent if new slot unavailable
"""
from __future__ import annotations

import logging

from core.scheduler import build_schedule_request, schedule as run_schedule
from data.backends.sqlite import Database
from data.models import BookingData, BookingStatus

log = logging.getLogger(__name__)


class UpdateAgent:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def handle(
        self,
        client_id: str,
        patch: dict,
    ) -> tuple[BookingData | None, str]:
        """
        Apply `patch` to the most recent upcoming booking for this client.

        patch keys (all optional):
          date, time, service, preferred_staff, notes

        Returns (updated_booking, reply_text).
        updated_booking is None if the update could not be applied.
        """
        # Find booking to update: prefer active (PENDING), then last confirmed upcoming
        booking = await self._db.get_active_booking(client_id)
        if booking is None:
            booking = await self._db.get_last_confirmed_booking(client_id)
        if booking is None:
            return None, (
                "Mình không tìm thấy lịch hẹn nào sắp tới của bạn để cập nhật ạ. "
                "Bạn có muốn đặt lịch mới không?"
            )

        # Apply patch
        if "date" in patch and patch["date"]:
            booking.date = patch["date"]
        if "time" in patch and patch["time"]:
            booking.time = patch["time"]
        if "service" in patch and patch["service"]:
            booking.service = patch["service"]
        if "preferred_staff" in patch:
            booking.preferred_staff = patch["preferred_staff"]
        if "notes" in patch and patch["notes"]:
            booking.notes = patch["notes"]

        # Re-run scheduler with updated params
        from core.agents.booking_handler import _resolve_multi_service
        svc = _resolve_multi_service(booking.service or "")
        if svc is None:
            return None, f"Dịch vụ '{booking.service}' không tồn tại trong hệ thống ạ."

        # Exclude the current booking's own assignments to avoid self-collision
        existing = await self._db.get_resource_assignments_for_date(
            booking.date or "", exclude_booking_id=booking.booking_id
        )
        all_staff    = await self._db.list_staff()
        all_stations = await self._db.list_stations()

        preferred_staff_id: str | None = None
        if booking.preferred_staff:
            for s in all_staff:
                if s.id == booking.preferred_staff or s.name.lower() == booking.preferred_staff.lower():
                    preferred_staff_id = s.id
                    break

        req = build_schedule_request(
            date=booking.date or "",
            preferred_time=booking.time or "09:00",
            service=svc,
            all_staff=all_staff,
            all_stations=all_stations,
            existing_assignments=existing,
            preferred_staff_id=preferred_staff_id,
        )

        result = await run_schedule(req)
        if not result.success:
            return None, (
                f"Rất tiếc, khung giờ mới không khả dụng: {result.failure_reason}\n"
                "Bạn có muốn thử giờ khác không?"
            )

        # Persist update
        booking.steps_schedule     = result.steps
        booking.assigned_resources = list({s.resource_id for s in result.steps if s.resource_type != "wait"})
        booking.duration_minutes   = result.total_duration
        booking.time               = result.start_time

        async with self._db.transaction():
            await self._db.update_booking(booking)

        # Resolve assigned stylist's display name
        staff_step = next((s for s in result.steps if s.resource_type == "stylist"), None)
        if staff_step:
            staff_obj = next((s for s in all_staff if s.id == staff_step.resource_id), None)
            staff_name = staff_obj.name if staff_obj else staff_step.resource_id
        else:
            staff_name = "bất kỳ"

        reply = (
            f"✅ Đã cập nhật lịch hẹn của bạn!\n\n"
            f"📋 **Lịch mới:**\n"
            f"• Dịch vụ: {booking.service}\n"
            f"• Ngày: {booking.date}\n"
            f"• Giờ: {result.start_time} – {result.end_time}\n"
            f"• Nhân viên: {staff_name}\n"
            f"• Thời gian: ~{result.total_duration} phút\n\n"
            "Nếu cần thay đổi thêm, bạn cứ nhắn mình nhé!"
        )
        return booking, reply
