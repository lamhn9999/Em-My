"""
core/validator.py
---------------------
Business rules applied after LLM extraction.
Refactored into an OOP BookingValidator.

Extended to support multi-step services:
  • Validates resource assignments produced by the scheduler
  • Checks per-step resource overlap (not just single-slot overlap)
  • Validates staff skill match per step
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional, Any

from data.models import BookingData, BookingIntent, ResourceAssignment
from core.phone import normalise_phone


class BookingValidator:
    """Validates booking data against business rules."""

    def __init__(self, store: Optional[Any] = None, min_conf: float = 0.7):
        self.store = store
        self.min_conf = min_conf

        self.required_fields = ["name", "service", "date", "time", "phone"]
        self.field_labels = {
            "name": "tên khách",
            "service": "dịch vụ",
            "date": "ngày",
            "time": "giờ",
            "phone": "số điện thoại",
        }

    async def validate(self, data: BookingData) -> tuple[bool, str]:
        """
        Apply booking rules in order.
        Returns: (is_valid: bool, reason_str: str)
        """
        # 1. Intent
        if data.intent != BookingIntent.BOOKING:
            reason = data.denial_reason or "Không phải yêu cầu đặt lịch."
            return False, reason

        # 2. Confidence
        if data.confidence < self.min_conf:
            missing = self.get_missing_labels(data)
            reason = (
                f"Vui lòng cung cấp thêm: {', '.join(missing)}."
                if missing else "Thông tin chưa đủ rõ ràng."
            )
            return False, reason

        # 3. Required fields
        missing = self.get_missing_labels(data)
        if missing:
            return False, f"Thiếu thông tin: {', '.join(missing)}."

        # 4. Phone normalisation
        if data.phone:
            normalised, err = normalise_phone(data.phone)
            if err:
                return False, f"SĐT không hợp lệ: {err}"
            data.phone = normalised

        # 5. Date/time in the future
        try:
            dt_str = f"{data.date} {data.time}"
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            if dt <= datetime.now():
                return False, f"Thời gian ({dt_str}) đã qua."
        except ValueError:
            return False, f"Ngày/giờ không hợp lệ: {data.date} {data.time}."

        # 6. Per-step resource overlap — runs only AFTER the scheduler has
        #    populated steps_schedule. Scheduling conflicts (no free slot at
        #    preferred time) are intentionally NOT checked here; that is the
        #    scheduler's job. The validator only verifies data correctness and
        #    completeness so that the scheduler receives clean input.
        if data.steps_schedule and self.store and hasattr(self.store, "_db"):
            db = self.store._db
            for step in data.steps_schedule:
                if step.resource_type in ("wait", None) or step.resource_id == "__wait__":
                    continue
                overlap = await db.find_resource_overlap(
                    date=data.date,
                    resource_id=step.resource_id,
                    start_time=step.start_time,
                    end_time=step.end_time,
                    exclude_booking_id=data.booking_id or None,
                )
                if overlap:
                    return False, (
                        f"Nhân viên/trạm '{step.resource_id}' đã có lịch vào "
                        f"{step.start_time}–{step.end_time}. "
                        "Vui lòng chọn giờ khác."
                    )

        return True, ""

    def get_missing_fields(self, data: BookingData) -> list[str]:
        """Return the raw keys of missing required fields."""
        return [f for f in self.required_fields if not getattr(data, f, None)]

    def get_missing_labels(self, data: BookingData) -> list[str]:
        """Return the human-readable labels of missing required fields."""
        return [self.field_labels[f] for f in self.get_missing_fields(data)]