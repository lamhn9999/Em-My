"""
core/validator.py
---------------------
Business rules applied after LLM extraction.
Refactored into an OOP BookingValidator.
"""
from __future__ import annotations
from datetime import datetime
from typing import Optional, Any

from data.models import BookingData, BookingIntent
from core.phone import normalise_phone

class BookingValidator:
    """Validates booking data against business rules."""
    
    def __init__(self, store: Optional[Any] = None, min_conf: float = 0.7):
        self.store = store
        self.min_conf = min_conf
        
        # Phone is optional to provide, but strictly validated if present
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
            reason = f"Vui lòng cung cấp thêm: {', '.join(missing)}." if missing else "Thông tin chưa đủ rõ ràng."
            return False, reason

        # 3. Required fields
        missing = self.get_missing_labels(data)
        if missing:
            return False, f"Thiếu thông tin: {', '.join(missing)}."

        # 4. Phone normalisation (if provided)
        if data.phone:
            normalised, err = normalise_phone(data.phone)
            if err:
                return False, f"SĐT không hợp lệ: {err}"
            data.phone = normalised  # Mutate in-place to ensure clean number downstream

        # 5. Date/time in the future
        try:
            dt_str = f"{data.date} {data.time}"
            dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
            if dt <= datetime.now():
                return False, f"Thời gian ({dt_str}) đã qua."
        except ValueError:
            return False, f"Ngày/giờ không hợp lệ: {data.date} {data.time}."

        # 6. Overlap Check (Requires store implementation)
        if self.store and hasattr(self.store, "find_overlap"):
            conflict = await self.store.find_overlap(data.date, data.time, data.duration_minutes)
            if conflict:
                return False, (
                    f"Trùng lịch với khách khác "
                    f"({conflict.get('date')} {conflict.get('time')})."
                )

        return True, ""

    def get_missing_fields(self, data: BookingData) -> list[str]:
        """Return the raw keys of missing required fields."""
        return [f for f in self.required_fields if not getattr(data, f, None)]

    def get_missing_labels(self, data: BookingData) -> list[str]:
        """Return the human-readable labels of missing required fields."""
        return [self.field_labels[f] for f in self.get_missing_fields(data)]