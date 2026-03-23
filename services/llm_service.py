"""
llm_service.py
--------------
LangChain chain that extracts structured booking intent from Vietnamese text.
Uses JsonOutputParser + manual dataclass construction (no Pydantic required).

Free model: Groq  →  llama-3.3-70b-versatile  (generous free tier)
Fallback:   Ollama → llama3.2  (fully offline, no API key needed)

Set GROQ_API_KEY in .env to use Groq (recommended).
Set USE_OLLAMA=true to force local Ollama instead.
"""
from __future__ import annotations

import os
from datetime import datetime
from textwrap import dedent

from langchain_core.output_parsers import JsonOutputParser
from langchain_core.prompts import ChatPromptTemplate

from data.models import BookingData, BookingIntent


# ── Model factory ──────────────────────────────────────────────────────────────

def _build_llm():
    use_ollama = os.getenv("USE_OLLAMA", "false").lower() == "true"

    if use_ollama:
        from langchain_ollama import ChatOllama
        model = os.getenv("OLLAMA_MODEL", "llama3.2")
        print(f"🦙  Using Ollama ({model}) — make sure `ollama serve` is running")
        return ChatOllama(model=model, temperature=0)

    groq_key = os.getenv("GROQ_API_KEY", "")
    if groq_key:
        from langchain_groq import ChatGroq
        model = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
        print(f"⚡  Using Groq ({model})")
        return ChatGroq(api_key=groq_key, model=model, temperature=0)

    raise EnvironmentError(
        "No LLM configured.\n"
        "  Option A (Groq — free):    set GROQ_API_KEY in .env\n"
        "  Option B (Ollama — local): set USE_OLLAMA=true in .env"
    )


# ── Prompt ─────────────────────────────────────────────────────────────────────

_SYSTEM = dedent("""
    Bạn là trợ lý đặt lịch thông minh cho spa/phòng khám/tư vấn.
    Hôm nay là {today} (Asia/Ho_Chi_Minh).

    Nhiệm vụ: Trích xuất thông tin đặt lịch từ tin nhắn tiếng Việt (kể cả viết tắt, không dấu).

    Quy tắc chuyển đổi thời gian:
    - "ngày mai"        → ngày hôm sau
    - "tuần sau"        → +7 ngày, giữ thứ tương ứng
    - "thứ X tuần này"  → ngày thứ X trong tuần hiện tại
    - "sáng" (không giờ cụ thể) → 09:00
    - "chiều"                    → 14:00
    - "tối"                      → 18:00
    - "3h chiều" / "15h" / "3pm" / "3h chiều nay" → 15:00  

    Quy tắc confidence:
    - 0.9+ : đủ tất cả thông tin (tên, dịch vụ, ngày, giờ)
    - 0.7–0.9 : có thể đặt nhưng thiếu vài chi tiết phụ
    - < 0.7 : thiếu thông tin cốt lõi

    Trả về JSON hợp lệ (KHÔNG có markdown, KHÔNG có backtick) với cấu trúc:
    {{
      "intent": "booking" | "cancel" | "query" | "unknown",
      "name": "<tên hoặc null>",
      "phone": "<10 số hoặc null>",
      "service": "<dịch vụ hoặc null>",
      "date": "<YYYY-MM-DD hoặc null>",
      "time": "<HH:MM hoặc null>",
      "duration_minutes": <số nguyên, mặc định 60>,
      "notes": "<ghi chú hoặc null>",
      "confidence": <0.0 đến 1.0>,
      "missing_fields": ["<tên trường còn thiếu>"],
      "denial_reason": "<lý do nếu unknown, ngược lại null>"
    }}
""").strip()

_HUMAN = "Tin nhắn của khách: {message}"


# ── Parser: JSON → BookingData dataclass ───────────────────────────────────────

def _parse(raw: dict) -> BookingData:
    """Convert the raw JSON dict from the LLM into a BookingData dataclass."""
    try:
        intent = BookingIntent(raw.get("intent", "unknown"))
    except ValueError:
        intent = BookingIntent.UNKNOWN

    return BookingData(
        intent=intent,
        name=raw.get("name"),
        phone=raw.get("phone"),
        service=raw.get("service"),
        date=raw.get("date"),
        time=raw.get("time"),
        duration_minutes=int(raw.get("duration_minutes") or 60),
        notes=raw.get("notes"),
        confidence=float(raw.get("confidence") or 0.0),
        missing_fields=raw.get("missing_fields") or [],
        denial_reason=raw.get("denial_reason"),
    )


# ── Chain ──────────────────────────────────────────────────────────────────────

class IntentExtractionChain:
    """
    LCEL chain:  prompt | llm | JsonOutputParser | _parse → BookingData
    """

    def __init__(self):
        self._llm    = _build_llm()
        self._prompt = ChatPromptTemplate.from_messages(
            [("system", _SYSTEM), ("human", _HUMAN)]
        )
        self._chain  = self._prompt | self._llm | JsonOutputParser()

    def extract(self, message: str) -> BookingData:
        today  = datetime.now().strftime("%A, %d/%m/%Y")
        raw    = self._chain.invoke({"message": message, "today": today})
        return _parse(raw)