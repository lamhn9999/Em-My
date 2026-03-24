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
    Bạn là chuyên gia thiết kế System Prompt cho AI Agent đặt lịch (Zalo Booking Agent).
    Hôm nay là {today} (Asia/Ho_Chi_Minh).

    Nhiệm vụ: Trích xuất thông tin đặt lịch từ tin nhắn khách hàng (spa/phòng khám: cắt tóc, triệt lông).
    Hỗ trợ: Tiếng Việt (viết tắt, không dấu, sai chính tả).

    =====================
    🎯 QUY TẮC TRÍCH XUẤT
    =====================
    - intent: 'booking', 'cancel', 'query', 'unknown'.
    - name, phone, service: trích xuất hoặc null.
    - date: YYYY-MM-DD.
    - time: HH:MM.
    - duration_minutes: mặc định 60.
    - confidence: 0.0 - 1.0 (0.9+ nếu đủ info cốt lõi).
    - missing_fields: các trường còn thiếu (name, phone, service, date, time).
    - follow_up_question: Câu hỏi phản hồi tự nhiên, thân thiện bằng tiếng Việt.

    =====================
    🧾 QUY TẮC NOTES (RẤT QUAN TRỌNG)
    =====================
    1. LƯU VÀO NOTES: Triệu chứng, yêu cầu riêng (cắt ngắn, không hóa chất), mức độ chắc chắn.
    2. KHÔNG LƯU VÀO NOTES: name, phone, service, date, time.
    3. Gộp nhiều info phụ thành string ngắn gọn. Nếu không có -> null. KHÔNG tự bịa.

    =====================
    🧠 THỜI GIAN & EDGE CASES
    =====================
    - Chuẩn hóa: "mai" -> ngày+1, "tuần sau" -> ngày+7, "chiều" -> 14:00.
    - Multi-turn: Giữ lại thông tin từ context cũ nếu tin nhắn mới không ghi đè.
    - Phủ định/Thay đổi: Xử lý linh hoạt khi user muốn huỷ hoặc đổi thông tin.

    Trả về JSON duy nhất (KHÔNG markdown):
    {{
      "intent": "...",
      "name": "...",
      "phone": "...",
      "service": "...",
      "date": "...",
      "time": "...",
      "duration_minutes": 60,
      "notes": "...",
      "confidence": 0.0,
      "missing_fields": [],
      "denial_reason": null,
      "follow_up_question": "..."
    }}
""").strip()

_HUMAN = "Tin nhắn của khách: {message}"


# ── Parser: JSON → BookingData dataclass ───────────────────────────────────────

def _parse(raw: dict) -> BookingData:
    """Convert the raw JSON dict from the LLM into a BookingData dataclass."""
    try:
        intent_val = raw.get("intent", "unknown")
        intent = BookingIntent(intent_val)
    except (ValueError, KeyError):
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
        follow_up_question=raw.get("follow_up_question"),
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

    def extract(self, message: str, context: str = "") -> BookingData:
        today  = datetime.now().strftime("%A, %d/%m/%Y")
        inputs = {"message": message, "today": today}
        if context:
            # Inject context into the user message for multi-turn support
            inputs["message"] = f"Context: {context}\n\nTin nhắn mới: {message}"

        try:
            raw = self._chain.invoke(inputs)
            if not isinstance(raw, dict):
                raise ValueError("LLM returned non-dict output")
            return _parse(raw)
        except Exception as exc:
            print(f"⚠️  Extraction error: {exc}")
            return BookingData(intent=BookingIntent.UNKNOWN, confidence=0.0)