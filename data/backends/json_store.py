"""
json_store.py
-------------
Persist bookings to bookings.json in the project root.
Includes overlap detection before inserting a new booking.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path

STORE_PATH = Path(__file__).parent.parent / "store/bookings.json"
STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
def _to_dt(date: str, time: str) -> datetime:
    return datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")


class JsonStore:
    def __init__(self, path: Path = STORE_PATH):
        self.path = path
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load(self) -> list[dict]:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, records: list[dict]) -> None:
        self.path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── Overlap check ─────────────────────────────────────────────────────────

    def find_overlap(self, date: str, time: str, duration_minutes: int) -> dict | None:
        """
        Return the first existing booking that overlaps [start, end).
        Returns None if the slot is free.
        """
        new_start = _to_dt(date, time)
        new_end   = new_start + timedelta(minutes=duration_minutes)

        for rec in self._load():
            ex_start = _to_dt(rec["date"], rec["time"])
            ex_end   = ex_start + timedelta(minutes=rec["duration_minutes"])

            # Overlap when: new_start < ex_end AND new_end > ex_start
            if new_start < ex_end and new_end > ex_start:
                return rec

        return None

    # ── Write ─────────────────────────────────────────────────────────────────

    def add_booking(self, data) -> dict:
        """
        Append a confirmed booking.
        Caller is responsible for running find_overlap() first.
        Returns the saved record.
        """
        records = self._load()

        record = {
            "id":               str(uuid.uuid4())[:8],
            "created_at":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "name":             data.name,
            "phone":            data.phone,
            "service":          data.service,
            "date":             data.date,
            "time":             data.time,
            "duration_minutes": data.duration_minutes,
            "notes":            data.notes,
        }

        records.append(record)
        self._save(records)
        return record

    def all_bookings(self) -> list[dict]:
        return self._load()

    def clear(self) -> None:
        """Delete all bookings (used by test teardown)."""
        self._save([])