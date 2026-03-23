"""
booking_agent.py  —  Zalo Booking Agent MVP
============================================
Flow:
  User message → LLM extraction → (feedback loop for missing fields) → validate → save

Feedback loop:
  If required fields are missing, ask the user for each one interactively.
  Natural-language date/time answers are re-parsed by the LLM.
  The user can type 'skip' to abort the current booking at any prompt.
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import datetime
from pathlib import Path

from data.backends.json_store import JsonStore
from data.models import BookingData, BookingIntent
from validator import validate

DIV  = "─" * 58
DIV2 = "╌" * 58

# ── Field metadata ─────────────────────────────────────────────────────────────
# field → (display label, hint, needs_llm_reparse)
FIELD_META: dict[str, tuple[str, str, bool]] = {
    "name":    ("Tên khách hàng",  "vd: Nguyễn Lan",           False),
    "service": ("Dịch vụ",         "vd: massage, chăm sóc da", False),
    "date":    ("Ngày",            "vd: thứ 6 tuần này, 28/3", True),
    "time":    ("Giờ",             "vd: 3h chiều, 15:00",      True),
    "phone":   ("Số điện thoại",   "vd: 0912345678 (tuỳ chọn)", False),
}

REQUIRED = ["name", "service", "date", "time"]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_dt(date: str, time: str) -> str:
    dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
    return dt.strftime("%A, %d/%m/%Y lúc %H:%M").capitalize()


def _print_banner():
    print(f"\n{'═'*58}")
    print("  🗓️   ZALO BOOKING AGENT  —  MVP")
    print("  LLM : Groq / Llama-3.3-70B (free)  |  Store: JSON")
    print(f"{'═'*58}")
    print("  Gõ tin nhắn đặt lịch bằng tiếng Việt.")
    print("  Gõ  'exit'  để thoát. Gõ  'skip'  để huỷ lịch đang đặt.\n")


def _print_extracted(data: BookingData):
    print("\n🤖  Kết quả trích xuất:")
    for k, v in {
        "intent":     data.intent.value,
        "name":       data.name,
        "phone":      data.phone,
        "service":    data.service,
        "date":       data.date,
        "time":       data.time,
        "duration":   f"{data.duration_minutes} phút",
        "notes":      data.notes,
        "confidence": f"{data.confidence:.0%}",
    }.items():
        mark = " ⚠️" if v is None and k in REQUIRED else ""
        print(f"   {k:<12}: {v}{mark}")


def _missing_required(data: BookingData) -> list[str]:
    return [f for f in REQUIRED if not getattr(data, f, None)]


# ── Feedback loop ─────────────────────────────────────────────────────────────

class _Skip(Exception):
    """Raised when the user aborts mid-booking."""


def _ask_field(field: str) -> str:
    """Prompt for a single field. Raises _Skip if user wants to abort."""
    label, hint, _ = FIELD_META[field]
    try:
        answer = input(f"   ✏️  {label} ({hint}): ").strip()
    except (KeyboardInterrupt, EOFError):
        raise _Skip

    if answer.lower() in ("skip", "exit", "q", "thoát", "huỷ", "huy"):
        raise _Skip
    return answer


def _merge_field(data: BookingData, field: str, raw_answer: str, chain) -> BookingData:
    """
    Merge one user answer into BookingData.
    - date/time: re-parse with LLM (handles "thứ 6 tuần này" etc.)
    - name/service/phone: set directly
    """
    _, _, needs_llm = FIELD_META[field]

    if needs_llm:
        synthetic = (
            f"Tên: {data.name or 'N/A'}, "
            f"dịch vụ: {data.service or 'N/A'}, "
            f"{field}: {raw_answer}"
        )
        try:
            parsed = chain.extract(synthetic)
            value  = getattr(parsed, field)
        except Exception:
            value = None

        if not value:
            print(f"   ⚠️  Không hiểu '{raw_answer}'. Thử lại (vd: 28/03/2026 hoặc 15:00).")
            return data  # leave as None → loop will retry
    else:
        value = raw_answer or None

    return dataclasses.replace(data, **{field: value})


def collect_missing_fields(data: BookingData, chain) -> BookingData:
    """
    Ask for each missing required field interactively (up to MAX_RETRIES each).
    Returns updated BookingData. Raises _Skip if user aborts or retries exhausted.
    """
    MAX_RETRIES = 3

    for field in REQUIRED:
        retries = 0
        while not getattr(data, field, None):
            if retries >= MAX_RETRIES:
                print(f"   ❌  Đã thử {MAX_RETRIES} lần. Huỷ đặt lịch.")
                raise _Skip
            raw  = _ask_field(field)
            data = _merge_field(data, field, raw, chain)
            retries += 1

    return data


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    # Lazy import so test files can import this module without langchain installed
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    from services.llm_service import IntentExtractionChain

    _print_banner()

    print("🔧  Initialising…")
    try:
        chain = IntentExtractionChain()
        store = JsonStore()
        
        # CLEAR EXISTING DATA INSIDE BOOKINGS.JSON !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        JsonStore.clear(self=store)
    except EnvironmentError as exc:
        print(f"\n❌  Setup error:\n{exc}")
        sys.exit(1)

    print(f"✅  Ready. Bookings → {store.path}\n")

    while True:
        try:
            user_msg = input("📨  Tin nhắn: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋  Tạm biệt!")
            break

        if not user_msg:
            continue
        if user_msg.lower() in ("exit", "quit", "thoát", "q"):
            print("👋  Tạm biệt!")
            break

        print(f"\n{DIV}")
        print("⏳  Đang phân tích…")

        # Step 1 — LLM extraction
        try:
            data = chain.extract(user_msg)
        except Exception as exc:
            print(f"❌  DENIED — Lỗi LLM: {exc}\n")
            continue

        _print_extracted(data)

        # Step 2 — Non-booking intent → deny immediately
        if data.intent != BookingIntent.BOOKING:
            reason = data.denial_reason or "Không phải yêu cầu đặt lịch."
            print(f"\n❌  DENIED — {reason}\n")
            continue

        # Step 3 — Feedback loop for missing fields
        missing = _missing_required(data)
        if missing:
            labels = [FIELD_META[f][0] for f in missing]
            print(f"\n{DIV2}")
            print(f"💬  Thiếu: {', '.join(labels)}.")
            print(f"    Vui lòng bổ sung (gõ 'skip' để huỷ):")
            print(DIV2)
            try:
                data = collect_missing_fields(data, chain)
            except _Skip:
                print("↩️   Đã huỷ. Vui lòng bắt đầu lại.\n")
                continue

            print(f"\n✔️   Đã bổ sung: {data.name} | {data.service} | {data.date} {data.time}")

        # Step 4 — Validate (includes overlap check)
        ok, reason = validate(data, store=store)
        if not ok:
            print(f"\n❌  DENIED — {reason}\n")
            continue

        # Step 5 — Write to JSON
        record = store.add_booking(data)

        print(f"\n✅  BOOKED  (id: {record['id']})")
        print(f"   👤  {data.name}  ({data.phone or 'SĐT chưa có'})")
        print(f"   💆  {data.service}")
        print(f"   🕐  {_fmt_dt(data.date, data.time)}  ({data.duration_minutes} phút)")
        if data.notes:
            print(f"   📝  {data.notes}")
        print(f"   💾  Saved → {store.path}\n")


if __name__ == "__main__":
    main()