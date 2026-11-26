from __future__ import annotations

import array
import asyncio
import hmac
import json
import logging
import socket
import time
import uuid
from collections.abc import Awaitable, Callable
from http import HTTPStatus
from typing import Any

from .protocol import (
    MAX_CONTROL_BYTES,
    AudioChunk,
    PendingCache,
    ProtocolError,
    decode_audio,
    decode_packet,
    encode_downlink_audio,
    encode_packet,
    make_audio_out_packet,
    make_packet,
    make_reply_packets,
)

logger = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], Awaitable[None]]
AudioHandler = Callable[[AudioChunk], Awaitable[None]]

_DEVICE_EVENTS = frozenset(
    {"wake", "asr_text", "battery", "audio_start", "audio_end", "heartbeat", "ack"}
)
_RELIABLE_DOWNLINK = frozenset({"reply_text", "tts", "expression"})


def local_ip() -> str:
    # 用 UDP connect 取默认出口地址，不会真的发包。
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("223.5.5.5", 53))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class BridgeServer:
    def __init__(
        self,
        *,
        host: str = "0.0.0.0",
        port: int = 8765,
        token: str,
        on_event: EventHandler,
        on_audio: AudioHandler,
        cache_capacity: int = 128,
        hello_timeout: float = 10,
        ping_interval: float = 20,
        ping_timeout: float = 20,
    ):
        if not token:
            raise ValueError("LAN authentication token is required")
        self.host = host
        self.port = port
        self._token = token
        self._on_event = on_event
        self._on_audio = on_audio
        self._hello_timeout = hello_timeout
        self._ping_interval = ping_interval
        self._ping_timeout = ping_timeout
        self._pending = PendingCache(cache_capacity)
        self._websocket: Any | None = None
        self._server: Any | None = None
        self._send_lock = asyncio.Lock()
        self._reply_lock = asyncio.Lock()
        self._audio_streams: dict[int, int] = {}
        self._remote: str | None = None
        self._device_name = ""
        self._connected_since: float | None = None
        self._last_attempt: str | None = None
        self._last_attempt_at: float | None = None
        self._last_attempt_ok = False
        # 前端音频镜像连接（N.E.K.O 播放的 TTS 原始音频）。
        self._tap_ws: Any | None = None
        self._tap_passive: set[Any] = set()
        self._tap_warned = False
        self._out_active = False
        self._out_stream = 0
        self._out_seq = 0
        self._out_last_at = 0.0
        self._pcm_carry = b""
        self._out_lock = asyncio.Lock()
        self._pcm_queue: asyncio.Queue[bytes] | None = None
        self._sender_task: asyncio.Task[None] | None = None
        # 由插件填充的附加行，面板与 /api/status 直接透出。
        self.extra_status: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    async def start(self) -> None:
        if self._server is not None:
            raise RuntimeError("bridge server is already running")
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handle_connection,
            self.host,
            self.port,
            max_size=262144,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_timeout,
            process_request=self._process_request,
        )

    def status_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        attempt = None
        if self._last_attempt is not None:
            attempt = {
                "remote": self._last_attempt,
                "seconds": int(now - (self._last_attempt_at or now)),
                "authorized": self._last_attempt_ok,
            }
        return {
            "bind": f"{self.host}:{self.port}",
            "server": f"ws://{local_ip()}:{self.port}",
            "token": self._token,
            "pending": len(self._pending),
            "device": {
                "connected": self._websocket is not None,
                "remote": self._remote,
                "name": self._device_name,
                "seconds": int(now - self._connected_since)
                if self._connected_since is not None
                else 0,
            },
            "last_attempt": attempt,
            "audio_tap": {
                "connected": self._tap_ws is not None,
                "streaming": self._out_active,
            },
            "extra": dict(self.extra_status),
        }

    async def _process_request(self, connection: Any, request: Any) -> Any | None:
        # 带 Upgrade 头的是设备 WebSocket 握手，继续走正常流程。
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        if request.path == "/api/status":
            body = json.dumps(self.status_snapshot(), ensure_ascii=False)
            return self._http_response(
                connection, HTTPStatus.OK, body, "application/json; charset=utf-8"
            )
        if request.path in ("/", "/index.html"):
            return self._http_response(
                connection, HTTPStatus.OK, self._panel_html(),
                "text/html; charset=utf-8",
            )
        return self._http_response(
            connection, HTTPStatus.NOT_FOUND, "not found", "text/plain; charset=utf-8"
        )

    @staticmethod
    def _http_response(
        connection: Any, status: HTTPStatus, body: str, content_type: str
    ) -> Any:
        response = connection.respond(status, body)
        # Headers 是 multidict，先删掉默认的 Content-Type 再写入。
        del response.headers["Content-Type"]
        response.headers["Content-Type"] = content_type
        return response

    def _panel_html(self) -> str:
        snapshot = self.status_snapshot()
        device = snapshot["device"]
        if device["connected"]:
            device_line = f'已连接 {device["remote"]}（{device["seconds"]} 秒）'
        else:
            device_line = "未连接"
        attempt = snapshot["last_attempt"]
        if attempt is None:
            attempt_line = "无"
        else:
            verdict = "鉴权通过" if attempt["authorized"] else "Token 不匹配"
            attempt_line = f'{attempt["remote"]}（{attempt["seconds"]} 秒前，{verdict}）'
        extra_rows = "".join(
            f"<tr><td>{key}</td><td>{value}</td></tr>"
            for key, value in snapshot["extra"].items()
        )
        return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="2">
<title>NEKO Passport Bridge</title></head><body>
<h3>NEKO Passport Bridge</h3>
<table border="1" cellpadding="6" cellspacing="0">
<tr><td>电脑监听</td><td>{snapshot["bind"]}</td></tr>
<tr><td>设备自动发现</td><td><b>{snapshot["server"]}</b></td></tr>
<tr><td>Token</td><td>{snapshot["token"]}</td></tr>
<tr><td>设备连接</td><td>{device_line}</td></tr>
<tr><td>最近连接尝试</td><td>{attempt_line}</td></tr>
<tr><td>待补发消息</td><td>{snapshot["pending"]}</td></tr>{extra_rows}
</table>
<p>页面每 2 秒自动刷新。设备保持默认设置即可自动找到本机，无需填写地址。</p>
</body></html>"""

    async def stop(self) -> None:
        websocket, self._websocket = self._websocket, None
        if websocket is not None:
            await websocket.close(code=1001, reason="plugin stopping")
        tap, self._tap_ws = self._tap_ws, None
        if tap is not None:
            await tap.close(code=1001, reason="plugin stopping")
        for standby in list(self._tap_passive):
            try:
                await standby.close(code=1001, reason="plugin stopping")
            except Exception:
                pass
        self._tap_passive.clear()
        if self._sender_task is not None:
            task, self._sender_task = self._sender_task, None
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._drain_pcm_queue()
        self._pcm_queue = None
        if self._server is not None:
            server, self._server = self._server, None
            server.close()
            await server.wait_closed()
        self._audio_streams.clear()

    async def send_reply(self, message_id: str, text: str) -> None:
        async with self._reply_lock:
            for packet in make_reply_packets(message_id, text):
                await self.send_packet(packet)

    async def send_packet(self, packet: dict[str, Any]) -> bool:
        kind = packet.get("type")
        reliable = kind in _RELIABLE_DOWNLINK
        async with self._send_lock:
            data = self._pending.add(packet) if reliable else encode_packet(packet)
            if self._websocket is None:
                return False
            try:
                await self._websocket.send(data)
                return True
            except Exception as exc:
                logger.info("downlink deferred until reconnect: %s", exc)
                return False

    async def _handle_connection(self, websocket: Any) -> None:
        request = getattr(websocket, "request", None)
        path = getattr(request, "path", "/") or "/"
        if path.split("?", 1)[0].rstrip("/") == "/audio":
            await self._handle_audio_tap(websocket)
            return
        address = websocket.remote_address
        self._last_attempt = f"{address[0]}:{address[1]}" if address else None
        self._last_attempt_at = time.monotonic()
        self._last_attempt_ok = False
        try:
            raw_hello = await asyncio.wait_for(
                websocket.recv(), timeout=self._hello_timeout
            )
            hello = decode_packet(raw_hello)
            if hello.get("type") != "hello":
                raise ProtocolError("first packet must be hello")
            token = hello.get("token")
            if not isinstance(token, str) or not hmac.compare_digest(token, self._token):
                await self._reject(websocket, "unauthorized", 4001)
                return

            previous, self._websocket = self._websocket, websocket
            self._remote = self._last_attempt
            self._device_name = (
                hello.get("device") if isinstance(hello.get("device"), str) else ""
            )
            self._connected_since = time.monotonic()
            self._last_attempt_ok = True
            logger.info("device connected from %s (%s)", self._remote, self._device_name)
            self._audio_streams.clear()
            if previous is not None and previous is not websocket:
                await previous.close(code=4000, reason="replaced by new connection")

            async with self._send_lock:
                await websocket.send(
                    encode_packet(
                        make_packet(
                            "ack", ack_id=hello["id"], state="connected"
                        )
                    )
                )
                for data in self._pending.pending():
                    await websocket.send(data)

            async for message in websocket:
                if self._websocket is not websocket:
                    break
                try:
                    if isinstance(message, bytes):
                        await self._handle_binary(message)
                    else:
                        await self._handle_packet(decode_packet(message))
                except ProtocolError as exc:
                    await self._send_error(str(exc))
                except Exception as exc:
                    logger.exception("device event handler failed")
                    await self._send_error(f"event handler failed: {exc}")
        except TimeoutError:
            await self._reject(websocket, "hello timeout", 4008)
        except ProtocolError as exc:
            await self._reject(websocket, str(exc), 4002)
        except Exception as exc:
            logger.info("device connection closed: %s", exc)
        finally:
            if self._websocket is websocket:
                self._websocket = None
                self._remote = None
                self._device_name = ""
                self._connected_since = None
                self._audio_streams.clear()

    async def _handle_audio_tap(self, websocket: Any) -> None:
        """接收前端镜像过来的 TTS 原始音频，重采样后转给设备。"""
        address = websocket.remote_address
        if address is not None and address[0] not in ("127.0.0.1", "::1"):
            await websocket.close(code=4003, reason="audio tap is loopback only")
            return
        previous = self._tap_ws
        self._tap_warned = False
        if previous is None:
            self._tap_ws = websocket
            logger.info("audio tap connected")
        else:
            # 多窗口/重复连接时保留已有连接，新连接作为备用，避免互相踢掉。
            self._tap_passive.add(websocket)
            logger.info("audio tap connected (standby)")
        try:
            async for message in websocket:
                if websocket is not self._tap_ws:
                    continue
                if isinstance(message, (bytes, bytearray)):
                    await self._feed_tap_audio(bytes(message))
        except Exception as exc:
            logger.info("audio tap closed: %s", exc)
        finally:
            self._tap_passive.discard(websocket)
            if self._tap_ws is websocket:
                self._tap_ws = None
                # 有备用连接就直接接替，避免全部等待 3 秒重连。
                if self._tap_passive:
                    self._tap_ws = self._tap_passive.pop()
                    logger.info("audio tap standby promoted")
                else:
                    async with self._out_lock:
                        self._out_active = False
                        self._pcm_carry = b""
                    self._drain_pcm_queue()
                logger.info("audio tap disconnected")

    async def _feed_tap_audio(self, data: bytes) -> None:
        if not data or not self.connected:
            return
        if data[:4] == b"OggS":
            if not self._tap_warned:
                self._tap_warned = True
                logger.warning("audio tap received Ogg Opus; decoding not supported yet")
            return
        if len(data) < 2:
            return

        async with self._out_lock:
            now = time.monotonic()
            if not self._out_active or now - self._out_last_at > 1.5:
                self._out_stream = (self._out_stream + 1) & 0xFFFFFFFF or 1
                self._out_seq = 0
                self._out_active = True
                self._pcm_carry = b""
                # 不清空待发送队列：上一句的尾巴可能还没发完，清空会造成截断。
                await self.send_packet(
                    make_audio_out_packet(uuid.uuid4().hex, "start", 16000)
                )
            self._out_last_at = now

            buf = self._pcm_carry + data
            usable = len(buf) - (len(buf) % 6)
            if usable <= 0:
                self._pcm_carry = buf
                return
            samples = memoryview(buf[:usable]).cast("h")
            mono = array.array(
                "h",
                (
                    (samples[i] + samples[i + 1] + samples[i + 2]) // 3
                    for i in range(0, len(samples), 3)
                ),
            )
            self._pcm_carry = buf[usable:]
            pcm = mono.tobytes()

        for offset in range(0, len(pcm), 1024):
            chunk = pcm[offset : offset + 1024]
            if len(chunk) < 2:
                continue
            self._enqueue_pcm(chunk)

    async def _send_audio_frame(self, pcm: bytes) -> bool:
        frame = encode_downlink_audio(self._out_stream, self._out_seq, pcm)
        async with self._send_lock:
            if self._websocket is None:
                return False
            try:
                await self._websocket.send(frame)
            except Exception as exc:
                logger.info("audio downlink failed: %s", exc)
                return False
        self._out_seq = (self._out_seq + 1) & 0xFFFFFFFF
        return True

    def _ensure_sender(self) -> None:
        if self._sender_task is None or self._sender_task.done():
            # 不设上限：发送端已是实时速率，这里只做缓冲，丢块会造成语音缺失。
            self._pcm_queue = asyncio.Queue()
            self._sender_task = asyncio.create_task(self._pcm_sender())

    def _enqueue_pcm(self, chunk: bytes) -> None:
        self._ensure_sender()
        queue = self._pcm_queue
        if queue is None:
            return
        queue.put_nowait(chunk)

    def _drain_pcm_queue(self) -> None:
        queue = self._pcm_queue
        if queue is None:
            return
        while not queue.empty():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _pcm_sender(self) -> None:
        # 16kHz 单声道 16bit = 32000 B/s，按实时速率发送，避免设备端堆积/丢帧。
        # 首次发送前给出 300ms 的预缓冲额度，抵消 WiFi 抖动。
        bytes_per_second = 32000.0
        next_at: float | None = None
        while True:
            queue = self._pcm_queue
            if queue is None:
                return
            try:
                chunk = await asyncio.wait_for(queue.get(), timeout=0.25)
            except TimeoutError:
                async with self._out_lock:
                    if (
                        self._out_active
                        and queue.empty()
                        and time.monotonic() - self._out_last_at > 1.5
                    ):
                        self._out_active = False
                        self._pcm_carry = b""
                        await self.send_packet(
                            make_audio_out_packet(uuid.uuid4().hex, "done", 16000)
                        )
                next_at = None
                continue
            if not self.connected:
                next_at = None
                continue
            now = time.monotonic()
            if next_at is None:
                next_at = now - 0.30
            elif now < next_at:
                await asyncio.sleep(next_at - now)
                now = time.monotonic()
            if not await self._send_audio_frame(chunk):
                next_at = None
                continue
            next_at += len(chunk) / bytes_per_second
            if next_at < now:
                next_at = now

    async def _handle_packet(self, packet: dict[str, Any]) -> None:
        kind = packet["type"]
        if kind not in _DEVICE_EVENTS:
            raise ProtocolError(f"packet type not allowed from device: {kind}")
        if kind == "ack":
            ack_id = packet.get("ack_id")
            if not isinstance(ack_id, str):
                raise ProtocolError("ack_id must be a string")
            self._pending.ack(ack_id)
            return
        if kind == "heartbeat":
            await self.send_packet(make_packet("ack", ack_id=packet["id"]))
            return
        if kind == "audio_start":
            stream_id = self._stream_id(packet)
            self._audio_streams[stream_id] = 0
        elif kind == "audio_end":
            self._audio_streams.pop(self._stream_id(packet), None)
        await self._on_event(packet)

    async def _handle_binary(self, data: bytes) -> None:
        chunk = decode_audio(data)
        expected = self._audio_streams.get(chunk.stream_id)
        if expected is None:
            raise ProtocolError(f"unknown audio stream: {chunk.stream_id}")
        if chunk.seq != expected:
            del self._audio_streams[chunk.stream_id]
            await self._on_event(
                make_packet(
                    "audio_end",
                    stream_id=chunk.stream_id,
                    aborted=True,
                    reason="sequence_mismatch",
                )
            )
            raise ProtocolError(
                f"audio sequence mismatch on stream {chunk.stream_id}: "
                f"expected {expected}, received {chunk.seq}"
            )
        self._audio_streams[chunk.stream_id] = expected + 1
        await self._on_audio(chunk)

    async def _send_error(self, message: str) -> None:
        await self.send_packet(make_packet("error", message=message[:512]))

    async def _reject(self, websocket: Any, message: str, code: int) -> None:
        try:
            await websocket.send(
                encode_packet(make_packet("error", message=message[:512]))
            )
        finally:
            await websocket.close(code=code, reason=message[:120])

    @staticmethod
    def _stream_id(packet: dict[str, Any]) -> int:
        stream_id = packet.get("stream_id")
        if not isinstance(stream_id, int) or not 0 <= stream_id <= 0xFFFFFFFF:
            raise ProtocolError("stream_id must be uint32")
        return stream_id
