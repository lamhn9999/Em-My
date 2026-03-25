"""
data/backends/sqlite.py
────────────────────────────────────────────────────────────────────────────
Tables
──────
  clients   — one row per Zalo client (ClientProfile)
  messages  — full chat history, append-only
  bookings  — BookingData records (pending → confirmed / cancelled)
 
Design rules
────────────
  • All public methods are typed; callers never write raw SQL.
  • Transactions are explicit — callers decide when to commit.
  • `last_active` lives only in the DB (not in ClientProfile) — it's
    a convenience column for ops queries, not a domain field.
"""

from __future__ import annotations

import sqlite3
import aiosqlite
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncGenerator, Optional

from data.models import (
    BookingData,
    BookingIntent,
    BookingStatus,
    Role,
    Profile,
    Message,
)

def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

# ──────────────────────────────────────────────────────────────────────────────
# DDL
# ──────────────────────────────────────────────────────────────────────────────

_CREATE_PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    id          TEXT    PRIMARY KEY,
    name        TEXT    NOT NULL DEFAULT '',
    -- Matches Role Enum: 'client', 'member', 'owner', 'oa'
    role        TEXT    NOT NULL CHECK(role IN ('client', 'member', 'owner', 'oa')), 
    last_active TEXT    NOT NULL
);
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id          TEXT    PRIMARY KEY,
    sender_id       TEXT    NOT NULL,
    recipient_id    TEXT    NOT NULL,
    -- Message dataclass stores these as strings (usually 'user' or 'assistant' per Zalo)
    sender_role     TEXT    NOT NULL,
    recipient_role  TEXT    NOT NULL,
    text            TEXT    NOT NULL DEFAULT '',
    timestamp       TEXT    NOT NULL,
    synced_from_api INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (sender_id) REFERENCES profiles(id),
    FOREIGN KEY (recipient_id) REFERENCES profiles(id)
);
"""

_CREATE_BOOKINGS = """
CREATE TABLE IF NOT EXISTS bookings (
    booking_id       TEXT    PRIMARY KEY,
    client_id        TEXT    NOT NULL,
    -- Matches BookingIntent Enum
    intent           TEXT    NOT NULL CHECK(intent IN ('booking', 'cancel', 'query', 'unknown')),
    name             TEXT,
    phone            TEXT,
    service          TEXT,
    date             TEXT,   -- YYYY-MM-DD
    time             TEXT,   -- HH:MM 24h
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    notes            TEXT,
    confidence       REAL,
    denial_reason    TEXT,

    -- Matches BookingStatus Enum
    status           TEXT    NOT NULL CHECK(status IN ('pending', 'confirmed', 'cancelled')),
    created_at       TEXT    NOT NULL,
    FOREIGN KEY (client_id) REFERENCES profiles(id)
);
"""

_CREATE_MESSAGES_IDX = """
CREATE INDEX IF NOT EXISTS idx_messages_sender_ts
    ON messages(sender_id, timestamp);
"""

_CREATE_BOOKINGS_IDX = """
CREATE INDEX IF NOT EXISTS idx_bookings_user_status
    ON bookings(client_id, status);
"""

# ──────────────────────────────────────────────────────────────────────────────
# Database
# ──────────────────────────────────────────────────────────────────────────────

class Database:
    def __init__(self, db_path: str | Path = "data/store/app.db") -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: aiosqlite.Connection | None = None
 
    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(str(self._path))
        self._conn.row_factory = sqlite3.Row
        await self._conn.execute("PRAGMA journal_mode=WAL;")
        await self._conn.execute("PRAGMA foreign_keys=ON;")
        await self._migrate()
 
    async def _migrate(self) -> None:
        async with self.transaction():
            await self._conn.execute(_CREATE_PROFILES)
            await self._conn.execute(_CREATE_MESSAGES)
            await self._conn.execute(_CREATE_MESSAGES_IDX)
            await self._conn.execute(_CREATE_BOOKINGS)
            await self._conn.execute(_CREATE_BOOKINGS_IDX)
 
    @asynccontextmanager
    async def transaction(self) -> AsyncGenerator[None, None]:
        try:
            yield
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise
 
    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

     # ── Clients ───────────────────────────────────────────────────────────────
 
    async def upsert_profile(self, profile: Profile) -> None:
        """Saves or updates a profile. Converts Role enum to string for DB."""
        await self._conn.execute("""
            INSERT INTO profiles (id, name, role, last_active)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name        = excluded.name,
                role        = excluded.role,
                last_active = excluded.last_active;
            """,
            (profile.id, profile.name, profile.role.value, _utcnow()),
        )

    async def get_profile(self, profile_id: str) -> Profile | None:
        """Fetch by id from the 'profiles' table."""
        async with self._conn.execute(
            "SELECT id, name, role FROM profiles WHERE id = ?", (profile_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_profile(row) if row else None

    async def touch_profile(self, profile_id: str) -> None:
        """Update last_active timestamp without changing any profile fields."""
        await self._conn.execute(
            "UPDATE profiles SET last_active = ? WHERE id = ?",
            (_utcnow(), profile_id),
        )

    async def list_profiles(self, role: Optional[Role] = None) -> list[Profile]:
        """List all, optionally filtered by role (e.g., list all 'client')."""
        if role:
            async with self._conn.execute(
                "SELECT id, name, role FROM profiles WHERE role = ? ORDER BY last_active DESC",
                (role.value,)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._conn.execute(
                "SELECT id, name, role FROM profiles ORDER BY last_active DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_profile(r) for r in rows]
    
    # ── Messages ──────────────────────────────────────────────────────────────

    async def insert_message(self, msg: Message) -> bool:
        """Returns True if inserted, False if msg_id already existed."""
        try:
            await self._conn.execute(
                """INSERT INTO messages
                   (msg_id, sender_id, recipient_id, sender_role, recipient_role, text, timestamp, synced_from_api)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.msg_id,
                    msg.sender_id,
                    msg.recipient_id,
                    msg.sender_role,
                    msg.recipient_role,
                    msg.text,
                    msg.timestamp,
                    int(msg.synced_from_api),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False 

    async def get_messages(self, profile_id: str, last_n: int = 20) -> list[Message]:
        """Fetches the conversation history for a specific profile."""
        async with self._conn.execute(
            """SELECT * FROM messages
               WHERE sender_id = ? OR recipient_id = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (profile_id, profile_id, last_n),
        ) as cursor:
            rows = await cursor.fetchall()
        # Return in chronological order
        return [_row_to_message(r) for r in reversed(rows)]

    async def known_msg_ids(self, profile_id: str) -> set[str]:
        """Returns all message IDs associated with a specific profile."""
        async with self._conn.execute(
            "SELECT msg_id FROM messages WHERE sender_id = ? OR recipient_id = ?", 
            (profile_id, profile_id)
        ) as cursor:
            rows = await cursor.fetchall()
        return {r["msg_id"] for r in rows}
    
    # ── Bookings ──────────────────────────────────────────────────────────────

    async def insert_booking(self, booking: BookingData) -> None:
        await self._conn.execute(
            """INSERT INTO bookings
               (booking_id, client_id, intent, name, phone, service,
                date, time, duration_minutes, notes, confidence, denial_reason, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                booking.booking_id,
                booking.client_id,
                booking.intent.value,
                booking.name,
                booking.phone,
                booking.service,
                booking.date,
                booking.time,
                booking.duration_minutes,
                booking.notes,
                booking.confidence, 
                booking.denial_reason,
                booking.status.value,
                booking.created_at,
            ),
        )

    async def get_active_booking(self, client_id: str) -> BookingData | None:
        async with self._conn.execute(
            """SELECT * FROM bookings
               WHERE client_id = ? AND status = ?
               ORDER BY created_at DESC LIMIT 1""",
            (client_id, BookingStatus.PENDING.value),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_booking_data(row) if row else None

    async def get_last_confirmed_booking(self, client_id: str) -> BookingData | None:
        async with self._conn.execute(
            """SELECT * FROM bookings
               WHERE client_id = ? AND status = ?
               ORDER BY created_at DESC""",
            (client_id, BookingStatus.CONFIRMED.value),
        ) as cursor:
            rows = await cursor.fetchall()

        for row in rows:
            b = _row_to_booking_data(row)
            if b.is_upcoming():
                return b
        return None

    async def get_confirmed_bookings_by_date(self, date: str) -> list[dict]:
        async with self._conn.execute(
            """SELECT time, duration_minutes FROM bookings 
               WHERE date = ? AND status = ?
               ORDER BY time ASC""",
            (date, BookingStatus.CONFIRMED.value),
        ) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_bookings_for_client(self, client_id: str) -> list[BookingData]:
        async with self._conn.execute(
            "SELECT * FROM bookings WHERE client_id = ? ORDER BY created_at DESC",
            (client_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_booking_data(r) for r in rows]

    async def update_booking(self, booking: BookingData) -> None:
        """Overwrite an existing booking row with new data."""
        await self._conn.execute(
            """UPDATE bookings SET
               intent = ?, name = ?, phone = ?, service = ?,
               date = ?, time = ?, duration_minutes = ?, 
               notes = ?, confidence = ?, denial_reason = ?, status = ?
               WHERE booking_id = ?""",
            (
                booking.intent.value,
                booking.name,
                booking.phone,
                booking.service,
                booking.date,
                booking.time,
                booking.duration_minutes,
                booking.notes,
                booking.confidence,
                booking.denial_reason,
                booking.status.value,
                booking.booking_id,
            ),
        )

    async def update_booking_status(self, booking_id: str, status: BookingStatus) -> None:
        """Quickly update only the status (e.g., to 'confirmed')."""
        await self._conn.execute(
            "UPDATE bookings SET status = ? WHERE booking_id = ?",
            (status.value, booking_id),
        )

    async def find_overlap(self, date: str, time: str, duration: int) -> dict | None:
        """
        Check if any CONFIRMED booking exists for the same date that overlaps in time.
        """
        async with self._conn.execute(
            """SELECT name, date, time, duration_minutes FROM bookings 
               WHERE date = ? AND status = ?""",
            (date, BookingStatus.CONFIRMED.value),
        ) as cursor:
            rows = await cursor.fetchall()

        def time_to_min(t_str: str) -> int:
            h, m = map(int, t_str.split(':'))
            return h * 60 + m

        try:
            new_start = time_to_min(time)
            new_end = new_start + duration

            for row in rows:
                if not row["time"]:
                    continue
                exist_start = time_to_min(row["time"])
                exist_end = exist_start + row["duration_minutes"]
                
                # Check for overlap: max(start1, start2) < min(end1, end2)
                if max(new_start, exist_start) < min(new_end, exist_end):
                    return dict(row)
        except Exception:
            pass # fallback if time strings are severely malformed

        return None
    
# ──────────────────────────────────────────────────────────────────────────────
# Row converters
# ──────────────────────────────────────────────────────────────────────────────
 
def _row_to_profile(row: sqlite3.Row) -> Profile:
    return Profile(
        id=row["id"],
        name=row["name"],
        role=Role(row["role"])
    )

def _row_to_message(row: sqlite3.Row) -> Message:
    return Message(
        msg_id=row["msg_id"],
        sender_id=row["sender_id"],
        recipient_id=row["recipient_id"],
        sender_role=row["sender_role"],
        recipient_role=row["recipient_role"],
        text=row["text"],
        timestamp=row["timestamp"],
        synced_from_api=bool(row["synced_from_api"])
    )

def _row_to_booking_data(row: sqlite3.Row) -> BookingData:
    return BookingData(
        booking_id=row["booking_id"],
        client_id=row["client_id"],  # Match DDL column name
        intent=BookingIntent(row["intent"]),
        name=row["name"],
        phone=row["phone"],
        service=row["service"],
        date=row["date"],
        time=row["time"],
        duration_minutes=row["duration_minutes"],
        notes=row["notes"],
        confidence=row["confidence"],
        denial_reason=row["denial_reason"],
        status=BookingStatus(row["status"]),
        created_at=row["created_at"]
    )