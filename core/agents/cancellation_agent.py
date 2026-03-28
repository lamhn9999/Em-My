"""
core/agents/cancellation_agent.py
──────────────────────────────────────────────────────────────────────────────
Cancellation Agent — handles Type 5 (CANCELLATION) requests.

Responsibilities:
  • Cancel active (PENDING) or a specific upcoming CONFIRMED booking
  • Disambiguate by date/service hints when the customer has multiple bookings
  • Free the associated resources
  • Trigger the Waitlist Agent to notify waiting customers
  • Warn if there are no bookings to cancel
"""
from __future__ import annotations

import logging

from data.backends.sqlite import Database
from data.models import BookingData, BookingStatus

log = logging.getLogger(__name__)


class CancellationAgent:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def handle(
        self,
        client_id: str,
        hint_date: str | None = None,
        hint_service: str | None = None,
    ) -> tuple[BookingData | None, str]:
        """
        Cancel the most relevant upcoming booking.

        If hint_date / hint_service are supplied, find the booking that best
        matches those hints. This prevents "cancel booking" from always
        cancelling the soonest booking when the customer has multiple.

        Returns (cancelled_booking, reply_text).
        cancelled_booking is None if there was nothing to cancel.
        """
        # Try active (PENDING) first — no hints needed, there's only one pending
        booking = await self._db.get_active_booking(client_id)
        if booking:
            # If hints strongly suggest a different booking, skip to confirmed list
            hint_mismatch = (
                (hint_date and booking.date and booking.date != hint_date)
                or (hint_service and booking.service
                    and hint_service.lower() not in (booking.service or "").lower())
            )
            if not hint_mismatch:
                async with self._db.transaction():
                    await self._db.update_booking_status(
                        booking.booking_id, BookingStatus.CANCELLED
                    )
                return booking, (
                    "✅ Đã huỷ lịch hẹn đang chờ xác nhận của bạn.\n"
                    "Bạn có muốn đặt lịch khác không?"
                )

        # Fetch all upcoming confirmed bookings
        upcoming = await self._db.get_upcoming_confirmed_bookings(client_id)
        if not upcoming:
            return None, (
                "Mình không tìm thấy lịch hẹn nào sắp tới của bạn để huỷ ạ.\n"
                "Bạn có muốn đặt lịch mới không?"
            )

        # Disambiguate: find the booking matching the hints
        target: BookingData | None = None
        if hint_date or hint_service:
            for b in upcoming:
                date_ok = (not hint_date) or (b.date == hint_date)
                svc_ok  = (not hint_service) or (
                    hint_service.lower() in (b.service or "").lower()
                )
                if date_ok and svc_ok:
                    target = b
                    break

        # Fall back to the soonest upcoming booking
        if target is None:
            target = upcoming[0]

        async with self._db.transaction():
            await self._db.update_booking_status(
                target.booking_id, BookingStatus.CANCELLED
            )
        log.info("Cancelled booking %s for %s", target.booking_id, client_id)

        # Resources are now free — WaitlistAgent will be notified by the orchestrator
        reply = (
            f"✅ Đã huỷ lịch hẹn của bạn:\n"
            f"• Dịch vụ: {target.service}\n"
            f"• Ngày: {target.date} lúc {target.time}\n\n"
        )

        # Inform about remaining bookings so the customer knows what's still scheduled
        remaining = [b for b in upcoming if b.booking_id != target.booking_id]
        if remaining:
            lines = [f"📌 Bạn vẫn còn {len(remaining)} lịch hẹn sắp tới:"]
            for b in remaining:
                lines.append(f"  • {b.service} — {b.date} lúc {b.time}")
            reply += "\n".join(lines) + "\n\n"

        reply += "Nếu bạn muốn đặt lịch mới, mình luôn sẵn sàng hỗ trợ! 😊"
        return target, reply
