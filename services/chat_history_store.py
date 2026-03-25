from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from data.backends.sqlite import Database
from data.models import (
    BookingData, 
    BookingIntent, 
    BookingStatus, 
    Profile, 
    Role, 
    Message, 
    _utcnow
)

class ChatHistoryStore:
    def __init__(self, db: Database, oa_id: str) -> None:
        self._db = db
        self.oa_id = oa_id

    async def init(self) -> None:
        await self.ensure_profile(self.oa_id, "OA ACCOUNT", Role.OA)

    # ── Profiles ──────────────────────────────────────────────────────────────

    async def ensure_profile(self, profile_id: str, name: str = "", role: Role = Role.CLIENT) -> None:
        """Create profile row on first contact if it does not exist yet."""
        if await self._db.get_profile(profile_id) is None:
            profile = Profile(id=profile_id, name=name, role=role)
            async with self._db.transaction():
                await self._db.upsert_profile(profile)

    async def get_known_msg_ids(self, profile_id: str, limit: int = 50) -> set[str]:
        """Retrieve a set of message IDs already stored for this user to prevent duplicates."""
        messages = await self.get_history(profile_id, last_n=limit)
        return {m.msg_id for m in messages if m.msg_id}

    # ── Messages ──────────────────────────────────────────────────────────────

    async def append_message(
        self,
        sender_id: str,
        recipient_id: str,
        text: str,
        sender_role: str,      
        recipient_role: str,  
        *,
        msg_id: str | None = None,
        timestamp_ms: int | None = None,
        synced_from_api: bool = False,
    ) -> Message:
        await self.ensure_profile(sender_id)
        await self.ensure_profile(recipient_id)

        ts = (
            datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc).isoformat()
            if timestamp_ms else _utcnow()
        )

        msg = Message(
            msg_id=msg_id or f"local_{uuid.uuid4().hex}",
            sender_id=sender_id,
            recipient_id=recipient_id,
            sender_role=sender_role,
            recipient_role=recipient_role,
            text=text,
            timestamp=ts,
            synced_from_api=synced_from_api,
        )

        async with self._db.transaction():
            await self._db.insert_message(msg)
            await self._db.touch_profile(sender_id)
            await self._db.touch_profile(recipient_id)
        return msg

    async def get_history(self, profile_id: str, last_n: int = 10) -> list[Message]:
        return await self._db.get_messages(profile_id, last_n)

    async def as_llm_context(self, profile_id: str, last_n: int = 10) -> list[dict[str, str]]:
        """Return history formatted as OpenAI-style dicts for LLM calls."""
        history = await self.get_history(profile_id, last_n)
        return [{"role": m.sender_role, "content": m.text} for m in history]

    # ── Booking lifecycle ─────────────────────────────────────────────────────

    async def get_active_booking(self, client_id: str) -> BookingData | None:
        return await self._db.get_active_booking(client_id)

    async def start_booking(self, client_id: str, intent: BookingIntent) -> BookingData:
        await self.ensure_profile(client_id)
        booking = BookingData(
            booking_id=f"BK-{uuid.uuid4().hex[:8].upper()}",
            client_id=client_id, 
            intent=intent,
        )
        async with self._db.transaction():
            await self._db.insert_booking(booking)
        return booking

    async def confirm_active_booking(self, client_id: str) -> BookingData | None:
        booking = await self._db.get_active_booking(client_id)
        if booking is None:
            return None
        async with self._db.transaction():
            await self._db.update_booking_status(booking.booking_id, BookingStatus.CONFIRMED)
        booking.status = BookingStatus.CONFIRMED
        return booking

    async def cancel_active_booking(self, client_id: str) -> bool:
        booking = await self._db.get_active_booking(client_id)
        if booking is None:
            return False
        async with self._db.transaction():
            await self._db.update_booking_status(booking.booking_id, BookingStatus.CANCELLED)
        return True

    async def cancel_last_confirmed_booking(self, client_id: str) -> BookingData | None:
        booking = await self._db.get_last_confirmed_booking(client_id)
        if booking is None:
            return None
        async with self._db.transaction():
            await self._db.update_booking_status(booking.booking_id, BookingStatus.CANCELLED)
        booking.status = BookingStatus.CANCELLED
        return booking

    async def get_confirmed_bookings_by_date(self, date: str) -> list[dict]:
        return await self._db.get_confirmed_bookings_by_date(date)

    async def get_bookings_for_client(self, client_id: str) -> list[BookingData]:
        return await self._db.get_bookings_for_client(client_id)

    async def update_active_booking(self, client_id: str, patch: dict) -> BookingData | None:
        booking = await self._db.get_active_booking(client_id)
        if booking is None:
            return None
            
        # Create a new instance with updated fields
        updated = BookingData(
            booking_id=booking.booking_id,
            client_id=booking.client_id,
            intent=BookingIntent(patch.get("intent", booking.intent.value)),
            name=patch.get("name", booking.name),
            phone=patch.get("phone", booking.phone),
            service=patch.get("service", booking.service),
            date=patch.get("date", booking.date),
            time=patch.get("time", booking.time),
            duration_minutes=patch.get("duration_minutes", booking.duration_minutes),
            notes=patch.get("notes", booking.notes),
            confidence=patch.get("confidence", booking.confidence),
            denial_reason=patch.get("denial_reason", booking.denial_reason),
            status=booking.status,
            created_at=booking.created_at,
        )
        
        async with self._db.transaction():
            await self._db.update_booking(updated)
        return updated

    async def find_overlap(self, date: str, time: str, duration: int) -> dict | None:
        """Gateway for the Validator to check the DB for conflicts."""
        return await self._db.find_overlap(date, time, duration)

if __name__ == '__main__':
    # Usage of the script main block will require an async runner now.
    import asyncio
    async def run_test():
        from services import ZALOOA_ACCESS_TOKEN, ZALOOA_ID
        db = Database()
        await db.connect()
        chs = ChatHistoryStore(db, ZALOOA_ID)
        await chs.init()
        await db.close()
    
    # asyncio.run(run_test())