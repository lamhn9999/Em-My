"""
booking_agent.py
─────────────────────────────────────────────────────────────────────────────
Multi-agent webhook-driven booking agent.

Message flow:
  Zalo webhook → buffer (debounce) → SafetyAgent → IntentClassifier → router
    ├── Type 0  ABUSE        → reply & stop
    ├── Type 1  INFO         → CustomerSupportAgent
    ├── Type 2  AVAILABILITY → AvailabilityAgent
    ├── Type 3  BOOKING      → LLM extract → BookingHandler → [NegotiationAgent]
    ├── Type 4  UPDATE       → LLM extract → UpdateAgent
    ├── Type 5  CANCELLATION → CancellationAgent → WaitlistAgent.notify
    ├── Type 6  GREETING     → CustomerSupportAgent
    └── Type 7  OTHER        → FallbackAgent

Flask is synchronous; the async agent runs in a background event loop thread.
Webhook handlers dispatch via asyncio.run_coroutine_threadsafe() and return
200 immediately.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
from pathlib import Path

import unicodedata

import ngrok
from dotenv import load_dotenv
from flask import Flask, Response, abort, request, send_from_directory

from config.business import seed_business_config
from core.schedule_report import generate_report
from data.backends.sqlite import Database
from data.models import BookingData, BookingIntent, BookingStatus, MessageType
from services.chat_history_store import ChatHistoryStore
from services.zalo_message_sync import ZaloMessageSync
from services.llm_service import IntentExtractionChain
from core.validator import BookingValidator
from core.agents import (
    SafetyAgent,
    IntentClassifier,
    CustomerSupportAgent,
    AvailabilityAgent,
    BookingHandler,
    NegotiationAgent,
    UpdateAgent,
    CancellationAgent,
    WaitlistAgent,
    FallbackAgent,
)
from core.scheduler import ScheduleResult
from services import zalo_api as api

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("BookingAgent")

# How long to wait after the last message before flushing (seconds).
DEBOUNCE_SEC = 10


class BookingAgent:
    _SEEN_IDS_MAX = 5_000

    def __init__(
        self,
        store: ChatHistoryStore,
        sync_service: ZaloMessageSync,
        chain: IntentExtractionChain,
        validator: BookingValidator,
        # Specialized agents
        safety: SafetyAgent,
        classifier: IntentClassifier,
        support: CustomerSupportAgent,
        availability: AvailabilityAgent,
        booking_handler: BookingHandler,
        negotiation: NegotiationAgent,
        update: UpdateAgent,
        cancellation: CancellationAgent,
        waitlist: WaitlistAgent,
        fallback: FallbackAgent,
    ):
        self.store      = store
        self.sync       = sync_service
        self.chain      = chain
        self.validator  = validator

        self._safety      = safety
        self._classifier  = classifier
        self._support     = support
        self._availability = availability
        self._booking     = booking_handler
        self._negotiation = negotiation
        self._update      = update
        self._cancellation = cancellation
        self._waitlist    = waitlist
        self._fallback    = fallback

        self._seen_msg_ids: set[str] = set()
        self._buffers:  dict[str, list[str]] = {}
        self._timers:   dict[str, asyncio.TimerHandle] = {}
        # Alternatives offered during negotiation, keyed by user_id.
        self._pending_alternatives: dict[str, list[ScheduleResult]] = {}
        # Bookings scheduled but awaiting explicit customer confirmation.
        # Value: (BookingData with steps already patched, ScheduleResult)
        self._awaiting_confirmation: dict[str, tuple[BookingData, ScheduleResult]] = {}

    # ── Webhook entry point ───────────────────────────────────────────────────

    async def handle_webhook_event(self, payload: dict) -> None:
        event_name = payload.get("event_name")
        if event_name != "user_send_text":
            log.info("⏭️  [Ignored] Unhandled event type: %r", event_name)
            return

        msg_id: str | None = payload.get("message", {}).get("msg_id")
        if msg_id:
            if msg_id in self._seen_msg_ids:
                return
            self._seen_msg_ids.add(msg_id)
            if len(self._seen_msg_ids) > self._SEEN_IDS_MAX:
                evict = list(self._seen_msg_ids)[: self._SEEN_IDS_MAX // 2]
                self._seen_msg_ids.difference_update(evict)

        user_id: str = payload["sender"]["id"]
        text: str    = payload["message"]["text"]

        log.info("📥 [Buffering] from %s: %s", user_id[:8], text)

        await self.store.append_message(
            sender_id=user_id,
            recipient_id=self.store.oa_id,
            text=text,
            sender_role="user",
            recipient_role="assistant",
            synced_from_api=False,
        )

        self._buffers.setdefault(user_id, []).append(text)

        existing = self._timers.pop(user_id, None)
        if existing:
            existing.cancel()

        loop = asyncio.get_event_loop()
        handle = loop.call_later(
            DEBOUNCE_SEC,
            lambda uid=user_id: asyncio.ensure_future(self._flush(uid)),
        )
        self._timers[user_id] = handle

    # ── Debounce flush ────────────────────────────────────────────────────────

    async def _flush(self, user_id: str) -> None:
        self._timers.pop(user_id, None)
        texts = self._buffers.pop(user_id, [])
        if not texts:
            return
        combined = "\n".join(texts)
        log.info("⏱️  [Flushing] %d message(s) for %s", len(texts), user_id[:8])
        await self._process_message(user_id, combined)

    # ── Core routing ──────────────────────────────────────────────────────────

    async def _process_message(self, user_id: str, text: str) -> None:
        log.info("⚙️  [Processing] %s", user_id[:8])

        # ── 0. Confirmation gate ──────────────────────────────────────────────
        # If this user has a scheduled booking awaiting explicit confirmation,
        # intercept the reply before the classifier sees it.
        if user_id in self._awaiting_confirmation:
            norm = _strip_diacritics(text.strip().lower())
            _YES = {"xac nhan", "yes", "ok", "oke", "dong y", "co",
                    "chac chan", "dung"}
            _NO  = {"huy", "khong", "no", "cancel", "thoi", "bo"}
            if norm in _YES:
                await self._confirm_awaiting(user_id)
                return
            if norm in _NO:
                await self.store.cancel_active_booking(user_id)
                self._awaiting_confirmation.pop(user_id, None)
                await self._send_reply(user_id, "Đã huỷ đặt lịch. Bạn có thể đặt lại bất cứ lúc nào nhé!")
                return
            # Anything else while awaiting → re-show the confirmation prompt
            _, pending_result = self._awaiting_confirmation[user_id]
            active_bk = await self.store.get_active_booking(user_id)
            if active_bk:
                await self._send_reply(user_id, _confirmation_prompt(active_bk, pending_result))
            return

        # ── 0b. Abort shortcut ────────────────────────────────────────────────
        if _strip_diacritics(text.strip().lower()) in ("huy", "skip", "thoat"):
            await self.store.cancel_active_booking(user_id)
            await self._send_reply(
                user_id,
                "Đã huỷ tiến trình đặt lịch. Bạn có thể bắt đầu lại bất cứ lúc nào.",
            )
            return

        # ── 0b. Negotiation alternative selection ─────────────────────────────
        # When the user has been presented a numbered list of alternatives,
        # a bare digit ("1", "2", "3"…) picks that option — no LLM needed.
        if user_id in self._pending_alternatives:
            stripped = text.strip()
            if re.match(r'^[1-6]$', stripped):
                await self._apply_alternative(user_id, int(stripped))
                return

        # ── 1. Safety check ───────────────────────────────────────────────────
        is_safe, safety_reply = await self._safety.check(user_id, text)
        if not is_safe:
            await self._send_reply(user_id, safety_reply)
            return

        # ── 2. Intent classification ──────────────────────────────────────────
        msg_type = await self._classifier.classify(text)
        log.info("🏷️  [Intent] %s → %s", user_id[:8], msg_type.name)

        # ── 2b. Context gate — override classifier for booking continuations ──
        # When the user has an active PENDING booking with missing required fields,
        # any message that isn't an explicit BOOKING/UPDATE/CANCELLATION should be
        # treated as a data-supply continuation (name, phone, date, time, etc.).
        # This handles: bare names ("lam"), phone numbers ("0912345678"),
        # natural answers ("mình tên Lam"), etc. that the classifier misfires on.
        if msg_type in (MessageType.OTHER, MessageType.GREETING, MessageType.INFO):
            active_peek = await self.store.get_active_booking(user_id)
            if active_peek and active_peek.status == BookingStatus.PENDING:
                _required = ["name", "service", "date", "time", "phone"]
                _missing  = [f for f in _required if not getattr(active_peek, f, None)]
                if _missing:
                    log.info(
                        "🔀 [Context gate] Pending booking missing %s → BOOKING continuation",
                        _missing,
                    )
                    msg_type = MessageType.BOOKING

        # ── 3. Route ──────────────────────────────────────────────────────────
        if msg_type == MessageType.GREETING:
            profile = await self.store._db.get_profile(user_id)
            name = profile.name if profile else ""
            reply = await self._support.handle_greeting(name)
            await self._send_reply(user_id, reply)

        elif msg_type == MessageType.INFO:
            reply = await self._support.handle_info_query(text)
            await self._send_reply(user_id, reply)

        elif msg_type == MessageType.AVAILABILITY:
            await self._handle_availability(user_id, text)

        elif msg_type == MessageType.BOOKING:
            await self._handle_booking(user_id, text)

        elif msg_type == MessageType.UPDATE:
            await self._handle_update(user_id, text)

        elif msg_type == MessageType.CANCELLATION:
            await self._handle_cancellation(user_id, text)

        else:
            # Type 0 (ABUSE already handled by safety), Type 7 (OTHER)
            reply = await self._fallback.handle(text)
            await self._send_reply(user_id, reply)

    # ── Type 2: Availability ──────────────────────────────────────────────────

    async def _handle_availability(self, user_id: str, text: str) -> None:
        # Use LLM to extract date/service/staff from the query
        history_text = await self._build_history_text(user_id)
        data = self.chain.extract(history_text)

        preferred_staff_id: str | None = None
        if data.preferred_staff:
            all_staff = await self.store._db.list_staff()
            for s in all_staff:
                if s.name.lower() == data.preferred_staff.lower() or s.id == data.preferred_staff:
                    preferred_staff_id = s.id
                    break

        if data.date:
            reply = await self._availability.handle(
                date=data.date,
                service_name=data.service,
                preferred_staff_id=preferred_staff_id,
            )
        else:
            reply = await self._availability.handle_upcoming_slots(days_ahead=3)

        await self._send_reply(user_id, reply)

    # ── Type 3: Booking ───────────────────────────────────────────────────────

    async def _handle_booking(self, user_id: str, text: str) -> None:
        active = await self.store.get_active_booking(user_id)
        if not active:
            await self.store.start_booking(user_id, BookingIntent.BOOKING)
            # Starting a fresh booking clears any stale negotiation alternatives
            self._pending_alternatives.pop(user_id, None)

        history_text = await self._build_history_text(user_id)
        data = self.chain.extract(history_text)
        data.message_type = MessageType.BOOKING.value

        # Patch active booking with extracted fields
        patch = {
            k: getattr(data, k)
            for k in ["intent", "name", "service", "date", "time", "phone",
                      "confidence", "denial_reason", "preferred_staff"]
            if getattr(data, k) is not None
        }
        patch["message_type"] = data.message_type
        await self.store.update_active_booking(user_id, patch)

        active = await self.store.get_active_booking(user_id)
        is_valid, reason = await self.validator.validate(active)

        if not is_valid:
            # Missing info — ask for it
            log.info("🟡 [Incomplete] %s: %s", user_id[:8], reason)
            await self._send_reply(user_id, reason)
            return

        # All fields present — run scheduler
        result, sched_err = await self._booking.handle(active)

        if result and result.success:
            # Scheduler found a valid slot — persist scheduling output to DB
            # (so subsequent bookings see resource assignments) but do NOT
            # confirm yet; wait for the customer's explicit "xác nhận".
            active.steps_schedule     = result.steps
            active.assigned_resources = list({s.resource_id for s in result.steps if s.resource_type != "wait"})
            active.duration_minutes   = result.total_duration
            active.time               = result.start_time
            await self.store.update_active_booking(user_id, {
                "time":               active.time,
                "duration_minutes":   active.duration_minutes,
                "steps_schedule":     active.steps_schedule,
                "assigned_resources": active.assigned_resources,
            })
            staff_names = await self._resolve_staff_names(result)
            # Notify skill-mismatch substitution before showing the summary
            if result.staff_warning:
                await self._send_reply(user_id, f"ℹ️ {result.staff_warning}")
            # Park in confirmation state
            self._awaiting_confirmation[user_id] = (active, result)
            await self._send_reply(user_id, _confirmation_prompt(active, result, staff_names))

        else:
            # Preferred slot unavailable → negotiate
            log.info("🔄 [Negotiating] for %s: %s", user_id[:8], sched_err)
            alternatives, neg_reply = await self._negotiation.negotiate(active, sched_err)

            if alternatives:
                self._pending_alternatives[user_id] = alternatives
                await self._send_reply(user_id, neg_reply)
            else:
                # No alternatives — offer waitlist
                await self._send_reply(user_id, neg_reply)
                if active.date and active.service:
                    profile = await self.store._db.get_profile(user_id)
                    name = profile.name if profile else ""
                    entry = await self._waitlist.add(
                        client_id=user_id,
                        client_name=name,
                        service=active.service,
                        preferred_date=active.date,
                        preferred_time=active.time,
                        preferred_staff=active.preferred_staff,
                    )
                    await self._send_reply(user_id, self._waitlist.waitlist_reply(entry))

    # ── Type 4: Update ────────────────────────────────────────────────────────

    async def _handle_update(self, user_id: str, text: str) -> None:
        # Capture the current booking before any changes — used later for
        # waitlist notification (the old slot may be freed by this update).
        old_booking = await self.store._db.get_active_booking(user_id)
        if old_booking is None:
            old_booking = await self.store._db.get_last_confirmed_booking(user_id)

        history_text = await self._build_history_text(user_id)
        data = self.chain.extract(history_text)

        patch = {
            k: getattr(data, k)
            for k in ["date", "time", "service", "preferred_staff", "notes"]
            if getattr(data, k) is not None
        }
        patch["message_type"] = MessageType.UPDATE.value

        updated, reply = await self._update.handle(user_id, patch)
        await self._send_reply(user_id, reply)

        # If the date or time changed, the old slot is freed — notify waitlist
        if updated and old_booking:
            if old_booking.date != updated.date or old_booking.time != updated.time:
                notifications = await self._waitlist.notify_on_cancellation(old_booking)
                for notif_user_id, notif_msg in notifications:
                    await self._send_reply(notif_user_id, notif_msg)

    # ── Type 5: Cancellation ──────────────────────────────────────────────────

    async def _handle_cancellation(self, user_id: str, text: str) -> None:
        # Extract date/service hints from the message so we cancel the right booking
        # when a customer has multiple upcoming confirmed bookings.
        history_text = await self._build_history_text(user_id)
        data = self.chain.extract(history_text)

        cancelled, reply = await self._cancellation.handle(
            user_id,
            hint_date=data.date,
            hint_service=data.service,
        )
        await self._send_reply(user_id, reply)

        if cancelled:
            # Notify waitlist customers whose slot just opened
            notifications = await self._waitlist.notify_on_cancellation(cancelled)
            for notif_user_id, notif_msg in notifications:
                await self._send_reply(notif_user_id, notif_msg)

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _build_history_text(self, user_id: str) -> str:
        raw_history = await self.store.as_llm_context(user_id, last_n=8)
        lines = []
        for m in raw_history:
            role_label = "Khách hàng" if m["role"] == "user" else "Trợ lý Salon"
            lines.append(f"{role_label}: {m['content']}")
        return "\n".join(lines)

    async def _apply_alternative(self, user_id: str, choice: int) -> None:
        """Apply a numbered alternative from a previous negotiation round."""
        alternatives = self._pending_alternatives.get(user_id, [])
        if not alternatives:
            await self._send_reply(user_id, "Hết thời hạn chọn lịch. Bạn vui lòng đặt lịch lại nhé!")
            return
        if choice < 1 or choice > len(alternatives):
            await self._send_reply(
                user_id,
                f"Vui lòng chọn số từ **1** đến **{len(alternatives)}** ạ."
            )
            return

        selected = alternatives[choice - 1]
        active = await self.store.get_active_booking(user_id)
        if not active:
            await self._send_reply(user_id, "Không tìm thấy lịch đặt nào đang chờ xác nhận.")
            self._pending_alternatives.pop(user_id, None)
            return

        # Patch the booking with the selected schedule
        active.steps_schedule     = selected.steps
        active.assigned_resources = list({s.resource_id for s in selected.steps if s.resource_type != "wait"})
        active.duration_minutes   = selected.total_duration
        active.time               = selected.start_time

        await self.store.update_active_booking(user_id, {
            "time":               active.time,
            "duration_minutes":   active.duration_minutes,
            "steps_schedule":     active.steps_schedule,
            "assigned_resources": active.assigned_resources,
        })
        self._pending_alternatives.pop(user_id, None)

        staff_names = await self._resolve_staff_names(selected)
        if selected.staff_warning:
            await self._send_reply(user_id, f"ℹ️ {selected.staff_warning}")

        # Park in confirmation state instead of auto-confirming
        self._awaiting_confirmation[user_id] = (active, selected)
        await self._send_reply(user_id, _confirmation_prompt(active, selected, staff_names))

    async def _confirm_awaiting(self, user_id: str) -> None:
        """Commit the parked booking after the customer says 'xác nhận'."""
        booking, result = self._awaiting_confirmation.pop(user_id)
        await self.store.confirm_active_booking(user_id)
        # staff_warning already shown before the confirmation prompt — do not repeat
        await self._send_success_reply(user_id, booking, result)

        try:
            report_path = await generate_report(self.store._db, booking.date)
            log.info("📊 [Report] Schedule snapshot → %s", report_path)
        except Exception as exc:
            log.warning("⚠️  [Report] Failed to generate schedule report: %s", exc)

    async def _resolve_staff_names(self, result: ScheduleResult) -> list[str]:
        """Return display names for all unique stylists in step order."""
        if not result or not result.steps:
            return []
        all_staff = await self.store._db.list_staff()
        seen: dict[str, str] = {}  # resource_id → name, preserves insertion order
        for step in result.steps:
            if step.resource_type == "stylist" and step.resource_id not in seen:
                s_obj = next((s for s in all_staff if s.id == step.resource_id), None)
                seen[step.resource_id] = s_obj.name if s_obj else step.resource_id
        return list(seen.values())

    async def _send_success_reply(self, user_id: str, booking: BookingData, result=None) -> None:
        staff_names = await self._resolve_staff_names(result) if result else []

        end_time = result.end_time if result else ""
        time_line = f"🕐 Giờ vào salon: {booking.time}"
        if end_time:
            time_line += f" – {end_time}"
        time_line += f" (~{booking.duration_minutes} phút)"

        staff_line = f"💅 Nhân viên: {', '.join(staff_names)}\n" if staff_names else ""

        msg = (
            f"✅ Đặt lịch thành công!\n"
            f"👤 Khách hàng: {booking.name}\n"
            f"💈 Dịch vụ: {booking.service}\n"
            f"📅 Ngày: {booking.date}\n"
            f"{time_line}\n"
            + staff_line
            + f"\nCảm ơn bạn đã đặt lịch! 🙏"
        )
        await self._send_reply(user_id, msg)

    async def _send_reply(self, user_id: str, text: str) -> None:
        log.info("📤 [Reply] to %s: %s", user_id[:8], text[:80].replace("\n", " "))
        url     = api.Endpoint.POST_SEND_MESSAGE
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
                synced_from_api=False,
            )
        except Exception as exc:
            log.error("❌ [Error] Failed to send message to %s: %s", user_id, exc)


# ── Module-level helpers ───────────────────────────────────────────────────────

def _strip_diacritics(text: str) -> str:
    """Remove Vietnamese (and other) diacritics so 'hủy' == 'huỷ' == 'huy'."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(c)
    )


def _confirmation_prompt(
    booking: BookingData,
    result: ScheduleResult,
    staff_names: list[str] | None = None,
) -> str:
    """Format a booking summary and ask the customer to confirm or cancel.

    staff_names: resolved display names for all assigned stylists (in step order,
                 de-duplicated). Pass None to fall back to booking.preferred_staff.
    """
    end_time = result.end_time if result else ""
    duration = booking.duration_minutes or (result.total_duration if result else 0)
    time_line = f"🕐 Giờ: {booking.time}"
    if end_time:
        time_line += f" – {end_time}"
    if duration:
        time_line += f" (~{duration} phút)"

    lines = [
        "📋 **Thông tin đặt lịch của bạn:**\n",
        f"👤 Tên: {booking.name or '(chưa có)'}",
        f"💈 Dịch vụ: {booking.service or '(chưa có)'}",
        f"📅 Ngày: {booking.date or '(chưa có)'}",
        time_line,
    ]

    # Show actual assigned stylist(s), not the preferred_staff field which may be substituted
    if staff_names:
        lines.append(f"✂️  Nhân viên: {', '.join(staff_names)}")
    elif booking.preferred_staff:
        lines.append(f"✂️  Nhân viên: {booking.preferred_staff}")

    lines.append(
        "\nBạn có muốn **xác nhận** đặt lịch này không?\n"
        "Nhắn **xác nhận** để đặt, hoặc **huỷ** để bỏ qua."
    )
    return "\n".join(lines)


# ── Flask app ──────────────────────────────────────────────────────────────────

def build_flask_app(agent: BookingAgent, loop: asyncio.AbstractEventLoop) -> Flask:
    app = Flask(__name__)
    static_dir = Path(__file__).parent.parent / "static"

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


# ── Bootstrap ──────────────────────────────────────────────────────────────────

async def bootstrap():
    db = Database()
    await db.connect()

    # Seed staff, stations, services on first run (idempotent)
    await seed_business_config(db)

    store = ChatHistoryStore(db, oa_id=os.getenv("ZALOOA_ID"))
    await store.init()

    sync_service = ZaloMessageSync(
        access_token=os.getenv("ZALOOA_ACCESS_TOKEN"),
        history_store=store,
    )
    chain     = IntentExtractionChain()
    validator = BookingValidator(store=store)

    # Specialized agents (share the DB; no separate processes needed)
    safety       = SafetyAgent(db)
    classifier   = IntentClassifier(llm=chain.llm)
    support      = CustomerSupportAgent()
    availability = AvailabilityAgent(db)
    bk_handler   = BookingHandler(db)
    negotiation  = NegotiationAgent(db)
    update       = UpdateAgent(db)
    cancellation = CancellationAgent(db)
    waitlist     = WaitlistAgent(db)
    fallback     = FallbackAgent()

    agent = BookingAgent(
        store=store,
        sync_service=sync_service,
        chain=chain,
        validator=validator,
        safety=safety,
        classifier=classifier,
        support=support,
        availability=availability,
        booking_handler=bk_handler,
        negotiation=negotiation,
        update=update,
        cancellation=cancellation,
        waitlist=waitlist,
        fallback=fallback,
    )
    return agent, sync_service, db


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    load_dotenv(Path(__file__).parent.parent / ".env")

    port = int(os.getenv("PORT", 5000))

    loop = asyncio.new_event_loop()
    agent, sync_service, db = loop.run_until_complete(bootstrap())

    listener = ngrok.forward(port, authtoken=os.getenv("NGROK_AUTH_TOKEN"))
    log.info("🌐 Ngrok tunnel active: %s/webhook", listener.url())

    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()

    log.info("🚀 Multi-Agent Booking System ready — buffering %ds after last message", DEBOUNCE_SEC)

    app = build_flask_app(agent, loop)

    try:
        app.run(host="0.0.0.0", port=port, use_reloader=False)
    except KeyboardInterrupt:
        log.info("Shutting down...")
    finally:
        asyncio.run_coroutine_threadsafe(sync_service.close(), loop).result(timeout=5)
        asyncio.run_coroutine_threadsafe(db.close(), loop).result(timeout=5)
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        log.info("Goodbye.")


if __name__ == "__main__":
    main()
