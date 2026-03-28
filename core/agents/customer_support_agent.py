"""
core/agents/customer_support_agent.py
──────────────────────────────────────────────────────────────────────────────
Customer Support Agent — handles Type 1 (INFO) and Type 6 (GREETING).

Responsibilities:
  • Answer questions about services, prices, working hours, staff
  • Handle greetings warmly
  • Never touch booking state
"""
from __future__ import annotations

from config.business import BUSINESS_HOURS, SERVICES, STAFF


class CustomerSupportAgent:

    async def handle_greeting(self, client_name: str = "") -> str:
        name_part = f" {client_name}" if client_name else ""
        return (
            f"Xin chào{name_part}! 👋 Chào mừng bạn đến với salon của chúng mình.\n"
            "Mình có thể giúp bạn:\n"
            "• Đặt lịch làm tóc\n"
            "• Kiểm tra lịch trống\n"
            "• Tư vấn dịch vụ\n"
            "Bạn cần hỗ trợ gì ạ?"
        )

    async def handle_info_query(self, text: str) -> str:
        lower = text.lower()

        # Price queries
        if any(w in lower for w in ["giá", "bao nhiêu", "tiền", "price", "cost", "how much"]):
            return await self._reply_prices()

        # Service list queries
        if any(w in lower for w in ["dịch vụ", "làm gì", "service", "what"]):
            return await self._reply_services()

        # Working hours
        if any(w in lower for w in ["giờ", "mấy giờ", "open", "close", "mở cửa", "đóng cửa"]):
            return await self._reply_hours()

        # Staff queries
        if any(w in lower for w in ["nhân viên", "thợ", "stylist", "ai", "staff", "who"]):
            return await self._reply_staff()

        # Generic fallback
        return await self._reply_generic()

    async def _reply_prices(self) -> str:
        lines = ["💈 **Bảng giá dịch vụ:**\n"]
        price_map = {
            "cắt tóc":       "80.000đ",
            "cắt + gội":     "100.000đ",
            "uốn tóc":       "350.000đ",
            "nhuộm tóc":     "300.000đ",
            "tẩy + nhuộm":   "550.000đ",
            "highlight":     "450.000đ",
            "duỗi tóc":      "300.000đ",
            "hấp dầu":       "150.000đ",
            "gội đầu massage": "80.000đ",
        }
        for svc_name, price in price_map.items():
            lines.append(f"• {svc_name.capitalize()}: {price}")
        lines.append("\n_Giá có thể thay đổi tuỳ độ dài và tình trạng tóc._")
        return "\n".join(lines)

    async def _reply_services(self) -> str:
        svc_names = [s.name.capitalize() for s in SERVICES]
        return (
            "💈 **Dịch vụ của chúng mình:**\n"
            + "\n".join(f"• {n}" for n in svc_names)
            + "\n\nBạn muốn đặt lịch dịch vụ nào ạ?"
        )

    async def _reply_hours(self) -> str:
        day_vn = {
            "mon": "Thứ 2", "tue": "Thứ 3", "wed": "Thứ 4",
            "thu": "Thứ 5", "fri": "Thứ 6", "sat": "Thứ 7", "sun": "Chủ nhật",
        }
        lines = ["🕐 **Giờ làm việc:**\n"]
        for day, (open_, close) in BUSINESS_HOURS.items():
            lines.append(f"• {day_vn[day]}: {open_} – {close}")
        return "\n".join(lines)

    async def _reply_staff(self) -> str:
        lines = ["👥 **Đội ngũ nhân viên:**\n"]
        for s in STAFF:
            skills_str = ", ".join(s.skills)
            lines.append(f"• **{s.name}** — {skills_str}")
        lines.append("\nBạn có muốn đặt lịch với nhân viên cụ thể không ạ?")
        return "\n".join(lines)

    async def _reply_generic(self) -> str:
        return (
            "Cảm ơn bạn đã liên hệ! 😊\n"
            "Bạn có thể hỏi mình về:\n"
            "• Giá dịch vụ\n"
            "• Danh sách dịch vụ\n"
            "• Giờ làm việc\n"
            "• Đội ngũ nhân viên\n"
            "Hoặc nhắn **'đặt lịch'** để bắt đầu đặt lịch nhé!"
        )
