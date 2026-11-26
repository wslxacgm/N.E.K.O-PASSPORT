import unittest

from neko_passport_bridge.protocol import (
    MAX_AUDIO_BYTES,
    CacheFullError,
    PendingCache,
    ProtocolError,
    decode_audio,
    decode_packet,
    encode_audio,
    encode_packet,
    fragment_text,
    make_packet,
    make_reply_packets,
)


class ControlPacketTests(unittest.TestCase):
    def test_json_round_trip_preserves_chinese_text(self):
        packet = make_packet("asr_text", text="你好，猫娘")
        self.assertEqual(decode_packet(encode_packet(packet)), packet)

    def test_control_packets_are_text_frames(self):
        # The device only accepts TEXT opcodes; binary frames are treated as PCM.
        data = encode_packet(make_packet("wake"))
        self.assertIsInstance(data, str)
        self.assertEqual(decode_packet(data)["type"], "wake")

    def test_invalid_envelope_is_rejected(self):
        with self.assertRaises(ProtocolError):
            decode_packet(b"[]")
        with self.assertRaises(ProtocolError):
            decode_packet(b'{"v":99,"type":"wake","id":"1"}')

    def test_reply_fragments_never_split_utf8(self):
        text = "连接成功。猫娘回复：你好！abc" * 8
        fragments = fragment_text(text, max_bytes=16)
        self.assertEqual("".join(fragments), text)
        self.assertTrue(all(len(part.encode("utf-8")) <= 16 for part in fragments))

        packets = make_reply_packets("message-1", text, max_bytes=16)
        self.assertEqual([p["part"] for p in packets], list(range(1, len(packets) + 1)))
        self.assertTrue(all(p["parts"] == len(packets) for p in packets))
        self.assertFalse(packets[-2]["final"])
        self.assertTrue(packets[-1]["final"])


class PendingCacheTests(unittest.TestCase):
    def test_unacknowledged_packets_can_be_replayed(self):
        cache = PendingCache(capacity=2)
        first = make_packet("wake")
        second = make_packet("battery", percent=88)
        first_data = cache.add(first)
        cache.add(second)

        self.assertEqual(cache.pending(), [first_data, encode_packet(second)])
        self.assertTrue(cache.ack(first["id"]))
        self.assertFalse(cache.ack(first["id"]))
        self.assertEqual(len(cache), 1)

    def test_cache_does_not_silently_drop_messages(self):
        cache = PendingCache(capacity=1)
        cache.add(make_packet("wake"))
        with self.assertRaises(CacheFullError):
            cache.add(make_packet("wake"))


class AudioPacketTests(unittest.TestCase):
    def test_pcm_round_trip(self):
        pcm = b"\x01\x00\x02\x00"
        frame = encode_audio(stream_id=7, seq=3, pcm=pcm)
        chunk = decode_audio(frame)
        self.assertEqual((chunk.stream_id, chunk.seq, chunk.pcm), (7, 3, pcm))

    def test_bad_audio_frames_are_rejected(self):
        frame = bytearray(encode_audio(stream_id=7, seq=3, pcm=b"\x01\x00"))
        frame[0] = 0
        with self.assertRaises(ProtocolError):
            decode_audio(bytes(frame))
        with self.assertRaises(ProtocolError):
            encode_audio(stream_id=7, seq=3, pcm=b"\x00" * (MAX_AUDIO_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
