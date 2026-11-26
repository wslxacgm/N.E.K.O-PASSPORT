import asyncio
import unittest

from neko_passport_bridge.protocol import AudioChunk, ProtocolError, make_packet
from neko_passport_bridge.whisper_asr import WhisperASR


def audio_start(stream_id=7):
    return make_packet(
        "audio_start", stream_id=stream_id, sample_rate=16000, channels=1, bits=16
    )


class WhisperASRTests(unittest.IsolatedAsyncioTestCase):
    def make_asr(self, **overrides):
        self.transcripts = []
        self.errors = []
        self.transcript_received = asyncio.Event()

        async def on_text(stream_id, text):
            self.transcripts.append((stream_id, text))
            self.transcript_received.set()

        async def on_error(message):
            self.errors.append(message)

        options = dict(on_text=on_text, on_error=on_error)
        options.update(overrides)
        return WhisperASR(**options)

    async def test_pcm_is_aggregated_before_recognition(self):
        asr = self.make_asr()
        recognized_pcm = []

        def recognize(pcm):
            recognized_pcm.append(pcm)
            return "你好，猫娘"

        asr._recognize = recognize
        await asr.handle_event(audio_start())
        await asr.handle_audio(AudioChunk(7, 0, b"\x01\x00"))
        await asr.handle_audio(AudioChunk(7, 1, b"\x02\x00\x03\x00"))
        await asr.handle_event(make_packet("audio_end", stream_id=7))
        await asyncio.wait_for(self.transcript_received.wait(), timeout=1)

        self.assertEqual(recognized_pcm, [b"\x01\x00\x02\x00\x03\x00"])
        self.assertEqual(self.transcripts, [(7, "你好，猫娘")])
        self.assertEqual(self.errors, [])

    async def test_audio_format_is_validated(self):
        asr = self.make_asr()
        packet = audio_start()
        packet["sample_rate"] = 8000
        with self.assertRaisesRegex(ProtocolError, "16 kHz"):
            await asr.handle_event(packet)

    async def test_duration_limit_discards_stream(self):
        asr = self.make_asr(max_seconds=0.001)
        await asr.handle_event(audio_start())
        with self.assertRaisesRegex(ProtocolError, "duration limit"):
            await asr.handle_audio(AudioChunk(7, 0, b"\x00" * 34))
        with self.assertRaisesRegex(ProtocolError, "unknown audio stream"):
            await asr.handle_event(make_packet("audio_end", stream_id=7))

    async def test_aborted_stream_is_not_transcribed(self):
        asr = self.make_asr()
        asr._recognize = lambda pcm: self.fail("aborted audio must not be recognized")
        await asr.handle_event(audio_start())
        await asr.handle_audio(AudioChunk(7, 0, b"\x01\x00"))
        await asr.handle_event(make_packet("audio_end", stream_id=7, aborted=True))
        self.assertEqual(asr._tasks, set())

    async def test_recognition_error_reaches_error_callback(self):
        asr = self.make_asr()

        def recognize(pcm):
            raise RuntimeError("model missing")

        asr._recognize = recognize
        await asr.handle_event(audio_start())
        await asr.handle_audio(AudioChunk(7, 0, b"\x01\x00"))
        await asr.handle_event(make_packet("audio_end", stream_id=7))
        for _ in range(20):
            if self.errors:
                break
            await asyncio.sleep(0.01)
        self.assertIn("model missing", self.errors[0])


if __name__ == "__main__":
    unittest.main()
