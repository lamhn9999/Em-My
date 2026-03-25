"""
booking_agent.py
-------------
Background daemon that polls Zalo for new messages every 10 seconds,
processes booking intents statefully, and replies via the Zalo API.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from dotenv import load_dotenv

from data.backends.sqlite import Database
from data.models import BookingData, BookingIntent, BookingStatus
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

    async def _preload_processed_messages(self):
        """Fetches recent history once on startup to avoid processing old messages."""
        log.info("Pre-loading recent conversations to bypass historical messages...")
        await self.sync.sync_all_recent(count=10)
        log.info("Ignored historical messages. Ready.")

    async def run(self, interval_sec: int = 10):
        """Starts the infinite polling loop."""
        await self._preload_processed_messages()
        
        log.info(f"🚀 Started Auto Booking Agent (Polling every {interval_sec}s)...")
        while True:
            try:
                await self._tick()
            except Exception as e:
                log.error(f"Tick error: {e}")
            await asyncio.sleep(interval_sec)

    async def _tick(self):
        """One cycle of syncing and processing."""
        new_messages = await self.sync.sync_all_recent(count=10)
        
        # Group new messages by user ID
        unprocessed_by_user = {}
        for msg in new_messages:
            if msg.sender_role == "user":
                unprocessed_by_user.setdefault(msg.sender_id, []).append(msg.text)
                
        for user_id, texts in unprocessed_by_user.items():
            combined_text = "\n".join(texts)
            await self._process_message(user_id, combined_text)

    async def _process_message(self, user_id: str, text: str):
        """Evaluates a single new message and routes it through the LLM & Validator."""
        log.info(f"📥 [New Message] from {user_id[:8]}... : {text}")
        log.info(f"⚙️  [Processing] Analyzing intent for {user_id[:8]}...")
        
        # Abort/Skip command check
        if text.strip().lower() in ("huỷ", "huy", "skip", "thoát"):
            log.info(f"⚠️  [Aborted] User {user_id[:8]}... aborted booking.")
            await self.store.cancel_active_booking(user_id)
            await self._send_reply(user_id, "Đã huỷ tiến trình đặt lịch hiện tại. Bạn có thể bắt đầu lại bất cứ lúc nào.")
            return

        active_booking = await self.store.get_active_booking(user_id)

        # Generate conversational script from the chat store for pure LLM context
        raw_history_dicts = await self.store.as_llm_context(user_id, last_n=8)
        history_lines = []
        for m in raw_history_dicts:
            role_label = "Khách hàng" if m["role"] == "user" else "Trợ lý Spa"
            history_lines.append(f"{role_label}: {m['content']}")
            
        history_text = "\n".join(history_lines)

        data = self.chain.extract(history_text)
        
        if active_booking and data.intent == BookingIntent.CANCEL:
            await self.store.cancel_active_booking(user_id)
            await self._send_reply(user_id, data.denial_reason or "Đã huỷ tiến trình đặt lịch hiện tại. Bạn có thể bắt đầu lại bất cứ lúc nào.")
            return

        if data.intent == BookingIntent.QUERY:
            if data.query_type == "missing_fields":
                if active_booking:
                    missing = self.validator.get_missing_labels(active_booking)
                    if missing:
                        await self._send_reply(user_id, f"Dạ thông tin còn thiếu là: {', '.join(missing)}.")
                    else:
                        await self._send_reply(user_id, "Dạ thông tin của bạn đã đủ rồi ạ.")
                else:
                    await self._send_reply(user_id, "Bạn chưa bắt đầu đặt lịch nên không có thông tin nào bị thiếu ạ.")
            
            elif data.query_type == "upcoming_schedule":
                history = await self.store.get_bookings_for_client(user_id)
                upcoming = [b for b in history if b.status == BookingStatus.CONFIRMED and b.is_upcoming()]
                if upcoming:
                    lines = [f"- {b.service} lúc {b.time} ngày {b.date}" for b in upcoming[:3]]
                    await self._send_reply(user_id, "Lịch hẹn sắp tới của bạn:\n" + "\n".join(lines))
                else:
                    await self._send_reply(user_id, "Dạ bạn không có lịch hẹn nào sắp tới ạ.")
            
            else: # empty_schedule
                if data.date:
                    bookings = await self.store.get_confirmed_bookings_by_date(data.date)
                    if not bookings:
                        await self._send_reply(user_id, f"Ngày {data.date} hiện đang trống lịch. Bạn có thể đặt bất cứ lúc nào.")
                    else:
                        busy_times = [f"{b['time']} ({b['duration_minutes']}p)" for b in bookings if b['time']]
                        await self._send_reply(user_id, f"Ngày {data.date} spa đã có khách đặt vào: {', '.join(busy_times)}. Các giờ khác vẫn trống.")
                else:
                    await self._send_reply(user_id, data.denial_reason or "Bạn muốn hỏi lịch trống cho ngày nào ạ?")
            
            if active_booking and data.date:
                patch = {"date": data.date}
                await self.store.update_active_booking(user_id, patch)
                
            return

        if not active_booking:
            if data.intent == BookingIntent.CANCEL:
                cancelled = await self.store.cancel_last_confirmed_booking(user_id)
                if cancelled:
                    await self._send_reply(user_id, f"Đã huỷ thành công lịch đặt ngày {cancelled.date} lúc {cancelled.time}.")
                else:
                    await self._send_reply(user_id, "Bạn không có lịch đặt nào để huỷ.")
                return
            
            elif data.intent != BookingIntent.BOOKING:
                log.info(f"⏭️  [Ignored] Message from {user_id[:8]}... is not a booking intent.")
                return
            
            await self.store.start_booking(user_id, data.intent)

        # Unified patch creation gracefully skipping nulls
        patch = {
            k: getattr(data, k) 
            for k in ["intent", "name", "service", "date", "time", "phone", "confidence", "denial_reason"]
            if getattr(data, k) is not None
        }
        await self.store.update_active_booking(user_id, patch)

        updated_active = await self.store.get_active_booking(user_id)
        is_valid, reason = await self.validator.validate(updated_active)
        
        if is_valid:
            await self.store.confirm_active_booking(user_id)
            await self._send_success_reply(user_id, updated_active)
        else:
            log.info(f"🟡 [Incomplete] Missing info for {user_id[:8]}... requesting more data.")
            await self._send_reply(user_id, reason)

    async def _send_success_reply(self, user_id: str, booking: BookingData):
        """Formats and sends the final confirmation receipt."""
        dt_str = f"{booking.date} {booking.time}"
        msg = (
            f"✅ Đặt lịch thành công!\n"
            f"👤 Khách hàng: {booking.name}\n"
            f"💆 Dịch vụ: {booking.service}\n"
            f"🕐 Thời gian: {dt_str} ({booking.duration_minutes} phút)\n\n"
            f"Cảm ơn bạn đã đặt lịch!"
        )
        await self._send_reply(user_id, msg)

    async def _send_reply(self, user_id: str, text: str):
        """Sends a message via the Zalo API and saves it to local history."""
        log.info(f"📤 [Sending Reply] to {user_id[:8]}... : {text.replace(chr(10), ' ')}")
        
        url = api.Endpoint.POST_SEND_MESSAGE
        payload = api.build_post_send_message_payload(user_id, text)
        
        try:
            resp = await self.sync._client.post(url, json=payload)
            resp.raise_for_status()
            
            await self.store.append_message(
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

async def main():
    load_dotenv(Path(__file__).parent.parent / ".env")
    
    db = Database()
    await db.connect()
    
    store = ChatHistoryStore(db, oa_id=os.getenv("ZALOOA_ID"))
    await store.init()
    
    sync_service = ZaloMessageSync(
        access_token=os.getenv("ZALOOA_ACCESS_TOKEN"), 
        history_store=store
    )
    chain = IntentExtractionChain()
    validator = BookingValidator(store=store)
    
    agent = BookingAgent(store, sync_service, chain, validator)
    
    try:
        await agent.run(interval_sec=10)
    except KeyboardInterrupt:
        log.info("Shutting down cleanly...")
    except asyncio.CancelledError:
        log.info("Async loop cancelled, shutting down...")
    finally:
        await sync_service.close()
        await db.close()

if __name__ == '__main__':
    asyncio.run(main())