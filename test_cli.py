"""
test_cli.py
──────────────────────────────────────────────────────────────────────────────
Interactive REPL for testing the booking agent locally without Zalo.

Type a message and press Enter — the agent replies immediately.

Usage:
    python test_cli.py                  # persistent test DB at data/store/test.db
    python test_cli.py --fresh          # wipe test DB and start clean
    python test_cli.py --user custom_id # simulate a specific user ID

Built-in commands (prefix with /):
    /reset      — cancel active booking and clear negotiation state for this user
    /status     — show the current active booking data
    /history    — print the last 10 messages in the conversation
    /bookings   — list all confirmed bookings in the DB
    /quit       — exit
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

# ── Make sure project root is on sys.path ─────────────────────────────────────
PROJECT_ROOT = Path(__file__).parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from config.business import seed_business_config
from core.booking_agent import BookingAgent
from core.scheduler import ScheduleResult
from core.schedule_report import generate_report
from core.validator import BookingValidator
from core.agents import (
    SafetyAgent, IntentClassifier, CustomerSupportAgent,
    AvailabilityAgent, BookingHandler, NegotiationAgent,
    UpdateAgent, CancellationAgent, WaitlistAgent, FallbackAgent,
)
from data.backends.sqlite import Database
from data.models import BookingData
from services.chat_history_store import ChatHistoryStore
from services.llm_service import IntentExtractionChain

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_TEST_DB  = "data/store/test.db"
DEFAULT_USER_ID  = "test_user_001"
DEFAULT_OA_ID    = "test_oa"

# ── ANSI colours ──────────────────────────────────────────────────────────────
_RESET  = "\033[0m"
_BOLD   = "\033[1m"
_CYAN   = "\033[96m"
_GREEN  = "\033[92m"
_YELLOW = "\033[93m"
_DIM    = "\033[2m"


def _color(text: str, code: str) -> str:
    """Wrap text with an ANSI colour code if stdout is a TTY."""
    if sys.stdout.isatty():
        return f"{code}{text}{_RESET}"
    return text


# ── Stub HTTP client ─────────────────────────────────────────────────────────
class _FakeHttpResponse:
    status_code = 200
    def raise_for_status(self): pass


class _FakeHttpClient:
    """Accepts post() calls without making any network request."""
    async def post(self, url: str, *, json: dict | None = None, **kwargs) -> _FakeHttpResponse:
        return _FakeHttpResponse()

    async def aclose(self): pass


class _FakeSync:
    """Minimal stand-in for ZaloMessageSync — only the _client attr is needed."""
    def __init__(self):
        self._client = _FakeHttpClient()

    async def close(self): pass


# ── Testable agent subclass ───────────────────────────────────────────────────
class TestableBookingAgent(BookingAgent):
    """
    Overrides _send_reply so responses are captured locally instead of
    being POSTed to the Zalo API.  Replies accumulate in _reply_buffer
    and are flushed to stdout after each user turn.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._reply_buffer: list[str] = []

    async def _send_reply(self, user_id: str, text: str) -> None:
        # Store to DB (same as real agent) so the LLM sees bot messages in history
        await self.store.append_message(
            sender_id=self.store.oa_id,
            recipient_id=user_id,
            text=text,
            sender_role="assistant",
            recipient_role="user",
            synced_from_api=False,
        )
        # Buffer instead of posting to Zalo
        self._reply_buffer.append(text)

    def flush_replies(self) -> list[str]:
        replies, self._reply_buffer = self._reply_buffer, []
        return replies


# ── Bootstrap (no ngrok, no Flask) ───────────────────────────────────────────
async def _bootstrap(db_path: str, fresh: bool) -> tuple[TestableBookingAgent, Database]:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fresh and path.exists():
        path.unlink()
        print(_color(f"🗑  Wiped existing test DB: {path}", _YELLOW))

    db = Database(db_path)
    await db.connect()
    await seed_business_config(db)

    store     = ChatHistoryStore(db, oa_id=DEFAULT_OA_ID)
    await store.init()

    fake_sync = _FakeSync()
    chain     = IntentExtractionChain()
    validator = BookingValidator(store=store)

    agent = TestableBookingAgent(
        store=store,
        sync_service=fake_sync,
        chain=chain,
        validator=validator,
        safety=SafetyAgent(db),
        classifier=IntentClassifier(llm=chain.llm),
        support=CustomerSupportAgent(),
        availability=AvailabilityAgent(db),
        booking_handler=BookingHandler(db),
        negotiation=NegotiationAgent(db),
        update=UpdateAgent(db),
        cancellation=CancellationAgent(db),
        waitlist=WaitlistAgent(db),
        fallback=FallbackAgent(),
    )
    return agent, db


# ── Built-in slash commands ───────────────────────────────────────────────────
async def _cmd_reset(agent: TestableBookingAgent, user_id: str) -> None:
    await agent.store.cancel_active_booking(user_id)
    agent._pending_alternatives.pop(user_id, None)
    agent._awaiting_confirmation.pop(user_id, None)
    print(_color("↺  Active booking cancelled. State cleared.", _YELLOW))


async def _cmd_status(agent: TestableBookingAgent, user_id: str) -> None:
    bk: BookingData | None = await agent.store.get_active_booking(user_id)
    if bk is None:
        print(_color("  No active booking.", _DIM))
        return
    print(_color("── Active booking ──────────────────────", _DIM))
    for field in ["booking_id", "status", "intent", "name", "phone",
                  "service", "date", "time", "duration_minutes",
                  "preferred_staff", "confidence"]:
        val = getattr(bk, field, None)
        if val is not None:
            print(f"  {field:<20} {val}")
    print(_color("────────────────────────────────────────", _DIM))


async def _cmd_history(agent: TestableBookingAgent, user_id: str) -> None:
    history = await agent.store.as_llm_context(user_id, last_n=10)
    print(_color("── Chat history (last 10) ──────────────", _DIM))
    for m in history:
        role = "You  " if m["role"] == "user" else "Agent"
        content = m["content"].replace("\n", " ")[:120]
        print(f"  [{role}] {content}")
    print(_color("────────────────────────────────────────", _DIM))


async def _cmd_bookings(agent: TestableBookingAgent) -> None:
    rows = await agent.store._db.get_bookings_for_client(DEFAULT_USER_ID)
    if not rows:
        print(_color("  No bookings in DB.", _DIM))
        return
    print(_color("── All bookings ────────────────────────", _DIM))
    for bk in rows:
        print(f"  {bk.booking_id}  {str(bk.status):<12}  "
              f"{bk.service or '—':<25}  {bk.date or '—'}  {bk.time or '—'}")
    print(_color("────────────────────────────────────────", _DIM))


# ── Main REPL loop ────────────────────────────────────────────────────────────
async def _repl(agent: TestableBookingAgent, user_id: str) -> None:
    print()
    print(_color("╔═════════════════════════════════════════==═╗", _CYAN))
    print(_color("║   Em-My Booking Agent  —  Local Test CLI   ║", _CYAN))
    print(_color("╚══════════════════════════════════════════==╝", _RESET))
    print(_color(f"  User ID : {user_id}", _DIM))
    print(_color("  Commands: /reset  /status  /history  /bookings  /quit", _DIM))
    print()

    loop = asyncio.get_event_loop()

    while True:
        # Prompt — run blocking input() in a thread so the event loop stays alive
        try:
            raw = await loop.run_in_executor(None, lambda: input(_color("You: ", _BOLD)))
        except (EOFError, KeyboardInterrupt):
            print()
            break

        text = raw.strip()
        if not text:
            continue

        # ── Slash commands ────────────────────────────────────────────────────
        lower = text.lower()
        if lower in ("/quit", "/exit", "quit", "exit"):
            break
        if lower == "/reset":
            await _cmd_reset(agent, user_id)
            continue
        if lower == "/status":
            await _cmd_status(agent, user_id)
            continue
        if lower == "/history":
            await _cmd_history(agent, user_id)
            continue
        if lower == "/bookings":
            await _cmd_bookings(agent)
            continue

        # ── Send to agent ─────────────────────────────────────────────────────
        # Store user message (mirrors what the webhook handler does)
        await agent.store.append_message(
            sender_id=user_id,
            recipient_id=DEFAULT_OA_ID,
            text=text,
            sender_role="user",
            recipient_role="assistant",
            synced_from_api=False,
        )

        # Process immediately — no debounce in test mode
        await agent._process_message(user_id, text)

        # Print all replies that were buffered
        replies = agent.flush_replies()
        for reply in replies:
            print()
            for line in reply.split("\n"):
                print(_color("Agent: ", _GREEN) + line)
        print()


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Test the booking agent without Zalo.")
    parser.add_argument("--user",  default=DEFAULT_USER_ID, help="Simulated user ID")
    parser.add_argument("--db",    default=DEFAULT_TEST_DB,  help="Path to test SQLite DB")
    parser.add_argument("--fresh", action="store_true",      help="Wipe DB before starting")
    args = parser.parse_args()

    async def run():
        agent, db = await _bootstrap(args.db, args.fresh)
        try:
            await _repl(agent, args.user)
        finally:
            await db.close()
            print(_color("Goodbye.", _DIM))

    asyncio.run(run())


if __name__ == "__main__":
    main()
