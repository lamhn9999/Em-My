"""
booking_agent.py
-------------
Webhook-driven booking agent. Receives Zalo events via POST /webhook,
processes booking intents statefully, and replies via the Zalo API.

Replicates the original polling behaviour: messages from the same user
are buffered for DEBOUNCE_SEC seconds. If another message arrives within
that window the timer resets. When the timer fires all buffered texts are
joined with "\n" and fed to _process_message as one unit — identical to
how _tick() grouped messages across a 10-second poll interval.

Flask is synchronous, so the async agent runs in a dedicated background
event loop thread. Webhook handlers dispatch into it via
asyncio.run_coroutine_threadsafe() and return 200 immediately.

Run:  python booking_agent.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
from pathlib import Path

import ngrok
from dotenv import load_dotenv
from flask import Flask, Response, abort, request, send_from_directory

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

# How long to wait after the last message before flushing, in seconds.
# Mirrors the original 10-second poll interval.
DEBOUNCE_SEC = 10


class BookingAgent:
    # Simple in-memory dedup guard for msg_id values.
    _SEEN_IDS_MAX = 5_000

    def __init__(
        self,
        store: ChatHistoryStore,
        sync_service: ZaloMessageSync,
        chain: IntentExtractionChain,
        validator: BookingValidator,
    ):
        self.store = store
        self.sync = sync_service
        self.chain = chain
        self.validator = validator

        self._seen_msg_ids: set[str] = set()

        # Per-user debounce state (all access is on the async event loop thread)
        # { user_id -> [text, ...] }
        self._buffers: dict[str, list[str]] = {}
        # { user_id -> asyncio.TimerHandle }
        self._timers: dict[str, asyncio.TimerHandle] = {}

    # ── Webhook entry point ───────────────────────────────────────────────────

    async def handle_webhook_event(self, payload: dict) -> None:
        """
        Receive one Zalo webhook event.

        Immediately persists the raw message, then arms/resets a per-user
        debounce timer. When the timer fires it flushes the buffer to
        _process_message exactly once — matching the original _tick() grouping.
        """
        event_name = payload.get("event_name")
        if event_name != "user_send_text":
            log.info(f"⏭️  [Ignored] Unhandled event type: {event_name!r}")
            return

        # Dedup by msg_id
        msg_id: str | None = payload.get("message", {}).get("msg_id")
        if msg_id:
            if msg_id in self._seen_msg_ids:
                log.debug(f"[Dedup] Already processed msg_id={msg_id!r}, skipping.")
                return
            self._seen_msg_ids.add(msg_id)
            if len(self._seen_msg_ids) > self._SEEN_IDS_MAX:
                evict = list(self._seen_msg_ids)[: self._SEEN_IDS_MAX // 2]
                self._seen_msg_ids.difference_update(evict)

        user_id: str = payload["sender"]["id"]
        text: str = payload["message"]["text"]

        log.info(f"📥 [Buffering] from {user_id[:8]}... : {text}")

        # Persist immediately so as_llm_context always has the full history
        await self.store.append_message(
            sender_id=user_id,
            recipient_id=self.store.oa_id,
            text=text,
            sender_role="user",
            recipient_role="assistant",
            synced_from_api=False,
        )

        # Accumulate text in the per-user buffer
        self._buffers.setdefault(user_id, []).append(text)

        # Cancel any existing timer for this user and start a fresh one
        existing = self._timers.pop(user_id, None)
        if existing:
            existing.cancel()

        loop = asyncio.get_event_loop()
        handle = loop.call_later(
            DEBOUNCE_SEC,
            lambda uid=user_id: asyncio.ensure_future(self._flush(uid))
        )
        self._timers[user_id] = handle

    # ── Debounce flush ────────────────────────────────────────────────────────

    async def _flush(self, user_id: str) -> None:
        """
        Timer callback: join all buffered texts exactly as _tick() did and
        hand them to _process_message as one combined string.
        """
        self._timers.pop(user_id, None)
        texts = self._buffers.pop(user_id, [])
        if not texts:
            return

        # Identical to _tick(): combined_text = "\n".join(texts)
        combined_text = "\n".join(texts)
        log.info(f"⏱️  [Flushing] {len(texts)} message(s) for {user_id[:8]}...")
        await self._process_message(user_id, combined_text)

    # ── Core processing (unchanged from original) ─────────────────────────────

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
        print(active_booking)

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

            else:  # empty_schedule
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


# ── Flask app ─────────────────────────────────────────────────────────────────

def build_flask_app(agent: BookingAgent, loop: asyncio.AbstractEventLoop) -> Flask:
    app = Flask(__name__)
    static_dir = Path(__file__).parent / "static"

    @app.post("/webhook")
    def receive_webhook():
        payload = request.get_json(silent=True)
        if not payload:
            abort(400)
        asyncio.run_coroutine_threadsafe(agent.handle_webhook_event(payload), loop)
        return Response("OK", status=200)

    @app.get("/webhook/<filename>")
    def serve_verification(filename: str):
        if not (static_dir / filename).exists():
            abort(404)
        return send_from_directory(static_dir, filename)

    return app


# ── Bootstrap ─────────────────────────────────────────────────────────────────

async def bootstrap():
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
    return agent, sync_service, db


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    load_dotenv(Path(__file__).parent.parent / ".env")

    port = int(os.getenv("PORT", 5000))

    loop = asyncio.new_event_loop()
    agent, sync_service, db = loop.run_until_complete(bootstrap())

    listener = ngrok.forward(port, authtoken=os.getenv("NGROK_AUTH_TOKEN"))
    log.info(f"🌐 Ngrok tunnel active: {listener.url()}/webhook")

    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    log.info(f"🚀 Booking Agent ready — buffering for {DEBOUNCE_SEC}s after last message...")

    app = build_flask_app(agent, loop)

    try:
        app.run(host="0.0.0.0", port=port, use_reloader=False)
    except KeyboardInterrupt:
        log.info("Shutting down cleanly...")
    finally:
        asyncio.run_coroutine_threadsafe(sync_service.close(), loop).result(timeout=5)
        asyncio.run_coroutine_threadsafe(db.close(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        log.info("Goodbye.")


if __name__ == "__main__":
    main()