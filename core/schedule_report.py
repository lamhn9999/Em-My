"""
core/schedule_report.py
──────────────────────────────────────────────────────────────────────────────
Generates a markdown snapshot of the day's confirmed schedule.

Called after every booking confirmation. Writes to:
  data/schedule_YYYY-MM-DD.md

The report shows:
  • A timeline grid (30-min slots) for each staff member and station
  • A booking-by-booking breakdown with step assignments
  • CP-SAT / greedy solver output embedded from the log
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from data.backends.sqlite import Database

SLOT_MIN = 30          # minutes per cell in the timeline grid
REPORT_DIR = Path("data/reports")


def _hhmm_to_min(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _min_to_hhmm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


async def generate_report(db: Database, date: str) -> Path:
    """
    Build a markdown schedule report for *date* (YYYY-MM-DD) and write it to
    data/reports/schedule_YYYY-MM-DD.md.  Returns the file path.
    """
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"schedule_{date}.md"

    # ── Fetch data ────────────────────────────────────────────────────────────
    bookings     = await db.get_confirmed_bookings_by_date(date)
    all_staff    = await db.list_staff()
    all_stations = await db.list_stations()
    assignments  = await db.get_resource_assignments_for_date(date)

    # Build busy map: resource_id → list of (start_min, end_min, booking_id)
    busy_map: dict[str, list[tuple[int, int, str]]] = {}
    for a in assignments:
        rid = a["resource_id"]
        s   = _hhmm_to_min(a["start_time"])
        e   = _hhmm_to_min(a["end_time"])
        bid = a.get("booking_id", "?")
        busy_map.setdefault(rid, []).append((s, e, bid))

    # ── Determine time range ──────────────────────────────────────────────────
    from config.business import BUSINESS_HOURS
    from core.scheduler import _day_of_week
    day = _day_of_week(date)
    biz = BUSINESS_HOURS.get(day, ("09:00", "20:00"))
    open_min  = _hhmm_to_min(biz[0])
    close_min = _hhmm_to_min(biz[1])
    slots = list(range(open_min, close_min, SLOT_MIN))

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _cell(resource_id: str, slot_start: int) -> str:
        slot_end = slot_start + SLOT_MIN
        for s, e, bid in busy_map.get(resource_id, []):
            if max(slot_start, s) < min(slot_end, e):
                # Show short booking id
                tag = bid.split("-")[-1] if "-" in bid else bid[:6]
                return f"`{tag}`"
        return "·"

    def _day_vn(dt: datetime) -> str:
        days = ["Thứ Hai", "Thứ Ba", "Thứ Tư", "Thứ Năm",
                "Thứ Sáu", "Thứ Bảy", "Chủ Nhật"]
        return days[dt.weekday()]

    # ── Header ────────────────────────────────────────────────────────────────
    now = datetime.now().strftime("%H:%M:%S %d/%m/%Y")
    try:
        dt_obj  = datetime.strptime(date, "%Y-%m-%d")
        date_vn = f"{_day_vn(dt_obj)} {dt_obj.strftime('%d/%m/%Y')}"
    except Exception:
        date_vn = date

    lines: list[str] = [
        f"# 📅 Lịch Salon — {date_vn}",
        f"_Cập nhật lúc {now}_",
        "",
        f"> Giờ mở cửa: {biz[0]} – {biz[1]} | Slot: {SLOT_MIN} phút",
        "",
    ]

    # ── Timeline grid ─────────────────────────────────────────────────────────
    lines.append("## 🗓️ Timeline")
    lines.append("")

    # Header row
    header_slots = " | ".join(_min_to_hhmm(s) for s in slots)
    lines.append(f"| Nhân viên/Trạm | {header_slots} |")
    lines.append(f"|{'---|' * (len(slots) + 1)}")

    # Staff rows
    lines.append("**Nhân viên**|" + "|" * len(slots))
    for staff in all_staff:
        cells = " | ".join(_cell(staff.id, s) for s in slots)
        lines.append(f"| {staff.name} ({staff.id}) | {cells} |")

    lines.append("|" + "---|" * (len(slots) + 1))
    lines.append("**Trạm**|" + "|" * len(slots))

    # Station rows
    for station in all_stations:
        cells = " | ".join(_cell(station.id, s) for s in slots)
        lines.append(f"| {station.name} ({station.id}) | {cells} |")

    lines.append("")

    # ── Legend ────────────────────────────────────────────────────────────────
    lines.append("> **Chú thích:** `XXXXXXXX` = mã booking | `·` = trống")
    lines.append("")

    # ── Booking details ───────────────────────────────────────────────────────
    lines.append("## 📋 Chi tiết booking")
    lines.append("")

    if not bookings:
        lines.append("_Chưa có booking nào được xác nhận._")
    else:
        for bk in bookings:
            bid  = bk.get("booking_id", "?")
            name = bk.get("name") or "Chưa có tên"
            svc  = bk.get("service") or "?"
            time = bk.get("time") or "?"
            dur  = bk.get("duration_minutes") or "?"
            ph   = bk.get("phone") or "—"

            lines.append(f"### {bid}")
            lines.append(f"- **Khách:** {name} | **SĐT:** {ph}")
            lines.append(f"- **Dịch vụ:** {svc}")
            lines.append(f"- **Giờ vào:** {time} | **Thời lượng:** ~{dur} phút")
            lines.append("")
            lines.append("| Bước | Loại | Tài nguyên | Bắt đầu | Kết thúc |")
            lines.append("|------|------|-----------|---------|---------|")

            # Load step assignments for this booking
            steps = await db._load_resource_assignments(bid)
            if steps:
                for step in steps:
                    resource_label = step.resource_id
                    # Resolve staff name
                    for staff in all_staff:
                        if staff.id == step.resource_id:
                            resource_label = f"{staff.name} ({staff.id})"
                            break
                    # Resolve station name
                    for station in all_stations:
                        if station.id == step.resource_id:
                            resource_label = f"{station.name} ({station.id})"
                            break
                    if step.resource_id == "__wait__":
                        resource_label = "_(chờ không cần nhân viên)_"
                    lines.append(
                        f"| {step.step_index} | {step.step_type} | "
                        f"{resource_label} | {step.start_time} | {step.end_time} |"
                    )
            else:
                lines.append("| — | — | _(chưa có bước nào)_ | — | — |")

            lines.append("")

    # ── OR-Tools solver summary ───────────────────────────────────────────────
    lines.append("## ⚙️ OR-Tools CP-SAT — Thông số giải")
    lines.append("")
    lines.append("Xem chi tiết log ứng dụng với filter `[CP-SAT]` hoặc `[Greedy]`.")
    lines.append("")
    lines.append("```")
    lines.append("grep -E '\\[(CP-SAT|Greedy|Alternatives)\\]' <app.log>")
    lines.append("```")
    lines.append("")

    # Summary table per booking
    lines.append("| Booking | Dịch vụ | Số bước | Tổng thời gian |")
    lines.append("|---------|---------|---------|----------------|")
    for bk in bookings:
        bid  = bk.get("booking_id", "?")
        svc  = bk.get("service") or "?"
        dur  = bk.get("duration_minutes") or 0
        steps = await db._load_resource_assignments(bid)
        n_steps = len([s for s in steps if s.resource_id != "__wait__"])
        lines.append(f"| {bid} | {svc} | {n_steps} | {dur} phút |")

    lines.append("")
    lines.append(f"_Tổng số booking: **{len(bookings)}**_")
    lines.append("")

    # ── Write file ────────────────────────────────────────────────────────────
    content = "\n".join(lines)
    path.write_text(content, encoding="utf-8")
    return path
