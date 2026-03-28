"""
core/agents/waitlist_agent.py
──────────────────────────────────────────────────────────────────────────────
Waitlist Agent — manages the priority queue of waiting customers.

Responsibilities:
  • Add customers to the waitlist when their preferred slot is full
  • On cancellation: scan the waitlist for eligible customers and notify them
  • Priority order: FIFO with a boost for customers whose preferred staff
    just became free

Called by:
  • BookingHandler / NegotiationAgent (add to waitlist)
  • BookingAgent orchestrator (notify after cancellation)
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from data.backends.sqlite import Database
from data.models import BookingData, WaitlistEntry

log = logging.getLogger(__name__)


class WaitlistAgent:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(
        self,
        client_id: str,
        client_name: str,
        service: str,
        preferred_date: str,
        preferred_time: str | None = None,
        preferred_staff: str | None = None,
    ) -> WaitlistEntry:
        """Add a customer to the waitlist. Returns the created entry."""
        entry = WaitlistEntry(
            id=f"WL-{uuid.uuid4().hex[:8].upper()}",
            client_id=client_id,
            client_name=client_name,
            service=service,
            preferred_date=preferred_date,
            preferred_time=preferred_time,
            preferred_staff=preferred_staff,
            created_at=datetime.now(timezone.utc).isoformat(),
            notified=False,
        )
        async with self._db.transaction():
            await self._db.add_to_waitlist(entry)
        log.info("Added %s to waitlist for %s on %s", client_id, service, preferred_date)
        return entry

    async def notify_on_cancellation(
        self, cancelled_booking: BookingData
    ) -> list[tuple[str, str]]:
        """
        After a booking is cancelled, find waitlist entries for the same date
        that could now be served. Returns [(client_id, message)] to be sent.

        Priority:
          1. Same preferred_staff (staff just became free)
          2. FIFO (created_at order)
        """
        if not cancelled_booking.date:
            return []

        entries = await self._db.get_waitlist_for_date(cancelled_booking.date)
        if not entries:
            return []

        freed_staff = set(cancelled_booking.assigned_resources)

        # Sort: preferred-staff match first, then FIFO
        def priority(e: WaitlistEntry) -> tuple[int, str]:
            staff_match = 0 if (e.preferred_staff and e.preferred_staff in freed_staff) else 1
            return (staff_match, e.created_at)

        entries.sort(key=priority)

        notifications: list[tuple[str, str]] = []
        for entry in entries:
            if entry.service.lower() != (cancelled_booking.service or "").lower():
                # Waitlist entry is for a different service — may still fit the
                # freed slot, but let's only notify exact service matches for now
                continue

            msg = (
                f"🎉 Tin vui! Vừa có lịch trống cho dịch vụ **{entry.service}** "
                f"ngày **{entry.preferred_date}**"
            )
            if entry.preferred_time:
                msg += f" lúc **{entry.preferred_time}**"
            if entry.preferred_staff and entry.preferred_staff in freed_staff:
                msg += f" với nhân viên bạn yêu thích"
            msg += ".\n\nBạn có muốn đặt lịch ngay không? Nhắn **'đặt lịch'** để tiếp tục!"

            async with self._db.transaction():
                await self._db.mark_waitlist_notified(entry.id)
            notifications.append((entry.client_id, msg))

        return notifications

    async def get_position(self, client_id: str, date: str) -> int | None:
        """Return the waitlist position (1-based) for a client on a given date."""
        entries = await self._db.get_waitlist_for_date(date)
        for i, e in enumerate(entries, start=1):
            if e.client_id == client_id:
                return i
        return None

    async def remove(self, client_id: str, date: str) -> bool:
        """Remove a client from the waitlist for a specific date."""
        entries = await self._db.get_waitlist_for_client(client_id)
        for e in entries:
            if e.preferred_date == date:
                await self._db.remove_from_waitlist(e.id)
                return True
        return False

    @staticmethod
    def waitlist_reply(entry: WaitlistEntry) -> str:
        return (
            f"Mình đã thêm bạn vào danh sách chờ cho dịch vụ **{entry.service}** "
            f"ngày **{entry.preferred_date}**"
            + (f" lúc **{entry.preferred_time}**" if entry.preferred_time else "")
            + ".\n"
            "Mình sẽ báo bạn ngay khi có lịch trống nhé! 😊"
        )
