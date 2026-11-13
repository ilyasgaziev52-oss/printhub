"""Elegoo SDCP V3 (WebSocket :3030): Centauri Carbon, Saturn 4 Ultra / 16K, Mars 5 Ultra.

ВАЖНО: прошивка Centauri Carbon падает (и обрывает печать) от неизвестных команд или
неверных параметров. Здесь отправляются только задокументированные Cmd с полными данными.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import socket
import time
import uuid
from typing import Any, AsyncIterator, Optional

import httpx
import websockets

from ..models import Capabilities, DiscoveredPrinter, FileEntry, PrinterState, Technology, Temperature
from .base import DriverError, NotSupported, PrinterDriver, ProgressCallback, ffmpeg_jpeg_frames, mjpeg_frames_from_http

CMD_STATUS, CMD_ATTRS = 0, 1
CMD_START, CMD_PAUSE, CMD_STOP, CMD_RESUME = 128, 129, 130, 131
CMD_FILES, CMD_DELETE = 258, 259
CMD_VIDEO = 386
CMD_SET = 403
CMD_PUSH_PERIOD = 512

MACHINE_STATUS = {
    0: "готов", 1: "печать", 2: "передача файла", 3: "тест экспозиции", 4: "самопроверка",
    5: "автовыравнивание", 6: "тест резонанса", 7: "занят", 8: "проверка файла", 9: "парковка",
    10: "выгрузка филамента", 11: "калибровка PID",
}
PRINT_SUB = {
    0: "", 1: "парковка", 2: "опускание", 3: "засветка", 4: "подъём", 5: "ставится на паузу", 6: "пауза",
    7: "остановка", 8: "остановлено", 9: "завершено", 10: "проверка файла", 11: "проверка принтера",
    12: "возобновление", 13: "печать", 14: "ошибка", 15: "автовыравнивание", 16: "преднагрев",
    17: "тест резонанса", 18: "старт печати", 19: "выравнивание завершено", 20: "нагрев завершён",
    21: "парковка завершена", 22: "тест резонанса завершён", 23: "автоподача", 24: "выгрузка",
    25: "ошибка выгрузки", 26: "выгрузка на паузе",
}
PREPARING_SUB = {1, 10, 11, 15, 16, 17, 18}
ERROR_NUMBER = {1: "ошибка MD5 файла", 2: "ошибка чтения файла", 3: "не то разрешение экрана",
                4: "неверный формат файла", 5: "файл для другой модели"}
START_ACK = {1: "принтер занят", 2: "файл не найден", 3: "ошибка MD5", 4: "ошибка чтения файла",
             5: "не то разрешение", 6: "неизвестный формат", 7: "файл для другой модели"}
SPEED_PCT = {1: 50, 2: 100, 3: 130, 4: 160}
SPEED_NAME = {50: "тихий", 100: "стандарт", 130: "спорт", 160: "турбо"}
RESIN_WORDS = ("saturn", "mars", "jupiter")


def is_resin_name(name: str) -> bool:
    return any(w in (name or "").lower() for w in RESIN_WORDS)


def _temp(cur: Any, tgt: Any) -> Temperature:
    # старые прошивки присылали пару [цель, текущая]
    if isinstance(cur, list) and len(cur) == 2:
        return Temperature(float(cur[1]), float(cur[0]))
    try:
        c = float(cur) if cur is not None else None
    except (TypeError, ValueError):
        c = None
    try:
        t = float(tgt) if tgt not in (None, 0, 0.0) else None
    except (TypeError, ValueError):
        t = None
    return Temperature(c, t)


class ElegooSdcpDriver(PrinterDriver):
    kind = "elegoo_sdcp"
    title = "Elegoo SDCP (Centauri Carbon, Saturn/Mars Ultra)"
    default_port = 3030
    required_fields = ("host",)
    help = ("Centauri Carbon, Saturn 4 Ultra (16K), Mars 5 Ultra — без пароля, достаточно IP. "
            "Centauri Carbon 2 / Centauri 2 — выберите «Elegoo Centauri 2». Neptune 4 — «Klipper (Moonraker)».")

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.ws: Any = None
        self.attrs: dict[str, Any] = {}
        self.raw_status: dict[str, Any] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self._last_status = 0.0
        self._send_lock = asyncio.Lock()
        self._rgb = [0, 0, 0]
        self.mainboard_id = self.cfg.serial

    @property
    def resin(self) -> bool:
        if self.cfg.technology == Technology.RESIN.value:
            return True
        return is_resin_name(self.attrs.get("MachineName") or self.cfg.model)

    def accepted_extensions(self) -> tuple[str, ...]:
        if self.resin:
            ft = [f".{t.lower()}" for t in self.attrs.get("SupportFileType", []) if isinstance(t, str)]
            return tuple(ft) or (".goo", ".ctb")
        return (".gcode",)

    # ------------------------------------------------------------------ соединение
    async def _open(self) -> None:
        if not self.mainboard_id:
            d = await self.probe(self.cfg.host, timeout=3)
            if d and d.serial:
                self.mainboard_id = self.cfg.serial = d.serial
        url = f"ws://{self.cfg.host}:{self.port}/websocket"
        try:
            self.ws = await websockets.connect(url, max_size=8 * 2**20, ping_interval=None, open_timeout=10)
        except websockets.InvalidStatus as e:  # type: ignore[attr-defined]
            if "500" in str(e):
                raise DriverError("Принтер отклонил соединение: слишком много клиентов (закройте другие программы)") from e
            raise

    async def run(self) -> None:
        await self._open()
        reader = asyncio.create_task(self._reader())
        try:
            await self._send(CMD_ATTRS, {})
            await self._send(CMD_STATUS, {})
            await self._send(CMD_PUSH_PERIOD, {"TimePeriod": 3000})
            n = 0
            while not reader.done():
                await asyncio.sleep(5)
                n += 1
                if n % 4 == 0:
                    await self.ws.send("ping")
                if time.monotonic() - self._last_status > 10:
                    await self._send(CMD_STATUS, {})
            reader.result()
        finally:
            reader.cancel()
            await self.close()

    async def close(self) -> None:
        if self.ws is not None:
            ws, self.ws = self.ws, None
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        for f in self.pending.values():
            if not f.done():
                f.set_exception(DriverError("Соединение закрыто"))
        self.pending.clear()

    async def _reader(self) -> None:
        async for raw in self.ws:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            raw = raw.strip()
            if not raw or raw == "pong":
                continue
            raw = re.sub(r"^\d+(?=\{)", "", raw)  # некоторые прошивки добавляют длину перед JSON
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        topic = str(msg.get("Topic", ""))
        data = msg.get("Data") if isinstance(msg.get("Data"), dict) else {}
        if "Status" in msg or (data and "Status" in data) or topic.startswith("sdcp/status"):
            status = msg.get("Status") or data.get("Status") or {}
            self.raw_status.update(status)
            self._last_status = time.monotonic()
            await self._apply()
        elif "Attributes" in msg or (data and "Attributes" in data) or topic.startswith("sdcp/attributes"):
            self.attrs.update(msg.get("Attributes") or data.get("Attributes") or {})
            if self.attrs.get("MainboardID"):
                self.mainboard_id = self.attrs["MainboardID"]
            self.status.firmware = self.attrs.get("FirmwareVersion", self.status.firmware)
        elif topic.startswith("sdcp/response") or "RequestID" in data:
            rid = data.get("RequestID")
            fut = self.pending.pop(rid, None)
            if fut and not fut.done():
                fut.set_result(data.get("Data") or {})
        elif topic.startswith("sdcp/error"):
            code = ((data.get("Data") or {}).get("ErrorCode"))
            self.status.error = {1: "Ошибка MD5 при передаче файла", 2: "Неверный формат файла"}.get(code, f"Ошибка {code}")
            await self.emit()

    async def _send(self, cmd: int, data: dict[str, Any]) -> str:
        if self.ws is None:
            raise DriverError("Нет соединения с принтером")
        rid = secrets.token_hex(16)
        mbid = self.mainboard_id or ""
        msg = {"Id": mbid, "Data": {"Cmd": cmd, "Data": data, "RequestID": rid, "MainboardID": mbid,
                                    "TimeStamp": int(time.time()), "From": 0},
               "Topic": f"sdcp/request/{mbid}"}
        async with self._send_lock:
            await self.ws.send(json.dumps(msg))
        return rid

    async def request(self, cmd: int, data: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        rid = await self._send(cmd, data)
        self.pending[rid] = fut
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            self.pending.pop(rid, None)
            raise DriverError(f"Принтер не ответил на команду {cmd}") from e

    # ------------------------------------------------------------------ статус
    def _machine_codes(self) -> list[int]:
        cs = self.raw_status.get("CurrentStatus", 0)
        if isinstance(cs, list):
            return [int(x) for x in cs if isinstance(x, (int, float))] or [0]
        try:
            return [int(cs)]
        except (TypeError, ValueError):
            return [0]

    async def _apply(self) -> None:
        s = self.raw_status
        st = self.status
        pi = s.get("PrintInfo") or {}
        codes = self._machine_codes()
        primary = codes[1] if codes[0] == 2 and len(codes) > 1 else codes[0]
        sub = int(pi.get("Status") or 0)
        st.state_raw = f"{codes}/{sub}"
        msg = ""
        if primary == 1:
            if sub in (5, 6, 26):
                st.state = PrinterState.PAUSED
            elif sub == 7:
                st.state = PrinterState.BUSY
                msg = "Остановка печати…"
            elif sub == 8:
                st.state = PrinterState.CANCELLED
            elif sub == 9:
                st.state = PrinterState.FINISHED
            elif sub == 14:
                st.state = PrinterState.ERROR
            elif sub in PREPARING_SUB:
                st.state = PrinterState.PREPARING
                msg = PRINT_SUB.get(sub, "")
            else:
                st.state = PrinterState.PRINTING
        elif primary == 0:
            # подстатус «залипает» после окончания печати
            st.state = {9: PrinterState.FINISHED, 8: PrinterState.CANCELLED}.get(sub, PrinterState.IDLE)
        else:
            st.state = PrinterState.BUSY
            msg = MACHINE_STATUS.get(primary, f"занят ({primary})").capitalize()
        if 2 in codes and primary != 2:
            msg = (msg + " · " if msg else "") + "идёт передача файла"
        st.message = msg

        st.filename = pi.get("Filename") or ""
        st.layer = pi.get("CurrentLayer")
        st.total_layers = pi.get("TotalLayer")
        cur, tot = pi.get("CurrentTicks"), pi.get("TotalTicks")
        scale = 1000.0 if self.resin else 1.0  # CC1 — секунды, смола — миллисекунды
        try:
            st.elapsed_s = float(cur) / scale if cur is not None else None
            st.remaining_s = max(float(tot) - float(cur), 0) / scale if tot and cur is not None else None
        except (TypeError, ValueError):
            st.elapsed_s = st.remaining_s = None
        prog = pi.get("Progress")
        if prog:
            st.progress = float(prog)
        elif st.total_layers:
            st.progress = round(100.0 * float(st.layer or 0) / float(st.total_layers), 1)
        else:
            st.progress = None
        err = int(pi.get("ErrorNumber") or 0)
        st.error = ERROR_NUMBER.get(err, f"Ошибка {err}") if err else (st.error if st.state == PrinterState.ERROR else "")

        temps: dict[str, Temperature] = {}
        if self.resin:
            if "TempOfUVLED" in s:
                temps["uvled"] = _temp(s.get("TempOfUVLED"), None)
            if "TempOfTank" in s:
                temps["vat"] = _temp(s.get("TempOfTank"), s.get("TempTargetTank"))
            if s.get("TempOfBox"):
                temps["box"] = _temp(s.get("TempOfBox"), s.get("TempTargetBox"))
        else:
            temps["nozzle"] = _temp(s.get("TempOfNozzle"), s.get("TempTargetNozzle"))
            temps["bed"] = _temp(s.get("TempOfHotbed"), s.get("TempTargetHotbed"))
            if "TempOfBox" in s:
                temps["chamber"] = _temp(s.get("TempOfBox"), s.get("TempTargetBox"))
        st.temps = {k: v for k, v in temps.items() if v.current is not None}
        fans = s.get("CurrentFanSpeed") or {}
        st.fans = {k: v for k, v in (("part", fans.get("ModelFan")), ("aux", fans.get("AuxiliaryFan")),
                                     ("chamber", fans.get("BoxFan"))) if v is not None}
        pct = pi.get("PrintSpeedPct")
        st.speed_pct = float(pct) if pct else None
        st.speed_level = SPEED_NAME.get(int(pct), "") if pct else ""
        light = s.get("LightStatus") or {}
        if "SecondLight" in light:
            st.light = bool(light.get("SecondLight"))
            if isinstance(light.get("RgbLight"), list):
                self._rgb = light["RgbLight"]
        await self.emit()

    # ------------------------------------------------------------------ команды
    def capabilities(self) -> Capabilities:
        caps = [c for c in self.attrs.get("Capabilities", []) if isinstance(c, str)]
        cam = "VIDEO_STREAM" in caps if caps else not self.resin
        return Capabilities(upload=True, start=True, pause=True, resume=True, stop=True, files=True, delete=True,
                            camera=cam, light=not self.resin, gcode=False, speed=not self.resin)

    async def _ack(self, cmd: int, data: dict[str, Any], what: str) -> dict[str, Any]:
        res = await self.request(cmd, data)
        ack = int(res.get("Ack") or 0)
        if ack:
            raise DriverError(f"{what}: принтер ответил кодом {ack}")
        return res

    async def pause(self) -> None:
        await self._ack(CMD_PAUSE, {}, "Пауза")

    async def resume(self) -> None:
        await self._ack(CMD_RESUME, {}, "Продолжение")

    async def cancel(self) -> None:
        await self._ack(CMD_STOP, {}, "Остановка")

    async def start_print(self, remote_path: str, options: Optional[dict[str, Any]] = None) -> None:
        o = options or {}
        name = os.path.basename(remote_path)
        if self.resin:
            data: dict[str, Any] = {"Filename": name, "StartLayer": 0}
        else:
            data = {"Filename": name, "StartLayer": 0,
                    "Calibration_switch": 1 if o.get("calibration", True) else 0,
                    "PrintPlatformType": int(o.get("platform_side", 0)),
                    "Tlp_Switch": 1 if o.get("timelapse") else 0, "slot_map": []}
        res = await self.request(CMD_START, data, timeout=20)
        ack = int(res.get("Ack") or 0)
        if ack:
            raise DriverError(f"Печать не запущена: {START_ACK.get(ack, f'код {ack}')}")

    async def set_light(self, on: bool) -> None:
        if self.resin:
            raise NotSupported("У фотополимерного принтера нет подсветки")
        await self._ack(CMD_SET, {"LightStatus": {"SecondLight": 1 if on else 0, "RgbLight": self._rgb}}, "Свет")

    async def set_speed(self, value: str) -> None:
        v = int(float(value))
        pct = SPEED_PCT.get(v) if v <= 4 else min(SPEED_PCT.values(), key=lambda x: abs(x - v))
        await self._ack(CMD_SET, {"PrintSpeedPct": pct}, "Скорость")

    async def list_files(self) -> list[FileEntry]:
        res = await self.request(CMD_FILES, {"Url": "/local"}, timeout=15)
        out = []
        for f in res.get("FileList") or []:
            name = f.get("name", "")
            if int(f.get("type", 1)) == 0:
                continue
            out.append(FileEntry(name=os.path.basename(name), path=name,
                                 size=f.get("FileSize") or f.get("usedSize"), modified=f.get("CreateTime"),
                                 meta={k: f.get(k) for k in ("TotalLayers", "LayerHeight", "EstFilamentLength") if k in f}))
        return sorted(out, key=lambda x: x.modified or 0, reverse=True)

    async def delete_file(self, remote_path: str) -> None:
        if os.path.basename(remote_path) == self.status.filename and self.status.state in (
                PrinterState.PRINTING, PrinterState.PAUSED, PrinterState.PREPARING):
            raise DriverError("Нельзя удалить файл, который сейчас печатается")
        path = remote_path if remote_path.startswith("/") else f"/local/{remote_path}"
        res = await self.request(CMD_DELETE, {"FileList": [path], "FolderList": []})
        if res.get("ErrData"):
            raise DriverError(f"Не удалось удалить: {res['ErrData']}")

    async def upload(self, local_path: str, remote_name: str,
                     progress: Optional[ProgressCallback] = None) -> str:
        total = os.path.getsize(local_path)
        md5 = hashlib.md5()
        with open(local_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                md5.update(chunk)
        digest = md5.hexdigest()
        up_id = uuid.uuid4().hex
        name = os.path.basename(remote_name)
        if self.cfg.extra.get("upload_port"):
            ports = [int(self.cfg.extra["upload_port"])]
        else:
            ports = [3030] if self.resin else [80, 3030]  # CC1: SDK Elegoo грузит на :80
        last_err: Optional[Exception] = None
        for port in ports:
            url = f"http://{self.cfg.host}:{port}/uploadFile/upload"
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(60)) as c:
                    with open(local_path, "rb") as fh:
                        offset = 0
                        while True:
                            chunk = fh.read(1 << 20)
                            if not chunk and offset > 0:
                                break
                            r = await c.post(url, data={"Check": "1", "S-File-MD5": digest, "Offset": str(offset),
                                                        "Uuid": up_id, "TotalSize": str(total)},
                                             files={"File": (name, chunk, "application/octet-stream")})
                            r.raise_for_status()
                            body = r.json()
                            if not body.get("success") and body.get("code") != "000000":
                                raise DriverError(f"Ошибка загрузки: {body.get('messages')}")
                            offset += len(chunk)
                            if progress:
                                progress(offset, total)
                            if not chunk or offset >= total:
                                break
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_err = e
                continue
        else:
            raise DriverError(f"Не удалось подключиться для загрузки: {last_err}")
        # ждём, пока принтер проверит файл (CurrentStatus содержит 2)
        deadline = time.monotonic() + 120
        await asyncio.sleep(1.5)
        while time.monotonic() < deadline and 2 in self._machine_codes():
            await asyncio.sleep(1)
        if self.status.error.startswith("Ошибка MD5") or self.status.error.startswith("Неверный формат"):
            raise DriverError(self.status.error)
        return name

    async def camera_frames(self) -> AsyncIterator[bytes]:
        url = ""
        try:
            res = await self.request(CMD_VIDEO, {"Enable": 1})
            ack = int(res.get("Ack") or 0)
            if ack == 1:
                raise DriverError("Камера: превышено число одновременных просмотров")
            if ack == 2:
                raise NotSupported("У принтера нет камеры")
            url = str(res.get("VideoUrl") or "")
        except DriverError:
            if self.resin:
                raise
        try:
            if url.lower().startswith("rtsp"):
                async for f in ffmpeg_jpeg_frames(url, ["-rtsp_transport", "udp"], fps=2):
                    yield f
            else:
                if url and not url.startswith("http"):
                    url = "http://" + url
                url = url or f"http://{self.cfg.host}:3031/video"
                async for f in mjpeg_frames_from_http(url):
                    yield f
        finally:
            if self.ws is not None:
                try:
                    await self._send(CMD_VIDEO, {"Enable": 0})
                except Exception:  # noqa: BLE001
                    pass

    async def test_connection(self) -> dict[str, Any]:
        d = await self.probe(self.cfg.host, timeout=3)
        if d is None:
            raise DriverError("Принтер не отвечает на порту 3030")
        if d.extra.get("protocol", "").startswith("V1"):
            return {"ok": False, "error": "Этот принтер использует протокол SDCP V1 (MQTT) — пока не поддерживается"}
        return {"ok": True, "model": d.model, "serial": d.serial, "firmware": d.firmware}

    # ------------------------------------------------------------------ поиск
    @staticmethod
    def _parse_discovery(data: bytes, src: str) -> Optional[DiscoveredPrinter]:
        try:
            msg = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return None
        d = msg.get("Data") or {}
        a = d.get("Attributes", d) if isinstance(d, dict) else {}
        if not isinstance(a, dict) or not (a.get("MainboardID") or a.get("MachineName")):
            return None
        name = a.get("MachineName") or a.get("Name") or "Elegoo"
        proto = str(a.get("ProtocolVersion", ""))
        resin = is_resin_name(name)
        notes = ""
        if proto.startswith("V1"):
            notes = "Протокол SDCP V1 (MQTT): Saturn 3 Ultra / Mars 4 Ultra пока не поддерживаются."
        return DiscoveredPrinter(kind="elegoo_sdcp", host=a.get("MainboardIP") or src, name=a.get("Name") or name,
                                 model=name if name.lower().startswith("elegoo") else f"Elegoo {name}",
                                 serial=a.get("MainboardID", ""), firmware=a.get("FirmwareVersion", ""),
                                 technology="resin" if resin else "fdm", port=3030, notes=notes,
                                 extra={"protocol": proto})

    @classmethod
    async def _udp(cls, targets: list[str], timeout: float) -> list[DiscoveredPrinter]:
        loop = asyncio.get_running_loop()
        found: dict[str, DiscoveredPrinter] = {}

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                d = cls._parse_discovery(data, addr[0])
                if d:
                    found[d.serial or d.host] = d

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        sock.setblocking(False)
        transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
        try:
            for _ in range(3):
                for t in targets:
                    try:
                        transport.sendto(b"M99999", (t, 3000))
                    except OSError:
                        pass
                await asyncio.sleep(timeout / 3)
        finally:
            transport.close()
        return list(found.values())

    @classmethod
    async def discover(cls, timeout: float = 4.0) -> list[DiscoveredPrinter]:
        return await cls._udp(["255.255.255.255"], timeout)

    @classmethod
    async def probe(cls, host: str, timeout: float = 1.5) -> Optional[DiscoveredPrinter]:
        res = await cls._udp([host], max(timeout, 1.5))
        if res:
            return res[0]
        # некоторые прошивки не отвечают на M99999 без шлюза — проверим, открыт ли порт
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, 3030), timeout)
            w.close()
            return DiscoveredPrinter(kind="elegoo_sdcp", host=host, name=f"Elegoo {host}", port=3030,
                                     notes="Порт SDCP открыт, но принтер не ответил на запрос поиска")
        except (OSError, asyncio.TimeoutError):
            return None
