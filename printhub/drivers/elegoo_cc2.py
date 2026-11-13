"""Elegoo Centauri Carbon 2 / Centauri 2 (2026): JSON-RPC поверх MQTT-брокера на принтере (:1883).

Требуется режим «LAN Only» на принтере. Пароль MQTT — 123456 или код доступа, если он включён.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import socket
import time
from typing import Any, AsyncIterator, Optional

import httpx

from ..models import Capabilities, DiscoveredPrinter, FileEntry, MaterialSlot, PrinterState, Temperature
from .base import DriverError, PrinterDriver, ProgressCallback, mjpeg_frames_from_http
from .mqtt_util import MqttBridge

M_ATTRS, M_STATUS = 1001, 1002
M_START, M_PAUSE, M_STOP, M_RESUME = 1020, 1021, 1022, 1023
M_LIGHT, M_SPEED = 1029, 1031
M_VIDEO, M_FILES, M_DELETE = 1042, 1044, 1047
M_CANVAS = 2005
PUSH_STATUS, PUSH_ATTRS = 6000, 6008

MACHINE = {0: "инициализация", 1: "готов", 2: "печать", 3: "работа с филаментом", 4: "работа с филаментом",
           5: "автовыравнивание", 6: "калибровка PID", 7: "тест резонанса", 8: "самопроверка",
           9: "обновление", 10: "парковка", 11: "передача файла", 12: "сборка таймлапса",
           13: "работа экструдера", 14: "аварийная остановка", 15: "восстановление после сбоя питания"}
SPEED_NAME = {0: "тихий", 1: "стандарт", 2: "спорт", 3: "турбо"}
ERRORS = {1003: "неверный параметр", 1009: "принтер занят", 1010: "принтер не печатает", 1021: "файл не найден"}


def deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            deep_merge(dst[k], v)
        else:
            dst[k] = v


class ElegooCC2Driver(PrinterDriver):
    kind = "elegoo_cc2"
    title = "Elegoo Centauri 2 / Carbon 2"
    default_port = 1883
    required_fields = ("host",)
    help = ("Centauri Carbon 2 и Centauri 2 (2026). Включите на принтере режим «LAN Only». "
            "Код доступа — если он задан на принтере (иначе оставьте пустым).")

    MIN_INTERVAL = 1.2  # принтер «замолкает», если слать запросы чаще

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.mqtt: Optional[MqttBridge] = None
        self.client_id = f"1_PC_{random.randint(1000, 9999)}"
        self.sn = self.cfg.serial
        self.full: dict[str, Any] = {}
        self.attrs: dict[str, Any] = {}
        self.canvas: dict[str, Any] = {}
        self._id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self._req_lock = asyncio.Lock()
        self._last_req = 0.0

    @property
    def token(self) -> str:
        return self.cfg.access_code or "123456"

    # ------------------------------------------------------------------ MQTT
    async def _ensure_sn(self) -> None:
        if self.sn:
            return
        d = await self.probe(self.cfg.host, timeout=3)
        if d and d.serial:
            self.sn = self.cfg.serial = d.serial
            return
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                r = await c.get(f"http://{self.cfg.host}/system/info", params={"X-Token": self.token})
                self.sn = self.cfg.serial = ((r.json().get("system_info") or {}).get("sn")) or ""
        except Exception:  # noqa: BLE001
            pass
        if not self.sn:
            raise DriverError("Не удалось узнать серийный номер принтера — введите его вручную")

    async def run(self) -> None:
        await self._ensure_sn()
        m = MqttBridge(self.cfg.host, self.port, username="elegoo", password=self.token, client_id=self.client_id)
        try:
            await m.connect(timeout=15)
        except ConnectionError as e:
            if "authorized" in str(e).lower() or "password" in str(e).lower():
                raise DriverError("Неверный код доступа") from e
            raise
        self.mqtt = m
        base = f"elegoo/{self.sn}"
        m.subscribe(f"{base}/{self.client_id}/api_response")
        m.subscribe(f"{base}/api_status")
        m.subscribe(f"{base}/{self.client_id}_req/register_response")
        m.publish(f"{base}/api_register", {"client_id": self.client_id, "request_id": f"{self.client_id}_req"})
        reader = asyncio.create_task(self._reader())
        try:
            await asyncio.sleep(1.0)
            self.attrs = (await self.request(M_ATTRS, {})) or {}
            self.status.firmware = str(self.attrs.get("firmware_version") or self.attrs.get("version") or "")
            self.full = (await self.request(M_STATUS, {})) or {}
            await self._apply()
            try:
                self.canvas = await self.request(M_CANVAS, {})
            except DriverError:
                self.canvas = {}
            n = 0
            while not reader.done():
                await asyncio.sleep(10)
                n += 1
                m.publish(f"{base}/{self.client_id}/api_request", {"type": "PING"})
                if n % 6 == 0:  # раз в минуту — полное состояние на случай потерянных дельт
                    self.full = (await self.request(M_STATUS, {})) or self.full
                    await self._apply()
            reader.result()
        finally:
            reader.cancel()
            await self.close()

    async def close(self) -> None:
        if self.mqtt:
            m, self.mqtt = self.mqtt, None
            await m.close()

    async def _reader(self) -> None:
        assert self.mqtt
        async for topic, payload in self.mqtt.messages():
            try:
                msg = json.loads(payload)
            except ValueError:
                continue
            if topic.endswith("register_response"):
                if msg.get("error") not in (None, "ok"):
                    raise DriverError(f"Принтер отклонил подключение: {msg.get('error')}")
                continue
            if msg.get("type") == "PONG":
                continue
            method = msg.get("method")
            if topic.endswith("api_status"):
                params = msg.get("params") or msg.get("result") or {}
                if method == PUSH_STATUS:
                    deep_merge(self.full, params)
                    await self._apply()
                elif method == PUSH_ATTRS:
                    deep_merge(self.attrs, params)
                continue
            fut = self.pending.pop(msg.get("id"), None)
            if fut and not fut.done():
                res = msg.get("result") or {}
                code = int(res.get("error_code") or 0)
                if code:
                    fut.set_exception(DriverError(ERRORS.get(code, f"ошибка {code}")))
                else:
                    fut.set_result(res)

    async def request(self, method: int, params: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
        if not self.mqtt:
            raise DriverError("Нет соединения с принтером")
        async with self._req_lock:
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last_req)
            if wait > 0:
                await asyncio.sleep(wait)
            self._id += 1
            rid = self._id
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self.pending[rid] = fut
            self.mqtt.publish(f"elegoo/{self.sn}/{self.client_id}/api_request",
                              {"id": rid, "method": method, "params": params})
            self._last_req = time.monotonic()
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            self.pending.pop(rid, None)
            raise DriverError(f"Принтер не ответил (метод {method})") from e

    # ------------------------------------------------------------------ статус
    async def _apply(self) -> None:
        f = self.full
        st = self.status
        ms = f.get("machine_status") or {}
        ps = f.get("print_status") or {}
        code = int(ms.get("status") or 0)
        sub = int(ms.get("sub_status") or 0)
        st.state_raw = f"{code}/{sub}"
        st.message = ""
        if code == 2:
            if sub in (2501, 2502):
                st.state = PrinterState.PAUSED
            elif sub in (2503,):
                st.state = PrinterState.BUSY
                st.message = "Остановка…"
            elif sub == 2504:
                st.state = PrinterState.CANCELLED
            elif sub == 2077:
                st.state = PrinterState.FINISHED
            elif sub in (1045, 1405):
                st.state = PrinterState.PREPARING
            else:
                st.state = PrinterState.PRINTING
        elif code == 1:
            state = str(ps.get("state", "")).lower()
            st.state = {"complete": PrinterState.FINISHED, "completed": PrinterState.FINISHED,
                        "cancelled": PrinterState.CANCELLED, "error": PrinterState.ERROR}.get(state, PrinterState.IDLE)
        elif code == 14:
            st.state = PrinterState.ERROR
            st.error = "Аварийная остановка"
        else:
            st.state = PrinterState.BUSY
            st.message = MACHINE.get(code, f"занят ({code})").capitalize()
        exc = ms.get("exception_status") or []
        st.error = f"Ошибки: {exc}" if exc else ("" if st.state != PrinterState.ERROR else st.error)
        st.filename = ps.get("filename") or ""
        st.layer = ps.get("current_layer")
        st.total_layers = ps.get("total_layer")
        st.elapsed_s = ps.get("print_duration")
        st.remaining_s = ps.get("remaining_time_sec")
        prog = ps.get("progress", ms.get("progress"))
        st.progress = float(prog) if prog is not None else None
        temps = {}
        for key, label in (("extruder", "nozzle"), ("heater_bed", "bed"), ("ztemperature_sensor", "chamber")):
            o = f.get(key) or {}
            if o.get("temperature") is not None:
                temps[label] = Temperature(o.get("temperature"), o.get("target") or None)
        st.temps = temps
        fans = f.get("fans") or {}
        st.fans = {label: round(float((fans.get(k) or {}).get("speed", 0)) / 255 * 100)
                   for k, label in (("fan", "part"), ("aux_fan", "aux"), ("box_fan", "chamber")) if k in fans}
        gm = f.get("gcode_move_inf") or {}
        if "speed_mode" in gm:
            st.speed_level = SPEED_NAME.get(int(gm["speed_mode"]), "")
        led = f.get("led") or {}
        st.light = bool(led.get("status")) if "status" in led else None
        slots = []
        for cv in (self.canvas.get("canvas_list") or []):
            for t in cv.get("tray_list") or []:
                slots.append(MaterialSlot(id=f"{cv.get('canvas_id')}.{t.get('tray_id')}",
                                          name=t.get("filament_name") or t.get("filament_type", ""),
                                          type=t.get("filament_type", ""), color=t.get("filament_color", ""),
                                          active=self.canvas.get("active_tray_id") == t.get("tray_id")))
        st.materials = slots
        await self.emit()

    # ------------------------------------------------------------------ команды
    def capabilities(self) -> Capabilities:
        return Capabilities(upload=True, start=True, pause=True, resume=True, stop=True, files=True,
                            delete=True, camera=True, light=True, speed=True)

    async def pause(self) -> None:
        await self.request(M_PAUSE, {})

    async def resume(self) -> None:
        await self.request(M_RESUME, {})

    async def cancel(self) -> None:
        await self.request(M_STOP, {})

    async def set_light(self, on: bool) -> None:
        await self.request(M_LIGHT, {"power": 1 if on else 0})

    async def set_speed(self, value: str) -> None:
        v = int(float(value))
        mode = v - 1 if v <= 4 else (0 if v <= 75 else 1 if v <= 125 else 2 if v <= 175 else 3)
        await self.request(M_SPEED, {"mode": max(0, min(mode, 3))})

    async def start_print(self, remote_path: str, options: Optional[dict[str, Any]] = None) -> None:
        o = options or {}
        await self.request(M_START, {"storage_media": "local", "filename": os.path.basename(remote_path),
                                     "config": {"delay_video": bool(o.get("timelapse")),
                                                "printer_check": bool(o.get("calibration", True)),
                                                "print_layout": "A", "bedlevel_force": False, "slot_map": []}},
                           timeout=20)

    async def list_files(self) -> list[FileEntry]:
        res = await self.request(M_FILES, {"storage_media": "local"}, timeout=15)
        items = res.get("file_list") or res.get("files") or res.get("list") or []
        out = []
        for f in items:
            name = f.get("filename") or f.get("name") or f.get("file_name") or ""
            if not name:
                continue
            out.append(FileEntry(name=os.path.basename(name), path=name, size=f.get("size"),
                                 modified=f.get("create_time") or f.get("modified")))
        return out

    async def delete_file(self, remote_path: str) -> None:
        if os.path.basename(remote_path) == self.status.filename and self.status.state in (
                PrinterState.PRINTING, PrinterState.PAUSED):
            raise DriverError("Нельзя удалить файл, который сейчас печатается")
        await self.request(M_DELETE, {"storage_media": "local", "file_path": [remote_path]})

    async def upload(self, local_path: str, remote_name: str,
                     progress: Optional[ProgressCallback] = None) -> str:
        total = os.path.getsize(local_path)
        md5 = hashlib.md5()
        with open(local_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                md5.update(chunk)
        name = os.path.basename(remote_name)
        async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as c:
            with open(local_path, "rb") as fh:
                offset = 0
                while offset < total or total == 0:
                    chunk = fh.read(1 << 20)
                    end = offset + len(chunk) - 1
                    r = await c.put(f"http://{self.cfg.host}/upload", content=chunk, headers={
                        "Content-Range": f"bytes {offset}-{max(end, offset)}/{total}", "X-File-Name": name,
                        "X-File-MD5": md5.hexdigest(), "X-Token": self.token,
                        "Content-Type": "application/octet-stream"})
                    if r.status_code == 401:
                        raise DriverError("Неверный код доступа")
                    r.raise_for_status()
                    body = r.json() if r.content else {}
                    if int(body.get("error_code") or 0):
                        raise DriverError(f"Ошибка загрузки: {body}")
                    offset += len(chunk)
                    if progress:
                        progress(offset, total)
                    if total == 0:
                        break
        return name

    async def camera_frames(self) -> AsyncIterator[bytes]:
        try:
            await self.request(M_VIDEO, {"enable": True})
        except DriverError:
            pass
        async for f in mjpeg_frames_from_http(f"http://{self.cfg.host}:8080/?action=stream"):
            yield f

    async def test_connection(self) -> dict[str, Any]:
        d = await self.probe(self.cfg.host, timeout=3)
        res: dict[str, Any] = {"ok": True}
        if d:
            res.update(model=d.model, serial=d.serial)
            if d.extra.get("lan_status") == 0:
                res["warning"] = "Режим «LAN Only» выключен — локальное управление может не работать"
        m = MqttBridge(self.cfg.host, self.port, username="elegoo", password=self.token, client_id=self.client_id)
        try:
            await m.connect(timeout=10)
        except ConnectionError as e:
            return {"ok": False, "error": f"MQTT: {e}"}
        finally:
            await m.close()
        return res

    @classmethod
    async def _udp(cls, target: str, timeout: float) -> list[DiscoveredPrinter]:
        loop = asyncio.get_running_loop()
        found: dict[str, DiscoveredPrinter] = {}

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                try:
                    r = json.loads(data).get("result") or {}
                except ValueError:
                    return
                if not r.get("sn"):
                    return
                model = r.get("machine_model") or "Centauri Carbon 2"
                notes = "Включите «LAN Only» на принтере." if r.get("lan_status") == 0 else ""
                needs = ["access_code"] if r.get("token_status") == 1 else []
                found[r["sn"]] = DiscoveredPrinter(kind="elegoo_cc2", host=addr[0], name=r.get("host_name") or model,
                                                   model=f"Elegoo {model}", serial=r["sn"], port=1883, notes=notes,
                                                   needs=needs, extra={"lan_status": r.get("lan_status")})

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.setblocking(False)
        transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
        try:
            for _ in range(3):
                try:
                    transport.sendto(b'{"id":0,"method":7000}', (target, 52700))
                except OSError:
                    pass
                await asyncio.sleep(timeout / 3)
        finally:
            transport.close()
        return list(found.values())

    @classmethod
    async def discover(cls, timeout: float = 4.0) -> list[DiscoveredPrinter]:
        return await cls._udp("255.255.255.255", timeout)

    @classmethod
    async def probe(cls, host: str, timeout: float = 1.5) -> Optional[DiscoveredPrinter]:
        res = await cls._udp(host, max(timeout, 1.5))
        return res[0] if res else None
