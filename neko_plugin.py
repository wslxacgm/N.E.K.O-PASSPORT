from __future__ import annotations

import asyncio
import inspect
import threading
import time
import webbrowser
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    neko_plugin,
    plugin_entry,
)

from .bridge_server import BridgeServer
from .discovery import DISCOVERY_PORT, DiscoveryResponder
from .frontend_tap import patch_frontend, patch_status, spawn_uninstall_watcher
from .protocol import AudioChunk, make_packet, make_tts_packet
from .reply_adapter import AssistantReplyAdapter
from .whisper_asr import WhisperASR


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001 - 打不开浏览器不影响插件运行
        pass


async def _await_value(value: Any) -> Any:
    return await value


class _BridgeRuntime:
    """在独立线程的事件循环里跑 WebSocket 服务。

    宿主的 lifecycle 入口各自 asyncio.run 一次，startup 返回后循环即关闭，
    绑定在入口循环上的服务会随之消失，所以必须另开线程长期持有。
    """

    def __init__(self, server: Any, logger: Any, extras: tuple[Any, ...] = ()):
        self._server = server
        self._logger = logger
        self._extras = extras
        self._started: list[Any] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None

    def start(self, timeout: float = 10.0) -> None:
        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._server.start())
                for extra in self._extras:
                    try:
                        loop.run_until_complete(extra.start())
                        self._started.append(extra)
                    except BaseException as exc:  # noqa: BLE001 - 附加服务失败不影响主服务
                        self._logger.warning("bridge extra service failed: %s", exc)
            except BaseException as exc:  # noqa: BLE001
                self._error = exc
                self._ready.set()
                loop.close()
                return
            self._ready.set()
            try:
                loop.run_forever()
            finally:
                for extra in self._started:
                    try:
                        loop.run_until_complete(extra.stop())
                    except BaseException:  # noqa: BLE001
                        pass
                self._started.clear()
                try:
                    loop.run_until_complete(self._server.stop())
                except BaseException:  # noqa: BLE001
                    pass
                loop.close()

        self._thread = threading.Thread(target=run, name="neko-bridge", daemon=True)
        self._thread.start()
        self._ready.wait(timeout)
        if self._error is not None:
            raise RuntimeError(f"bridge server failed: {self._error}")
        if not self._ready.is_set():
            raise RuntimeError("bridge server start timeout")

    @property
    def loop(self) -> asyncio.AbstractEventLoop | None:
        return self._loop

    def call(self, coro: Any) -> Any:
        if self._loop is None:
            raise RuntimeError("bridge runtime not started")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def close(self, timeout: float = 5.0) -> None:
        loop = self._loop
        thread = self._thread
        if loop is None or thread is None:
            return
        try:
            loop.call_soon_threadsafe(loop.stop)
        except RuntimeError:
            pass
        thread.join(timeout)


@neko_plugin
class NekoPassportBridgePlugin(NekoPluginBase):
    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self.logger = ctx.logger
        self._server: BridgeServer | None = None
        self._asr: WhisperASR | None = None
        self._reply_adapter: AssistantReplyAdapter | None = None
        self._discovery: DiscoveryResponder | None = None
        self._runtime: _BridgeRuntime | None = None
        self._poll_stop: threading.Event | None = None
        self._poll_thread: threading.Thread | None = None
        self._last_conv_count = -1
        self._port = 8765
        self._frontend_js: str | None = None
        self._patch_frontend = True

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        try:
            config = await self.config.dump()
            settings = config.get("bridge", {})
            self._port = int(settings.get("port") or 8765)
            token = str(settings.get("token") or "").strip() or "neko-passport-dev"
            host = str(settings.get("host") or "").strip() or "0.0.0.0"
            language = settings.get("whisper_language") or None
            self._asr = WhisperASR(
                model_name=settings.get("whisper_model", "base"),
                language=language,
                local_files_only=bool(settings.get("local_files_only", False)),
                max_seconds=float(settings.get("max_audio_seconds", 30)),
                on_text=self._on_asr_text,
                on_error=self._on_device_error,
            )
            self._server = BridgeServer(
                host=host,
                port=self._port,
                token=token,
                cache_capacity=int(settings.get("cache_capacity", 128)),
                on_event=self._on_device_event,
                on_audio=self._on_audio,
            )
            enable_discovery = bool(settings.get("discovery", True))
            extras: tuple[Any, ...] = ()
            if enable_discovery:
                self._discovery = DiscoveryResponder(self._port)
                extras = (self._discovery,)
                self._server.extra_status["自动发现"] = f"UDP {DISCOVERY_PORT} 已开启"
            else:
                self._server.extra_status["自动发现"] = "已关闭，设备需手动填地址"

            frontend_js = settings.get("frontend_js_path") or None
            self._frontend_js = frontend_js
            self._patch_frontend = bool(settings.get("patch_frontend", True))
            if self._patch_frontend:
                result = await asyncio.to_thread(patch_frontend, frontend_js)
                self.logger.info("frontend patch: %s", result)
            state = await asyncio.to_thread(patch_status, frontend_js)
            if state.get("patched"):
                self._server.extra_status["前端钩子"] = "已注入"
            elif state.get("found"):
                self._server.extra_status["前端钩子"] = "未注入（可手动重试）"
            else:
                self._server.extra_status["前端钩子"] = "未找到前端文件"
            self._runtime = _BridgeRuntime(self._server, self.logger, extras)
            await asyncio.to_thread(self._runtime.start)
            panel_url = f"http://127.0.0.1:{self._port}/"
            self.logger.info("AI Passport bridge panel: %s", panel_url)
            if bool(settings.get("panel_auto_open", True)):
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, _open_browser, panel_url)

            records = await asyncio.to_thread(
                lambda: self.bus.conversations.get(max_count=100)
            )
            if inspect.isawaitable(records):
                records = await records
            self._reply_adapter = AssistantReplyAdapter(self._forward_reply)
            self._reply_adapter.seed(records)
            self._start_polling()
            if self._asr is not None and self._runtime is not None:
                # 后台预热模型，首次说话不用再等下载。
                self._runtime.call(self._asr.warmup())
            return Ok({"status": "ready", "port": self._port, "panel": panel_url})
        except Exception as exc:
            self.logger.error("AI Passport bridge startup failed: %s", exc, exc_info=True)
            await self._stop_components()
            return Err(SdkError(f"AI Passport bridge startup failed: {exc}"))

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        if self._patch_frontend:
            try:
                # 正常退出保留钩子（页面比插件先加载）；只有卸载才由清理进程还原前端。
                result = await asyncio.to_thread(spawn_uninstall_watcher, self._frontend_js)
                self.logger.info("uninstall watcher: %s", result)
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响插件退出
                self.logger.warning("uninstall watcher failed: %s", exc)
        await self._stop_components()
        return Ok({"status": "stopped"})

    @plugin_entry(id="status", name="Bridge status", description="Show LAN bridge state")
    async def status(self, **_):
        return Ok(
            {
                "connected": bool(self._server and self._server.connected),
                "pending_messages": self._server.pending_count if self._server else 0,
                "port": self._port,
            }
        )

    async def _stop_components(self) -> None:
        stop, self._poll_stop = self._poll_stop, None
        thread, self._poll_thread = self._poll_thread, None
        if stop is not None:
            stop.set()
        if thread is not None:
            thread.join(3.0)
        if self._asr is not None:
            await self._asr.stop()
            self._asr = None
        self._discovery = None
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            # 服务器在独立线程里，关闭失败不影响插件退出。
            try:
                await asyncio.to_thread(runtime.close)
            except Exception as exc:  # noqa: BLE001
                self.logger.debug("bridge server stop skipped: %s", exc)

    def _start_polling(self) -> None:
        stop = threading.Event()
        self._poll_stop = stop

        def run() -> None:
            while not stop.is_set():
                try:
                    records = self.bus.conversations.get(max_count=100)
                    if inspect.isawaitable(records):
                        in_loop = asyncio.run_coroutine_threadsafe(
                            _await_value(records), self._runtime.loop
                        )
                        records = in_loop.result(5.0)
                    if self._reply_adapter is not None and self._runtime is not None:
                        count = len(records)
                        if count != self._last_conv_count:
                            self._last_conv_count = count
                            recent = [
                                (getattr(r, "turn_type", None), (getattr(r, "content", "") or "")[:20])
                                for r in records[-3:]
                            ]
                            self.logger.info("conversations records=%d recent=%s", count, recent)
                        self._runtime.call(
                            self._reply_adapter.handle_records(records)
                        ).result(5.0)
                except Exception as exc:  # noqa: BLE001 - 轮询失败不终止插件
                    self.logger.debug("message poll failed: %s", exc)
                stop.wait(0.4)

        self._poll_thread = threading.Thread(
            target=run, name="neko-reply-poll", daemon=True
        )
        self._poll_thread.start()

    async def _on_device_event(self, packet: dict[str, Any]) -> None:
        kind = packet["type"]
        if kind in {"audio_start", "audio_end"}:
            await self._asr.handle_event(packet)
        elif kind == "asr_text":
            text = packet.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("asr_text requires non-empty text")
            await self._submit_text(f"passport-text-{packet['id']}", text.strip())
        elif kind == "wake":
            self.logger.debug("AI Passport wake event: %s", packet["id"])
        elif kind == "battery":
            self.logger.debug("AI Passport battery: %s", packet.get("percent"))

    async def _on_audio(self, chunk: AudioChunk) -> None:
        await self._asr.handle_audio(chunk)

    async def _on_asr_text(self, stream_id: int, text: str) -> None:
        if not text:
            await self._on_device_error("没有听清，请重试")
            return
        self.logger.info("ASR -> %s", text)
        await self._submit_text(f"passport-audio-{stream_id}", text)

    async def _submit_text(self, message_id: str, text: str) -> None:
        result = self.push_message(
            source="ai_passport",
            visibility=["chat"],
            ai_behavior="respond",
            parts=[{"type": "text", "text": text}],
            metadata={"transport": "ai_passport", "client_message_id": message_id},
        )
        if not result.get("submitted"):
            raise RuntimeError(result.get("reason", "message submission failed"))

    async def _forward_reply(self, message_id: str, text: str) -> None:
        if self._server is None:
            return
        self.logger.info("reply -> device: %s", text[:60])
        await self._server.send_packet(make_tts_packet(message_id, "start"))
        try:
            await self._server.send_reply(message_id, text)
        finally:
            await self._server.send_packet(make_tts_packet(message_id, "done"))

    async def _on_device_error(self, message: str) -> None:
        if self._server is not None:
            await self._server.send_packet(
                make_packet("error", message=message[:512])
            )
