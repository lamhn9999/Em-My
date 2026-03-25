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

from langchain_core.output_parsers import JsonOutputParser, StrOutputParser
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

_THINK_SYSTEM = dedent("""
    Bạn là trợ lý đặt lịch thông minh cho spa/phòng khám/tư vấn.
    Hôm nay là {today} (Asia/Ho_Chi_Minh).

    Dựa vào Lịch sử chat (tin nhắn mới nhất nằm cuối), hãy suy nghĩ và phân tích xem khách hàng đang muốn làm gì trong tin nhắn mới nhất.
    Nếu họ đang trả lời một câu hỏi của bạn (ví dụ: bổ sung ngày/giờ cho câu hỏi 'Bạn muốn đặt ngày nào?'), hãy hiểu ngữ cảnh để nhận diện đây là hành động bổ sung thông tin đặt lịch, chứ không phải là một câu hỏi trống không.
    
    Quy tắc chuyển đổi thời gian:
    - "ngày mai"        → ngày hôm sau
    - "tuần sau"        → +7 ngày, giữ thứ tương ứng
    - "thứ X tuần này"  → ngày thứ X trong tuần hiện tại
    - "sáng" (không giờ cụ thể) → 09:00
    - "chiều"                    → 14:00
    - "tối"                      → 18:00
    - "3h chiều" / "15h" / "3pm" / "3h chiều nay" → 15:00  

    Hãy viết ra suy nghĩ của bạn về: intent của họ, họ muốn hỏi gì, và các trường (name, phone, service, date, time) được suy luận từ ngữ cảnh đoạn chat.
""").strip()

_JSON_SYSTEM = dedent("""
    Dựa vào Lịch sử chat và Phân tích trước đó, hãy trích xuất thông tin thành JSON.
    BẮT BUỘC: Bạn phải cố gắng tìm và trích xuất 5 trường quan trọng nhất: name, phone, service, date, time.

    Quy tắc confidence:
    - 0.9+ : đủ tất cả thông tin (tên, dịch vụ, ngày, giờ)
    - 0.7–0.9 : có thể đặt nhưng thiếu vài chi tiết phụ
    - < 0.7 : thiếu thông tin cốt lõi

    Trả về JSON hợp lệ (KHÔNG có markdown, KHÔNG có backtick, KHÔNG bọc trong markdown) với cấu trúc:
    {{
      "intent": "booking" | "cancel" | "query" | "unknown",
      "query_type": "empty_schedule" | "upcoming_schedule" | "missing_fields" | null,
      "name": "<tên (BẮT BUỘC tìm) hoặc null>",
      "phone": "<10 số (BẮT BUỘC tìm) hoặc null>",
      "service": "<dịch vụ (BẮT BUỘC tìm) hoặc null>",
      "date": "<YYYY-MM-DD (BẮT BUỘC tìm) hoặc null>",
      "time": "<HH:MM (BẮT BUỘC tìm) hoặc null>",
      "duration_minutes": <số nguyên, mặc định 60>,
      "notes": "<ghi chú hoặc null>",
      "confidence": <0.0 đến 1.0>,
      "denial_reason": "<lý do nếu unknown, ngược lại null>"
    }}
""").strip()

_HUMAN_THINK = "Lịch sử chat:\n{history}"
_HUMAN_JSON = "Lịch sử chat:\n{history}\n\nPhân tích của bạn:\n{thought}"


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
        query_type=raw.get("query_type"),
        denial_reason=raw.get("denial_reason"),
    )


# ── Chain ──────────────────────────────────────────────────────────────────────

class IntentExtractionChain:
    """
    LCEL 2-step chain: 
      1. history -> LLM -> thought (StrOutputParser)
      2. history + thought -> LLM -> JSON (JsonOutputParser)
    """

    def __init__(self):
        self._llm = _build_llm()
        
        self._think_chain = ChatPromptTemplate.from_messages([
            ("system", _THINK_SYSTEM), 
            ("human", _HUMAN_THINK)
        ]) | self._llm | StrOutputParser()
        
        self._json_chain = ChatPromptTemplate.from_messages([
            ("system", _JSON_SYSTEM), 
            ("human", _HUMAN_JSON)
        ]) | self._llm | JsonOutputParser()

    def extract(self, history_text: str) -> BookingData:
        today = datetime.now().strftime("%A, %d/%m/%Y")
        
        print("\n[LLM] 🤔 Analyzing conversation history...")
        thought = self._think_chain.invoke({"history": history_text, "today": today})
        print(f"[LLM THOUGHT]:\n{thought}\n")
        
        print("[LLM] 📝 Extracting structured JSON...")
        raw = self._json_chain.invoke({"history": history_text, "today": today, "thought": thought})
        print(f"[LLM RAW PARSED]: {raw}\n")
        
        return _parse(raw)