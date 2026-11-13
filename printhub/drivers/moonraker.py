"""Klipper через Moonraker API.

Подходит для: Elegoo Neptune 4 / 4 Pro / 4 Plus / 4 Max, OrangeStorm Giga, Anycubic Kobra
с прошивкой Rinkhals, Creality K1/K2 с root, любые самосборные принтеры на Klipper.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

import httpx
import websockets

from ..models import Capabilities, DiscoveredPrinter, FileEntry, PrinterState, Temperature
from .base import DriverError, PrinterDriver, ProgressCallback, mjpeg_frames_from_http

STATE_MAP = {
    "standby": PrinterState.IDLE,
    "printing": PrinterState.PRINTING,
    "paused": PrinterState.PAUSED,
    "complete": PrinterState.FINISHED,
    "cancelled": PrinterState.CANCELLED,
    "error": PrinterState.ERROR,
}

SUBSCRIBE = {
    "print_stats": None, "display_status": None, "virtual_sdcard": None, "extruder": None,
    "heater_bed": None, "fan": None, "gcode_move": None, "webhooks": None, "toolhead": None,
    "temperature_sensor chamber": None, "heater_generic chamber": None,
    "temperature_sensor Chamber": None, "output_pin caselight": None, "led caselight": None,
}


class MoonrakerDriver(PrinterDriver):
    kind = "moonraker"
    title = "Klipper (Moonraker)"
    default_port = 7125
    required_fields = ("host",)
    help = ("Elegoo Neptune 4, Anycubic с Rinkhals, Creality K1/K2 (root) и другие принтеры на Klipper. "
            "API-ключ нужен, только если в Moonraker включена авторизация.")

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._ws: Any = None
        self._objects: dict[str, dict[str, Any]] = {}
        self._req_id = 0
        self._meta_cache: dict[str, dict[str, Any]] = {}
        self._port = self.cfg.port or 0

    # ------------------------------------------------------------------ http
    @property
    def base(self) -> str:
        port = self._port or self.default_port
        return f"http://{self.cfg.host}" + ("" if port == 80 else f":{port}")

    async def _resolve_port(self) -> None:
        """7125 — порт Moonraker; у Elegoo Neptune 4 он бывает закрыт снаружи, тогда работает :80 (nginx)."""
        if self._port:
            return
        for port in (7125, 80):
            try:
                async with httpx.AsyncClient(timeout=4) as c:
                    url = f"http://{self.cfg.host}" + ("" if port == 80 else f":{port}") + "/server/info"
                    r = await c.get(url, headers=self._headers())
                    if r.status_code == 401 or (r.status_code == 200 and "result" in r.text):
                        self._port = port
                        return
            except httpx.HTTPError:
                continue
        raise DriverError("Moonraker не отвечает ни на порту 7125, ни на 80")

    def _headers(self) -> dict[str, str]:
        return {"X-Api-Key": self.cfg.api_key} if self.cfg.api_key else {}

    async def _get(self, path: str, **params: Any) -> Any:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(self.base + path, params=params or None, headers=self._headers())
            r.raise_for_status()
            return r.json().get("result")

    async def _post(self, path: str, timeout: float = 15, **params: Any) -> Any:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(self.base + path, params=params or None, headers=self._headers())
            if r.status_code >= 400:
                raise DriverError(f"{r.status_code}: {r.text[:300]}")
            return r.json().get("result")

    # ------------------------------------------------------------------ цикл
    async def run(self) -> None:
        await self._resolve_port()
        info = await self._get("/printer/info")
        self.status.firmware = f"Klipper {info.get('software_version', '')}"
        url = self.base.replace("http://", "ws://") + "/websocket"
        if self.cfg.api_key:
            try:
                tok = await self._get("/access/oneshot_token")
                url += f"?token={tok}"
            except Exception:  # noqa: BLE001
                pass
        async with websockets.connect(url, max_size=16 * 2**20, ping_interval=20) as ws:
            self._ws = ws
            # какие объекты есть у принтера
            avail_resp = await self._get("/printer/objects/list")
            avail = set(avail_resp.get("objects", []))
            objs = {k: None for k in SUBSCRIBE if k in avail or k in ("print_stats", "display_status",
                                                                       "virtual_sdcard", "extruder", "webhooks")}
            for name in avail:  # дополнительные экструдеры/вентиляторы
                if name.startswith("extruder") or name.startswith("heater_fan") or name.startswith("fan_generic"):
                    objs[name] = None
            sub_id = await self._rpc("printer.objects.subscribe", {"objects": objs})
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("id") == sub_id and "result" in msg:
                    self._merge(msg["result"].get("status", {}))
                    await self._apply()
                elif msg.get("method") == "notify_status_update":
                    self._merge(msg["params"][0])
                    await self._apply()
                elif msg.get("method") in ("notify_klippy_shutdown", "notify_klippy_disconnected"):
                    self.status.state = PrinterState.ERROR
                    self.status.error = "Klipper остановлен (shutdown)"
                    await self.emit()
                elif msg.get("method") == "notify_klippy_ready":
                    await self._rpc("printer.objects.subscribe", {"objects": objs})

    async def _rpc(self, method: str, params: Optional[dict[str, Any]] = None) -> int:
        self._req_id += 1
        await self._ws.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {},
                                        "id": self._req_id}))
        return self._req_id

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    def _merge(self, diff: dict[str, Any]) -> None:
        for k, v in diff.items():
            if isinstance(v, dict):
                self._objects.setdefault(k, {}).update(v)

    async def _apply(self) -> None:
        o = self._objects
        st = self.status
        ps = o.get("print_stats", {})
        wh = o.get("webhooks", {})
        raw = ps.get("state", "")
        st.state_raw = raw
        if wh.get("state") in ("shutdown", "error"):
            st.state = PrinterState.ERROR
            st.error = wh.get("state_message", "")
        else:
            st.state = STATE_MAP.get(raw, PrinterState.UNKNOWN)
            st.error = ps.get("message", "") if raw == "error" else ""
        st.filename = ps.get("filename", "") or ""
        prog = o.get("display_status", {}).get("progress")
        if not prog:
            prog = o.get("virtual_sdcard", {}).get("progress")
        st.progress = round(float(prog) * 100, 1) if prog is not None else None
        info = ps.get("info") or {}
        st.layer = info.get("current_layer")
        st.total_layers = info.get("total_layer")
        st.elapsed_s = ps.get("print_duration")
        # оставшееся время: по метаданным файла, иначе пропорционально прогрессу
        st.remaining_s = None
        if st.state in (PrinterState.PRINTING, PrinterState.PAUSED) and st.filename:
            meta = await self._file_meta(st.filename)
            est = meta.get("estimated_time")
            if est and st.elapsed_s is not None:
                if st.progress and st.progress > 1:
                    st.remaining_s = max(st.elapsed_s / (st.progress / 100) - st.elapsed_s, 0)
                else:
                    st.remaining_s = max(est - st.elapsed_s, 0)
            elif st.progress and st.progress > 1 and st.elapsed_s:
                st.remaining_s = st.elapsed_s / (st.progress / 100) - st.elapsed_s
        temps: dict[str, Temperature] = {}
        for name, obj in o.items():
            if name.startswith("extruder") and "temperature" in obj:
                label = "nozzle" if name == "extruder" else f"nozzle{name[8:]}"
                temps[label] = Temperature(obj.get("temperature"), obj.get("target"))
        if "heater_bed" in o:
            temps["bed"] = Temperature(o["heater_bed"].get("temperature"), o["heater_bed"].get("target"))
        for key in ("temperature_sensor chamber", "heater_generic chamber", "temperature_sensor Chamber"):
            if key in o:
                temps["chamber"] = Temperature(o[key].get("temperature"), o[key].get("target"))
        st.temps = temps
        fan = o.get("fan", {}).get("speed")
        st.fans = {"part": round(fan * 100)} if fan is not None else {}
        sf = o.get("gcode_move", {}).get("speed_factor")
        st.speed_pct = round(sf * 100) if sf is not None else None
        light = o.get("output_pin caselight", {}).get("value")
        st.light = None if light is None else bool(light)
        await self.emit()

    async def _file_meta(self, filename: str) -> dict[str, Any]:
        if filename not in self._meta_cache:
            try:
                self._meta_cache[filename] = await self._get("/server/files/metadata", filename=filename) or {}
            except Exception:  # noqa: BLE001
                self._meta_cache[filename] = {}
        return self._meta_cache[filename]

    # ------------------------------------------------------------------ операции
    def capabilities(self) -> Capabilities:
        return Capabilities(upload=True, start=True, pause=True, resume=True, stop=True, files=True,
                            delete=True, camera=True, light="output_pin caselight" in self._objects,
                            gcode=True, speed=True)

    async def list_files(self) -> list[FileEntry]:
        res = await self._get("/server/files/list", root="gcodes")
        out = [FileEntry(name=os.path.basename(f["path"]), path=f["path"], size=f.get("size"),
                         modified=f.get("modified")) for f in res or []]
        out.sort(key=lambda f: f.modified or 0, reverse=True)
        return out

    async def upload(self, local_path: str, remote_name: str,
                     progress: Optional[ProgressCallback] = None) -> str:
        size = os.path.getsize(local_path)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, write=600)) as c:
            with open(local_path, "rb") as fh:
                r = await c.post(self.base + "/server/files/upload", headers=self._headers(),
                                 data={"root": "gcodes"}, files={"file": (remote_name, fh, "application/octet-stream")})
            if r.status_code >= 400:
                raise DriverError(f"Загрузка не удалась: {r.status_code} {r.text[:200]}")
        if progress:
            progress(size, size)
        return remote_name

    async def delete_file(self, remote_path: str) -> None:
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.delete(f"{self.base}/server/files/gcodes/{quote(remote_path)}", headers=self._headers())
            r.raise_for_status()

    async def start_print(self, remote_path: str, options: Optional[dict[str, Any]] = None) -> None:
        await self._post("/printer/print/start", filename=remote_path)

    async def pause(self) -> None:
        await self._post("/printer/print/pause")

    async def resume(self) -> None:
        await self._post("/printer/print/resume")

    async def cancel(self) -> None:
        await self._post("/printer/print/cancel")

    async def send_gcode(self, gcode: str) -> None:
        await self._post("/printer/gcode/script", timeout=120, script=gcode)

    async def set_speed(self, value: str) -> None:
        pct = int(float(value))
        await self.send_gcode(f"M220 S{pct}")

    async def set_light(self, on: bool) -> None:
        await self.send_gcode(f"SET_PIN PIN=caselight VALUE={1 if on else 0}")

    async def camera_frames(self) -> AsyncIterator[bytes]:
        cams = (await self._get("/server/webcams/list") or {}).get("webcams", [])
        if not cams:
            raise DriverError("В Moonraker не настроено ни одной камеры")
        url = cams[0].get("stream_url") or cams[0].get("snapshot_url") or ""
        if url.startswith("/"):
            url = f"http://{self.cfg.host}{url}"
        async for frame in mjpeg_frames_from_http(url):
            yield frame

    async def test_connection(self) -> dict[str, Any]:
        await self._resolve_port()
        info = await self._get("/printer/info")
        return {"ok": True, "state": info.get("state"), "hostname": info.get("hostname"),
                "version": info.get("software_version")}

    # ------------------------------------------------------------------ поиск
    @classmethod
    async def probe(cls, host: str, timeout: float = 1.5) -> Optional[DiscoveredPrinter]:
        for port in (7125, 80):
            d = await cls._probe_port(host, port, timeout)
            if d:
                return d
        return None

    @classmethod
    async def _probe_port(cls, host: str, port: int, timeout: float) -> Optional[DiscoveredPrinter]:
        base = f"http://{host}" + ("" if port == 80 else f":{port}")
        try:
            async with httpx.AsyncClient(timeout=timeout) as c:
                r = await c.get(f"{base}/server/info")
                if r.status_code == 401:
                    return DiscoveredPrinter(kind=cls.kind, host=host, name=f"Klipper {host}",
                                             port=port, needs=["api_key"],
                                             notes="Moonraker требует API-ключ")
                if r.status_code != 200 or "moonraker_version" not in r.text:
                    return None
                res = r.json().get("result", {})
                name = host
                try:
                    pi = (await c.get(f"{base}/printer/info")).json().get("result", {})
                    name = pi.get("hostname") or host
                except Exception:  # noqa: BLE001
                    pass
                return DiscoveredPrinter(kind=cls.kind, host=host, name=name, model="Klipper",
                                         firmware=f"Moonraker {res.get('moonraker_version', '')}",
                                         port=port)
        except Exception:  # noqa: BLE001
            return None
