"""
services/zalo_api.py
────────────────────────────────────────────────────────────────────────────
Single source of truth for every Zalo OA API contract:
  • Base URLs & endpoint paths
  • Request payload builders
  • Response parsers / normalizers
  • Outbound message format helpers
 
Nothing in this file makes network calls — it only shapes data.
Network I/O lives in zalo_message_sync.
"""
 
from __future__ import annotations
 
import json
from dataclasses import dataclass
from enum import IntEnum
from typing import Any

BASE_URL_V2 = "https://openapi.zalo.me/v2.0/oa"
BASE_URL_V3 = "https://openapi.zalo.me/v3.0/oa"

class Endpoint:
    GET_LIST_RECENT_CHAT    = f"{BASE_URL_V2}/listrecentchat"
    GET_CONVERSATION        = f"{BASE_URL_V2}/conversation"
    POST_SEND_MESSAGE       = f"{BASE_URL_V3}/message/cs"

class MessageSrc(IntEnum):
    """Zalo `src` field: who sent the message."""
    CLIENT      = 1
    OA        = 0
 
class MessageType(IntEnum):
    TEXT    = 1
    IMAGE   = 2
    STICKER = 3
    GIF     = 4
    LINK    = 5

# ──────────────────────────────────────────────────────────────────────────────
# Inbound parsers  (Zalo → our domain)
# ──────────────────────────────────────────────────────────────────────────────
 
@dataclass(frozen=True)
class ParsedMessage:
    msg_id:         str
    from_id:        str
    to_id:          str
    from_display_name: str
    to_display_name:    str
    src:            MessageSrc
    type:           MessageType
    timestamp_ms:   int # epoch ms as returned by Zalo
    sent_time:      str # example: "11:35:37 23/03/2026"
    text:           str
    # Helper properties to match our DB/LLM roles
    sender_role:       str  # 'user' or 'assistant'
    recipient_role:    str  # 'user' or 'assistant'
 
def parse_message(raw: dict[str, Any], oa_id: str) -> ParsedMessage:
    """Normalize one raw Zalo message dict into a ParsedMessage."""
   
    msg_id  = raw.get("message_id") or raw.get("msg_id") or ""
    
    src_val = raw.get("src", MessageSrc.CLIENT)
    src     = MessageSrc(int(src_val))
    
    from_id = raw.get("from_id") or raw.get("user_id") or ""
    to_id   = raw.get("to_id") or ""
    
    # If Zalo doesn't provide to_id (common in some webhooks), infer it
    if not to_id:
        to_id = oa_id if src == MessageSrc.CLIENT else "unknown_client"

    sender_role    = "user" if src == MessageSrc.CLIENT else "assistant"
    recipient_role = "assistant" if src == MessageSrc.CLIENT else "user"

    return ParsedMessage(
        msg_id=msg_id,
        from_id=from_id,
        to_id=to_id,
        from_display_name=raw.get("from_display_name", ""),
        to_display_name=raw.get("to_display_name", ""),
        src=src,
        type=raw.get("type"),
        text=raw.get("message", {}).get("text", "") if isinstance(raw.get("message"), dict) else str(raw.get("message", "")),
        timestamp_ms=int(raw.get("time", 0)),
        sent_time=raw.get("sent_time"),
        sender_role=sender_role,
        recipient_role=recipient_role
    )
 
def parse_messages(raw_list: list[dict], oa_id: str) -> list[ParsedMessage]:
    return [parse_message(r, oa_id) for r in raw_list]
 
def unwrap_list(response: dict, key: str) -> list[dict]:
    """Safely extract a list from nested Zalo response envelopes."""
    data = response.get("data")
    if isinstance(data, list): return data # Some endpoints return list directly
    return data.get(key, []) if isinstance(data, dict) else []
 
# ──────────────────────────────────────────────────────────────────────────────
# Outbound builders (our domain → Zalo)
# ──────────────────────────────────────────────────────────────────────────────
 
def build_get_list_recent_chat_params(offset: int = 0, count: int = 10) -> dict:
    return {"data" : {"offset": offset, "count": count}}

def build_get_conversation_params(user_id: str, offset: int = 0, count: int = 10) -> dict:
    return {"data" : {"offset": offset, "count": count, "user_id": user_id}}
 
def build_post_send_message_payload(user_id: str, text: str) -> dict:
    """Plain-text outbound message payload for V3 API."""
    return {
        "recipient": {"user_id": user_id},
        "message":   {"text": text},
    }