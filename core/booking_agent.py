"""
booking_agent.py  —  Zalo Booking Agent MVP
============================================
Flow:
  User msg → LLM extract (with history) → if missing/low conf: follow-up
  If complete: transition to CONFIRMING state → ask for approval
  If approved: save to JSON → move to POST_BOOKING
  POST_BOOKING: allow updating optional fields or answering questions.
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import datetime
from pathlib import Path
from enum import Enum

# ── Imports & Path ─────────────────────────────────────────────────────────────
ROOT = Path(__file__).parent.parent.absolute()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Fix Windows console encoding issues for Vietnamese/Box characters
if sys.stdout.encoding != 'utf-8':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except AttributeError:
        import io
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from data.backends.json_store import JsonStore
from data.models import BookingData, BookingIntent
try:
    from validator import validate
except ImportError:
    from core.validator import validate

DIV  = "─" * 58
DIV2 = "╌" * 58

class AgentState(str, Enum):
    COLLECTING = "COLLECTING"
    CONFIRMING = "CONFIRMING"
    POST_BOOKING = "POST_BOOKING"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_dt(date: str, time: str) -> str:
    try:
        dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        return dt.strftime("%A, %d/%m/%Y lúc %H:%M").capitalize()
    except Exception:
        return f"{date} {time}"


def _print_banner():
    print(f"\n{'═'*58}")
    print("  🗓️   ZALO BOOKING AGENT  —  MVP")
    print("  LLM : Groq / Llama-3.3-70B  |  Store: JSON")
    print(f"{'═'*58}")
    print("  Gõ tin nhắn đặt lịch bằng tiếng Việt.")
    print("  Gõ  'exit'  để thoát. Gõ  'skip'  để huỷ/bắt đầu lại.\n")


def _print_extracted(data: BookingData):
    print("\n🤖  Phân tích:")
    for k, v in {
        "intent":     data.intent.value,
        "name":       data.name,
        "phone":      data.phone,
        "service":    data.service,
        "date":       data.date,
        "time":       data.time,
        "confidence": f"{data.confidence:.0%}",
    }.items():
        print(f"   {k:<12}: {v}")


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from services.llm_service import IntentExtractionChain

    _print_banner()

    print("🔧  Initialising…")
    try:
        chain = IntentExtractionChain()
        store = JsonStore()
    except EnvironmentError as exc:
        print(f"\n❌  Setup error:\n{exc}")
        sys.exit(1)

    print(f"✅  Ready. Bookings → {store.path}\n")

    context = ""
    state = AgentState.COLLECTING
    pending_data = None
    confirmed_id = None

    while True:
        try:
            user_msg = input("📨  User: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n👋  Tạm biệt!")
            break

        if not user_msg:
            continue
        
        lower_msg = user_msg.lower()
        if lower_msg in ("exit", "quit", "thoát", "q"):
            print("👋  Tạm biệt!")
            break

        if lower_msg == "skip":
            print("↩️   Đã huỷ phiên này. Bắt đầu lại.\n")
            context = ""
            state = AgentState.COLLECTING
            pending_data = None
            confirmed_id = None
            continue

        # --- STATE: CONFIRMING ---
        if state == AgentState.CONFIRMING:
            is_yes = any(kw in lower_msg for kw in ("có", "oke", "ok", "đúng", "vâng", "dạ", "chốt", "yes", "y", "xác nhận", "uầy"))
            is_no  = any(kw in lower_msg for kw in ("không", "no", "n", "huỷ", "huy", "đổi", "sửa", "sai"))

            if is_yes and not is_no:
                record = store.add_booking(pending_data)
                confirmed_id = record["id"]
                print(f"\n✅  ĐÃ ĐẶT LỊCH  (id: {confirmed_id})")
                print(f"   👤  {pending_data.name}")
                print(f"   💆  {pending_data.service}")
                print(f"   🕐  {_fmt_dt(pending_data.date, pending_data.time)}")
                print(f"   💾  Saved → {store.path}\n")
                
                print("🤖  AI: Cảm ơn bạn! Bạn có câu hỏi nào khác không? (Bạn vẫn có thể bổ sung SĐT hoặc ghi chú nhé)")
                
                # Move to POST_BOOKING and update history
                context += f"\nUser: {user_msg}\nAI: Đã xác nhận đặt lịch thành công (id: {confirmed_id})."
                state = AgentState.POST_BOOKING
                pending_data = None
                continue
            elif is_no:
                if any(kw in lower_msg for kw in ("đổi", "sửa", "sai", "lại")):
                    print("🤖  AI: Vâng, bạn muốn thay đổi thông tin nào ạ?")
                    state = AgentState.COLLECTING
                else:
                    print("🤖  AI: Vâng, bạn có muốn thay đổi thông tin nào không, hay mình bắt đầu lại từ đầu?")
                    state = AgentState.COLLECTING
                continue
            else:
                state = AgentState.COLLECTING # Fall through

        # --- STATE: COLLECTING or POST_BOOKING ---
        print(f"\n{DIV}")
        print("⏳  Đang phân tích…")

        try:
            data = chain.extract(user_msg, context=context)
        except Exception as exc:
            print(f"❌  Lỗi LLM: {exc}\n")
            continue

        _print_extracted(data)

        # Update context
        context += f"\nUser: {user_msg}\nAI Extracted: {data.intent.value}"
        if data.notes:
            context += f" (Notes: {data.notes})"

        # Handle intent by state
        if state == AgentState.POST_BOOKING:
            # check for updates to optional fields
            updates = {}
            if data.phone: updates["phone"] = data.phone
            if data.notes: updates["notes"] = data.notes
            
            if updates:
                store.update_booking(confirmed_id, updates)
                print(f"✅  Đã cập nhật thông tin bổ sung: {updates}")
            
            # Use follow_up question or generic response
            if data.follow_up_question:
                print(f"\n🤖  AI: {data.follow_up_question}\n")
            else:
                print("\n🤖  AI: Ghi nhận thông tin của bạn. Bạn còn câu hỏi nào khác không?\n")
            
            # Enforce "One booking per session"
            if data.intent == BookingIntent.BOOKING and not updates:
                print("⚠️  Lưu ý: Bạn đã có một lịch hẹn trong phiên này. Nếu muốn đặt lịch mới, vui lòng gõ 'skip' để bắt đầu phiên mới.")
            
            continue

        # COLLECTING specific logic
        if data.intent != BookingIntent.BOOKING:
            if data.follow_up_question:
                print(f"\n🤖  AI: {data.follow_up_question}\n")
            else:
                print(f"\n❌  Từ chối: {data.denial_reason or 'Không phải yêu cầu đặt lịch.'}\n")
            continue

        ok, reason = validate(data, store=store)
        
        if ok and data.confidence >= 0.9 and not data.missing_fields:
            print(f"\n{DIV2}")
            print("📝  XÁC NHẬN THÔNG TIN:")
            print(f"    - Khách hàng: {data.name}")
            print(f"    - SĐT:      {data.phone or 'Chưa có'}")
            print(f"    - Dịch vụ:   {data.service}")
            print(f"    - Thời gian: {_fmt_dt(data.date, data.time)}")
            if data.notes:
                print(f"    - Ghi chú:   {data.notes}")
            print(f"\n👉  Bạn xác nhận thông tin trên là ĐÚNG chứ? (Có/Không)")
            print(f"{DIV2}\n")
            state = AgentState.CONFIRMING
            pending_data = data
        else:
            if not ok:
                print(f"\n⚠️  Vấn đề: {reason}")
            if data.follow_up_question:
                print(f"\n🤖  AI: {data.follow_up_question}\n")
            else:
                missing_labels = [k for k, v in dataclasses.asdict(data).items() if v is None and k in ("name", "service", "date", "time")]
                print(f"\n💬  Vui lòng bổ sung: {', '.join(missing_labels) if missing_labels else 'thông tin.'}\n")


if __name__ == "__main__":
    main()