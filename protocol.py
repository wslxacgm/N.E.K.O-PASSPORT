from __future__ import annotations

import json
import struct
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 1
MAX_CONTROL_BYTES = 4096
MAX_AUDIO_BYTES = 2048
REPLY_CHUNK_BYTES = 192

_AUDIO_MAGIC = b"NKA1"
_AUDIO_HEADER = struct.Struct(">4sIII")
_DOWNLINK_MAGIC = b"NKP1"
_DOWNLINK_HEADER = struct.Struct(">4sII")
MAX_DOWNLINK_PCM_BYTES = 1024
_PACKET_TYPES = frozenset(
    {
        "hello",
        "wake",
        "asr_text",
        "battery",
        "audio_start",
        "audio_end",
        "heartbeat",
        "ack",
        "reply_text",
        "tts",
        "expression",
        "audio_out",
        "error",
    }
)


class ProtocolError(ValueError):
    pass


class CacheFullError(ProtocolError):
    pass


@dataclass(frozen=True)
class AudioChunk:
    stream_id: int
    seq: int
    pcm: bytes


def make_packet(kind: str, *, event_id: str | None = None, **fields: Any) -> dict[str, Any]:
    packet = {"v": PROTOCOL_VERSION, "type": kind, "id": event_id or uuid.uuid4().hex}
    packet.update(fields)
    _validate_envelope(packet)
    return packet


def encode_packet(packet: dict[str, Any]) -> str:
    # Control packets must travel as WebSocket text frames; the device only
    # accepts TEXT/CONTINUATION opcodes and treats binary frames as PCM.
    _validate_envelope(packet)
    data = json.dumps(packet, ensure_ascii=False, separators=(",", ":"))
    if len(data.encode("utf-8")) > MAX_CONTROL_BYTES:
        raise ProtocolError("control packet exceeds size limit")
    return data


def decode_packet(data: str | bytes) -> dict[str, Any]:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    if len(raw) > MAX_CONTROL_BYTES:
        raise ProtocolError("control packet exceeds size limit")
    try:
        packet = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid JSON packet") from exc
    if not isinstance(packet, dict):
        raise ProtocolError("packet must be a JSON object")
    _validate_envelope(packet)
    return packet


def fragment_text(text: str, max_bytes: int = REPLY_CHUNK_BYTES) -> list[str]:
    if max_bytes < 4:
        raise ValueError("max_bytes must fit one UTF-8 code point")
    if not text:
        return [""]

    parts: list[str] = []
    current: list[str] = []
    current_size = 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if current and current_size + char_size > max_bytes:
            parts.append("".join(current))
            current = []
            current_size = 0
        current.append(char)
        current_size += char_size
    parts.append("".join(current))
    return parts


def make_reply_packets(
    message_id: str, text: str, max_bytes: int = REPLY_CHUNK_BYTES
) -> list[dict[str, Any]]:
    fragments = fragment_text(text, max_bytes)
    total = len(fragments)
    return [
        make_packet(
            "reply_text",
            message_id=message_id,
            part=index,
            parts=total,
            final=index == total,
            text=fragment,
        )
        for index, fragment in enumerate(fragments, start=1)
    ]


def make_tts_packet(message_id: str, state: str) -> dict[str, Any]:
    if state not in {"start", "done"}:
        raise ValueError("TTS state must be start or done")
    return make_packet("tts", message_id=message_id, state=state)


def make_audio_out_packet(message_id: str, state: str, sample_rate: int = 16000) -> dict[str, Any]:
    if state not in {"start", "done"}:
        raise ValueError("audio_out state must be start or done")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    return make_packet("audio_out", message_id=message_id, state=state, sample_rate=sample_rate)


def make_expression_packet(message_id: str, name: str) -> dict[str, Any]:
    if not name:
        raise ValueError("expression name is required")
    return make_packet("expression", message_id=message_id, name=name)


class PendingCache:
    def __init__(self, capacity: int = 128):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._capacity = capacity
        self._items: OrderedDict[str, str] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def add(self, packet: dict[str, Any]) -> str:
        data = encode_packet(packet)
        packet_id = packet["id"]
        if packet_id in self._items:
            del self._items[packet_id]
        elif len(self._items) >= self._capacity:
            raise CacheFullError("pending message cache is full")
        self._items[packet_id] = data
        return data

    def ack(self, packet_id: str) -> bool:
        return self._items.pop(packet_id, None) is not None

    def pending(self) -> list[str]:
        return list(self._items.values())


def encode_audio(stream_id: int, seq: int, pcm: bytes) -> bytes:
    if not 0 <= stream_id <= 0xFFFFFFFF or not 0 <= seq <= 0xFFFFFFFF:
        raise ProtocolError("audio stream ID and sequence must be uint32")
    if not 0 < len(pcm) <= MAX_AUDIO_BYTES:
        raise ProtocolError("invalid PCM chunk size")
    return _AUDIO_HEADER.pack(_AUDIO_MAGIC, stream_id, seq, len(pcm)) + pcm


def decode_audio(data: bytes) -> AudioChunk:
    if len(data) <= _AUDIO_HEADER.size or len(data) > _AUDIO_HEADER.size + MAX_AUDIO_BYTES:
        raise ProtocolError("invalid audio frame size")
    magic, stream_id, seq, payload_size = _AUDIO_HEADER.unpack_from(data)
    if magic != _AUDIO_MAGIC or payload_size != len(data) - _AUDIO_HEADER.size:
        raise ProtocolError("invalid audio frame header")
    return AudioChunk(stream_id=stream_id, seq=seq, pcm=data[_AUDIO_HEADER.size :])


def encode_downlink_audio(stream_id: int, seq: int, pcm: bytes) -> bytes:
    if not 0 <= stream_id <= 0xFFFFFFFF or not 0 <= seq <= 0xFFFFFFFF:
        raise ProtocolError("downlink stream ID and sequence must be uint32")
    if not 0 < len(pcm) <= MAX_DOWNLINK_PCM_BYTES:
        raise ProtocolError("invalid downlink PCM chunk size")
    return _DOWNLINK_HEADER.pack(_DOWNLINK_MAGIC, stream_id, seq) + pcm


def _validate_envelope(packet: dict[str, Any]) -> None:
    if packet.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")
    if packet.get("type") not in _PACKET_TYPES:
        raise ProtocolError("unsupported packet type")
    packet_id = packet.get("id")
    if not isinstance(packet_id, str) or not packet_id or len(packet_id) > 64:
        raise ProtocolError("invalid packet ID")
