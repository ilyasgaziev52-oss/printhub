"""Bambu Lab по локальной сети: MQTT (TLS 8883) + FTPS (990) + камера (RTSPS 322 / JPEG 6000).

Начиная с прошивок 2025 года (Authorization Control) команды печати/паузы/стопа принимаются
от сторонних программ только в режиме «LAN Only» с включённым «Режимом разработчика».
Статус, подсветка, FTP и камера работают и без него.
"""
from __future__ import annotations

import asyncio
import ftplib
import os
import re
import socket
import ssl
import struct
import time
from datetime import datetime
from typing import Any, AsyncIterator, Optional

from ..models import (Capabilities, DiscoveredPrinter, FileEntry, MaterialSlot, PrinterState,
                      Temperature)
from .base import DriverError, NotSupported, PrinterDriver, ProgressCallback, ffmpeg_jpeg_frames
from .mqtt_util import MqttBridge

# DevModel (SSDP) → модель
DEV_MODELS = {
    "3DPrinter-X1-Carbon": "X1 Carbon", "BL-P001": "X1 Carbon", "3DPrinter-X1": "X1", "BL-P002": "X1",
    "C13": "X1E", "C11": "P1P", "C12": "P1S", "N1": "A1 mini", "N2S": "A1", "N7": "P2S",
    "O1D": "H2D", "O1E": "H2D Pro", "O2D": "H2D Pro", "O1S": "H2S", "O1C": "H2C", "O1C2": "H2C",
    "N6": "X2D", "N9": "A2L", "N10": "H3C",
}
SERIAL_PREFIX = {
    "00M": "X1 Carbon", "00W": "X1", "03W": "X1E", "01S": "P1P", "01P": "P1S", "030": "A1 mini",
    "039": "A1", "22E": "P2S", "094": "H2D", "239": "H2D Pro", "093": "H2S", "31B": "H2C",
    "20P": "X2D", "26A": "A2L", "32X": "H3C",
}
LEGACY_SDCARD = {"X1", "X1 Carbon", "X1E", "P1P", "P1S", "A1", "A1 mini"}
CAMERA_RTSP = {"X1", "X1 Carbon", "X1E", "P2S", "H2D", "H2D Pro", "H2S", "H2C", "X2D", "H3C"}
CAMERA_6000 = {"P1P", "P1S", "A1", "A1 mini", "A2L"}
HAS_CHAMBER = {"X1", "X1 Carbon", "X1E", "P2S", "H2D", "H2D Pro", "H2S", "H2C", "X2D", "H3C"}
H2_LIGHTS = {"H2D", "H2D Pro", "H2S", "H2C"}

GCODE_STATE = {
    "IDLE": PrinterState.IDLE, "PREPARE": PrinterState.PREPARING, "SLICING": PrinterState.PREPARING,
    "RUNNING": PrinterState.PRINTING, "PAUSE": PrinterState.PAUSED, "FINISH": PrinterState.FINISHED,
    "FAILED": PrinterState.ERROR, "INIT": PrinterState.BUSY,
}
SPEED_LEVELS = {1: "тихий", 2: "стандарт", 3: "спорт", 4: "турбо"}
CANCELLED_ERROR = 0x0300400C
DEV_MODE_HELP = ("Управление печатью недоступно: на экране принтера включите «LAN Only» и «Режим "
                 "разработчика» (Настройки → Сеть). Статус, свет и камера работают и так.")


FTP_530_HELP = ("Принтер отклонил вход по FTP (код 530), поэтому список файлов и загрузка недоступны. "
                "Проверьте: 1) в принтере стоит microSD-карта — без неё файлы хранить негде; "
                "2) код доступа актуальный — он меняется при включении/выключении «LAN Only», "
                "сверьте его на экране (Настройки → Сеть) и обновите в настройках принтера в PrintHub.")


def model_from(code: str = "", serial: str = "") -> str:
    if code in DEV_MODELS:
        return DEV_MODELS[code]
    return SERIAL_PREFIX.get(serial[:3].upper(), "") if serial else ""


def _tls_ctx() -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2  # P2S зависает на TLS 1.3
    return ctx


def merge_report(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Глубокое слияние дельт. Списки AMS/лотков/экструдеров сливаются по id."""
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            merge_report(dst[k], v)
        elif (isinstance(v, list) and k in ("ams", "tray", "vir_slot", "info") and isinstance(dst.get(k), list)
              and all(isinstance(x, dict) and "id" in x for x in v)
              and all(isinstance(x, dict) and "id" in x for x in dst[k])):
            index = {str(x["id"]): x for x in dst[k]}
            for item in v:
                cur = index.get(str(item["id"]))
                if cur is None:
                    dst[k].append(item)
                    index[str(item["id"])] = item
                else:
                    merge_report(cur, item)
        else:
            dst[k] = v


def _packed(v: Any) -> tuple[Optional[float], Optional[float]]:
    """H2D и новее: младшие 16 бит — текущая °C, старшие — целевая."""
    try:
        i = int(v)
    except (TypeError, ValueError):
        return None, None
    return float(i & 0xFFFF), float((i >> 16) & 0xFFFF)


def _color(c: str) -> str:
    c = (c or "").strip().lstrip("#")
    return f"#{c[:6]}" if len(c) >= 6 else ""


def _num(v: Any) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _color_dist(a: str, b: str) -> float:
    try:
        ra, ga, ba = (int(a.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
        rb, gb, bb = (int(b.lstrip("#")[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return 1e9
    return (ra - rb) ** 2 + (ga - gb) ** 2 + (ba - bb) ** 2


def build_ams_mapping(filaments: list[dict[str, Any]], trays: list[dict[str, Any]]
                      ) -> tuple[list[int], list[dict[str, int]]]:
    """Сопоставляет филаменты из проекта лоткам AMS по типу и ближайшему цвету."""
    if not filaments:
        return [], []
    n = max(int(f.get("id") or 0) for f in filaments)
    mapping = [-1] * n
    mapping2 = [{"ams_id": 255, "slot_id": 255} for _ in range(n)]
    used: set[int] = set()
    for f in sorted(filaments, key=lambda x: -float(x.get("used_g") or 0)):
        idx = int(f.get("id") or 0) - 1
        if idx < 0:
            continue
        ftype = str(f.get("type", "")).upper()
        cands = [t for t in trays if t.get("type", "").upper() == ftype and t["gid"] not in used]
        if not cands:
            cands = [t for t in trays if t.get("type", "").upper() == ftype]
        if not cands:
            mapping2[idx] = {"ams_id": 255, "slot_id": 0}  # внешняя катушка
            continue
        best = min(cands, key=lambda t: _color_dist(t.get("color", ""), str(f.get("color", ""))))
        used.add(best["gid"])
        mapping[idx] = best["gid"]
        mapping2[idx] = {"ams_id": best["ams_id"], "slot_id": best["slot_id"]}
    return mapping, mapping2


class _ImplicitFTP(ftplib.FTP_TLS):
    """FTPS с неявным TLS и переиспользованием TLS-сессии на канале данных (требование vsftpd Bambu)."""

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._sock: Any = None

    @property
    def sock(self) -> Any:  # type: ignore[override]
        return self._sock

    @sock.setter
    def sock(self, value: Any) -> None:
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value, server_hostname=self.host)
        self._sock = value

    def ntransfercmd(self, cmd: str, rest: Any = None) -> Any:
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size

    def stor_no_unwrap(self, cmd: str, fp: Any, blocksize: int = 65536, callback: Any = None) -> str:
        self.voidcmd("TYPE I")
        with self.transfercmd(cmd) as conn:
            while True:
                buf = fp.read(blocksize)
                if not buf:
                    break
                conn.sendall(buf)
                if callback:
                    callback(len(buf))
            conn.close()  # без unwrap(): на Bambu зависает
        self.sock.settimeout(90)
        return self.voidresp()


_LIST_RE = re.compile(r"^([\-dl])[rwx\-]{9}\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(\w{3})\s+(\d{1,2})\s+([\d:]{4,5})\s+(.+)$")


def parse_list_line(line: str) -> Optional[tuple[bool, int, Optional[float], str]]:
    m = _LIST_RE.match(line.strip())
    if not m:
        return None
    is_dir = m.group(1) == "d"
    size = int(m.group(2))
    mon, day, ty, name = m.group(3), int(m.group(4)), m.group(5), m.group(6)
    ts: Optional[float] = None
    try:
        now = datetime.now()
        if ":" in ty:
            dt = datetime.strptime(f"{mon} {day} {now.year} {ty}", "%b %d %Y %H:%M")
            if dt > now:
                dt = dt.replace(year=now.year - 1)
        else:
            dt = datetime.strptime(f"{mon} {day} {ty}", "%b %d %Y")
        ts = dt.timestamp()
    except ValueError:
        pass
    return is_dir, size, ts, name


class BambuDriver(PrinterDriver):
    kind = "bambu"
    title = "Bambu Lab (LAN)"
    default_port = 8883
    required_fields = ("host", "access_code")
    accepted_ext = (".gcode.3mf", ".3mf", ".gcode")
    help = ("X1/X1E/P1/P2S/A1/H2. Нужны IP и код доступа (Настройки → Сеть на экране принтера). "
            "Для запуска и остановки печати включите «LAN Only» и «Режим разработчика».")

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self.mqtt: Optional[MqttBridge] = None
        self.report: dict[str, Any] = {}
        self.seq = 20000
        self.model = self.cfg.model.replace("Bambu Lab ", "") if self.cfg.model else ""
        self.dev_mode: Optional[bool] = None
        self._last_msg = 0.0
        self._ftp_lock = asyncio.Lock()

    # ------------------------------------------------------------------ MQTT
    @property
    def serial(self) -> str:
        return self.cfg.serial.strip().upper()

    def _next_seq(self) -> str:
        self.seq = 20000 if self.seq >= 29999 else self.seq + 1
        return str(self.seq)

    async def _ensure_serial(self) -> None:
        if not self.cfg.serial:
            sn = await fetch_serial_from_cert(self.cfg.host)
            if not sn:
                raise DriverError("Не удалось узнать серийный номер — введите его вручную")
            self.cfg.serial = sn
        if not self.model:
            self.model = model_from(serial=self.serial)

    async def _connect(self) -> MqttBridge:
        await self._ensure_serial()
        m = MqttBridge(self.cfg.host, self.port, username="bblp", password=self.cfg.access_code,
                       client_id=f"printhub-{os.getpid()}-{int(time.time()) % 100000}", ssl_context=_tls_ctx())
        try:
            await m.connect(timeout=15)
        except ConnectionError as e:
            txt = str(e)
            if "authorized" in txt.lower() or "password" in txt.lower():
                raise DriverError("Неверный код доступа (LAN Access Code)") from e
            raise
        m.subscribe(f"device/{self.serial}/report")
        return m

    def _publish(self, section: str, payload: dict[str, Any], qos: int = 0) -> None:
        if not self.mqtt:
            raise DriverError("Нет соединения с принтером")
        body = {section: {"sequence_id": self._next_seq(), **payload}}
        self.mqtt.publish(f"device/{self.serial}/request", body, qos)

    async def run(self) -> None:
        self.mqtt = await self._connect()
        try:
            self._publish("info", {"command": "get_version"})
            self._publish("pushing", {"command": "pushall", "version": 1, "push_target": 1})
            self._last_msg = time.monotonic()
            while True:
                item = await self.mqtt.get(timeout=30)
                if item is None:
                    if time.monotonic() - self._last_msg > 60:
                        self._publish("pushing", {"command": "start"})
                    if time.monotonic() - self._last_msg > 180:
                        raise DriverError("Принтер перестал присылать данные")
                    continue
                self._last_msg = time.monotonic()
                topic, payload = item
                try:
                    import json
                    msg = json.loads(payload)
                except ValueError:
                    continue
                if "print" in msg:
                    merge_report(self.report, msg["print"])
                    await self._apply()
                if "info" in msg and msg["info"].get("command") == "get_version":
                    self._apply_version(msg["info"])
                if "system" in msg and msg["system"].get("result") not in (None, "success", "SUCCESS"):
                    self.log.info("system: %s", msg["system"])
        finally:
            await self.close()

    async def close(self) -> None:
        if self.mqtt:
            m, self.mqtt = self.mqtt, None
            await m.close()

    def _apply_version(self, info: dict[str, Any]) -> None:
        for mod in info.get("module", []) or []:
            if mod.get("name") == "ota":
                self.status.firmware = mod.get("sw_ver", "")
                pn = (mod.get("product_name") or "").replace("Bambu Lab ", "").replace("X1-Carbon", "X1 Carbon")
                if pn:
                    self.model = pn

    async def _apply(self) -> None:
        r = self.report
        st = self.status
        gs = str(r.get("gcode_state", "") or "")
        st.state_raw = gs
        err = int(r.get("print_error") or 0)
        st.state = GCODE_STATE.get(gs, PrinterState.IDLE if gs in ("", "OFFLINE") else PrinterState.UNKNOWN)
        if st.state == PrinterState.ERROR and err == CANCELLED_ERROR:
            st.state = PrinterState.CANCELLED
        st.progress = _num(r.get("mc_percent"))
        rem = r.get("mc_remaining_time", r.get("remain_time"))
        st.remaining_s = float(rem) * 60 if _num(rem) is not None else None
        st.layer = r.get("layer_num")
        st.total_layers = r.get("total_layer_num")
        st.filename = r.get("subtask_name") or os.path.basename(str(r.get("gcode_file", "")))
        start = _num(r.get("gcode_start_time"))
        st.elapsed_s = max(time.time() - start, 0) if start and st.state in (PrinterState.PRINTING, PrinterState.PAUSED, PrinterState.PREPARING) else None

        # температуры
        temps: dict[str, Temperature] = {}
        dev = r.get("device") if isinstance(r.get("device"), dict) else {}
        ext = (dev.get("extruder") or {}).get("info") if dev else None
        if isinstance(ext, list) and len(ext) > 1:
            for e in sorted(ext, key=lambda x: int(x.get("id", 0))):
                cur, tgt = _packed(e.get("temp"))
                label = "nozzle" if int(e.get("id", 0)) == 0 else "nozzle1"
                temps[label] = Temperature(cur, tgt)
        else:
            temps["nozzle"] = Temperature(_num(r.get("nozzle_temper")), _num(r.get("nozzle_target_temper")))
        bed_packed = ((dev.get("bed") or {}).get("info") or {}).get("temp") if dev else None
        if bed_packed is not None:
            temps["bed"] = Temperature(*_packed(bed_packed))
        else:
            temps["bed"] = Temperature(_num(r.get("bed_temper")), _num(r.get("bed_target_temper")))
        ctc = ((dev.get("ctc") or {}).get("info") or {}).get("temp") if dev else None
        if ctc is not None:
            temps["chamber"] = Temperature(*_packed(ctc))
        elif self.model in HAS_CHAMBER and r.get("chamber_temper") is not None:
            temps["chamber"] = Temperature(_num(r.get("chamber_temper")), None)
        st.temps = temps

        fans = {}
        for key, label in (("cooling_fan_speed", "part"), ("big_fan1_speed", "aux"), ("big_fan2_speed", "chamber")):
            v = _num(r.get(key))
            if v is not None:
                fans[label] = round(v / 15 * 100)
        st.fans = fans
        lvl = r.get("spd_lvl")
        st.speed_level = SPEED_LEVELS.get(int(lvl), "") if _num(lvl) is not None else ""
        st.speed_pct = _num(r.get("spd_mag"))
        lights = r.get("lights_report") or []
        cl = next((x for x in lights if x.get("node") == "chamber_light"), None)
        st.light = None if cl is None else cl.get("mode") == "on"
        st.wifi_signal = str(r.get("wifi_signal", ""))

        # ошибки
        errors = []
        if err and err != CANCELLED_ERROR:
            h = f"{err:08X}"
            errors.append(f"Ошибка {h[:4]}_{h[4:]}")
        for hms in (r.get("hms") or [])[:3]:
            try:
                a, c = int(hms.get("attr", 0)), int(hms.get("code", 0))
                errors.append(f"HMS_{a >> 16:04X}_{a & 0xFFFF:04X}_{c >> 16:04X}_{c & 0xFFFF:04X}")
            except (TypeError, ValueError):
                pass
        st.error = "; ".join(errors)

        # материалы
        st.materials = self._materials()

        # режим разработчика
        fun = r.get("fun")
        if fun:
            try:
                self.dev_mode = not bool(int(str(fun), 16) & 0x20000000)
            except ValueError:
                self.dev_mode = None
        st.message = DEV_MODE_HELP if self.dev_mode is False else ""
        if self.model and not self.cfg.model:
            self.cfg.model = f"Bambu Lab {self.model}"
        await self.emit()

    def _materials(self) -> list[MaterialSlot]:
        r = self.report
        ams = r.get("ams") or {}
        tray_now = str(ams.get("tray_now", "255"))
        out: list[MaterialSlot] = []
        for unit in ams.get("ams") or []:
            try:
                aid = int(unit.get("id", 0))
            except ValueError:
                continue
            for tray in unit.get("tray") or []:
                try:
                    sid = int(tray.get("id", 0))
                except ValueError:
                    continue
                gid = aid if aid >= 128 else aid * 4 + sid
                label = f"AMS{'-HT' if aid >= 128 else ''} {aid + 1 if aid < 128 else aid - 127}.{sid + 1}"
                if "tray_type" not in tray or not tray.get("tray_type"):
                    out.append(MaterialSlot(id=str(gid), name=f"{label}: пусто"))
                    continue
                out.append(MaterialSlot(id=str(gid), name=f"{label}: {tray.get('tray_sub_brands') or tray.get('tray_type')}",
                                        type=tray.get("tray_type", ""), color=_color(tray.get("tray_color", "")),
                                        remain_pct=_num(tray.get("remain")), active=tray_now == str(gid)))
        ext = r.get("vt_tray")
        ext_list = [ext] if isinstance(ext, dict) else []
        if not ext_list and isinstance(r.get("vir_slot"), list):
            ext_list = r["vir_slot"]
        for e in ext_list:
            if e.get("tray_type"):
                out.append(MaterialSlot(id=str(e.get("id", "254")), name=f"Внешняя катушка: {e.get('tray_type')}",
                                        type=e.get("tray_type", ""), color=_color(e.get("tray_color", "")),
                                        remain_pct=_num(e.get("remain")), active=tray_now == str(e.get("id"))))
        return out

    def _trays_for_mapping(self) -> list[dict[str, Any]]:
        out = []
        for unit in (self.report.get("ams") or {}).get("ams") or []:
            aid = int(unit.get("id", 0))
            for tray in unit.get("tray") or []:
                if not tray.get("tray_type"):
                    continue
                sid = int(tray.get("id", 0))
                out.append({"gid": aid if aid >= 128 else aid * 4 + sid, "ams_id": aid, "slot_id": sid,
                            "type": tray.get("tray_type", ""), "color": _color(tray.get("tray_color", ""))})
        return out

    # ------------------------------------------------------------------ возможности
    def capabilities(self) -> Capabilities:
        control = self.dev_mode is not False
        cam = self.model in CAMERA_RTSP or self.model in CAMERA_6000
        return Capabilities(upload=True, start=control, pause=control, resume=control, stop=control,
                            files=True, delete=True, camera=cam, light=True, gcode=control, speed=control)

    def _require_control(self) -> None:
        if self.dev_mode is False:
            raise NotSupported(DEV_MODE_HELP)

    async def pause(self) -> None:
        self._require_control()
        self._publish("print", {"command": "pause", "param": ""}, qos=1)

    async def resume(self) -> None:
        self._require_control()
        self._publish("print", {"command": "resume", "param": ""}, qos=1)

    async def cancel(self) -> None:
        self._require_control()
        self._publish("print", {"command": "stop", "param": ""}, qos=1)

    async def set_speed(self, value: str) -> None:
        self._require_control()
        v = int(float(value))
        if v > 4:  # передали проценты
            v = 1 if v <= 60 else 2 if v <= 110 else 3 if v <= 140 else 4
        self._publish("print", {"command": "print_speed", "param": str(max(1, min(v, 4)))})

    async def send_gcode(self, gcode: str) -> None:
        self._require_control()
        if self.status.state in (PrinterState.PRINTING, PrinterState.PREPARING):
            raise DriverError("Во время печати отправка G-code заблокирована ради безопасности")
        self._publish("print", {"command": "gcode_line", "param": gcode.strip() + "\n"})

    async def set_light(self, on: bool) -> None:
        nodes = ["chamber_light", "chamber_light2"] if self.model in H2_LIGHTS else ["chamber_light"]
        for node in nodes:
            self._publish("system", {"command": "ledctrl", "led_node": node, "led_mode": "on" if on else "off",
                                     "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0})

    async def start_print(self, remote_path: str, options: Optional[dict[str, Any]] = None) -> None:
        self._require_control()
        o = options or {}
        if self.status.state in (PrinterState.PRINTING, PrinterState.PAUSED, PrinterState.PREPARING):
            raise DriverError("Принтер занят — новая команда печати может прервать текущую")
        name = remote_path.lstrip("/")
        if name.lower().endswith(".gcode"):
            self._publish("print", {"command": "gcode_file", "param": f"/sdcard/{name}"}, qos=1)
            return
        plate = int(o.get("plate") or 1)
        url = f"file:///sdcard/{name}" if self.model in LEGACY_SDCARD else f"ftp:///{name}"
        trays = self._trays_for_mapping()
        use_ams = bool(o.get("use_ams", True)) and bool(trays)
        bed_lev = bool(o.get("bed_leveling", True))
        flow = bool(o.get("flow_cali", False))
        cmd: dict[str, Any] = {
            "command": "project_file", "param": f"Metadata/plate_{plate}.gcode", "url": url, "file": name,
            "subtask_name": re.sub(r"(\.gcode)?\.3mf$", "", os.path.basename(name), flags=re.I),
            "project_id": "0", "profile_id": "0", "task_id": "0", "subtask_id": "0", "bed_type": "auto",
            "timelapse": bool(o.get("timelapse", False)), "bed_leveling": bed_lev,
            "auto_bed_leveling": 1 if bed_lev else 0, "flow_cali": flow, "extrude_cali_flag": 1 if flow else 0,
            "vibration_cali": bool(o.get("vibration_cali", True)), "layer_inspect": bool(o.get("layer_inspect", False)),
            "use_ams": use_ams,
        }
        if use_ams:
            if o.get("ams_mapping"):
                cmd["ams_mapping"] = [int(x) for x in o["ams_mapping"]]
            else:
                mapping, mapping2 = build_ams_mapping(o.get("filaments") or [], trays)
                cmd["ams_mapping"] = mapping or [trays[0]["gid"]]
                if mapping2:
                    cmd["ams_mapping2"] = mapping2
        self._publish("print", cmd, qos=1)

    # ------------------------------------------------------------------ FTPS
    def _ftp_connect(self) -> _ImplicitFTP:
        ftp = _ImplicitFTP(context=_tls_ctx())
        try:
            ftp.connect(self.cfg.host, 990, timeout=30)
        except OSError as e:
            raise DriverError(f"Нет связи с FTP принтера (порт 990): {e}") from e
        try:
            ftp.login("bblp", self.cfg.access_code)
        except ftplib.error_perm as e:
            ftp.close()
            if str(e).startswith("530"):
                raise DriverError(FTP_530_HELP) from e
            raise DriverError(f"FTP отказал: {e}") from e
        ftp.prot_p()
        return ftp

    def check_ftp(self) -> str:
        """Пустая строка — FTP работает, иначе текст проблемы."""
        try:
            ftp = self._ftp_connect()
            try:
                ftp.voidcmd("NOOP")
            finally:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()
            return ""
        except (DriverError, ftplib.all_errors, OSError) as e:  # type: ignore[misc]
            return str(e)

    async def upload(self, local_path: str, remote_name: str,
                     progress: Optional[ProgressCallback] = None) -> str:
        total = os.path.getsize(local_path)
        name = os.path.basename(remote_name)
        if name.lower().endswith(".3mf") and not name.lower().endswith(".gcode.3mf"):
            name = name[:-4] + ".gcode.3mf"

        def work() -> None:
            sent = [0]

            def cb(n: int) -> None:
                sent[0] += n
                if progress:
                    progress(sent[0], total)

            ftp = self._ftp_connect()
            try:
                with open(local_path, "rb") as fh:
                    try:
                        ftp.stor_no_unwrap(f"STOR /{name}", fh, callback=cb)
                    except ftplib.error_temp as e:
                        # P2S иногда отвечает 426, хотя файл записан — проверяем размер
                        try:
                            if ftp.size(f"/{name}") == total:
                                return
                        except ftplib.all_errors:
                            pass
                        raise DriverError(f"FTP: {e}") from e
            finally:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()

        async with self._ftp_lock:
            await asyncio.to_thread(work)
        return name

    async def list_files(self) -> list[FileEntry]:
        def work() -> list[FileEntry]:
            ftp = self._ftp_connect()
            out: list[FileEntry] = []
            try:
                for d in ("/", "/cache"):
                    lines: list[str] = []
                    try:
                        ftp.retrlines(f"LIST {d}", lines.append)
                    except ftplib.all_errors:
                        continue
                    for ln in lines:
                        p = parse_list_line(ln)
                        if not p or p[0]:
                            continue
                        _, size, ts, name = p
                        if not name.lower().endswith((".3mf", ".gcode")):
                            continue
                        path = (d.rstrip("/") + "/" + name) if d != "/" else "/" + name
                        out.append(FileEntry(name=name, path=path, size=size, modified=ts))
            finally:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()
            return sorted(out, key=lambda f: f.modified or 0, reverse=True)

        async with self._ftp_lock:
            return await asyncio.to_thread(work)

    async def delete_file(self, remote_path: str) -> None:
        if self.status.state in (PrinterState.PRINTING, PrinterState.PAUSED) and \
                os.path.basename(remote_path) in (self.status.filename, os.path.basename(str(self.report.get("gcode_file", "")))):
            raise DriverError("Нельзя удалить файл, который сейчас печатается")

        def work() -> None:
            ftp = self._ftp_connect()
            try:
                ftp.delete(remote_path)
            finally:
                try:
                    ftp.quit()
                except ftplib.all_errors:
                    ftp.close()

        async with self._ftp_lock:
            await asyncio.to_thread(work)

    # ------------------------------------------------------------------ камера
    async def camera_frames(self) -> AsyncIterator[bytes]:
        if self.model in CAMERA_6000:
            async for f in self._camera_6000():
                yield f
            return
        if self.model in CAMERA_RTSP:
            url = str((self.report.get("ipcam") or {}).get("rtsp_url", ""))
            if url == "disable":
                raise DriverError("Трансляция выключена: включите «LAN Mode Liveview» в настройках принтера")
            rtsp = f"rtsps://bblp:{self.cfg.access_code}@{self.cfg.host}:322/streaming/live/1"
            async for f in ffmpeg_jpeg_frames(rtsp, ["-rtsp_transport", "tcp"], fps=2):
                yield f
            return
        raise NotSupported("Модель принтера не определена — камера недоступна")

    async def _camera_6000(self) -> AsyncIterator[bytes]:
        auth = struct.pack("<IIII", 0x40, 0x3000, 0, 0) + b"bblp".ljust(32, b"\0") + \
            self.cfg.access_code.encode().ljust(32, b"\0")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.cfg.host, 6000, ssl=_tls_ctx(), server_hostname=self.cfg.host), 15)
        try:
            writer.write(auth)
            await writer.drain()
            while True:
                try:
                    hdr = await asyncio.wait_for(reader.readexactly(16), 30)
                except asyncio.IncompleteReadError as e:
                    raise DriverError("Камера закрыла соединение (неверный код доступа?)") from e
                size = struct.unpack("<I", hdr[:4])[0]
                if size <= 0 or size > 10_000_000:
                    raise DriverError("Неверный заголовок кадра камеры")
                jpg = await reader.readexactly(size)
                if jpg[:2] == b"\xff\xd8":
                    yield jpg
        finally:
            writer.close()

    # ------------------------------------------------------------------ проверка/поиск
    async def test_connection(self) -> dict[str, Any]:
        self.mqtt = await self._connect()
        try:
            self._publish("info", {"command": "get_version"})
            self._publish("pushing", {"command": "pushall", "version": 1, "push_target": 1})
            deadline = time.monotonic() + 10
            got = False
            while time.monotonic() < deadline:
                item = await self.mqtt.get(timeout=deadline - time.monotonic())
                if not item:
                    break
                import json
                try:
                    msg = json.loads(item[1])
                except ValueError:
                    continue
                if "info" in msg:
                    self._apply_version(msg["info"])
                if "print" in msg:
                    merge_report(self.report, msg["print"])
                    fun = self.report.get("fun")
                    if fun:
                        self.dev_mode = not bool(int(str(fun), 16) & 0x20000000)
                    got = True
                if got and self.status.firmware:
                    break
            res = {"ok": True, "serial": self.serial, "model": f"Bambu Lab {self.model}".strip(),
                   "firmware": self.status.firmware, "developer_mode": self.dev_mode}
            warnings = []
            if self.dev_mode is False:
                warnings.append(DEV_MODE_HELP)
            ftp_problem = await asyncio.to_thread(self.check_ftp)
            res["ftp_ok"] = not ftp_problem
            if ftp_problem:
                warnings.append(ftp_problem)
            if warnings:
                res["warning"] = " ".join(warnings)
            return res
        finally:
            await self.close()

    @classmethod
    async def discover(cls, timeout: float = 4.0) -> list[DiscoveredPrinter]:
        found: dict[str, DiscoveredPrinter] = {}
        loop = asyncio.get_running_loop()
        socks = []

        class Proto(asyncio.DatagramProtocol):
            def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                d = parse_ssdp(data, addr[0])
                if d:
                    found[d.serial or d.host] = d

        transports = []
        for port in (2021, 1990):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    try:
                        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except OSError:
                        pass
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                s.bind(("", port))
                try:
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                 struct.pack("4sl", socket.inet_aton("239.255.255.250"), socket.INADDR_ANY))
                except OSError:
                    pass
                s.setblocking(False)
                socks.append(s)
                t, _ = await loop.create_datagram_endpoint(Proto, sock=s)
                transports.append(t)
            except OSError:
                continue
        try:
            msearch = ("M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1990\r\nMAN: \"ssdp:discover\"\r\n"
                       "MX: 3\r\nST: urn:bambulab-com:device:3dprinter:1\r\n\r\n").encode()
            if transports:
                for dst in (("239.255.255.250", 2021), ("255.255.255.255", 2021)):
                    try:
                        transports[0].sendto(msearch, dst)
                    except OSError:
                        pass
            await asyncio.sleep(max(timeout, 6.0))  # принтеры шлют NOTIFY примерно раз в 5 с
        finally:
            for t in transports:
                t.close()
        return list(found.values())

    @classmethod
    async def probe(cls, host: str, timeout: float = 1.5) -> Optional[DiscoveredPrinter]:
        sn = await fetch_serial_from_cert(host, timeout=3)
        if not sn:
            return None
        model = model_from(serial=sn)
        return DiscoveredPrinter(kind=cls.kind, host=host, name=f"Bambu Lab {model}".strip(), model=f"Bambu Lab {model}".strip(),
                                 serial=sn, needs=["access_code"],
                                 notes="Введите код доступа с экрана принтера (Настройки → Сеть)")


def parse_ssdp(data: bytes, src_ip: str) -> Optional[DiscoveredPrinter]:
    text = data.decode("utf-8", "ignore")
    if "bambu" not in text.lower():
        return None
    h: dict[str, str] = {}
    for line in text.split("\r\n")[1:]:
        k, sep, v = line.partition(":")
        if sep:
            h[k.strip().lower()] = v.strip()
    if "usn" not in h or "devmodel.bambu.com" not in h:
        return None
    ip = h.get("location") or src_ip
    ip = re.sub(r"^\w+://", "", ip).split("/")[0].split(":")[0] or src_ip
    serial = h["usn"].split("::")[0].replace("uuid:", "").strip()
    model = model_from(h.get("devmodel.bambu.com", ""), serial)
    connect = h.get("devconnect.bambu.com", "")
    notes = "Введите код доступа с экрана принтера."
    if connect and connect != "lan":
        notes += " Принтер в облачном режиме: для управления печатью включите «LAN Only» и «Режим разработчика»."
    return DiscoveredPrinter(kind="bambu", host=ip, name=h.get("devname.bambu.com") or f"Bambu Lab {model}",
                             model=f"Bambu Lab {model}".strip(), serial=serial,
                             firmware=h.get("devversion.bambu.com", ""), port=8883, needs=["access_code"],
                             notes=notes, extra={"connect": connect, "bind": h.get("devbind.bambu.com", "")})


async def fetch_serial_from_cert(host: str, timeout: float = 5.0) -> str:
    """Серийный номер = CN сертификата MQTT-брокера принтера."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, 8883, ssl=_tls_ctx(), server_hostname=host), timeout)
    except (OSError, asyncio.TimeoutError, ssl.SSLError):
        return ""
    try:
        sslobj = writer.get_extra_info("ssl_object")
        der = sslobj.getpeercert(binary_form=True) if sslobj else None
    finally:
        writer.close()
    if not der:
        return ""
    return _cn_from_der(der)


def _cn_from_der(der: bytes) -> str:
    """Минимальный разбор: последнее вхождение OID 2.5.4.3 (commonName) — это subject."""
    oid = b"\x06\x03\x55\x04\x03"
    idx = der.rfind(oid)
    if idx < 0:
        return ""
    p = idx + len(oid)
    if p + 2 > len(der):
        return ""
    ln = der[p + 1]
    val = der[p + 2:p + 2 + ln]
    try:
        cn = val.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""
    return cn if re.fullmatch(r"[0-9A-Z]{8,20}", cn) else ""
