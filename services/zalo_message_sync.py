"""
services/zalo_message_sync.py
────────────────────────────────────────────────────────────────────────────
Polls Zalo OA API and persists new messages into SQLite.
 
Responsibilities
────────────────
  • Fetch recent conversations & per-user message lists from Zalo.
  • Deduplicate against what is already stored.
  • Write net-new messages through ChatHistoryStore.
 
Does NOT contain any Zalo payload shapes — those live in services/zalo_api.py.
Does NOT write JSON files.
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
        # Use a persistent client for connection pooling
        self._client = httpx.Client(headers=self._headers, timeout=10.0)

    def sync_all_recent(self, count: int = 10) -> None:
        """Fetch N recent conversations and sync messages for each participant."""
        params = api.build_get_list_recent_chat_params(offset=0, count=count)
        try:
            resp = self._get(api.Endpoint.GET_LIST_RECENT_CHAT, params=params)
            conversations = api.unwrap_list(resp, "list_recent_chat")
         
            for convo in conversations:
                parsed = api.parse_message(convo, self._oa_id)
                if(parsed.src == api.MessageSrc.CLIENT):
                    self._sync_user(parsed.from_id, parsed.from_display_name)
                else:
                    self._sync_user(parsed.to_id, parsed.to_display_name)
        except Exception as e:
            log.error(f"Failed to sync recent chats: {e}")

    def sync_user_by_id(self, user_id: str, name: str = "") -> int:
        """Manual trigger to sync a specific user."""
        return self._sync_user(user_id, name)

    def _sync_user(self, user_id: str, name: str) -> int:
        """Ensures profile exists and fetches new messages."""
        self._store.ensure_profile(user_id, name=name)
        new_count = self._fetch_and_store_messages(user_id)
        if new_count > 0:
            log.info("Synced user=%s (%s) new_messages=%d", user_id, name, new_count)
        return new_count

    def _fetch_and_store_messages(self, user_id: str) -> int:
        # 1. Fetch raw data
        params = api.build_get_conversation_params(user_id)
        resp = self._get(api.Endpoint.GET_CONVERSATION, params=params)
        raw_messages = api.unwrap_list(resp, "list_message")

        # 2. Get known IDs to prevent duplicates (Checks last 50 messages)
        known_ids = self._store.get_known_msg_ids(user_id, limit=50)
        new_count = 0

        # 3. Process messages (Zalo is newest-first, so reverse for chronological order)
        for raw in reversed(raw_messages):
            parsed = api.parse_message(raw, oa_id=self._oa_id)
            
            if parsed.msg_id in known_ids:
                continue

            # 4. Save to DB
            self._store.append_message(
                sender_id=parsed.from_id,
                recipient_id=parsed.to_id,
                sender_role=parsed.sender_role,
                recipient_role=parsed.recipient_role,
                text=parsed.text,
                msg_id=parsed.msg_id,
                timestamp_ms=parsed.timestamp_ms,
                synced_from_api=True,
            )
            new_count += 1
            known_ids.add(parsed.msg_id)

        return new_count

    def _get(self, url: str, params: dict) -> dict:
        response = self._client.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def close(self):
        """Close the HTTP session."""
        self._client.close()
    
if __name__ == '__main__':
    from services import ZALOOA_ACCESS_TOKEN, ZALOOA_ID
    from data.backends.sqlite import Database
    db = Database()
    chs = ChatHistoryStore(db, ZALOOA_ID)
    
    zms = ZaloMessageSync(access_token=ZALOOA_ACCESS_TOKEN, history_store=chs)
    zms.sync_all_recent()
    zms.close()