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

import json
import sqlite3
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

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
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._migrate()
 
    def _migrate(self) -> None:
        with self.transaction():
            self._conn.execute(_CREATE_PROFILES)
            self._conn.execute(_CREATE_MESSAGES)
            self._conn.execute(_CREATE_MESSAGES_IDX)
            self._conn.execute(_CREATE_BOOKINGS)
            self._conn.execute(_CREATE_BOOKINGS_IDX)
 
    @contextmanager
    def transaction(self) -> Generator[None, None, None]:
        try:
            yield
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
 
    def close(self) -> None:
        self._conn.close()

     # ── Clients ───────────────────────────────────────────────────────────────
 
    def upsert_profile(self, profile: Profile) -> None:
        """Saves or updates a profile. Converts Role enum to string for DB."""
        self._conn.execute("""
            INSERT INTO profiles (id, name, role, last_active)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name        = excluded.name,
                role        = excluded.role,
                last_active = excluded.last_active;
            """,
            (profile.id, profile.name, profile.role.value, _utcnow()),
        )

    def get_profile(self, profile_id: str) -> Profile | None:
        """Fetch by id from the 'profiles' table."""
        row = self._conn.execute(
            "SELECT id, name, role FROM profiles WHERE id = ?", (profile_id,)
        ).fetchone()
        return _row_to_profile(row) if row else None

    def touch_profile(self, profile_id: str) -> None:
        """Update last_active timestamp without changing any profile fields."""
        self._conn.execute(
            "UPDATE profiles SET last_active = ? WHERE id = ?",
            (_utcnow(), profile_id),
        )

    def list_profiles(self, role: Optional[Role] = None) -> list[Profile]:
        """List all, optionally filtered by role (e.g., list all 'client')."""
        if role:
            rows = self._conn.execute(
                "SELECT id, name, role FROM profiles WHERE role = ? ORDER BY last_active DESC",
                (role.value,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT id, name, role FROM profiles ORDER BY last_active DESC"
            ).fetchall()
        return [_row_to_profile(r) for r in rows]
    
    # ── Messages ──────────────────────────────────────────────────────────────

    def insert_message(self, msg: Message) -> bool:
        """Returns True if inserted, False if msg_id already existed."""
        try:
            self._conn.execute(
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

    def get_messages(self, profile_id: str, last_n: int = 20) -> list[Message]:
        """Fetches the conversation history for a specific profile."""
        rows = self._conn.execute(
            """SELECT * FROM messages
               WHERE sender_id = ? OR recipient_id = ?
               ORDER BY timestamp DESC
               LIMIT ?""",
            (profile_id, profile_id, last_n),
        ).fetchall()
        # Return in chronological order
        return [_row_to_message(r) for r in reversed(rows)]

    def known_msg_ids(self, profile_id: str) -> set[str]:
        """Returns all message IDs associated with a specific profile."""
        rows = self._conn.execute(
            "SELECT msg_id FROM messages WHERE sender_id = ? OR recipient_id = ?", 
            (profile_id, profile_id)
        ).fetchall()
        return {r["msg_id"] for r in rows}
    
    # ── Bookings ──────────────────────────────────────────────────────────────

    def insert_booking(self, booking: BookingData) -> None:
        self._conn.execute(
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

    def get_active_booking(self, client_id: str) -> BookingData | None:
        row = self._conn.execute(
            """SELECT * FROM bookings
               WHERE client_id = ? AND status = ?
               ORDER BY created_at DESC LIMIT 1""",
            (client_id, BookingStatus.PENDING.value),
        ).fetchone()
        return _row_to_booking_data(row) if row else None

    def get_bookings_for_client(self, client_id: str) -> list[BookingData]:
        rows = self._conn.execute(
            "SELECT * FROM bookings WHERE client_id = ? ORDER BY created_at DESC",
            (client_id,),
        ).fetchall()
        return [_row_to_booking_data(r) for r in rows]

    def update_booking(self, booking: BookingData) -> None:
        """Overwrite an existing booking row with new data."""
        self._conn.execute(
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

    def update_booking_status(self, booking_id: str, status: BookingStatus) -> None:
        """Quickly update only the status (e.g., to 'confirmed')."""
        self._conn.execute(
            "UPDATE bookings SET status = ? WHERE booking_id = ?",
            (status.value, booking_id),
        )

    def find_overlap(self, date: str, time: str, duration: int) -> dict | None:
        """
        Check if any CONFIRMED booking exists for the same date/time.
        Simplified version: checks for exact start-time matches.
        """
        row = self._conn.execute(
            """SELECT name, date, time FROM bookings 
               WHERE date = ? AND time = ? AND status = ? LIMIT 1""",
            (date, time, BookingStatus.CONFIRMED.value),
        ).fetchone()
        return dict(row) if row else None
    
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