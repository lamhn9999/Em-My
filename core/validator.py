"""
validator.py
------------
Business rules applied after LLM extraction.
Returns (is_valid: bool, reason: str).
"""
from __future__ import annotations

from datetime import datetime

from data.models import BookingData, BookingIntent
from phone import normalise_phone

REQUIRED     = ["name", "service", "date", "time"]
MIN_CONF     = 0.7
FIELD_LABELS = {
    "name":    "tên khách",
    "service": "dịch vụ",
    "date":    "ngày",
    "time":    "giờ",
}


def validate(data: BookingData, store=None) -> tuple[bool, str]:
    """
    Apply booking rules in order:
      1. Intent must be BOOKING
      2. Confidence threshold
      3. Required fields present
      4. Phone normalisation (if provided)
      5. Date/time in the future
      6. No schedule overlap (if store provided)

    Returns:
        (True, "")            — ready to book
        (False, reason_str)   — denied
    """
    # 1. Intent
    if data.intent != BookingIntent.BOOKING:
        extra = f" {data.denial_reason}" if data.denial_reason else ""
        return False, f"Không phải yêu cầu đặt lịch.{extra}"

    # 2. Confidence
    if data.confidence < MIN_CONF:
        missing = _missing(data)
        return False, (
            f"Vui lòng cung cấp thêm: {', '.join(missing)}."
            if missing else "Thông tin chưa đủ rõ ràng."
        )

    # 3. Required fields
    missing = _missing(data)
    if missing:
        return False, f"Thiếu thông tin: {', '.join(missing)}."

    # 4. Phone (optional field, but validated when present)
    if data.phone:
        normalised, err = normalise_phone(data.phone)
        if err:
            return False, f"SĐT không hợp lệ: {err}"
        # Mutate in-place so downstream code gets the clean number
        data.phone = normalised

    # 5. Date/time in the future
    try:
        dt = datetime.strptime(f"{data.date} {data.time}", "%Y-%m-%d %H:%M")
    except ValueError:
        return False, f"Ngày/giờ không hợp lệ: {data.date} {data.time}."

    if dt <= datetime.now():
        return False, f"Thời gian ({data.date} {data.time}) đã qua."

    # 6. Overlap
    if store is not None:
        conflict = store.find_overlap(data.date, data.time, data.duration_minutes)
        if conflict:
            return False, (
                f"Trùng lịch với '{conflict['name']}' "
                f"({conflict['date']} {conflict['time']}, "
                f"{conflict['duration_minutes']} phút)."
            )

    return True, ""


def _missing(data: BookingData) -> list[str]:
    return [FIELD_LABELS[f] for f in REQUIRED if not getattr(data, f, None)]
