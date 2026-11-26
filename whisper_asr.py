from __future__ import annotations

import asyncio
import logging
import os
import threading
from collections.abc import Awaitable, Callable
from typing import Any

from .protocol import AudioChunk, ProtocolError

logger = logging.getLogger(__name__)

TextHandler = Callable[[int, str], Awaitable[None]]
ErrorHandler = Callable[[str], Awaitable[None]]


class WhisperASR:
    def __init__(
        self,
        *,
        model_name: str = "base",
        language: str | None = None,
        local_files_only: bool = True,
        max_seconds: float = 30,
        on_text: TextHandler,
        on_error: ErrorHandler,
    ):
        if max_seconds <= 0:
            raise ValueError("max_seconds must be positive")
        self.model_name = model_name
        self.language = language
        self.local_files_only = local_files_only
        self._max_audio_bytes = int(16000 * 2 * max_seconds)
        self._on_text = on_text
        self._on_error = on_error
        self._streams: dict[int, bytearray] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._model: Any | None = None
        self._inference_lock = threading.Lock()

    async def handle_event(self, packet: dict[str, Any]) -> None:
        kind = packet["type"]
        if kind == "audio_start":
            self._validate_format(packet)
            self._streams.clear()
            self._streams[packet["stream_id"]] = bytearray()
        elif kind == "audio_end":
            stream_id = packet["stream_id"]
            pcm = self._streams.pop(stream_id, None)
            if packet.get("aborted"):
                return
            if pcm is None:
                raise ProtocolError(f"unknown audio stream: {stream_id}")
            if not pcm or len(pcm) % 2:
                raise ProtocolError("audio stream contains no complete PCM16 samples")
            task = asyncio.create_task(self._transcribe(stream_id, bytes(pcm)))
            self._tasks.add(task)
            task.add_done_callback(self._task_done)

    async def handle_audio(self, chunk: AudioChunk) -> None:
        pcm = self._streams.get(chunk.stream_id)
        if pcm is None:
            raise ProtocolError(f"unknown audio stream: {chunk.stream_id}")
        if len(pcm) + len(chunk.pcm) > self._max_audio_bytes:
            del self._streams[chunk.stream_id]
            raise ProtocolError("audio stream exceeds configured duration limit")
        pcm.extend(chunk.pcm)

    async def stop(self) -> None:
        self._streams.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _transcribe(self, stream_id: int, pcm: bytes) -> None:
        try:
            text = await asyncio.to_thread(self._recognize, pcm)
            await self._on_text(stream_id, text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("faster-whisper transcription failed")
            await self._on_error(f"ASR failed: {exc}")

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        # 国内直连 huggingface 常失败，未显式配置时走镜像。
        os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
        from faster_whisper import WhisperModel

        logger.info(
            "loading ASR model %s (local_files_only=%s, endpoint=%s)",
            self.model_name,
            self.local_files_only,
            os.environ.get("HF_ENDPOINT"),
        )
        self._model = WhisperModel(
            self.model_name,
            device="cpu",
            compute_type="int8",
            local_files_only=self.local_files_only,
        )
        return self._model

    async def warmup(self) -> None:
        """提前加载/下载模型，避免第一句话先等下载。"""
        try:
            await asyncio.to_thread(self._ensure_model)
            logger.info("ASR model ready: %s", self.model_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("ASR model load failed")
            await self._on_error(f"ASR model load failed: {exc}")

    def _recognize(self, pcm: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        with self._inference_lock:
            model = self._ensure_model()
            segments, _ = model.transcribe(
                audio,
                language=self.language,
                beam_size=1,
                condition_on_previous_text=False,
            )
            return "".join(segment.text for segment in segments).strip()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            logger.error("ASR callback failed", exc_info=error)

    @staticmethod
    def _validate_format(packet: dict[str, Any]) -> None:
        expected = {"sample_rate": 16000, "channels": 1, "bits": 16}
        if any(packet.get(key) != value for key, value in expected.items()):
            raise ProtocolError("audio format must be 16 kHz, mono, signed PCM16 LE")
