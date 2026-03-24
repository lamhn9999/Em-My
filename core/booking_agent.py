"""
booking_agent.py
-------------
Background daemon that polls Zalo for new messages every 10 seconds,
processes booking intents statefully, and replies via the Zalo API.
"""
from __future__ import annotations

import time
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

from data.backends.sqlite import Database
from data.models import BookingData, BookingIntent
from services.chat_history_store import ChatHistoryStore
from services.zalo_message_sync import ZaloMessageSync
from services.llm_service import IntentExtractionChain
from core.validator import BookingValidator
from services import zalo_api as api

# Clean console logging configuration
logging.basicConfig(
    level=logging.INFO, 
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("BookingAgent")

class BookingAgent:
    def __init__(
        self, 
        store: ChatHistoryStore, 
        sync_service: ZaloMessageSync, 
        chain: IntentExtractionChain, 
        validator: BookingValidator
    ):
        self.store = store
        self.sync = sync_service
        self.chain = chain
        self.validator = validator
        self.processed_msgs = set()

        self._preload_processed_messages()

    def _preload_processed_messages(self):
        """Fetches recent history once on startup to avoid processing old messages."""
        log.info("Pre-loading recent conversations to bypass historical messages...")
        self.sync.sync_all_recent(count=10)
        
        params = api.build_get_list_recent_chat_params(offset=0, count=10)
        resp = self.sync._client.get(api.Endpoint.GET_LIST_RECENT_CHAT, params=params).json()
        
        for convo in api.unwrap_list(resp, "list_recent_chat"):
            parsed = api.parse_message(convo, self.sync._oa_id)
            user_id = parsed.from_id if parsed.src == api.MessageSrc.CLIENT else parsed.to_id
            
            if user_id:
                for msg in self.store.get_history(user_id, last_n=20):
                    self.processed_msgs.add(msg.msg_id)
                    
        log.info(f"Ignored {len(self.processed_msgs)} historical messages. Ready.")

    def run(self, interval_sec: int = 10):
        """Starts the infinite polling loop."""
        log.info(f"🚀 Started Auto Booking Agent (Polling every {interval_sec}s)...")
        while True:
            try:
                self._tick()
            except Exception as e:
                log.error(f"Tick error: {e}")
            time.sleep(interval_sec)

    def _tick(self):
        """One cycle of syncing and processing."""
        self.sync.sync_all_recent(count=10)
        
        params = api.build_get_list_recent_chat_params(offset=0, count=10)
        resp = self.sync._client.get(api.Endpoint.GET_LIST_RECENT_CHAT, params=params).json()
        
        for convo in api.unwrap_list(resp, "list_recent_chat"):
            parsed = api.parse_message(convo, self.sync._oa_id)
            user_id = parsed.from_id if parsed.src == api.MessageSrc.CLIENT else parsed.to_id
            
            if not user_id: 
                continue
            
            history = self.store.get_history(user_id, last_n=5)
            for msg in reversed(history):
                if msg.sender_role == "user" and msg.msg_id not in self.processed_msgs:
                    self.processed_msgs.add(msg.msg_id)
                    self._process_message(user_id, msg.text)

    def _process_message(self, user_id: str, text: str):
        """Evaluates a single new message and routes it through the LLM & Validator."""
        log.info(f"📥 [New Message] from {user_id[:8]}... : {text}")
        log.info(f"⚙️  [Processing] Analyzing intent for {user_id[:8]}...")
        
        # Abort/Skip command check
        if text.strip().lower() in ("huỷ", "huy", "skip", "thoát"):
            log.info(f"⚠️  [Aborted] User {user_id[:8]}... aborted booking.")
            self._send_reply(user_id, "Đã huỷ tiến trình đặt lịch hiện tại. Bạn có thể bắt đầu lại bất cứ lúc nào.")
            return

        active_booking = self.store.get_active_booking(user_id)

        if active_booking:
            synthetic_context = (
                f"Đã biết -> Tên: {active_booking.name}, Dịch vụ: {active_booking.service}, "
                f"Ngày: {active_booking.date}, Giờ: {active_booking.time}. "
                f"Khách vừa nhắn thêm (cập nhật thông tin trên): '{text}'"
            )
            data = self.chain.extract(synthetic_context)
            
            patch = {
                "name": data.name or active_booking.name,
                "service": data.service or active_booking.service,
                "date": data.date or active_booking.date,
                "time": data.time or active_booking.time,
                "phone": data.phone or active_booking.phone,
                "confidence": data.confidence,         
                "denial_reason": data.denial_reason   
            }
            self.store.update_active_booking(user_id, patch)
            
        else:
            data = self.chain.extract(text)
            if data.intent != BookingIntent.BOOKING:
                log.info(f"⏭️  [Ignored] Message from {user_id[:8]}... is not a booking intent.")
                return
                
            self.store.start_booking(user_id, data.intent)
            patch = {
                "name": data.name,
                "service": data.service,
                "date": data.date,
                "time": data.time,
                "phone": data.phone,
                "confidence": data.confidence,          
                "denial_reason": data.denial_reason   
            }
            self.store.update_active_booking(user_id, patch)

        updated_active = self.store.get_active_booking(user_id)
        is_valid, reason = self.validator.validate(updated_active)
        
        if is_valid:
            self.store.confirm_active_booking(user_id)
            self._send_success_reply(user_id, updated_active)
        else:
            log.info(f"🟡 [Incomplete] Missing info for {user_id[:8]}... requesting more data.")
            self._send_reply(user_id, reason)

    def _send_success_reply(self, user_id: str, booking: BookingData):
        """Formats and sends the final confirmation receipt."""
        dt_str = f"{booking.date} {booking.time}"
        msg = (
            f"✅ Đặt lịch thành công!\n"
            f"👤 Khách hàng: {booking.name}\n"
            f"💆 Dịch vụ: {booking.service}\n"
            f"🕐 Thời gian: {dt_str} ({booking.duration_minutes} phút)\n\n"
            f"Cảm ơn bạn đã đặt lịch!"
        )
        self._send_reply(user_id, msg)

    def _send_reply(self, user_id: str, text: str):
        """Sends a message via the Zalo API and saves it to local history."""
        log.info(f"📤 [Sending Reply] to {user_id[:8]}... : {text.replace(chr(10), ' ')}")
        
        url = api.Endpoint.POST_SEND_MESSAGE
        payload = api.build_post_send_message_payload(user_id, text)
        
        try:
            resp = self.sync._client.post(url, json=payload)
            resp.raise_for_status()
            
            self.store.append_message(
                sender_id=self.store.oa_id,
                recipient_id=user_id,
                text=text,
                sender_role="assistant",
                recipient_role="user",
                synced_from_api=False
            )
            log.info(f"✅ [Responded] Successfully sent message to {user_id[:8]}...")
        except Exception as e:
            log.error(f"❌ [Error] Failed to send Zalo message to {user_id}: {e}")


def main():
    load_dotenv(Path(__file__).parent.parent / ".env")
    
    db = Database()
    store = ChatHistoryStore(db, oa_id=os.getenv("ZALOOA_ID"))
    
    sync_service = ZaloMessageSync(
        access_token=os.getenv("ZALOOA_ACCESS_TOKEN"), 
        history_store=store
    )
    chain = IntentExtractionChain()
    validator = BookingValidator(store=store)
    
    agent = BookingAgent(store, sync_service, chain, validator)
    
    try:
        agent.run(interval_sec=10)
    except KeyboardInterrupt:
        log.info("Shutting down cleanly...")
    finally:
        sync_service.close()