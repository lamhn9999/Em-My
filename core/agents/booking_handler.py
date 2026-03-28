"""
core/agents/booking_handler.py
──────────────────────────────────────────────────────────────────────────────
Booking Handler Agent — handles Type 3 (BOOKING) requests.

Responsibilities:
  • Receive extracted BookingData from the LLM chain
  • Resolve service name → ServiceDefinition
  • Resolve preferred staff name → staff id
  • Build a ScheduleRequest and run the scheduler
  • If the preferred slot is free → confirm and return ScheduleResult
  • If not → hand off to NegotiationAgent for alternatives
  • Persist the confirmed booking with step assignments
"""
from __future__ import annotations

import logging
import re
import unicodedata

from config.business import SERVICES
from core.scheduler import (
    ScheduleResult,
    build_schedule_request,
    schedule as run_schedule,
)
from data.backends.sqlite import Database
from data.models import BookingData, BookingStatus, ServiceDefinition, ServiceStep

log = logging.getLogger(__name__)


class BookingHandler:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def handle(
        self, booking: BookingData
    ) -> tuple[ScheduleResult | None, str]:
        """
        Attempt to schedule the booking at the preferred date/time.

        Returns:
          (ScheduleResult, reply_text)  — ScheduleResult is None on failure.
        """
        if not booking.date or not booking.time or not booking.service:
            return None, "Mình cần biết ngày, giờ và dịch vụ để đặt lịch cho bạn ạ."

        svc = _resolve_multi_service(booking.service)
        if svc is None:
            svc_list = ", ".join(s.name for s in SERVICES)
            return None, (
                f"Salon chưa có dịch vụ **{booking.service}**.\n"
                f"Các dịch vụ hiện có: {svc_list}.\n"
                "Bạn muốn chọn dịch vụ nào ạ?"
            )

        # Resolve preferred staff
        preferred_staff_id = await _resolve_staff_id(booking.preferred_staff, self._db)

        # Load current resource assignments for that day
        existing = await self._db.get_resource_assignments_for_date(booking.date)
        all_staff    = await self._db.list_staff()
        all_stations = await self._db.list_stations()

        req = build_schedule_request(
            date=booking.date,
            preferred_time=booking.time,
            service=svc,
            all_staff=all_staff,
            all_stations=all_stations,
            existing_assignments=existing,
            preferred_staff_id=preferred_staff_id,
        )

        result = await run_schedule(req)
        if result.success:
            # Update booking with scheduler output
            booking.steps_schedule     = result.steps
            booking.assigned_resources = list({s.resource_id for s in result.steps if s.resource_type != "wait"})
            booking.duration_minutes   = result.total_duration
            booking.time               = result.start_time  # may differ if resource shifted
            return result, ""

        return None, result.failure_reason


def _norm(text: str) -> str:
    """
    Normalize Vietnamese text for fuzzy matching:
    strip diacritics (NFKD + remove combining marks) and lowercase.
    Handles LLMs that drop diacritics ("cat toc" → same as "cắt tóc").
    """
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.lower())
        if not unicodedata.combining(c)
    )


def _resolve_service(name: str) -> ServiceDefinition | None:
    """
    Find ServiceDefinition by name with a 4-tier priority:
      1. Exact match (Unicode-aware, lowercased)
      2. Exact match after diacritic removal  — handles LLM returning "cat toc"
      3. Prefix match after normalization      — "goi" → "goi dau massage" not "cat + goi"
      4. Substring match after normalization   — "nhuom" in "nhuom toc"
    """
    lower = name.lower().strip()
    norm  = _norm(name)

    # 1. Exact (with diacritics)
    for s in SERVICES:
        if s.name.lower() == lower:
            return s

    # 2. Exact (without diacritics)
    for s in SERVICES:
        if _norm(s.name) == norm:
            return s

    # 3. Prefix (normalized) — prefers "gội đầu massage" over "cắt + gội" for "gội"
    for s in SERVICES:
        snorm = _norm(s.name)
        if snorm.startswith(norm) or norm.startswith(snorm):
            return s

    # 4. Substring (normalized)
    for s in SERVICES:
        snorm = _norm(s.name)
        if norm in snorm or snorm in norm:
            return s

    return None


def _merge_services(resolved: list[ServiceDefinition]) -> ServiceDefinition:
    """Concatenate step lists from multiple services into one combined definition."""
    merged_steps: list[ServiceStep] = []
    idx = 0
    for svc in resolved:
        for step in svc.steps:
            merged_steps.append(ServiceStep(
                step_index=idx,
                step_type=step.step_type,
                duration=step.duration,
                resource_type=step.resource_type,
                skill_required=step.skill_required,
                staff_skill_required=step.staff_skill_required,
            ))
            idx += 1
    combined_name = " + ".join(s.name for s in resolved)
    log.info("Multi-service merged: %r (%d steps)", combined_name, len(merged_steps))
    return ServiceDefinition(name=combined_name, steps=merged_steps)


def _resolve_multi_service(name: str) -> ServiceDefinition | None:
    """
    Resolve a service string that may reference multiple services.

    Strategy (in order):
      1. Exact single-service match only (no fuzzy) — so "cắt + gội" is found
         directly without being split on "+".
      2. Explicit separator split: "," / " và " / " & " / " + "
         This runs BEFORE fuzzy matching so "cắt tóc và gội đầu" is treated as
         two services rather than fuzzy-matched to "cắt tóc" via prefix.
      3. Fuzzy single-service match (prefix / substring) — for abbreviated names.
      4. Sliding word-window over space-separated tokens (handles "Cắt gội nhuộm").
    """
    lower = name.lower().strip()
    norm  = _norm(name)

    # 1. Exact single-service match (handles "cắt + gội" without splitting on "+")
    for s in SERVICES:
        if s.name.lower() == lower:
            return s
    for s in SERVICES:
        if _norm(s.name) == norm:
            return s

    # 2. Explicit separator split — before any fuzzy matching so multi-service
    #    inputs like "cắt tóc và gội đầu" aren't gobbled up by prefix matching.
    sep_parts = re.split(r",\s*|\bvà\b|\s*&\s*|\s*\+\s*", name.strip(), flags=re.IGNORECASE)
    sep_parts = [p.strip() for p in sep_parts if p.strip()]

    if len(sep_parts) > 1:
        resolved = [_resolve_service(p) for p in sep_parts]
        resolved = [r for r in resolved if r is not None]
        if len(resolved) > 1:
            return _merge_services(resolved)
        if len(resolved) == 1:
            return resolved[0]

    # 3. Fuzzy single-service match (prefix / substring) — abbreviated names
    single = _resolve_service(name)
    if single is not None:
        return single

    # 4. Sliding word-window for space-separated inputs ("Cắt gội nhuộm")
    words = name.strip().split()
    if len(words) > 1:
        resolved = []
        i = 0
        while i < len(words):
            matched = None
            # Try longest window first (greedy)
            for j in range(len(words), i, -1):
                candidate = " ".join(words[i:j])
                svc = _resolve_service(candidate)
                if svc is not None:
                    matched = svc
                    i = j
                    break
            if matched is None:
                i += 1  # skip unrecognized token
            else:
                if not resolved or resolved[-1].name != matched.name:
                    resolved.append(matched)

        if len(resolved) > 1:
            return _merge_services(resolved)
        if len(resolved) == 1:
            return resolved[0]

    return None


async def _resolve_staff_id(name_or_id: str | None, db: Database) -> str | None:
    if not name_or_id:
        return None
    all_staff = await db.list_staff()
    lower = name_or_id.lower().strip()
    for s in all_staff:
        if s.id.lower() == lower or s.name.lower() == lower:
            return s.id
    return None
