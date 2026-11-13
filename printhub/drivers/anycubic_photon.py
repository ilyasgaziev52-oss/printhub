"""Anycubic Photon Mono X / Mono X 6K / Mono SE — текстовый протокол Wi-Fi-модуля на TCP 6000.

Умеет: статус, список файлов на флешке, печать, пауза, продолжение, стоп, удаление.
Загрузка файлов по сети этим протоколом невозможна — файл должен быть на USB-флешке принтера.
Опасные команды (detect, setwifi, setZ*, stopUV, setPara) здесь намеренно не используются.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

from ..models import Capabilities, DiscoveredPrinter, FileEntry, PrinterState
from .base import DriverError, NotSupported, PrinterDriver

STATE = {"print": PrinterState.PRINTING, "pause": PrinterState.PAUSED, "stop": PrinterState.IDLE}


def parse_status(resp: str) -> dict[str, Any]:
    parts = [p.strip() for p in resp.strip().split(",")]
    if not parts or parts[0] != "getstatus":
        raise DriverError(f"Неожиданный ответ: {resp[:80]}")
    out: dict[str, Any] = {"state": parts[1] if len(parts) > 1 else "stop"}
    if out["state"] in ("print", "pause") and len(parts) >= 8:
        name = parts[2]
        disp, _, alias = name.rpartition("/")
        out["filename"] = disp or name
        out["alias"] = alias or name

        def num(i: int) -> Optional[float]:
            try:
                return float(parts[i].lstrip("~").rstrip("mL"))
            except (IndexError, ValueError):
                return None
        out["total_layers"] = num(3)
        out["percent"] = num(4)
        out["layer"] = num(5)
        out["elapsed"] = num(6)
        out["remaining"] = num(7)
        out["volume_ml"] = num(8)
        out["layer_height"] = num(11)
    return out


def parse_files(resp: str) -> list[tuple[str, str]]:
    parts = [p.strip() for p in resp.strip().split(",")]
    if not parts or parts[0].lower() != "getfile":
        return []
    out = []
    for item in parts[1:]:
        if not item or item == "end":
            continue
        disp, _, alias = item.rpartition("/")
        out.append((disp or item, alias or item))
    return out


async def photon_cmd(host: str, port: int, command: str, timeout: float = 10.0) -> str:
    """Одна команда: подключиться, отправить, читать до «,end»."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    try:
        writer.write(command.encode())
        await writer.drain()
        buf = b""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not buf.rstrip().endswith(b",end"):
            left = deadline - loop.time()
            if left <= 0:
                break
            chunk = await asyncio.wait_for(reader.read(4096), left)
            if not chunk:
                break
            buf += chunk
        return buf.decode("utf-8", "replace")
    finally:
        writer.close()


def pick_elapsed(elapsed: Optional[float], remaining: Optional[float], percent: Optional[float]) -> Optional[float]:
    """У разных прошивок «прошло» в секундах или минутах — выбираем вариант, согласованный с процентом."""
    if elapsed is None or not remaining or not percent:
        return elapsed
    target = percent / 100.0
    as_sec = elapsed / (elapsed + remaining)
    as_min = elapsed * 60 / (elapsed * 60 + remaining)
    return elapsed * 60 if abs(as_min - target) < abs(as_sec - target) else elapsed


class AnycubicPhotonDriver(PrinterDriver):
    kind = "anycubic_photon"
    title = "Anycubic Photon (Wi-Fi, порт 6000)"
    default_port = 6000
    technology = "resin"
    required_fields = ("host",)
    accepted_ext = (".pwmx", ".pwmo", ".pwms", ".pwma", ".pwmb", ".px6s", ".pw0", ".pws")
    help = ("Photon Mono X, Mono X 6K, Mono SE. Протокол не позволяет загружать файлы — "
            "печать файлов, уже записанных на флешку принтера.")
    poll_interval = 6.0

    def __init__(self, *a: Any, **kw: Any):
        super().__init__(*a, **kw)
        self._lock = asyncio.Lock()
        self._aliases: dict[str, str] = {}

    async def cmd(self, command: str, timeout: float = 10.0) -> str:
        async with self._lock:
            return await photon_cmd(self.cfg.host, self.port, command, timeout)

    async def run(self) -> None:
        info = await self.cmd("sysinfo,")
        parts = [p.strip() for p in info.split(",")]
        if len(parts) > 2:
            if not self.cfg.model:
                self.cfg.model = f"Anycubic {parts[1]}"
            self.status.firmware = parts[2]
        while not self._stopping:
            resp = await self.cmd("getstatus,")
            s = parse_status(resp)
            st = self.status
            st.state_raw = s["state"]
            st.state = STATE.get(s["state"], PrinterState.UNKNOWN)
            st.filename = s.get("filename", "")
            st.progress = s.get("percent")
            st.layer = int(s["layer"]) if s.get("layer") is not None else None
            st.total_layers = int(s["total_layers"]) if s.get("total_layers") is not None else None
            el, rem = s.get("elapsed"), s.get("remaining")
            el = pick_elapsed(el, rem, st.progress)
            st.elapsed_s, st.remaining_s = el, rem
            st.message = f"Расход ≈ {s['volume_ml']:.0f} мл" if s.get("volume_ml") else ""
            await self.emit()
            await asyncio.sleep(self.poll_interval)

    def capabilities(self) -> Capabilities:
        return Capabilities(start=True, pause=True, resume=True, stop=True, files=True, delete=True)

    async def list_files(self) -> list[FileEntry]:
        files = parse_files(await self.cmd("getfile,", timeout=15))
        self._aliases = {disp: alias for disp, alias in files}
        return [FileEntry(name=disp, path=alias) for disp, alias in files]

    async def _alias(self, remote: str) -> str:
        if remote.endswith(tuple(f".{x}" for x in ("pwmx", "pwmb", "pwma", "pwms", "pwmo", "px6s", "pw0", "pws"))) \
                and "/" not in remote and remote in self._aliases.values():
            return remote
        if not self._aliases:
            await self.list_files()
        return self._aliases.get(remote, self._aliases.get(os.path.basename(remote), remote))

    async def start_print(self, remote_path: str, options: Optional[dict[str, Any]] = None) -> None:
        alias = await self._alias(remote_path)
        resp = await self.cmd(f"goprint,{alias},end")
        if "OK" not in resp:
            raise DriverError(f"Принтер отказал: {resp.strip()}")

    async def pause(self) -> None:
        await self.cmd("gopause,end")

    async def resume(self) -> None:
        await self.cmd("goresume,end")

    async def cancel(self) -> None:
        await self.cmd("gostop,end")

    async def delete_file(self, remote_path: str) -> None:
        alias = await self._alias(remote_path)
        await self.cmd(f"delfile,{alias},end")

    async def upload(self, local_path: str, remote_name: str, progress: Any = None) -> str:
        raise NotSupported("Photon Mono X/SE не поддерживает загрузку по сети — запишите файл на флешку")

    async def test_connection(self) -> dict[str, Any]:
        info = await self.cmd("sysinfo,")
        if not info.startswith("sysinfo"):
            raise DriverError("Принтер не ответил на sysinfo")
        parts = [p.strip() for p in info.split(",")]
        return {"ok": True, "model": f"Anycubic {parts[1]}" if len(parts) > 1 else "Anycubic Photon",
                "firmware": parts[2] if len(parts) > 2 else ""}

    @classmethod
    async def probe(cls, host: str, timeout: float = 1.5) -> Optional[DiscoveredPrinter]:
        try:
            info = await asyncio.wait_for(photon_cmd(host, cls.default_port, "sysinfo,", timeout=3), timeout + 3)
        except Exception:  # noqa: BLE001
            return None
        if not info.startswith("sysinfo"):
            return None
        parts = [p.strip() for p in info.split(",")]
        model = f"Anycubic {parts[1]}" if len(parts) > 1 else "Anycubic Photon"
        return DiscoveredPrinter(kind=cls.kind, host=host, name=model, model=model, technology="resin",
                                 firmware=parts[2] if len(parts) > 2 else "", serial=parts[3] if len(parts) > 4 else "",
                                 port=6000, notes="Печать только файлов с флешки принтера")
