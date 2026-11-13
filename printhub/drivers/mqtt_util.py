"""Асинхронная обёртка над paho-mqtt (клиент работает в своём потоке, сообщения — в asyncio-очередь)."""
from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any, AsyncIterator, Optional, Union

import paho.mqtt.client as mqtt

_CLOSED = object()


class MqttBridge:
    def __init__(self, host: str, port: int, *, username: str = "", password: str = "",
                 client_id: str = "", tls: bool = False, verify_tls: bool = False, keepalive: int = 60,
                 ssl_context: Optional[ssl.SSLContext] = None):
        self.host, self.port = host, port
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=2000)
        self._connected: Optional[asyncio.Future] = None
        self.keepalive = keepalive
        self.client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=client_id or "", protocol=mqtt.MQTTv311, clean_session=True)
        if username:
            self.client.username_pw_set(username, password)
        if ssl_context is not None:
            self.client.tls_set_context(ssl_context)
        elif tls:
            ctx = ssl.create_default_context()
            if not verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            self.client.tls_set_context(ctx)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.closed = False
        self.disconnect_reason = ""

    # ------------------------------------------------------------------ колбэки (поток paho)
    def _call(self, fn: Any, *args: Any) -> None:
        if self.loop and not self.loop.is_closed():
            self.loop.call_soon_threadsafe(fn, *args)

    def _on_connect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        def done() -> None:
            if self._connected and not self._connected.done():
                if reason_code.is_failure:
                    self._connected.set_exception(ConnectionError(f"MQTT: {reason_code}"))
                else:
                    self._connected.set_result(True)
        self._call(done)

    def _on_disconnect(self, client: Any, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        self.disconnect_reason = str(reason_code)

        def done() -> None:
            if self._connected and not self._connected.done():
                self._connected.set_exception(ConnectionError(f"MQTT отключение: {reason_code}"))
            self._put(_CLOSED)
        self._call(done)

    def _on_message(self, client: Any, userdata: Any, msg: Any) -> None:
        self._call(self._put, (msg.topic, msg.payload))

    def _put(self, item: Any) -> None:
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        self.queue.put_nowait(item)

    # ------------------------------------------------------------------ API
    async def connect(self, timeout: float = 15.0) -> None:
        self.loop = asyncio.get_running_loop()
        self._connected = self.loop.create_future()
        await asyncio.wait_for(asyncio.to_thread(self.client.connect, self.host, self.port, self.keepalive), timeout)
        self.client.loop_start()
        try:
            await asyncio.wait_for(self._connected, timeout)
        except Exception:
            self.client.loop_stop()
            raise

    def subscribe(self, topic: str, qos: int = 0) -> None:
        self.client.subscribe(topic, qos)

    def publish(self, topic: str, payload: Union[str, bytes, dict[str, Any]], qos: int = 0) -> None:
        if isinstance(payload, dict):
            payload = json.dumps(payload, separators=(",", ":"))
        info = self.client.publish(topic, payload, qos)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise ConnectionError(f"MQTT publish: {mqtt.error_string(info.rc)}")

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        while True:
            item = await self.queue.get()
            if item is _CLOSED:
                raise ConnectionError(f"MQTT соединение закрыто ({self.disconnect_reason})")
            yield item

    async def get(self, timeout: float) -> Optional[tuple[str, bytes]]:
        try:
            item = await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            return None
        if item is _CLOSED:
            raise ConnectionError(f"MQTT соединение закрыто ({self.disconnect_reason})")
        return item

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.client.disconnect()
        except Exception:  # noqa: BLE001
            pass
        await asyncio.to_thread(self.client.loop_stop)
