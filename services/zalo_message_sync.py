"""
services/zalo_message_sync.py
────────────────────────────────────────────────────────────────────────────
Polls Zalo OA API and persists new messages into SQLite.
 
Responsibilities
────────────────
  • Fetch recent conversations & per-user message lists from Zalo.
  • Deduplicate against what is already stored.
  • Write net-new messages through ChatHistoryStore.
"""

from __future__ import annotations

import logging
import httpx

from services.chat_history_store import ChatHistoryStore
from services import zalo_api as api

log = logging.getLogger(__name__)

class ZaloMessageSync:
    def __init__(self, access_token: str, history_store: ChatHistoryStore) -> None:
        self._headers = {"access_token": access_token}
        self._store = history_store
        self._oa_id = history_store.oa_id
        # Use a persistent async client for connection pooling
        self._client = httpx.AsyncClient(headers=self._headers, timeout=10.0)

    from data.models import Message

    async def sync_all_recent(self, count: int = 10) -> list[Message]:
        """Fetch N recent conversations and sync messages for each participant."""
        params = api.build_get_list_recent_chat_params(offset=0, count=count)
        new_messages = []
        try:
            resp = await self._get(api.Endpoint.GET_LIST_RECENT_CHAT, params=params)
            conversations = api.unwrap_list(resp, "list_recent_chat")
         
            for convo in conversations:
                parsed = api.parse_message(convo, self._oa_id)
                if(parsed.src == api.MessageSrc.CLIENT):
                    user_msgs = await self._sync_user(parsed.from_id, parsed.from_display_name)
                else:
                    user_msgs = await self._sync_user(parsed.to_id, parsed.to_display_name)
                new_messages.extend(user_msgs)
        except Exception as e:
            log.error(f"Failed to sync recent chats: {e}")
        return new_messages

    async def sync_user_by_id(self, user_id: str, name: str = "") -> list[Message]:
        """Manual trigger to sync a specific user."""
        return await self._sync_user(user_id, name)

    async def _sync_user(self, user_id: str, name: str) -> list[Message]:
        """Ensures profile exists and fetches new messages."""
        await self._store.ensure_profile(user_id, name=name)
        new_messages = await self._fetch_and_store_messages(user_id)
        if new_messages:
            log.info("Synced user=%s (%s) new_messages=%d", user_id, name, len(new_messages))
        return new_messages

    async def _fetch_and_store_messages(self, user_id: str) -> list[Message]:
        # 1. Fetch raw data
        params = api.build_get_conversation_params(user_id)
        resp = await self._get(api.Endpoint.GET_CONVERSATION, params=params)
        raw_messages = api.unwrap_list(resp, "list_message")

        # 2. Get known IDs to prevent duplicates (Checks last 50 messages)
        known_ids = await self._store.get_known_msg_ids(user_id, limit=50)
        new_messages = []

        # 3. Process messages (Zalo is newest-first, so reverse for chronological order)
        for raw in reversed(raw_messages):
            parsed = api.parse_message(raw, oa_id=self._oa_id)
            
            if parsed.msg_id in known_ids:
                continue

            # 4. Save to DB
            msg = await self._store.append_message(
                sender_id=parsed.from_id,
                recipient_id=parsed.to_id,
                sender_role=parsed.sender_role,
                recipient_role=parsed.recipient_role,
                text=parsed.text,
                msg_id=parsed.msg_id,
                timestamp_ms=parsed.timestamp_ms,
                synced_from_api=True,
            )
            new_messages.append(msg)
            known_ids.add(parsed.msg_id)

        return new_messages

    async def _get(self, url: str, params: dict) -> dict:
        response = await self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    async def close(self):
        """Close the HTTP session."""
        await self._client.aclose()
    
if __name__ == '__main__':
    import asyncio
    async def run_sync():
        from services import ZALOOA_ACCESS_TOKEN, ZALOOA_ID
        from data.backends.sqlite import Database
        db = Database()
        await db.connect()
        chs = ChatHistoryStore(db, ZALOOA_ID)
        await chs.init()
        zms = ZaloMessageSync(access_token=ZALOOA_ACCESS_TOKEN, history_store=chs)
        await zms.sync_all_recent()
        await zms.close()
        await db.close()