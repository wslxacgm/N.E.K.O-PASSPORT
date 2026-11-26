import unittest
from types import SimpleNamespace

from neko_passport_bridge.reply_adapter import AssistantReplyAdapter


class FakeRecord:
    def __init__(self, message_id, text, turn_type="assistant_message"):
        self.message_id = message_id
        self.content = text
        self.metadata = {"turn_type": turn_type}

    def key(self):
        return self.message_id


class AssistantReplyAdapterTests(unittest.IsolatedAsyncioTestCase):
    def make_adapter(self):
        self.replies = []

        async def on_reply(message_id, text):
            self.replies.append((message_id, text))

        return AssistantReplyAdapter(on_reply)

    async def test_only_assistant_text_is_forwarded(self):
        adapter = self.make_adapter()
        records = [
            FakeRecord("user-1", "问题", turn_type="user_message"),
            FakeRecord("reply-1", " 你好，主人 "),
            FakeRecord("reply-2", "   "),
        ]
        await adapter.handle_delta(SimpleNamespace(added=records))
        self.assertEqual(self.replies, [("reply-1", "你好，主人")])

    async def test_initial_records_and_duplicates_are_not_replayed(self):
        adapter = self.make_adapter()
        record = FakeRecord("reply-1", "你好")
        adapter.seed([record])
        await adapter.handle_delta(SimpleNamespace(added=[record]))
        await adapter.handle_delta(SimpleNamespace(added=[record]))
        self.assertEqual(self.replies, [])

    async def test_seen_cache_is_bounded(self):
        adapter = self.make_adapter()
        adapter._seen_capacity = 1
        first = FakeRecord("reply-1", "一")
        second = FakeRecord("reply-2", "二")
        await adapter.handle_delta(SimpleNamespace(added=[first, second]))
        await adapter.handle_delta(SimpleNamespace(added=[first]))
        self.assertEqual(self.replies, [("reply-1", "一"), ("reply-2", "二"), ("reply-1", "一")])


if __name__ == "__main__":
    unittest.main()
