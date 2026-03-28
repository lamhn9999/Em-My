"""
core/agents/fallback_agent.py
──────────────────────────────────────────────────────────────────────────────
Fallback Agent — handles Type 7 (OTHER) messages.

Provides a polite, context-aware "I don't understand" response and suggests
what the bot can actually do.
"""
from __future__ import annotations


class FallbackAgent:

    async def handle(self, text: str = "") -> str:
        return (
            "Xin lỗi, mình chưa hiểu yêu cầu của bạn lắm. 😅\n\n"
            "Mình có thể hỗ trợ bạn:\n"
            "• 📅 **Đặt lịch** — nhắn 'đặt lịch [dịch vụ] ngày [ngày] lúc [giờ]'\n"
            "• 🔍 **Kiểm tra lịch trống** — nhắn 'còn chỗ ngày [ngày] không?'\n"
            "• ✏️ **Đổi lịch** — nhắn 'dời lịch sang [ngày/giờ mới]'\n"
            "• ❌ **Huỷ lịch** — nhắn 'huỷ lịch'\n"
            "• 💬 **Hỏi dịch vụ** — nhắn 'dịch vụ gì?' hoặc 'giá bao nhiêu?'\n\n"
            "Bạn cần hỗ trợ gì ạ?"
        )
