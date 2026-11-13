"""OctoPrint REST API — для принтеров на Marlin, подключённых по USB к Raspberry Pi/ПК.

Подходит для: Anycubic Kobra / Kobra 2 / 2 Neo / Kobra Max, Elegoo Neptune 3 и др.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, AsyncIterator, Optional
from urllib.parse import quote

import httpx

from ..models import Capabilities, DiscoveredPrinter, FileEntry, PrinterState, Temperature
from .base import DriverError, PrinterDriver, ProgressCallback, mjpeg_frames_from_http


class OctoPrintDriver(PrinterDriver):
    kind = "octoprint"
    title = "OctoPrint (Marlin по USB)"
    default_port = 80
    required_fields = ("host", "api_key")
    help = ("Принтеры на Marlin без сети (Anycubic Kobra 2 Neo, Elegoo Neptune 3 и т.п.) через OctoPrint. "
            "API-ключ: OctoPrint → Настройки → Application Keys.")
    poll_interval = 3.0

    @property
    def base(self) -> str:
        port = f":{self.port}" if self.port not in (80, 0) else ""
        return f"http://{self.cfg.host}{port}"

    def _h(self) -> dict[str, str]:
        return {"X-Api-Key": self.cfg.api_key}

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        async with httpx.AsyncClient(timeout=kw.pop("timeout", 10)) as c:
            r = await c.request(method, self.base + path, headers=self._h(), **kw)
            if r.status_code == 409:
                raise DriverError("Принтер не готов (409): " + r.text[:200])
            if r.status_code >= 400:
                raise DriverError(f"{r.status_code}: {r.text[:200]}")
            return r.json() if r.content and "json" in r.headers.get("content-type", "") else None

    async def run(self) -> None:
        ver = await self._req("GET", "/api/version")
        self.status.firmware = f"OctoPrint {ver.get('server', '')}"
        while not self._stopping:
            await self._poll()
            await asyncio.sleep(self.poll_interval)

    async def _poll(self) -> None:
        st = self.status
        job = await self._req("GET", "/api/job")
        try:
            printer = await self._req("GET", "/api/printer")
        except DriverError as e:
            if "409" in str(e):  # принтер не подключён к OctoPrint
                st.state = PrinterState.OFFLINE
                st.message = "OctoPrint запущен, но принтер не подключён по USB"
                await self.emit()
                return
            raise
        flags = printer.get("state", {}).get("flags", {})
        st.state_raw = printer.get("state", {}).get("text", "")
        if flags.get("error") or flags.get("closedOrError"):
            st.state = PrinterState.ERROR
            st.error = job.get("state", "")
        elif flags.get("paused") or flags.get("pausing"):
            st.state = PrinterState.PAUSED
        elif flags.get("printing"):
            st.state = PrinterState.PRINTING
        elif flags.get("cancelling"):
            st.state = PrinterState.BUSY
        elif flags.get("ready") or flags.get("operational"):
            st.state = PrinterState.IDLE
            st.error = ""
        else:
            st.state = PrinterState.UNKNOWN
        prog = job.get("progress") or {}
        st.progress = round(prog["completion"], 1) if prog.get("completion") is not None else None
        st.elapsed_s = prog.get("printTime")
        st.remaining_s = prog.get("printTimeLeft")
        st.filename = ((job.get("job") or {}).get("file") or {}).get("path") or ""
        temps = {}
        for k, v in (printer.get("temperature") or {}).items():
            if not isinstance(v, dict):
                continue
            label = {"tool0": "nozzle", "bed": "bed", "chamber": "chamber"}.get(k, k)
            temps[label] = Temperature(v.get("actual"), v.get("target"))
        st.temps = temps
        st.message = ""
        await self.emit()

    def capabilities(self) -> Capabilities:
        return Capabilities(upload=True, start=True, pause=True, resume=True, stop=True, files=True,
                            delete=True, camera=True, gcode=True, speed=True)

    async def list_files(self) -> list[FileEntry]:
        res = await self._req("GET", "/api/files/local", params={"recursive": "true"})
        out: list[FileEntry] = []

        def walk(items: list[dict[str, Any]]) -> None:
            for f in items:
                if f.get("type") == "folder":
                    walk(f.get("children", []))
                else:
                    out.append(FileEntry(name=f.get("name", ""), path=f.get("path", ""), size=f.get("size"),
                                         modified=f.get("date"),
                                         meta={"estimated_time": (f.get("gcodeAnalysis") or {}).get("estimatedPrintTime")}))
        walk(res.get("files", []))
        out.sort(key=lambda f: f.modified or 0, reverse=True)
        return out

    async def upload(self, local_path: str, remote_name: str,
                     progress: Optional[ProgressCallback] = None) -> str:
        size = os.path.getsize(local_path)
        with open(local_path, "rb") as fh:
            await self._req("POST", "/api/files/local", timeout=httpx.Timeout(30, write=600),
                            files={"file": (remote_name, fh, "application/octet-stream")})
        if progress:
            progress(size, size)
        return remote_name

    async def delete_file(self, remote_path: str) -> None:
        await self._req("DELETE", f"/api/files/local/{quote(remote_path)}")

    async def start_print(self, remote_path: str, options: Optional[dict[str, Any]] = None) -> None:
        await self._req("POST", f"/api/files/local/{quote(remote_path)}", json={"command": "select", "print": True})

    async def pause(self) -> None:
        await self._req("POST", "/api/job", json={"command": "pause", "action": "pause"})

    async def resume(self) -> None:
        await self._req("POST", "/api/job", json={"command": "pause", "action": "resume"})

    async def cancel(self) -> None:
        await self._req("POST", "/api/job", json={"command": "cancel"})

    async def send_gcode(self, gcode: str) -> None:
        await self._req("POST", "/api/printer/command", json={"commands": gcode.splitlines()})

    async def set_speed(self, value: str) -> None:
        await self._req("POST", "/api/printer/printhead", json={"command": "feedrate", "factor": int(float(value))})

    async def camera_frames(self) -> AsyncIterator[bytes]:
        try:
            s = await self._req("GET", "/api/settings")
            url = (s.get("webcam") or {}).get("streamUrl") or "/webcam/?action=stream"
        except Exception:  # noqa: BLE001
            url = "/webcam/?action=stream"
        if url.startswith("/"):
            url = f"http://{self.cfg.host}{url}"
        async for f in mjpeg_frames_from_http(url):
            yield f

    async def test_connection(self) -> dict[str, Any]:
        ver = await self._req("GET", "/api/version")
        return {"ok": True, "version": ver.get("server")}

    @classmethod
    async def probe(cls, host: str, timeout: float = 1.5) -> Optional[DiscoveredPrinter]:
        try:
            async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as c:
                r = await c.get(f"http://{host}/")
                if "octoprint" in r.text[:5000].lower():
                    return DiscoveredPrinter(kind=cls.kind, host=host, name=f"OctoPrint {host}",
                                             model="OctoPrint", port=80, needs=["api_key"],
                                             notes="Нужен API-ключ OctoPrint")
        except Exception:  # noqa: BLE001
            return None
        return None
