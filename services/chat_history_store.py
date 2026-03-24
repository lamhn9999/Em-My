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
        self.ensure_profile(self.oa_id, "OA ACCOUNT", Role.OA)

    # ── Profiles ──────────────────────────────────────────────────────────────

    def ensure_profile(self, profile_id: str, name: str = "", role: Role = Role.CLIENT) -> None:
        """Create profile row on first contact if it does not exist yet."""
        if self._db.get_profile(profile_id) is None:
            profile = Profile(id=profile_id, name=name, role=role)
            with self._db.transaction():
                self._db.upsert_profile(profile)

    def get_known_msg_ids(self, profile_id: str, limit: int = 50) -> set[str]:
        """Retrieve a set of message IDs already stored for this user to prevent duplicates."""
        messages = self.get_history(profile_id, last_n=limit)
        return {m.msg_id for m in messages if m.msg_id}

    # ── Messages ──────────────────────────────────────────────────────────────

    def append_message(
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
        self.ensure_profile(sender_id)
        self.ensure_profile(recipient_id)

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

        with self._db.transaction():
            self._db.insert_message(msg)
            self._db.touch_profile(sender_id)
            self._db.touch_profile(recipient_id)
        return msg

    def get_history(self, profile_id: str, last_n: int = 10) -> list[Message]:
        return self._db.get_messages(profile_id, last_n)

    def as_llm_context(self, profile_id: str, last_n: int = 10) -> list[dict[str, str]]:
        """Return history formatted as OpenAI-style dicts for LLM calls."""
        # Note: 'role' here refers to the LLM role (user/assistant)
        return [
            {"role": m.sender_role, "content": m.text}
            for m in self.get_history(profile_id, last_n)
        ]

    # ── Booking lifecycle ─────────────────────────────────────────────────────

    def get_active_booking(self, client_id: str) -> BookingData | None:
        return self._db.get_active_booking(client_id)

    def start_booking(self, client_id: str, intent: BookingIntent) -> BookingData:
        self.ensure_profile(client_id)
        booking = BookingData(
            booking_id=f"BK-{uuid.uuid4().hex[:8].upper()}",
            client_id=client_id, 
            intent=intent,
        )
        with self._db.transaction():
            self._db.insert_booking(booking)
        return booking

    def confirm_active_booking(self, client_id: str) -> BookingData | None:
        booking = self._db.get_active_booking(client_id)
        if booking is None:
            return None
        with self._db.transaction():
            self._db.update_booking_status(booking.booking_id, BookingStatus.CONFIRMED)
        booking.status = BookingStatus.CONFIRMED
        return booking

    def update_active_booking(self, client_id: str, patch: dict) -> BookingData | None:
        booking = self._db.get_active_booking(client_id)
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
        
        with self._db.transaction():
            # CHANGED: Use update instead of insert to avoid Primary Key errors
            self._db.update_booking(updated)
        return updated

    def find_overlap(self, date: str, time: str, duration: int) -> dict | None:
        """Gateway for the Validator to check the DB for conflicts."""
        return self._db.find_overlap(date, time, duration)
if __name__ == '__main__':
    from services import ZALOOA_ACCESS_TOKEN, ZALOOA_ID
    chs = ChatHistoryStore(Database(), ZALOOA_ACCESS_TOKEN)
    history = chs.as_llm_context(profile_id="5039048321029636237")
    print(history)