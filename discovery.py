from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

from .bridge_server import local_ip

logger = logging.getLogger(__name__)

DISCOVERY_PORT = 8766
PROBE = b"NEKO_DISCOVER/1"


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    def __init__(self, port: int, on_probe=None):
        self._port = port
        self._on_probe = on_probe
        self._transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self._transport = transport
        sock = transport.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            except OSError:
                pass

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        if self._transport is None:
            return
        if data.strip() != PROBE:
            return
        # 设备只看 host/port，收到就用它拼 ws://，所以每次都回当前出口地址。
        reply = json.dumps({"host": local_ip(), "port": self._port}).encode()
        try:
            self._transport.sendto(reply, addr)
        except OSError as exc:
            logger.debug("discovery reply to %s failed: %s", addr, exc)
            return
        logger.info("discovery probe from %s -> ws://%s:%d", addr[0], local_ip(), self._port)
        if self._on_probe is not None:
            self._on_probe(addr)

    def error_received(self, exc: Exception) -> None:  # type: ignore[override]
        logger.debug("discovery socket error: %s", exc)


class DiscoveryResponder:
    """监听 UDP 8766，回应设备的局域网广播，让设备无需填写电脑 IP。"""

    def __init__(self, port: int, *, bind: str = "0.0.0.0"):
        self._port = port
        self._bind = bind
        self._transport: asyncio.DatagramTransport | None = None
        self._protocol: _DiscoveryProtocol | None = None
        self.last_probe: tuple[str, float] | None = None

    async def start(self) -> None:
        if self._transport is not None:
            return
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _DiscoveryProtocol(self._port, self._note_probe),
            local_addr=(self._bind, DISCOVERY_PORT),
            reuse_port=False,
            allow_broadcast=True,
        )
        self._transport = transport
        self._protocol = protocol  # type: ignore[assignment]
        logger.info("discovery listening on udp/%d", DISCOVERY_PORT)

    def _note_probe(self, addr) -> None:
        self.last_probe = (addr[0], time.monotonic())

    async def stop(self) -> None:
        transport, self._transport = self._transport, None
        self._protocol = None
        if transport is not None:
            transport.close()
            await asyncio.sleep(0)
