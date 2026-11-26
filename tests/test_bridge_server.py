import asyncio
import unittest

from neko_passport_bridge.bridge_server import BridgeServer
from neko_passport_bridge.protocol import (
    ProtocolError,
    decode_packet,
    encode_audio,
    encode_packet,
    make_packet,
    make_reply_packets,
)


class FakeWebSocket:
    def __init__(self, hello=None, messages=()):
        self._hello = encode_packet(hello) if hello is not None else None
        self._messages = messages
        self.sent = []
        self.closed = None

    async def recv(self):
        return self._hello

    def __aiter__(self):
        self._iterator = iter(self._messages)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration:
            raise StopAsyncIteration

    async def send(self, data):
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed = (code, reason)


class BridgeServerTests(unittest.IsolatedAsyncioTestCase):
    def make_server(self):
        self.events = []
        self.audio = []

        async def on_event(packet):
            self.events.append(packet)

        async def on_audio(chunk):
            self.audio.append(chunk)

        return BridgeServer(
            token="secret",
            on_event=on_event,
            on_audio=on_audio,
            hello_timeout=0.01,
        )

    async def test_hello_ack_replays_cached_reply(self):
        server = self.make_server()
        packets = make_reply_packets("message-1", "连接成功")
        for packet in packets:
            await server.send_packet(packet)
        self.assertEqual(server.pending_count, len(packets))

        socket = FakeWebSocket(make_packet("hello", token="secret", device="passport"))
        await server._handle_connection(socket)

        ack = decode_packet(socket.sent[0])
        self.assertEqual(ack["type"], "ack")
        self.assertEqual(ack["state"], "connected")
        self.assertEqual(decode_packet(socket.sent[1])["text"], "连接成功")
        self.assertFalse(server.connected)

    async def test_wrong_token_is_rejected(self):
        server = self.make_server()
        socket = FakeWebSocket(make_packet("hello", token="wrong"))
        await server._handle_connection(socket)

        error = decode_packet(socket.sent[0])
        self.assertEqual(error["message"], "unauthorized")
        self.assertEqual(socket.closed[0], 4001)
        self.assertFalse(server.connected)

    async def test_ack_removes_only_matching_pending_packet(self):
        server = self.make_server()
        first = make_reply_packets("message-1", "一")[0]
        second = make_reply_packets("message-2", "二")[0]
        await server.send_packet(first)
        await server.send_packet(second)

        await server._handle_packet(make_packet("ack", ack_id=first["id"]))

        self.assertEqual(server.pending_count, 1)
        self.assertEqual(server._pending.pending(), [encode_packet(second)])

    async def test_heartbeat_and_device_event_are_handled(self):
        server = self.make_server()
        socket = FakeWebSocket()
        server._websocket = socket
        heartbeat = make_packet("heartbeat", uptime_ms=100)
        wake = make_packet("wake", source="button")

        await server._handle_packet(heartbeat)
        await server._handle_packet(wake)

        self.assertEqual(decode_packet(socket.sent[0])["ack_id"], heartbeat["id"])
        self.assertEqual(self.events, [wake])

    async def test_pcm_sequence_is_strict_and_stream_can_restart(self):
        server = self.make_server()
        await server._handle_packet(make_packet("audio_start", stream_id=7))
        await server._handle_binary(encode_audio(7, 0, b"\x01\x00"))
        with self.assertRaisesRegex(ProtocolError, "expected 1, received 2"):
            await server._handle_binary(encode_audio(7, 2, b"\x02\x00"))
        with self.assertRaisesRegex(ProtocolError, "unknown audio stream"):
            await server._handle_binary(encode_audio(7, 1, b"\x03\x00"))

        await server._handle_packet(make_packet("audio_start", stream_id=7))
        await server._handle_binary(encode_audio(7, 0, b"\x04\x00"))
        self.assertEqual([chunk.pcm for chunk in self.audio], [b"\x01\x00", b"\x04\x00"])

    async def test_replies_do_not_interleave_fragments(self):
        class YieldingWebSocket(FakeWebSocket):
            async def send(self, data):
                await asyncio.sleep(0)
                self.sent.append(data)

        server = self.make_server()
        socket = YieldingWebSocket()
        server._websocket = socket
        await asyncio.gather(
            server.send_reply("message-1", "一" * 200),
            server.send_reply("message-2", "二" * 200),
        )

        order = []
        for data in socket.sent:
            packet = decode_packet(data)
            if packet["type"] == "reply_text" and (
                not order or order[-1] != packet["message_id"]
            ):
                order.append(packet["message_id"])
        self.assertIn(
            order,
            [["message-1", "message-2"], ["message-2", "message-1"]],
        )

    async def test_new_authenticated_connection_replaces_old_one(self):
        server = self.make_server()
        old_socket = FakeWebSocket(make_packet("hello", token="secret"))
        old_messages = FakeWebSocket(messages=[])
        server._websocket = old_messages
        await server._handle_connection(old_socket)
        self.assertEqual(old_messages.closed[0], 4000)


if __name__ == "__main__":
    unittest.main()
