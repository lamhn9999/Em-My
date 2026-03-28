"""
data/backends/sqlite.py
────────────────────────────────────────────────────────────────────────────
Tables
──────
  profiles            — one row per Zalo user / OA account
  messages            — full chat history, append-only
  bookings            — BookingData records (pending → confirmed / cancelled)
  staff               — staff members with skills and working hours
  stations            — physical resources (chairs, washing beds, etc.)
  service_definitions — named services (multi-step)
  service_steps       — individual steps per service
  resource_assignments— step-to-resource assignments per confirmed booking
  waitlist            — customers waiting for a slot
  blacklist           — blocked customers

Design rules
────────────
  • All public methods are typed; callers never write raw SQL.
  • Transactions are explicit — callers decide when to commit.
  • `last_active` lives only in the DB (not in Profile) — it's a
    convenience column for ops queries, not a domain field.
  • JSON columns (skills, working_hours) stored as TEXT, decoded on read.
"""

from __future__ import annotations

import json
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
    BlacklistEntry,
    ResourceAssignment,
    Role,
    Profile,
    Message,
    Staff,
    Station,
    ServiceDefinition,
    ServiceStep,
    WaitlistEntry,
)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ──────────────────────────────────────────────────────────────────────────────
# DDL — existing tables
# ──────────────────────────────────────────────────────────────────────────────

_CREATE_PROFILES = """
CREATE TABLE IF NOT EXISTS profiles (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL CHECK(role IN ('client', 'member', 'owner', 'oa')),
    favorite_staff TEXT,
    last_active   TEXT NOT NULL
);
"""

_CREATE_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    msg_id          TEXT    PRIMARY KEY,
    sender_id       TEXT    NOT NULL,
    recipient_id    TEXT    NOT NULL,
    sender_role     TEXT    NOT NULL,
    recipient_role  TEXT    NOT NULL,
    text            TEXT    NOT NULL DEFAULT '',
    timestamp       TEXT    NOT NULL,
    synced_from_api INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (sender_id)    REFERENCES profiles(id),
    FOREIGN KEY (recipient_id) REFERENCES profiles(id)
);
"""

_CREATE_BOOKINGS = """
CREATE TABLE IF NOT EXISTS bookings (
    booking_id       TEXT    PRIMARY KEY,
    client_id        TEXT    NOT NULL,
    intent           TEXT    NOT NULL CHECK(intent IN ('booking', 'cancel', 'query', 'unknown')),
    name             TEXT,
    phone            TEXT,
    service          TEXT,
    date             TEXT,
    time             TEXT,
    duration_minutes INTEGER NOT NULL DEFAULT 60,
    notes            TEXT,
    confidence       REAL,
    denial_reason    TEXT,
    preferred_staff  TEXT,
    message_type     INTEGER NOT NULL DEFAULT -1,
    status           TEXT    NOT NULL CHECK(status IN ('pending', 'confirmed', 'cancelled')),
    created_at       TEXT    NOT NULL,
    FOREIGN KEY (client_id) REFERENCES profiles(id)
);
"""

# ──────────────────────────────────────────────────────────────────────────────
# DDL — new tables
# ──────────────────────────────────────────────────────────────────────────────

_CREATE_STAFF = """
CREATE TABLE IF NOT EXISTS staff (
    id            TEXT    PRIMARY KEY,
    name          TEXT    NOT NULL,
    skills        TEXT    NOT NULL DEFAULT '[]',
    working_hours TEXT    NOT NULL DEFAULT '{}',
    active        INTEGER NOT NULL DEFAULT 1
);
"""

_CREATE_STATIONS = """
CREATE TABLE IF NOT EXISTS stations (
    id     TEXT    PRIMARY KEY,
    name   TEXT    NOT NULL,
    type   TEXT    NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
"""

_CREATE_SERVICE_DEFINITIONS = """
CREATE TABLE IF NOT EXISTS service_definitions (
    name TEXT PRIMARY KEY
);
"""

_CREATE_SERVICE_STEPS = """
CREATE TABLE IF NOT EXISTS service_steps (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    service_name         TEXT    NOT NULL,
    step_index           INTEGER NOT NULL,
    step_type            TEXT    NOT NULL,
    duration             INTEGER NOT NULL,
    resource_type        TEXT,
    skill_required       TEXT,
    staff_skill_required TEXT,
    FOREIGN KEY (service_name) REFERENCES service_definitions(name)
);
"""

_CREATE_RESOURCE_ASSIGNMENTS = """
CREATE TABLE IF NOT EXISTS resource_assignments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id    TEXT    NOT NULL,
    step_index    INTEGER NOT NULL,
    step_type     TEXT    NOT NULL,
    resource_id   TEXT    NOT NULL,
    resource_type TEXT    NOT NULL,
    start_time    TEXT    NOT NULL,
    end_time      TEXT    NOT NULL,
    duration      INTEGER NOT NULL,
    FOREIGN KEY (booking_id) REFERENCES bookings(booking_id)
);
"""

_CREATE_WAITLIST = """
CREATE TABLE IF NOT EXISTS waitlist (
    id              TEXT    PRIMARY KEY,
    client_id       TEXT    NOT NULL,
    client_name     TEXT    NOT NULL DEFAULT '',
    service         TEXT    NOT NULL,
    preferred_date  TEXT    NOT NULL,
    preferred_time  TEXT,
    preferred_staff TEXT,
    created_at      TEXT    NOT NULL,
    notified        INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (client_id) REFERENCES profiles(id)
);
"""

_CREATE_BLACKLIST = """
CREATE TABLE IF NOT EXISTS blacklist (
    client_id  TEXT    PRIMARY KEY,
    reason     TEXT    NOT NULL DEFAULT '',
    blocked    INTEGER NOT NULL DEFAULT 1,
    created_at TEXT    NOT NULL,
    FOREIGN KEY (client_id) REFERENCES profiles(id)
);
"""

# ──────────────────────────────────────────────────────────────────────────────
# Indexes
# ──────────────────────────────────────────────────────────────────────────────

_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_messages_sender_ts ON messages(sender_id, timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_bookings_user_status ON bookings(client_id, status);",
    "CREATE INDEX IF NOT EXISTS idx_resource_assignments_booking ON resource_assignments(booking_id);",
    "CREATE INDEX IF NOT EXISTS idx_waitlist_date ON waitlist(preferred_date, notified);",
    "CREATE INDEX IF NOT EXISTS idx_service_steps_service ON service_steps(service_name, step_index);",
]

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
            # Core tables
            await self._conn.execute(_CREATE_PROFILES)
            await self._conn.execute(_CREATE_MESSAGES)
            await self._conn.execute(_CREATE_BOOKINGS)
            # New tables
            await self._conn.execute(_CREATE_STAFF)
            await self._conn.execute(_CREATE_STATIONS)
            await self._conn.execute(_CREATE_SERVICE_DEFINITIONS)
            await self._conn.execute(_CREATE_SERVICE_STEPS)
            await self._conn.execute(_CREATE_RESOURCE_ASSIGNMENTS)
            await self._conn.execute(_CREATE_WAITLIST)
            await self._conn.execute(_CREATE_BLACKLIST)
            # Indexes
            for idx in _INDEXES:
                await self._conn.execute(idx)

        # Idempotent column additions for existing DBs (ALTER TABLE is not
        # inside a transaction because SQLite auto-commits DDL changes)
        for col_sql in [
            "ALTER TABLE profiles ADD COLUMN favorite_staff TEXT",
            "ALTER TABLE bookings ADD COLUMN preferred_staff TEXT",
            "ALTER TABLE bookings ADD COLUMN message_type INTEGER NOT NULL DEFAULT -1",
            "ALTER TABLE service_steps ADD COLUMN staff_skill_required TEXT",
        ]:
            try:
                await self._conn.execute(col_sql)
                await self._conn.commit()
            except Exception:
                pass  # Column already exists

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

    # ── Profiles ──────────────────────────────────────────────────────────────

    async def upsert_profile(self, profile: Profile) -> None:
        await self._conn.execute(
            """INSERT INTO profiles (id, name, role, favorite_staff, last_active)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name          = excluded.name,
                   role          = excluded.role,
                   favorite_staff= excluded.favorite_staff,
                   last_active   = excluded.last_active;""",
            (profile.id, profile.name, profile.role.value,
             profile.favorite_staff, _utcnow()),
        )

    async def get_profile(self, profile_id: str) -> Profile | None:
        async with self._conn.execute(
            "SELECT id, name, role, favorite_staff FROM profiles WHERE id = ?",
            (profile_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_profile(row) if row else None

    async def touch_profile(self, profile_id: str) -> None:
        await self._conn.execute(
            "UPDATE profiles SET last_active = ? WHERE id = ?",
            (_utcnow(), profile_id),
        )

    async def list_profiles(self, role: Optional[Role] = None) -> list[Profile]:
        if role:
            async with self._conn.execute(
                "SELECT id, name, role, favorite_staff FROM profiles WHERE role = ? ORDER BY last_active DESC",
                (role.value,),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._conn.execute(
                "SELECT id, name, role, favorite_staff FROM profiles ORDER BY last_active DESC"
            ) as cursor:
                rows = await cursor.fetchall()
        return [_row_to_profile(r) for r in rows]

    # ── Messages ──────────────────────────────────────────────────────────────

    async def insert_message(self, msg: Message) -> bool:
        """Returns True if inserted, False if msg_id already existed."""
        try:
            await self._conn.execute(
                """INSERT INTO messages
                   (msg_id, sender_id, recipient_id, sender_role, recipient_role,
                    text, timestamp, synced_from_api)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    msg.msg_id, msg.sender_id, msg.recipient_id,
                    msg.sender_role, msg.recipient_role,
                    msg.text, msg.timestamp, int(msg.synced_from_api),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    async def get_messages(self, profile_id: str, last_n: int = 20) -> list[Message]:
        async with self._conn.execute(
            """SELECT * FROM messages
               WHERE sender_id = ? OR recipient_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (profile_id, profile_id, last_n),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_message(r) for r in reversed(rows)]

    async def known_msg_ids(self, profile_id: str) -> set[str]:
        async with self._conn.execute(
            "SELECT msg_id FROM messages WHERE sender_id = ? OR recipient_id = ?",
            (profile_id, profile_id),
        ) as cursor:
            rows = await cursor.fetchall()
        return {r["msg_id"] for r in rows}

    # ── Bookings ──────────────────────────────────────────────────────────────

    async def insert_booking(self, booking: BookingData) -> None:
        async with self.transaction():
            await self._conn.execute(
                """INSERT INTO bookings
                   (booking_id, client_id, intent, name, phone, service,
                    date, time, duration_minutes, notes, confidence,
                    denial_reason, preferred_staff, message_type, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    booking.booking_id, booking.client_id,
                    booking.intent.value, booking.name, booking.phone,
                    booking.service, booking.date, booking.time,
                    booking.duration_minutes, booking.notes,
                    booking.confidence, booking.denial_reason,
                    booking.preferred_staff, booking.message_type,
                    booking.status.value, booking.created_at,
                ),
            )
            if booking.steps_schedule:
                await self._insert_resource_assignments(
                    booking.booking_id, booking.steps_schedule
                )

    async def _insert_resource_assignments(
        self, booking_id: str, steps: list[ResourceAssignment]
    ) -> None:
        await self._conn.execute(
            "DELETE FROM resource_assignments WHERE booking_id = ?", (booking_id,)
        )
        for s in steps:
            await self._conn.execute(
                """INSERT INTO resource_assignments
                   (booking_id, step_index, step_type, resource_id,
                    resource_type, start_time, end_time, duration)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (booking_id, s.step_index, s.step_type, s.resource_id,
                 s.resource_type, s.start_time, s.end_time, s.duration),
            )

    async def _load_resource_assignments(
        self, booking_id: str
    ) -> list[ResourceAssignment]:
        async with self._conn.execute(
            """SELECT step_index, step_type, resource_id, resource_type,
                      start_time, end_time, duration
               FROM resource_assignments
               WHERE booking_id = ?
               ORDER BY step_index""",
            (booking_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            ResourceAssignment(
                step_index=r["step_index"],
                step_type=r["step_type"],
                resource_id=r["resource_id"],
                resource_type=r["resource_type"],
                start_time=r["start_time"],
                end_time=r["end_time"],
                duration=r["duration"],
            )
            for r in rows
        ]

    async def get_active_booking(self, client_id: str) -> BookingData | None:
        async with self._conn.execute(
            """SELECT * FROM bookings
               WHERE client_id = ? AND status = ?
               ORDER BY created_at DESC LIMIT 1""",
            (client_id, BookingStatus.PENDING.value),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        b = _row_to_booking_data(row)
        b.steps_schedule = await self._load_resource_assignments(b.booking_id)
        b.assigned_resources = list({s.resource_id for s in b.steps_schedule})
        return b

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
                b.steps_schedule = await self._load_resource_assignments(b.booking_id)
                b.assigned_resources = list({s.resource_id for s in b.steps_schedule})
                return b
        return None

    async def get_confirmed_bookings_by_date(self, date: str) -> list[dict]:
        async with self._conn.execute(
            """SELECT booking_id, name, phone, service, time, duration_minutes, preferred_staff
               FROM bookings
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
        async with self.transaction():
            await self._conn.execute(
                """UPDATE bookings SET
                   intent = ?, name = ?, phone = ?, service = ?,
                   date = ?, time = ?, duration_minutes = ?,
                   notes = ?, confidence = ?, denial_reason = ?,
                   preferred_staff = ?, message_type = ?, status = ?
                   WHERE booking_id = ?""",
                (
                    booking.intent.value, booking.name, booking.phone,
                    booking.service, booking.date, booking.time,
                    booking.duration_minutes, booking.notes,
                    booking.confidence, booking.denial_reason,
                    booking.preferred_staff, booking.message_type,
                    booking.status.value, booking.booking_id,
                ),
            )
            if booking.steps_schedule:
                await self._insert_resource_assignments(
                    booking.booking_id, booking.steps_schedule
                )

    async def update_booking_status(
        self, booking_id: str, status: BookingStatus
    ) -> None:
        await self._conn.execute(
            "UPDATE bookings SET status = ? WHERE booking_id = ?",
            (status.value, booking_id),
        )

    async def find_overlap(
        self, date: str, time: str, duration: int,
        exclude_booking_id: Optional[str] = None,
    ) -> dict | None:
        """Check if any CONFIRMED booking on the same date overlaps in time."""
        query = """SELECT booking_id, name, date, time, duration_minutes
                   FROM bookings WHERE date = ? AND status = ?"""
        params: list = [date, BookingStatus.CONFIRMED.value]
        if exclude_booking_id:
            query += " AND booking_id != ?"
            params.append(exclude_booking_id)

        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()

        def to_min(t: str) -> int:
            h, m = map(int, t.split(":"))
            return h * 60 + m

        try:
            new_start = to_min(time)
            new_end = new_start + duration
            for row in rows:
                if not row["time"]:
                    continue
                exist_start = to_min(row["time"])
                exist_end = exist_start + row["duration_minutes"]
                if max(new_start, exist_start) < min(new_end, exist_end):
                    return dict(row)
        except Exception:
            pass
        return None

    async def find_resource_overlap(
        self,
        date: str,
        resource_id: str,
        start_time: str,
        end_time: str,
        exclude_booking_id: Optional[str] = None,
    ) -> bool:
        """Check if a specific resource is booked during the given window on date."""
        query = """
            SELECT ra.id FROM resource_assignments ra
            JOIN bookings b ON ra.booking_id = b.booking_id
            WHERE b.date = ? AND b.status = ? AND ra.resource_id = ?
              AND ra.start_time < ? AND ra.end_time > ?
        """
        params: list = [date, BookingStatus.CONFIRMED.value, resource_id, end_time, start_time]
        if exclude_booking_id:
            query += " AND ra.booking_id != ?"
            params.append(exclude_booking_id)

        async with self._conn.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return row is not None

    async def get_upcoming_confirmed_bookings(self, client_id: str) -> list[BookingData]:
        """Return all upcoming confirmed bookings for a client, sorted earliest first."""
        async with self._conn.execute(
            """SELECT * FROM bookings
               WHERE client_id = ? AND status = ?
               ORDER BY date ASC, time ASC""",
            (client_id, BookingStatus.CONFIRMED.value),
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            b = _row_to_booking_data(row)
            if b.is_upcoming():
                b.steps_schedule = await self._load_resource_assignments(b.booking_id)
                b.assigned_resources = list({s.resource_id for s in b.steps_schedule})
                result.append(b)
        return result

    async def get_resource_assignments_for_date(
        self, date: str, exclude_booking_id: Optional[str] = None
    ) -> list[dict]:
        """Returns all resource assignments for confirmed bookings on a date."""
        query = """SELECT ra.booking_id, ra.resource_id, ra.resource_type,
                          ra.start_time, ra.end_time, ra.duration, ra.step_type, b.date
                   FROM resource_assignments ra
                   JOIN bookings b ON ra.booking_id = b.booking_id
                   WHERE b.date = ? AND b.status = ?"""
        params: list = [date, BookingStatus.CONFIRMED.value]
        if exclude_booking_id:
            query += " AND ra.booking_id != ?"
            params.append(exclude_booking_id)
        query += " ORDER BY ra.start_time"
        async with self._conn.execute(query, params) as cursor:
            rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Staff ─────────────────────────────────────────────────────────────────

    async def upsert_staff(self, staff: Staff) -> None:
        await self._conn.execute(
            """INSERT INTO staff (id, name, skills, working_hours, active)
               VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(id) DO UPDATE SET
                   name          = excluded.name,
                   skills        = excluded.skills,
                   working_hours = excluded.working_hours,
                   active        = 1;""",
            (staff.id, staff.name,
             json.dumps(staff.skills, ensure_ascii=False),
             json.dumps(staff.working_hours, ensure_ascii=False)),
        )

    async def get_staff(self, staff_id: str) -> Staff | None:
        async with self._conn.execute(
            "SELECT id, name, skills, working_hours FROM staff WHERE id = ? AND active = 1",
            (staff_id,),
        ) as cursor:
            row = await cursor.fetchone()
        return _row_to_staff(row) if row else None

    async def list_staff(self) -> list[Staff]:
        async with self._conn.execute(
            "SELECT id, name, skills, working_hours FROM staff WHERE active = 1 ORDER BY name"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_staff(r) for r in rows]

    async def get_staff_by_skill(self, skill: str) -> list[Staff]:
        async with self._conn.execute(
            "SELECT id, name, skills, working_hours FROM staff WHERE active = 1"
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for r in rows:
            s = _row_to_staff(r)
            if skill in s.skills:
                result.append(s)
        return result

    # ── Stations ──────────────────────────────────────────────────────────────

    async def upsert_station(self, station: Station) -> None:
        await self._conn.execute(
            """INSERT INTO stations (id, name, type, active)
               VALUES (?, ?, ?, 1)
               ON CONFLICT(id) DO UPDATE SET
                   name   = excluded.name,
                   type   = excluded.type,
                   active = 1;""",
            (station.id, station.name, station.type),
        )

    async def list_stations(self, station_type: Optional[str] = None) -> list[Station]:
        if station_type:
            async with self._conn.execute(
                "SELECT id, name, type FROM stations WHERE type = ? AND active = 1",
                (station_type,),
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self._conn.execute(
                "SELECT id, name, type FROM stations WHERE active = 1"
            ) as cursor:
                rows = await cursor.fetchall()
        return [Station(id=r["id"], name=r["name"], type=r["type"]) for r in rows]

    # ── Service definitions ───────────────────────────────────────────────────

    async def upsert_service(self, svc: ServiceDefinition) -> None:
        async with self.transaction():
            await self._conn.execute(
                "INSERT INTO service_definitions (name) VALUES (?) ON CONFLICT(name) DO NOTHING",
                (svc.name,),
            )
            await self._conn.execute(
                "DELETE FROM service_steps WHERE service_name = ?", (svc.name,)
            )
            for step in svc.steps:
                await self._conn.execute(
                    """INSERT INTO service_steps
                       (service_name, step_index, step_type, duration,
                        resource_type, skill_required, staff_skill_required)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (svc.name, step.step_index, step.step_type,
                     step.duration, step.resource_type, step.skill_required,
                     step.staff_skill_required),
                )

    async def get_service(self, name: str) -> ServiceDefinition | None:
        async with self._conn.execute(
            "SELECT name FROM service_definitions WHERE name = ?", (name,)
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return await self._load_service(name)

    async def list_services(self) -> list[ServiceDefinition]:
        async with self._conn.execute(
            "SELECT name FROM service_definitions ORDER BY name"
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for r in rows:
            svc = await self._load_service(r["name"])
            if svc:
                result.append(svc)
        return result

    async def _load_service(self, name: str) -> ServiceDefinition | None:
        async with self._conn.execute(
            """SELECT step_index, step_type, duration, resource_type,
                      skill_required, staff_skill_required
               FROM service_steps WHERE service_name = ? ORDER BY step_index""",
            (name,),
        ) as cursor:
            rows = await cursor.fetchall()
        steps = [
            ServiceStep(
                step_index=r["step_index"],
                step_type=r["step_type"],
                duration=r["duration"],
                resource_type=r["resource_type"],
                skill_required=r["skill_required"],
                staff_skill_required=r["staff_skill_required"],
            )
            for r in rows
        ]
        return ServiceDefinition(name=name, steps=steps)

    # ── Waitlist ──────────────────────────────────────────────────────────────

    async def add_to_waitlist(self, entry: WaitlistEntry) -> None:
        await self._conn.execute(
            """INSERT INTO waitlist
               (id, client_id, client_name, service, preferred_date,
                preferred_time, preferred_staff, created_at, notified)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO NOTHING""",
            (entry.id, entry.client_id, entry.client_name, entry.service,
             entry.preferred_date, entry.preferred_time, entry.preferred_staff,
             entry.created_at, int(entry.notified)),
        )

    async def get_waitlist_for_date(self, date: str) -> list[WaitlistEntry]:
        async with self._conn.execute(
            """SELECT * FROM waitlist
               WHERE preferred_date = ? AND notified = 0
               ORDER BY created_at ASC""",
            (date,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_waitlist(r) for r in rows]

    async def get_waitlist_for_client(self, client_id: str) -> list[WaitlistEntry]:
        async with self._conn.execute(
            "SELECT * FROM waitlist WHERE client_id = ? ORDER BY created_at DESC",
            (client_id,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [_row_to_waitlist(r) for r in rows]

    async def mark_waitlist_notified(self, entry_id: str) -> None:
        await self._conn.execute(
            "UPDATE waitlist SET notified = 1 WHERE id = ?", (entry_id,)
        )

    async def remove_from_waitlist(self, entry_id: str) -> None:
        await self._conn.execute(
            "DELETE FROM waitlist WHERE id = ?", (entry_id,)
        )

    # ── Blacklist ─────────────────────────────────────────────────────────────

    async def upsert_blacklist(self, entry: BlacklistEntry) -> None:
        await self._conn.execute(
            """INSERT INTO blacklist (client_id, reason, blocked, created_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(client_id) DO UPDATE SET
                   reason  = excluded.reason,
                   blocked = excluded.blocked;""",
            (entry.client_id, entry.reason,
             int(entry.blocked), entry.created_at),
        )

    async def get_blacklist_entry(self, client_id: str) -> BlacklistEntry | None:
        async with self._conn.execute(
            "SELECT client_id, reason, blocked, created_at FROM blacklist WHERE client_id = ?",
            (client_id,),
        ) as cursor:
            row = await cursor.fetchone()
        if not row:
            return None
        return BlacklistEntry(
            client_id=row["client_id"],
            reason=row["reason"],
            blocked=bool(row["blocked"]),
            created_at=row["created_at"],
        )

    async def is_blacklisted(self, client_id: str) -> bool:
        entry = await self.get_blacklist_entry(client_id)
        return entry is not None and entry.blocked


# ──────────────────────────────────────────────────────────────────────────────
# Row converters
# ──────────────────────────────────────────────────────────────────────────────

def _row_to_profile(row: sqlite3.Row) -> Profile:
    return Profile(
        id=row["id"],
        name=row["name"],
        role=Role(row["role"]),
        favorite_staff=row["favorite_staff"] if "favorite_staff" in row.keys() else None,
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
        synced_from_api=bool(row["synced_from_api"]),
    )


def _row_to_booking_data(row: sqlite3.Row) -> BookingData:
    keys = row.keys()
    return BookingData(
        booking_id=row["booking_id"],
        client_id=row["client_id"],
        intent=BookingIntent(row["intent"]),
        name=row["name"],
        phone=row["phone"],
        service=row["service"],
        date=row["date"],
        time=row["time"],
        duration_minutes=row["duration_minutes"],
        notes=row["notes"],
        confidence=row["confidence"] or 0.0,
        denial_reason=row["denial_reason"],
        preferred_staff=row["preferred_staff"] if "preferred_staff" in keys else None,
        message_type=row["message_type"] if "message_type" in keys else -1,
        status=BookingStatus(row["status"]),
        created_at=row["created_at"],
    )


def _row_to_staff(row: sqlite3.Row) -> Staff:
    return Staff(
        id=row["id"],
        name=row["name"],
        skills=json.loads(row["skills"] or "[]"),
        working_hours=json.loads(row["working_hours"] or "{}"),
    )


def _row_to_waitlist(row: sqlite3.Row) -> WaitlistEntry:
    return WaitlistEntry(
        id=row["id"],
        client_id=row["client_id"],
        client_name=row["client_name"],
        service=row["service"],
        preferred_date=row["preferred_date"],
        preferred_time=row["preferred_time"],
        preferred_staff=row["preferred_staff"],
        created_at=row["created_at"],
        notified=bool(row["notified"]),
    )
