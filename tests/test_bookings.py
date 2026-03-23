"""
test_bookings.py
----------------
No LLM, no network — pure logic.

GROUP 1: 10 customers successfully booked (distinct slots)
GROUP 2: 10 denied for overlapping an existing booking
GROUP 3: Feedback-loop unit tests (monkeypatched I/O)
GROUP 4: Phone number normalisation & validation
"""
from __future__ import annotations

import dataclasses
import json
import sys
import unittest.mock as mock
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from data.models import BookingData, BookingIntent
from data.backends.json_store import JsonStore
from core.phone import normalise_phone
from core.validator import validate
import core.booking_agent as ba

# ── ANSI ──────────────────────────────────────────────────────────────────────
G, R, Y, B, X = "\033[92m", "\033[91m", "\033[93m", "\033[1m", "\033[0m"
DIV = "─" * 62


# ── Slot helper ───────────────────────────────────────────────────────────────
_BASE = (datetime.now() + timedelta(days=7)).replace(
    hour=9, minute=0, second=0, microsecond=0
)

def slot(offset_h: float) -> tuple[str, str]:
    dt = _BASE + timedelta(hours=offset_h)
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")

def booking(name, phone, service, date, time, duration=60) -> BookingData:
    return BookingData(
        intent=BookingIntent.BOOKING,
        name=name, phone=phone, service=service,
        date=date, time=time, duration_minutes=duration,
        confidence=0.95,
    )


# ── Test data ─────────────────────────────────────────────────────────────────
SUCCESSFUL = [
    booking("Nguyễn Lan",  "0901111111", "Massage thư giãn",   *slot(0.0)),
    booking("Trần Minh",   "0902222222", "Chăm sóc da mặt",   *slot(1.5)),
    booking("Lê Hương",    "0903333333", "Tẩy tế bào chết",   *slot(3.0)),
    booking("Phạm Quân",   "0904444444", "Gội đầu dưỡng tóc", *slot(4.5)),
    booking("Hoàng Yến",   "0905555555", "Wax lông",           *slot(6.0)),
    booking("Vũ Thành",    "0906666666", "Massage đá nóng",   *slot(7.5)),
    booking("Đặng Cúc",    "0907777777", "Làm móng tay",      *slot(9.0)),
    booking("Bùi Dũng",    "0908888888", "Tắm trắng",          *slot(10.5)),
    booking("Ngô Thảo",    "0909999999", "Xông hơi",           *slot(12.0)),
    booking("Đinh Khải",   "0910000000", "Chăm sóc lông mày", *slot(13.5)),
]

OVERLAPPING = [
    booking("Clash A", "0911000001", "Trùng slot 1",  *slot(0.5)),
    booking("Clash B", "0911000002", "Trùng slot 2",  *slot(2.0)),
    booking("Clash C", "0911000003", "Trùng slot 3",  *slot(3.5)),
    booking("Clash D", "0911000004", "Trùng slot 4",  *slot(5.0)),
    booking("Clash E", "0911000005", "Trùng slot 5",  *slot(6.5)),
    booking("Clash F", "0911000006", "Trùng slot 6",  *slot(8.0)),
    booking("Clash G", "0911000007", "Trùng slot 7",  *slot(9.5)),
    booking("Clash H", "0911000008", "Trùng slot 8",  *slot(11.0)),
    booking("Clash I", "0911000009", "Trùng slot 9",  *slot(12.5)),
    booking("Clash J", "0911000010", "Trùng slot 10", *slot(14.0)),
]


# ── Case runner ───────────────────────────────────────────────────────────────
def run_case(idx: int, data: BookingData, store: JsonStore, expect_ok: bool) -> bool:
    ok, reason = validate(data, store=store)
    if ok:
        rec    = store.add_booking(data)
        status = f"{G}✅ BOOKED{X}  (id: {rec['id']})"
        detail = f"{data.service}  {data.date} {data.time}"
        passed = expect_ok
    else:
        status = f"{R}❌ DENIED{X}"
        detail = reason
        passed = not expect_ok

    badge = f"{G}PASS{X}" if passed else f"{R}FAIL{X}"
    print(f"  [{idx:02d}] {badge} │ {status}")
    print(f"       {Y}{data.name:<18}{X} {detail}")
    return passed


# ── Group 1 & 2 ───────────────────────────────────────────────────────────────
def run_booking_tests(store: JsonStore) -> tuple[int, int]:
    passes = fails = 0

    print(f"{B}{DIV}{X}")
    print(f"{B}  GROUP 1 — 10 bookings  →  expect all BOOKED{X}")
    print(f"{B}{DIV}{X}")
    for i, d in enumerate(SUCCESSFUL, 1):
        ok = run_case(i, d, store, expect_ok=True)
        passes += ok; fails += (not ok)

    print(f"\n{B}{DIV}{X}")
    print(f"{B}  GROUP 2 — 10 bookings  →  expect all DENIED (overlap){X}")
    print(f"{B}{DIV}{X}")
    for i, d in enumerate(OVERLAPPING, 1):
        ok = run_case(i, d, store, expect_ok=False)
        passes += ok; fails += (not ok)

    return passes, fails


# ── Group 3: Feedback-loop unit tests ────────────────────────────────────────
def run_feedback_tests() -> tuple[int, int]:
    passes = fails = 0

    print(f"\n{B}{DIV}{X}")
    print(f"{B}  GROUP 3 — Feedback loop unit tests (no LLM){X}")
    print(f"{B}{DIV}{X}")

    def case(label: str, ok: bool, detail: str = ""):
        nonlocal passes, fails
        badge = f"{G}PASS{X}" if ok else f"{R}FAIL{X}"
        icon  = f"{G}✅{X}" if ok else f"{R}❌{X}"
        print(f"  {badge} │ {icon} {label}")
        if detail:
            print(f"       {Y}{detail}{X}")
        passes += ok; fails += (not ok)

    # A: date + time filled via LLM re-parse
    partial = BookingData(
        intent=BookingIntent.BOOKING, name="An", service="Massage",
        date=None, time=None, confidence=0.6,
    )
    fake_chain = mock.MagicMock()
    fake_chain.extract.return_value = BookingData(
        intent=BookingIntent.BOOKING, name="An", service="Massage",
        date="2099-03-28", time="15:00", confidence=0.95,
    )
    answers = iter(["28/03/2099", "15:00"])
    with mock.patch("booking_agent._ask_field", side_effect=lambda f: next(answers)):
        try:
            filled = ba.collect_missing_fields(partial, fake_chain)
            ok = filled.date == "2099-03-28" and filled.time == "15:00"
            case("Feedback fills date + time via LLM re-parse", ok,
                 f"date={filled.date}  time={filled.time}")
        except Exception as e:
            case("Feedback fills date + time via LLM re-parse", False, str(e))

    # B: name filled directly (no LLM)
    partial_name = BookingData(
        intent=BookingIntent.BOOKING, name=None, service="Wax",
        date="2099-04-10", time="09:00", confidence=0.8,
    )
    with mock.patch("booking_agent._ask_field", return_value="Bích"):
        try:
            filled = ba.collect_missing_fields(partial_name, mock.MagicMock())
            case("Missing name filled directly (no LLM)", filled.name == "Bích",
                 f"name={filled.name}")
        except Exception as e:
            case("Missing name filled directly (no LLM)", False, str(e))

    # C: 'skip' aborts loop
    with mock.patch("booking_agent._ask_field", side_effect=ba._Skip):
        try:
            ba.collect_missing_fields(dataclasses.replace(partial_name, name=None), mock.MagicMock())
            case("'skip' aborts loop → _Skip raised", False, "No exception raised")
        except ba._Skip:
            case("'skip' aborts loop → _Skip raised", True)
        except Exception as e:
            case("'skip' aborts loop → _Skip raised", False, str(e))

    # D: max retries on bad LLM parse
    bad_chain = mock.MagicMock()
    bad_chain.extract.return_value = dataclasses.replace(partial, date=None)
    with mock.patch("booking_agent._ask_field", return_value="không hiểu"):
        try:
            ba.collect_missing_fields(partial, bad_chain)
            case("Max retries → _Skip", False, "No exception raised")
        except ba._Skip:
            case("Max retries → _Skip", True)
        except Exception as e:
            case("Max retries → _Skip", False, str(e))

    # E: complete data passes through unchanged
    complete = BookingData(
        intent=BookingIntent.BOOKING, name="Full", service="Chăm sóc da",
        date="2099-05-01", time="11:00", confidence=0.95,
    )
    with mock.patch("booking_agent._ask_field", side_effect=AssertionError("should not prompt")):
        try:
            filled = ba.collect_missing_fields(complete, mock.MagicMock())
            case("Complete data passes through without prompting",
                 filled.name == "Full" and filled.date == "2099-05-01")
        except Exception as e:
            case("Complete data passes through without prompting", False, str(e))

    return passes, fails


# ── Group 4: Phone normalisation ──────────────────────────────────────────────
def run_phone_tests() -> tuple[int, int]:
    passes = fails = 0

    print(f"\n{B}{DIV}{X}")
    print(f"{B}  GROUP 4 — Phone number normalisation & validation{X}")
    print(f"{B}{DIV}{X}")

    # (input, expected_normalised_or_None, expect_valid)
    cases: list[tuple[str, str | None, bool]] = [
        # ── The three canonical formats ──────────────────────────────────────
        ("+84912345678",  "0912345678", True),
        ("0912345678",    "0912345678", True),
        ("84912345678",   "0912345678", True),

        # ── Separators / formatting noise ────────────────────────────────────
        ("+84 912 345 678",  "0912345678", True),
        ("091-234-5678",     "0912345678", True),
        ("0912.345.678",     "0912345678", True),

        # ── Other valid prefixes ──────────────────────────────────────────────
        ("0332123456",  "0332123456", True),   # Viettel
        ("0765432109",  "0765432109", True),   # Mobifone
        ("0865432109",  "0865432109", True),   # Vinaphone

        # ── Invalid: wrong length ─────────────────────────────────────────────
        ("091234567",    None, False),   # 9 digits
        ("09123456789",  None, False),   # 11 digits
        ("",             None, False),   # empty

        # ── Invalid: bad prefix ───────────────────────────────────────────────
        ("0112345678",  None, False),   # 011 not a VN prefix
        ("0212345678",  None, False),   # landline-style

        # ── Invalid: non-numeric ──────────────────────────────────────────────
        ("abcd123456",  None, False),
        ("+84abc45678", None, False),
    ]

    col_w = 22
    for raw, expected, expect_valid in cases:
        normalised, err = normalise_phone(raw)

        if expect_valid:
            ok     = normalised == expected and err is None
            result = f"→ {normalised}" if ok else f"→ {normalised!r}  (expected {expected!r})"
        else:
            ok     = normalised is None and err is not None
            result = f"DENIED: {err}" if ok else f"Should be invalid, got {normalised!r}"

        badge = f"{G}PASS{X}" if ok else f"{R}FAIL{X}"
        icon  = f"{G}✅{X}" if ok else f"{R}❌{X}"
        print(f"  {badge} │ {icon} {Y}{raw!r:<{col_w}}{X}  {result}")

        passes += ok; fails += (not ok)

    return passes, fails


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    store_path = Path(__file__).parent / "bookings.json"
    if store_path.exists():
        store_path.unlink()
        print(f"{Y}🗑️  Deleted{X} {store_path.name}")
    store = JsonStore(store_path)
    print(f"📄  Created fresh {store_path.name}\n")

    p1, f1 = run_booking_tests(store)
    p2, f2 = run_feedback_tests()
    p3, f3 = run_phone_tests()

    total_pass = p1 + p2 + p3
    total_fail = f1 + f2 + f3
    total      = total_pass + total_fail

    print(f"\n{B}{DIV}{X}")
    result = f"{G}ALL PASS ✅{X}" if total_fail == 0 else f"{R}{total_fail} FAILED ❌{X}"
    print(f"{B}  RESULTS: {total_pass}/{total} passed  {result}{X}")
    print(f"{B}{DIV}{X}")

    records = store.all_bookings()
    print(f"\n📄  bookings.json — {len(records)} record(s):\n")
    print(json.dumps(records, ensure_ascii=False, indent=2))

    sys.exit(0 if total_fail == 0 else 1)


if __name__ == "__main__":
    main()