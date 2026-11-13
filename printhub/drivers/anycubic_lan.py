"""Anycubic LAN-режим: Kobra 3 / 3 V2 / 3 Max, Kobra S1 / S1 Max, Kobra 4, Kobra X, Photon P1.

Протокол: GET :18910/info → подписанный POST /ctrl → AES-128-CBC → логин/пароль MQTT (TLS, 9883 у FDM,
8883 у Photon P1). Загрузка — HTTP на :18910, камера — HTTP-FLV на :18088 (через ffmpeg).
На принтере должен быть включён LAN-режим (Настройки → Сеть → LAN Mode); облако и приложение
Anycubic при этом отключаются.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import random
import re
import ssl
import string
import tempfile
import time
import uuid
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlsplit

import httpx

from ..models import Capabilities, DiscoveredPrinter, FileEntry, MaterialSlot, PrinterState, Temperature
from .base import DriverError, NotSupported, PrinterDriver, ProgressCallback, ffmpeg_jpeg_frames
from .mqtt_util import MqttBridge

P = "anycubic/anycubicCloud/v1"
MODEL_IDS = {
    "20021": "Kobra 2 Pro", "20022": "Kobra 2 Plus", "20023": "Kobra 2 Max", "20024": "Kobra 3",
    "20025": "Kobra S1", "20026": "Kobra 3 Max", "20027": "Kobra 3 V2", "20028": "Kobra 4",
    "20029": "Kobra S1 Max", "20030": "Kobra X", "131": "Photon P1",
}
ENCLOSED = {"20025", "20029"}
PROJECT_STATE = {
    "preheating": PrinterState.PREPARING, "auto_leveling": PrinterState.PREPARING,
    "vibrating": PrinterState.PREPARING, "flow_calibrating": PrinterState.PREPARING,
    "downloading": PrinterState.PREPARING, "checking": PrinterState.PREPARING,
    "printing": PrinterState.PRINTING, "resuming": PrinterState.PRINTING, "resumed": PrinterState.PRINTING,
    "pausing": PrinterState.PAUSED, "paused": PrinterState.PAUSED, "stopping": PrinterState.BUSY,
    "stoped": PrinterState.CANCELLED, "stopped": PrinterState.CANCELLED, "finished": PrinterState.FINISHED,
    "complete": PrinterState.FINISHED, "failed": PrinterState.ERROR,
}
CODES = {10101: "принтер занят другой задачей", 10107: "закончился филамент", 10402: "закончился филамент",
         10401: "пауза пользователем", 10601: "остановлено пользователем", 11518: "застрял филамент"}
SPEED_NAME = {1: "тихий", 2: "стандарт", 3: "спорт"}
LAN_HELP = "Включите LAN-режим на принтере: Настройки → Сеть → LAN Mode (облако Anycubic при этом отключится)."


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def decrypt_bundle(token: str, data: dict[str, Any]) -> dict[str, Any]:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    b64 = data["info"]
    ct = base64.b64decode(b64 + "=" * (-len(b64) % 4))
    iv = str(data["token"]).encode()[:16].ljust(16, b"\0")
    dec = Cipher(algorithms.AES(token[16:32].encode()), modes.CBC(iv)).decryptor()
    raw = dec.update(ct) + dec.finalize()
    n = raw[-1] if raw else 0
    if 1 <= n <= 16 and raw.endswith(bytes([n]) * n):
        raw = raw[:-n]
    raw = raw.rstrip(b"\0")
    return json.loads(raw[:raw.rfind(b"}") + 1])


def sign_params(token: str) -> dict[str, str]:
    ts = str(int(time.time() * 1000))
    nonce = "".join(random.choices(string.ascii_letters + string.digits, k=6))
    return {"ts": ts, "nonce": nonce, "sign": _md5(_md5(token[:16]) + ts + nonce), "did": uuid.uuid4().hex.upper()}


def _rgb_hex(c: Any) -> str:
    if isinstance(c, list) and len(c) >= 3:
        return "#%02X%02X%02X" % tuple(int(x) for x in c[:3])
    return ""


def _rgba(hexcolor: str) -> list[int]:
    h = (hexcolor or "#FFFFFF").lstrip("#")
    try:
        return [int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 255]
    except ValueError:
        return [255, 255, 255, 255]


class AnycubicLanDriver(PrinterDriver):
    kind = "anycubic_lan"
    title = "Anycubic LAN (Kobra 3/S1/4/X, Photon P1)"
    default_port = 18910
    required_fields = ("host",)
    accepted_ext = (".gcode", ".gcode.3mf", ".3mf")
    help = ("Kobra 3 / 3 Max / 3 V2, Kobra S1 / S1 Max, Kobra 4, Kobra X, Photon P1. Достаточно IP, но на "
            "принтере нужно включить LAN-режим (Настройки → Сеть). Если стоит Rinkhals — можно выбрать Klipper (Moonraker).")

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.mqtt: Optional[MqttBridge] = None
        self.info: dict[str, Any] = {}
        self.bundle: dict[str, Any] = {}
        self.model_id = ""
        self.device_id = ""
        self.lcd = False
        self.data: dict[str, dict[str, Any]] = {}     # последние отчёты по типам
        self.boxes: dict[int, dict[str, Any]] = {}
        self.waiters: dict[str, list[asyncio.Future]] = {}
        self._task_id = "-1"
        self._sizes: dict[str, int] = {}

    def accepted_extensions(self) -> tuple[str, ...]:
        return (".pp1", ".pp1m") if self.lcd else self.accepted_ext

    # ------------------------------------------------------------------ handshake
    async def handshake(self) -> int:
        async with httpx.AsyncClient(timeout=8) as c:
            r = await c.get(f"http://{self.cfg.host}:{self.port}/info")
            r.raise_for_status()
            info = r.json()
            if info.get("ctrlType") != "lan":
                raise DriverError(LAN_HELP)
            tok = info["token"]
            p = urlsplit(info.get("ctrlInfoUrl") or f"http://{self.cfg.host}:{self.port}/ctrl")
            r = await c.post(f"http://{self.cfg.host}:{p.port or self.port}{p.path or '/ctrl'}", params=sign_params(tok))
            ctrl = r.json()
        if ctrl.get("code") != 200:
            raise DriverError(f"Принтер отклонил подключение: {ctrl}")
        self.info = info
        self.bundle = decrypt_bundle(tok, ctrl["data"])
        self.model_id = str(info.get("modelId") or self.bundle.get("modeId") or "")
        self.device_id = str(self.bundle.get("deviceId") or "")
        self.lcd = info.get("deviceType") == "lcd" or self.bundle.get("deviceType") == "lcd"
        if not self.cfg.serial:
            self.cfg.serial = str(info.get("cn") or "")
        if not self.cfg.model:
            self.cfg.model = info.get("modelName") or f"Anycubic {MODEL_IDS.get(self.model_id, self.model_id)}"
        m = re.match(r"(?:mqtts?://)?[^:/]+:(\d+)", str(self.bundle.get("broker", "")))
        return int(m.group(1)) if m else (8883 if self.lcd else 9883)

    def _ssl_ctx(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
        except ssl.SSLError:
            pass
        crt, pk = self.bundle.get("devicecrt"), self.bundle.get("devicepk")
        if crt and pk:
            paths = []
            for content in (crt, pk):
                fd, path = tempfile.mkstemp(suffix=".pem")
                with os.fdopen(fd, "w") as f:
                    f.write(content)
                paths.append(path)
            try:
                ctx.load_cert_chain(paths[0], paths[1])
            except (ssl.SSLError, OSError):
                pass
            finally:
                for pth in paths:
                    try:
                        os.unlink(pth)
                    except OSError:
                        pass
        return ctx

    # ------------------------------------------------------------------ MQTT
    def _topic(self, typ: str) -> str:
        mid = "pc" if self.lcd else "web"
        return f"{P}/{mid}/printer/{self.model_id}/{self.device_id}/{typ}"

    def _send(self, typ: str, action: str, data: Any = None, topic_type: Optional[str] = None,
              slicer: bool = False) -> str:
        if not self.mqtt:
            raise DriverError("Нет соединения с принтером")
        msgid = str(uuid.uuid4())
        topic = (f"{P}/slicer/printer/{self.model_id}/{self.device_id}/{typ}" if slicer
                 else self._topic(topic_type or typ))
        self.mqtt.publish(topic, {"type": typ, "action": action, "timestamp": int(time.time() * 1000),
                                  "msgid": msgid, "data": data})
        return msgid

    async def _wait_report(self, typ: str, timeout: float = 10) -> dict[str, Any]:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self.waiters.setdefault(typ, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError as e:
            raise DriverError(f"Принтер не прислал ответ ({typ})") from e
        finally:
            lst = self.waiters.get(typ, [])
            if fut in lst:
                lst.remove(fut)

    async def run(self) -> None:
        port = await self.handshake()
        client_id = self.bundle.get("clientId") if self.lcd else f"printhub_{uuid.uuid4().hex[:10]}"
        m = MqttBridge(self.cfg.host, port, username=self.bundle.get("username", ""),
                       password=self.bundle.get("password", ""), client_id=client_id or "", ssl_context=self._ssl_ctx())
        await m.connect(timeout=15)
        self.mqtt = m
        if self.lcd:
            m.subscribe(f"{P}/printer/+/{self.model_id}/{self.device_id}/+/report")
        else:
            m.subscribe(f"{P}/printer/public/{self.model_id}/{self.device_id}/#")
        reader = asyncio.create_task(self._reader())
        try:
            await self._initial_queries()
            n = 0
            while not reader.done():
                await asyncio.sleep(10)
                n += 1
                if n % 3 == 0:
                    self._poll()
            reader.result()
        finally:
            reader.cancel()
            await self.close()

    async def _initial_queries(self) -> None:
        if self.lcd:
            self._send("status", "query", topic_type="status")
            self._send("print", "query")
            self._send("light", "query")
        else:
            self._send("info", "query")
            self._send("multiColorBox", "getInfo")
            self._send("light", "query")
            self._send("video", "stopCapture")  # не держим камеру (и свет S1) включёнными без нужды

    def _poll(self) -> None:
        if self.lcd:
            self._send("print", "query")
        else:
            self._send("info", "query")

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
            parts = topic.split("/")
            if parts[-1] == "response":
                continue
            typ = parts[-2] if parts[-1] == "report" else parts[-1]
            typ = {"tempature": "temp"}.get(typ, typ)
            data = msg.get("data") if isinstance(msg.get("data"), dict) else {}
            if typ == "info":
                self.data["info"] = data
                if data.get("project"):
                    self.data["project"] = data["project"]
                if data.get("temp"):
                    self.data["temp"] = data["temp"]
            elif typ == "print":
                if "progress" in data or "curr_layer" in data:
                    self.data.setdefault("project", {}).update(data)
                if msg.get("state"):
                    self.data.setdefault("project", {})["state"] = msg["state"]
                code = msg.get("code")
                if code and int(code) != 200:
                    self.data["print_code"] = {"code": int(code), "msg": msg.get("msg", "")}
                elif code:
                    self.data.pop("print_code", None)
            elif typ == "temp":
                self.data["temp"] = data
            elif typ == "status":
                self.data["status"] = {"state": msg.get("state"), **data}
            elif typ == "multiColorBox":
                for b in data.get("multi_color_box", []) or []:
                    self.boxes.setdefault(int(b.get("id", 0)), {}).update(b)
            elif typ == "light":
                self.data["light"] = data
            else:
                self.data[typ] = {**data, "_state": msg.get("state")}
            for fut in self.waiters.pop(typ, []):
                if not fut.done():
                    fut.set_result({**data, "_state": msg.get("state"), "_code": msg.get("code")})
            if typ in ("info", "print", "temp", "status", "multiColorBox", "light"):
                await self._apply()

    # ------------------------------------------------------------------ статус
    async def _apply(self) -> None:
        st = self.status
        info = self.data.get("info", {})
        proj = self.data.get("project") or {}
        top = (self.data.get("status") or {}).get("state") or info.get("state")
        pstate = str(proj.get("state") or "").lower()
        st.state_raw = pstate or str(top or "")
        if pstate in PROJECT_STATE:
            st.state = PROJECT_STATE[pstate]
        elif top == "busy":
            st.state = PrinterState.BUSY
        elif top == "free" or info:
            st.state = PrinterState.IDLE
        else:
            st.state = PrinterState.UNKNOWN
        if st.state in (PrinterState.FINISHED, PrinterState.CANCELLED) and top == "busy":
            st.state = PrinterState.BUSY
        st.progress = float(proj["progress"]) if proj.get("progress") is not None else None
        st.layer = proj.get("curr_layer")
        st.total_layers = proj.get("total_layers")

        def minutes(v: Any) -> Optional[float]:
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f * 60 if f < 60 * 24 * 30 else f  # минуты; слишком большие значения — уже секунды
        st.remaining_s = minutes(proj.get("remain_time"))
        st.elapsed_s = minutes(proj.get("print_time"))
        st.filename = proj.get("display_filename") or proj.get("filename") or ""
        if proj.get("task_id") or proj.get("taskid"):
            self._task_id = str(proj.get("task_id") or proj.get("taskid"))
        t = self.data.get("temp") or {}
        temps = {}
        if "curr_nozzle_temp" in t:
            temps["nozzle"] = Temperature(t.get("curr_nozzle_temp"), t.get("target_nozzle_temp") or None)
        if "curr_hotbed_temp" in t:
            temps["bed"] = Temperature(t.get("curr_hotbed_temp"), t.get("target_hotbed_temp") or None)
        if t.get("curr_chamber_temp") is not None and self.model_id in ENCLOSED:
            temps["chamber"] = Temperature(t.get("curr_chamber_temp"), t.get("target_chamber_temp") or None)
        st.temps = temps
        fans = {}
        for k, label in (("fan_speed_pct", "part"), ("aux_fan_speed_pct", "aux")):
            if info.get(k) is not None:
                fans[label] = float(info[k])
        st.fans = fans
        st.speed_level = SPEED_NAME.get(int(info.get("print_speed_mode") or 0), "")
        light = self.data.get("light") or {}
        lights = light.get("lights") if isinstance(light.get("lights"), list) else ([light] if "status" in light else [])
        want = 2 if self.model_id in ENCLOSED else 1
        lt = next((x for x in lights if x.get("type") == want), lights[0] if lights else None)
        st.light = bool(lt.get("status")) if lt else None
        st.firmware = str(info.get("version") or st.firmware)
        pc = self.data.get("print_code")
        st.error = f"{CODES.get(pc['code'], 'код ' + str(pc['code']))}{': ' + pc['msg'] if pc.get('msg') else ''}" \
            if pc and pc["code"] not in (10401, 10601) else ""
        slots = []
        for bid, b in sorted(self.boxes.items()):
            for s in b.get("slots", []) or []:
                idx = int(s.get("index", 0))
                slots.append(MaterialSlot(id=f"{bid}.{idx}", name=f"ACE {bid + 1}.{idx + 1}: {s.get('type') or 'пусто'}",
                                          type=s.get("type") or "", color=_rgb_hex(s.get("color")),
                                          remain_pct=s.get("consumables_percent"),
                                          active=b.get("loaded_slot") == idx))
        st.materials = slots
        st.message = "Photon P1: запуск печати и загрузка файлов пока поддерживаются только из Photon Workshop" if self.lcd else ""
        await self.emit()

    # ------------------------------------------------------------------ команды
    def capabilities(self) -> Capabilities:
        return Capabilities(upload=not self.lcd, start=not self.lcd, pause=True, resume=True, stop=True,
                            files=not self.lcd, delete=not self.lcd, camera=not self.lcd, light=True,
                            speed=not self.lcd)

    def _ctrl_data(self) -> Any:
        return None if self.lcd else {"taskid": self._task_id or "-1"}

    async def pause(self) -> None:
        self._send("print", "pause", self._ctrl_data())

    async def resume(self) -> None:
        self._send("print", "resume", self._ctrl_data())

    async def cancel(self) -> None:
        self._send("print", "stop", self._ctrl_data())

    async def set_light(self, on: bool) -> None:
        typ = 2 if self.model_id in ENCLOSED else 1
        self._send("light", "control", {"type": typ, "status": 1 if on else 0, "brightness": 100})
        self._send("light", "query")

    async def set_speed(self, value: str) -> None:
        v = int(float(value))
        mode = v if v <= 3 else (1 if v <= 75 else 2 if v <= 125 else 3)
        self._send("print", "update", {"taskid": self._task_id or "-1", "settings": {"print_speed_mode": max(1, min(mode, 3))}})

    async def start_print(self, remote_path: str, options: Optional[dict[str, Any]] = None) -> None:
        if self.lcd:
            raise NotSupported("Запуск печати на Photon P1 пока не поддерживается")
        o = options or {}
        name = os.path.basename(remote_path)
        ams: dict[str, Any] = {"use_ams": False, "ams_box_mapping": []}
        box = self.boxes.get(0)
        fil = o.get("filaments") or []
        if box and box.get("slots") and o.get("use_ams", True):
            slots = {int(s.get("index", 0)): s for s in box["slots"]}
            used = sorted({int(f.get("id") or 1) for f in fil}) or [1]
            mapping = []
            for n in used:
                s = slots.get(n - 1, {})
                want = next((f for f in fil if int(f.get("id") or 0) == n), {})
                mapping.append({"paint_index": n - 1, "ams_index": n - 1,
                                "paint_color": _rgba(want.get("color") or _rgb_hex(s.get("color"))),
                                "ams_color": _rgba(_rgb_hex(s.get("color"))),
                                "material_type": want.get("type") or s.get("type") or "PLA"})
            ams = {"use_ams": True, "ams_box_mapping": mapping}
        data = {"taskid": str(random.randint(10**8, 10**9)), "filename": name, "filepath": "/", "filetype": 1,
                "task_mode": 1, "filesize": int(o.get("filesize") or self._sizes.get(name, 0)), "ams_settings": ams,
                "task_settings": {"auto_leveling": 1 if o.get("bed_leveling") else 0, "vibration_compensation": 0,
                                  "flow_calibration": 0, "timelapse": {"status": 1 if o.get("timelapse") else 0},
                                  "ai_settings": {"status": 0}, "dry_mode": 0}}
        self._send("print", "start", data, slicer=True)
        try:
            rep = await self._wait_report("print", timeout=30)
        except DriverError:
            return  # большие .3mf распаковываются долго — статус придёт позже
        code = rep.get("_code")
        if code and int(code) == 10101:
            raise DriverError("Принтер занят другой задачей")

    async def list_files(self) -> list[FileEntry]:
        self._send("file", "listLocal", {"path": "/"})
        rep = await self._wait_report("file", timeout=15)
        recs = rep.get("records") or rep.get("file_list") or []
        out = [FileEntry(name=r.get("filename", ""), path=r.get("filename", ""), size=r.get("size"),
                         modified=(r.get("timestamp") / 1000 if (r.get("timestamp") or 0) > 1e11 else r.get("timestamp")),
                         is_dir=bool(r.get("is_dir"))) for r in recs]
        return sorted([f for f in out if not f.is_dir], key=lambda f: f.modified or 0, reverse=True)

    async def delete_file(self, remote_path: str) -> None:
        if os.path.basename(remote_path) == self.status.filename and self.status.state in (
                PrinterState.PRINTING, PrinterState.PAUSED):
            raise DriverError("Нельзя удалить файл, который сейчас печатается")
        self._send("file", "deleteLocal", {"path": "/", "filename": os.path.basename(remote_path)})

    async def upload(self, local_path: str, remote_name: str,
                     progress: Optional[ProgressCallback] = None) -> str:
        if self.lcd:
            raise NotSupported("Загрузка на Photon P1 пока не поддерживается")
        size = os.path.getsize(local_path)
        name = os.path.basename(remote_name)
        async with httpx.AsyncClient(timeout=10) as c:
            info = (await c.get(f"http://{self.cfg.host}:{self.port}/info")).json()
        url = info.get("fileUploadurl") or ((self.data.get("info") or {}).get("urls") or {}).get("fileUploadurl")
        if not url:
            raise DriverError("Принтер не сообщил адрес для загрузки")
        parts = urlsplit(url)
        url = f"http://{self.cfg.host}:{parts.port or self.port}{parts.path}?{parts.query}"
        # без автоповтора: повторная загрузка может испортить файл на принтере
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, write=900, read=900)) as c:
            with open(local_path, "rb") as fh:
                r = await c.post(url, data={"filename": name}, files={"gcode": (name, fh, "application/octet-stream")},
                                 headers={"X-File-Length": str(size)})
        if r.status_code >= 400:
            raise DriverError(f"Загрузка не удалась: HTTP {r.status_code} {r.text[:200]}")
        try:
            body = r.json()
            code = int(body.get("code", 200))
            if code not in (0, 200):
                raise DriverError(f"Загрузка не удалась: код {code} {body.get('message', '')}")
        except ValueError:
            pass
        self._sizes[name] = size
        if progress:
            progress(size, size)
        return name

    async def camera_frames(self) -> AsyncIterator[bytes]:
        if self.lcd:
            raise NotSupported("Камера Photon P1 пока не поддерживается")
        self._send("video", "startCapture")
        await asyncio.sleep(1.5)
        url = (((self.data.get("info") or {}).get("urls") or {}).get("rtspUrl")) or f"http://{self.cfg.host}:18088/flv"
        parts = urlsplit(url)
        url = f"{parts.scheme or 'http'}://{self.cfg.host}:{parts.port or 18088}{parts.path}"
        try:
            async for f in ffmpeg_jpeg_frames(url, fps=2):
                yield f
        finally:
            if self.mqtt:
                try:
                    self._send("video", "stopCapture")
                except Exception:  # noqa: BLE001
                    pass

    async def test_connection(self) -> dict[str, Any]:
        port = await self.handshake()
        return {"ok": True, "model": self.cfg.model, "serial": self.cfg.serial, "mqtt_port": port}

    # ------------------------------------------------------------------ поиск
    @classmethod
    async def probe(cls, host: str, timeout: float = 1.5) -> Optional[DiscoveredPrinter]:
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"http://{host}:18910/info")
                info = r.json()
        except Exception:  # noqa: BLE001
            return None
        if "modelId" not in info and "modelName" not in info:
            return None
        mid = str(info.get("modelId", ""))
        name = info.get("modelName") or f"Anycubic {MODEL_IDS.get(mid, mid)}"
        lcd = info.get("deviceType") == "lcd"
        notes = "" if info.get("ctrlType") == "lan" else LAN_HELP
        return DiscoveredPrinter(kind=cls.kind, host=host, name=info.get("deviceName") or name, model=name,
                                 serial=str(info.get("cn", "")), technology="resin" if lcd else "fdm",
                                 port=18910, notes=notes, extra={"modelId": mid, "ctrlType": info.get("ctrlType")})
