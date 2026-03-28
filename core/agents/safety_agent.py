"""
core/agents/safety_agent.py
──────────────────────────────────────────────────────────────────────────────
Safety Agent — runs before any business logic.

Responsibilities:
  • Detect prompt-injection attempts (Type 0 sub-type: injection)
  • Detect abuse / disrespect (Type 0 sub-type: abuse)
  • Check if the sender is blacklisted → block and warn
  • Return (safe: bool, reply: str | None)

If safe=False, the orchestrator sends the reply and stops processing.
"""
from __future__ import annotations

import re

from data.backends.sqlite import Database

# Patterns that strongly suggest prompt injection
_INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"forget\s+(everything|all|your|the)",
    r"you\s+are\s+now\s+a",
    r"act\s+as\s+(if\s+you\s+are|a)",
    r"disregard\s+your",
    r"new\s+system\s+prompt",
    r"<\s*system\s*>",
    r"\[system\]",
    r"###\s*instruction",
]

_INJECTION_RE = re.compile(
    "|".join(_INJECTION_PATTERNS), re.IGNORECASE
)

# Crude Vietnamese + English abuse word list (expand as needed)
_ABUSE_WORDS = {
    "đụ", "địt", "đéo", "chó", "ngu", "óc chó", "cút", "fuck", "shit",
    "asshole", "idiot", "stupid", "bitch",
}


class SafetyAgent:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def check(self, user_id: str, text: str) -> tuple[bool, str | None]:
        """
        Returns (is_safe, reply_message).
        If is_safe=False, the caller should send reply_message and stop.
        """
        # 1. Blacklist check
        if await self._db.is_blacklisted(user_id):
            entry = await self._db.get_blacklist_entry(user_id)
            reason = entry.reason if entry else ""
            return False, (
                "Tài khoản của bạn đã bị hạn chế sử dụng dịch vụ đặt lịch. "
                "Vui lòng liên hệ trực tiếp salon để được hỗ trợ."
            )

        # 2. Prompt injection detection
        if _INJECTION_RE.search(text):
            return False, (
                "Xin lỗi, mình không thể xử lý yêu cầu đó. "
                "Mình chỉ hỗ trợ đặt lịch và tư vấn dịch vụ tóc. 😊"
            )

        # 3. Abuse detection
        lower = text.lower()
        for word in _ABUSE_WORDS:
            if word in lower:
                return False, (
                    "Mình nhận thấy tin nhắn có nội dung không phù hợp. "
                    "Vui lòng giữ thái độ lịch sự để mình có thể hỗ trợ bạn tốt nhất nhé! 🙏"
                )

        return True, None
