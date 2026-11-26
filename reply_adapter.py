from __future__ import annotations

from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

ReplyHandler = Callable[[str, str], Awaitable[None]]

# 宿主把猫娘回话记为 assistant_message（普通对话）或 proactive_reply（插件消息触发的主动回复）。
REPLY_TURN_TYPES = frozenset({"assistant_message", "proactive_reply"})


class AssistantReplyAdapter:
    def __init__(self, on_reply: ReplyHandler, *, seen_capacity: int = 256):
        if seen_capacity < 1:
            raise ValueError("seen_capacity must be positive")
        self._on_reply = on_reply
        self._seen_capacity = seen_capacity
        self._seen: OrderedDict[str, None] = OrderedDict()

    def seed(self, records: Iterable[Any]) -> None:
        for record in records:
            self._remember(record.key())

    async def handle_delta(self, delta: Any) -> None:
        await self.handle_records(delta.added)

    async def handle_records(self, records: Iterable[Any]) -> None:
        for record in records:
            metadata = getattr(record, "metadata", {}) or {}
            text = getattr(record, "content", None)
            turn_type = metadata.get("turn_type") or getattr(record, "turn_type", None)
            if turn_type not in REPLY_TURN_TYPES:
                continue
            if not isinstance(text, str) or not text.strip():
                continue
            key = f"{record.key()}|{text[:40]}"
            if not self._remember(key):
                continue
            message_id = getattr(record, "conversation_id", None) or record.key()
            await self._on_reply(message_id, text.strip())

    def _remember(self, key: str) -> bool:
        if key in self._seen:
            return False
        self._seen[key] = None
        while len(self._seen) > self._seen_capacity:
            self._seen.popitem(last=False)
        return True
