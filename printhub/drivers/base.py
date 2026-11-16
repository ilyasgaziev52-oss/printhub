"""Базовый класс драйвера принтера."""
from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from ..models import Capabilities, DiscoveredPrinter, FileEntry, PrinterConfig, PrinterState, PrinterStatus

log = logging.getLogger("printhub.driver")

StatusCallback = Callable[["PrinterDriver"], Awaitable[None] | None]
ProgressCallback = Callable[[int, int], None]


class NotSupported(Exception):
    """Операция не поддерживается этим принтером/протоколом."""


class DriverError(Exception):
    pass


class PrinterDriver(ABC):
    kind: str = ""
    title: str = ""                 # человекочитаемое имя протокола
    default_port: int = 0
    technology: str = "fdm"
    # поля конфигурации, которые нужны для подключения
    required_fields: tuple[str, ...] = ("host",)
    help: str = ""
    accepted_ext: tuple[str, ...] = (".gcode", ".gco", ".g")

    def __init__(self, cfg: PrinterConfig, on_update: Optional[StatusCallback] = None):
        self.cfg = cfg
        self.on_update = on_update
        self.status = PrinterStatus()
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self.log = logging.getLogger(f"printhub.{self.kind}.{cfg.name or cfg.host}")
        self.last_error = ""

    # ------------------------------------------------------------------ жизненный цикл
    @property
    def port(self) -> int:
        return self.cfg.port or self.default_port

    async def start(self) -> None:
        self._stopping = False
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_forever(), name=f"driver-{self.cfg.id}")

    async def stop(self) -> None:
        self._stopping = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.close()
        self._task = None

    async def _run_forever(self) -> None:
        backoff = 2.0
        while not self._stopping:
            started = time.monotonic()
            try:
                await self.run()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.last_error = f"{type(e).__name__}: {e}"
                self.log.warning("соединение потеряно: %s", self.last_error)
            if self._stopping:
                break
            if self.status.state != PrinterState.OFFLINE:
                self.status.state = PrinterState.OFFLINE
                self.status.message = self.last_error
                await self.emit()
            if time.monotonic() - started > 60:
                backoff = 2.0
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)

    @abstractmethod
    async def run(self) -> None:
        """Подключиться и обрабатывать обновления, пока соединение живо."""

    async def close(self) -> None:
        """Закрыть соединения."""

    async def emit(self) -> None:
        self.status.updated_at = time.time()
        if self.on_update:
            r = self.on_update(self)
            if asyncio.iscoroutine(r):
                await r

    # ------------------------------------------------------------------ возможности
    def capabilities(self) -> Capabilities:
        return Capabilities()

    def accepted_extensions(self) -> tuple[str, ...]:
        """Какие файлы принтер умеет печатать."""
        return self.accepted_ext

    async def list_files(self) -> list[FileEntry]:
        raise NotSupported("Просмотр файлов не поддерживается")

    async def upload(self, local_path: str, remote_name: str,
                     progress: Optional[ProgressCallback] = None) -> str:
        raise NotSupported("Загрузка файлов не поддерживается")

    async def delete_file(self, remote_path: str) -> None:
        raise NotSupported("Удаление файлов не поддерживается")

    async def start_print(self, remote_path: str, options: Optional[dict[str, Any]] = None) -> None:
        raise NotSupported("Запуск печати не поддерживается")

    async def pause(self) -> None:
        raise NotSupported("Пауза не поддерживается")

    async def resume(self) -> None:
        raise NotSupported("Продолжение не поддерживается")

    async def cancel(self) -> None:
        raise NotSupported("Остановка не поддерживается")

    async def set_light(self, on: bool) -> None:
        raise NotSupported("Управление подсветкой не поддерживается")

    async def send_gcode(self, gcode: str) -> None:
        raise NotSupported("Отправка G-code не поддерживается")

    async def set_speed(self, value: str) -> None:
        raise NotSupported("Изменение скорости не поддерживается")

    async def camera_frames(self) -> AsyncIterator[bytes]:
        """Асинхронный генератор JPEG-кадров."""
        raise NotSupported("Камера не поддерживается")
        yield b""  # pragma: no cover

    async def test_connection(self) -> dict[str, Any]:
        """Быстрая проверка доступности (для мастера добавления)."""
        return {"ok": True}

    async def diagnose(self) -> list[dict[str, Any]]:
        """Пошаговая диагностика: [{"step", "ok", "detail"}]. По умолчанию — порт и проверка соединения."""
        steps: list[dict[str, Any]] = []
        if self.port:
            ok = await tcp_open(self.cfg.host, self.port)
            steps.append({"step": f"Порт {self.port} на {self.cfg.host}", "ok": ok,
                          "detail": "открыт" if ok else "не отвечает — принтер выключен, другой IP или другая сеть"})
            if not ok:
                return steps
        try:
            r = await asyncio.wait_for(self.test_connection(), 25)
            steps.append({"step": "Подключение к принтеру", "ok": bool(r.get("ok")),
                          "detail": r.get("error") or r.get("warning") or "успешно"})
        except Exception as e:  # noqa: BLE001
            steps.append({"step": "Подключение к принтеру", "ok": False, "detail": str(e) or type(e).__name__})
        return steps

    # ------------------------------------------------------------------ поиск в сети
    @classmethod
    async def discover(cls, timeout: float = 3.0) -> list[DiscoveredPrinter]:
        return []

    @classmethod
    async def probe(cls, host: str, timeout: float = 1.5) -> Optional[DiscoveredPrinter]:
        """Проверить конкретный адрес (используется сканером подсети)."""
        return None

    @classmethod
    def describe(cls) -> dict[str, Any]:
        return {"kind": cls.kind, "title": cls.title, "default_port": cls.default_port,
                "technology": cls.technology, "required_fields": list(cls.required_fields), "help": cls.help}


async def tcp_open(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        w.close()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def ffmpeg_jpeg_frames(url: str, input_args: Optional[list[str]] = None, fps: float = 2.0,
                             width: int = 1280) -> AsyncIterator[bytes]:
    """RTSP/RTSPS → JPEG-кадры через ffmpeg (нужен установленный ffmpeg)."""
    import shutil

    exe = shutil.which("ffmpeg")
    if not exe:
        raise NotSupported("Для этой камеры нужен ffmpeg (установите и добавьте в PATH)")
    cmd = [exe, "-hide_banner", "-loglevel", "error", *(input_args or []), "-i", url,
           "-vf", f"fps={fps},scale='min({width},iw)':-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "6", "-"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    buf = bytearray()
    try:
        assert proc.stdout is not None
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                err = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
                raise DriverError(f"ffmpeg завершился: {err.strip()[-300:]}")
            buf.extend(chunk)
            while True:
                s = buf.find(b"\xff\xd8")
                e = buf.find(b"\xff\xd9", s + 2) if s >= 0 else -1
                if s < 0 or e < 0:
                    break
                yield bytes(buf[s:e + 2])
                del buf[:e + 2]
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass


async def mjpeg_frames_from_http(url: str, headers: Optional[dict[str, str]] = None,
                                 timeout: float = 10.0) -> AsyncIterator[bytes]:
    """Читает multipart MJPEG-поток и отдаёт отдельные JPEG-кадры."""
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=None)) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            resp.raise_for_status()
            ctype = resp.headers.get("content-type", "")
            buf = bytearray()
            if "multipart" not in ctype:
                # одиночный снимок
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                yield bytes(buf)
                return
            async for chunk in resp.aiter_bytes():
                buf.extend(chunk)
                while True:
                    start = buf.find(b"\xff\xd8")
                    if start < 0:
                        if len(buf) > 4_000_000:
                            buf.clear()
                        break
                    end = buf.find(b"\xff\xd9", start + 2)
                    if end < 0:
                        if start > 0:
                            del buf[:start]
                        break
                    frame = bytes(buf[start:end + 2])
                    del buf[:end + 2]
                    yield frame
