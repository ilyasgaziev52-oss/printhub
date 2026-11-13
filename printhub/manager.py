"""Менеджер принтеров: драйверы, статусы, история заданий, очередь, камеры, уведомления."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .analysis.cost import calc_cost
from .analysis.gcode import format_duration
from .db import Database
from .drivers import get_driver_class
from .drivers.base import DriverError, NotSupported, PrinterDriver
from .models import ACTIVE_STATES, PrinterConfig, PrinterState
from .notify import send_telegram

log = logging.getLogger("printhub.manager")

STATE_RU = {
    "offline": "нет связи", "idle": "готов", "preparing": "подготовка", "printing": "печать",
    "paused": "пауза", "finished": "готово", "cancelled": "отменено", "error": "ошибка",
    "busy": "занят", "unknown": "неизвестно",
}


class CameraHub:
    """Одно подключение к камере на принтер, кадры раздаются всем зрителям."""

    IDLE_TIMEOUT = 15.0

    def __init__(self, driver: PrinterDriver):
        self.driver = driver
        self.subs: set[asyncio.Queue[bytes]] = set()
        self.latest: Optional[bytes] = None
        self.latest_at = 0.0
        self.error = ""
        self._task: Optional[asyncio.Task] = None
        self._last_sub = time.monotonic()

    def _ensure(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._upstream())

    async def _upstream(self) -> None:
        try:
            async for frame in self.driver.camera_frames():
                self.latest, self.latest_at, self.error = frame, time.time(), ""
                for q in list(self.subs):
                    if q.full():
                        try:
                            q.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                    q.put_nowait(frame)
                if not self.subs and time.monotonic() - self._last_sub > self.IDLE_TIMEOUT:
                    break
        except NotSupported as e:
            self.error = str(e)
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"
            log.info("камера %s: %s", self.driver.cfg.name, self.error)
        finally:
            for q in list(self.subs):
                q.put_nowait(b"")  # сигнал завершения

    async def frames(self) -> AsyncIterator[bytes]:
        q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)
        self.subs.add(q)
        self._last_sub = time.monotonic()
        self._ensure()
        try:
            if self.latest and time.time() - self.latest_at < 5:
                yield self.latest
            while True:
                frame = await q.get()
                if not frame:
                    if self.error:
                        break
                    self._ensure()
                    continue
                yield frame
        finally:
            self.subs.discard(q)
            self._last_sub = time.monotonic()

    async def snapshot(self, timeout: float = 8.0) -> Optional[bytes]:
        if self.latest and time.time() - self.latest_at < 3:
            return self.latest
        agen = self.frames()
        try:
            return await asyncio.wait_for(agen.__anext__(), timeout)
        except (asyncio.TimeoutError, StopAsyncIteration):
            return None
        finally:
            await agen.aclose()


class PrinterManager:
    def __init__(self, db: Database, data_dir: Path):
        self.db = db
        self.data_dir = Path(data_dir)
        self.library_dir = self.data_dir / "library"
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.drivers: dict[str, PrinterDriver] = {}
        self.clients: set[asyncio.Queue[dict[str, Any]]] = set()
        self.transfers: dict[str, dict[str, Any]] = {}
        self.last_sent: dict[str, dict[str, Any]] = {}
        self.ready_flags: dict[str, bool] = {}
        self.prev_state: dict[str, PrinterState] = {}
        self.cameras: dict[str, CameraHub] = {}
        self._bg: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ жизненный цикл
    async def start(self) -> None:
        for cfg in self.db.list_printers():
            if cfg.enabled:
                await self.add_driver(cfg)

    async def stop(self) -> None:
        for d in list(self.drivers.values()):
            await d.stop()
        self.drivers.clear()

    def _spawn(self, coro: Any) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)
        return t

    def make_driver(self, cfg: PrinterConfig) -> PrinterDriver:
        cls = get_driver_class(cfg.kind)
        if not cls:
            raise DriverError(f"Неизвестный тип принтера: {cfg.kind}")
        return cls(cfg, on_update=self._on_update)

    async def add_driver(self, cfg: PrinterConfig) -> None:
        await self.remove_driver(cfg.id)
        drv = self.make_driver(cfg)
        self.drivers[cfg.id] = drv
        self.prev_state[cfg.id] = PrinterState.OFFLINE
        await drv.start()
        await self.broadcast({"type": "printers_changed"})

    async def remove_driver(self, pid: str) -> None:
        drv = self.drivers.pop(pid, None)
        self.cameras.pop(pid, None)
        if drv:
            await drv.stop()

    def get(self, pid: str) -> PrinterDriver:
        drv = self.drivers.get(pid)
        if not drv:
            raise KeyError(pid)
        return drv

    # ------------------------------------------------------------------ события для UI
    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=500)
        self.clients.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        self.clients.discard(q)

    async def broadcast(self, msg: dict[str, Any]) -> None:
        for q in list(self.clients):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    def snapshot(self, pid: str) -> dict[str, Any]:
        drv = self.drivers.get(pid)
        cfg = drv.cfg if drv else self.db.get_printer(pid)
        if cfg is None:
            raise KeyError(pid)
        d: dict[str, Any] = {"config": cfg.to_dict(hide_secrets=True)}
        if drv:
            d["status"] = drv.status.to_dict()
            d["capabilities"] = drv.capabilities().to_dict()
            d["driver_title"] = drv.title
            d["last_error"] = drv.last_error
            d["accepts"] = list(drv.accepted_extensions())
        else:
            d["status"] = {"state": "offline", "message": "Принтер отключён в настройках"}
            d["capabilities"] = {}
            d["accepts"] = []
        d["ready_for_next"] = self.ready_flags.get(pid, False)
        d["queue"] = [q for q in self.db.list_queue() if q["printer_id"] == pid]
        return d

    def all_snapshots(self) -> list[dict[str, Any]]:
        return [self.snapshot(c.id) for c in self.db.list_printers()]

    # ------------------------------------------------------------------ обработка статусов
    async def _on_update(self, drv: PrinterDriver) -> None:
        pid = drv.cfg.id
        st = drv.status
        prev = self.prev_state.get(pid, PrinterState.OFFLINE)
        self.prev_state[pid] = st.state
        await self.broadcast({"type": "status", "printer_id": pid, "status": st.to_dict(),
                              "capabilities": drv.capabilities().to_dict()})
        if prev != st.state:
            try:
                await self._on_transition(drv, prev, st.state)
            except Exception:  # noqa: BLE001
                log.exception("ошибка обработки смены статуса")

    async def _on_transition(self, drv: PrinterDriver, prev: PrinterState, new: PrinterState) -> None:
        pid, cfg, st = drv.cfg.id, drv.cfg, drv.status
        log.info("%s: %s → %s", cfg.name, prev.value, new.value)
        job = self.db.open_job(pid)

        if new in (PrinterState.PRINTING, PrinterState.PREPARING):
            fname = st.filename or "(без имени)"
            if job and job["filename"] != fname and st.filename:
                self.db.finish_job(job["id"], "cancelled")
                job = None
            if not job:
                sent = self.last_sent.get(pid, {})
                lib_id = sent.get("library_id", "") if sent.get("remote_name") and \
                    _same_file(sent.get("remote_name", ""), fname) else ""
                self.db.start_job(pid, cfg.name, fname, lib_id, sent.get("material_id") if lib_id else None)
                self.ready_flags[pid] = False
            return

        if job and new in (PrinterState.FINISHED, PrinterState.CANCELLED, PrinterState.ERROR, PrinterState.IDLE):
            progress = st.progress or 0.0
            if new == PrinterState.FINISHED or (new == PrinterState.IDLE and progress >= 99.5):
                status = "finished"
                frac = 1.0
            elif new == PrinterState.ERROR:
                status = "error"
                frac = progress / 100.0
            else:
                status = "cancelled"
                frac = progress / 100.0
            mat_g = mat_ml = cost = None
            est = self._job_estimate(job)
            if est:
                if est.get("material_g") is not None:
                    mat_g = est["material_g"] * frac
                if est.get("material_ml") is not None:
                    mat_ml = est["material_ml"] * frac
                material = self.db.get_material(job["material_id"]) if job.get("material_id") else None
                dur = time.time() - (job["started_at"] or time.time())
                c = calc_cost(technology=cfg.technology, time_s=dur, material_g=mat_g, material_ml=mat_ml,
                              material=material, settings=self.db.get_settings(), power_w=cfg.power_w)
                cost = c["total"]
                if job.get("material_id") and mat_g:
                    self.db.consume_material(job["material_id"], mat_g if cfg.technology == "fdm"
                                             else (mat_ml or 0) * float((material or {}).get("density") or 1.1))
            self.db.finish_job(job["id"], status, material_g=mat_g, material_ml=mat_ml, cost=cost)
            if new != PrinterState.IDLE:
                await self._notify(drv, {"finished": "finished", "error": "error", "cancelled": "cancelled"}[status])
            await self.broadcast({"type": "jobs_changed"})

        if new == PrinterState.PAUSED and prev == PrinterState.PRINTING:
            await self._notify(drv, "paused")
        if new == PrinterState.OFFLINE and prev in ACTIVE_STATES:
            await self._notify(drv, "offline")
        if new == PrinterState.ERROR and not job:
            await self._notify(drv, "error")

        if new in (PrinterState.IDLE, PrinterState.FINISHED, PrinterState.CANCELLED):
            await self.check_queue(pid)

    def _job_estimate(self, job: dict[str, Any]) -> Optional[dict[str, Any]]:
        if not job.get("library_id"):
            return None
        item = self.db.get_library_item(job["library_id"])
        if not item:
            return None
        return summarize_analysis(item["analysis"], plate=self.last_sent.get(job["printer_id"], {}).get("plate", 1))

    async def _notify(self, drv: PrinterDriver, event: str) -> None:
        s = self.db.get_settings()
        if event not in (s.get("notify_events") or []):
            return
        st = drv.status
        icons = {"finished": "✅", "error": "❌", "paused": "⏸", "offline": "📡", "cancelled": "⏹"}
        names = {"finished": "Печать завершена", "error": "Ошибка", "paused": "Печать на паузе",
                 "offline": "Принтер пропал из сети во время печати", "cancelled": "Печать отменена"}
        text = f"{icons.get(event, '')} <b>{drv.cfg.name}</b>: {names.get(event, event)}"
        if st.filename:
            text += f"\nФайл: {st.filename}"
        if st.error:
            text += f"\nОшибка: {st.error}"
        if event == "paused" and st.progress is not None:
            text += f"\nПрогресс: {st.progress:.0f}%, осталось {format_duration(st.remaining_s)}"
        await self.broadcast({"type": "toast", "level": "error" if event in ("error", "offline") else "info",
                              "text": re.sub(r"<[^>]+>", "", text)})
        token, chat = s.get("telegram_token"), s.get("telegram_chat_id")
        if token and chat:
            photo = None
            if drv.capabilities().camera and event in ("finished", "error", "paused"):
                try:
                    photo = await self.camera(drv.cfg.id).snapshot(timeout=6)
                except Exception:  # noqa: BLE001
                    photo = None
            self._spawn(send_telegram(token, chat, text, photo))

    # ------------------------------------------------------------------ камера
    def camera(self, pid: str) -> CameraHub:
        drv = self.get(pid)
        hub = self.cameras.get(pid)
        if hub is None or hub.driver is not drv:
            hub = CameraHub(drv)
            self.cameras[pid] = hub
        return hub

    # ------------------------------------------------------------------ отправка файлов
    def prepare_file_for(self, drv: PrinterDriver, item: dict[str, Any], plate: int = 1) -> tuple[str, str, bool]:
        """Возвращает (локальный путь, имя на принтере, временный ли файл)."""
        path = item["stored_path"]
        name = item["filename"]
        low = name.lower()
        accepts = tuple(e.lower() for e in drv.accepted_extensions())
        if low.endswith(accepts):
            return path, name, False
        if low.endswith(".3mf") and ".gcode" in accepts:
            with zipfile.ZipFile(path) as z:
                member = f"Metadata/plate_{plate}.gcode"
                if member not in z.namelist():
                    raise DriverError("В 3MF нет нарезанной пластины №%d — сначала нарежьте проект в слайсере" % plate)
                fd, tmp = tempfile.mkstemp(suffix=".gcode", dir=self.data_dir)
                with os.fdopen(fd, "wb") as out, z.open(member) as src:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
            base = re.sub(r"(\.gcode)?\.3mf$", "", name, flags=re.I)
            suffix = f"_plate{plate}" if plate != 1 else ""
            return tmp, f"{base}{suffix}.gcode", True
        if item["kind"] == "mesh":
            raise DriverError("Это 3D-модель, а не файл для печати. Нарежьте её в слайсере (вкладка «Подготовка»).")
        raise DriverError(f"Принтер {drv.cfg.name} принимает файлы: {', '.join(accepts)}")

    async def send_file(self, pid: str, library_id: str, start: bool = True, plate: int = 1,
                        options: Optional[dict[str, Any]] = None, material_id: Optional[int] = None,
                        queue_id: Optional[int] = None) -> str:
        drv = self.get(pid)
        item = self.db.get_library_item(library_id)
        if not item:
            raise KeyError(library_id)
        local, remote, is_tmp = self.prepare_file_for(drv, item, plate)
        if start and drv.status.state in ACTIVE_STATES:
            if is_tmp:
                os.unlink(local)
            raise DriverError("Принтер уже печатает")
        tid = uuid.uuid4().hex[:10]
        tr = {"id": tid, "printer_id": pid, "printer_name": drv.cfg.name, "filename": remote, "sent": 0,
              "total": os.path.getsize(local), "status": "uploading", "error": "", "start": start}
        self.transfers[tid] = tr
        self._spawn(self._do_send(drv, tr, local, remote, is_tmp, item, start, plate, options or {}, material_id, queue_id))
        await self.broadcast({"type": "transfer", "transfer": tr})
        return tid

    async def _do_send(self, drv: PrinterDriver, tr: dict[str, Any], local: str, remote: str, is_tmp: bool,
                       item: dict[str, Any], start: bool, plate: int, options: dict[str, Any],
                       material_id: Optional[int], queue_id: Optional[int]) -> None:
        loop = asyncio.get_running_loop()
        last = [0.0]

        def progress(sent: int, total: int) -> None:
            tr["sent"], tr["total"] = sent, total
            now = time.monotonic()
            if now - last[0] > 0.5 or sent >= total:
                last[0] = now
                loop.call_soon_threadsafe(lambda: self._spawn(self.broadcast({"type": "transfer", "transfer": dict(tr)})))

        try:
            remote_path = await drv.upload(local, remote, progress)
            tr["sent"] = tr["total"]
            if queue_id:
                self.db.update_queue(queue_id, status="uploaded", message="Файл загружен")
            if start:
                tr["status"] = "starting"
                await self.broadcast({"type": "transfer", "transfer": dict(tr)})
                opts = dict(options)
                opts.setdefault("plate", plate)
                if item["filename"].lower().endswith(".3mf"):
                    plates = ((item.get("analysis") or {}).get("threemf") or {}).get("plates") or []
                    pl = next((x for x in plates if x.get("index") == plate), None)
                    if pl and pl.get("filaments"):
                        opts.setdefault("filaments", pl["filaments"])
                self.last_sent[drv.cfg.id] = {"remote_name": remote_path, "library_id": item["id"],
                                              "material_id": material_id, "plate": plate, "at": time.time()}
                await drv.start_print(remote_path, opts)
                if queue_id:
                    self.db.update_queue(queue_id, status="started", message="Печать запущена")
            tr["status"] = "done"
        except Exception as e:  # noqa: BLE001
            tr["status"] = "error"
            tr["error"] = str(e) or type(e).__name__
            log.warning("отправка %s на %s: %s", remote, drv.cfg.name, tr["error"])
            if queue_id:
                self.db.update_queue(queue_id, status="failed", message=tr["error"])
        finally:
            if is_tmp:
                try:
                    os.unlink(local)
                except OSError:
                    pass
            await self.broadcast({"type": "transfer", "transfer": dict(tr)})
            if queue_id:
                await self.broadcast({"type": "queue_changed"})

    # ------------------------------------------------------------------ очередь
    def set_ready(self, pid: str, ready: bool = True) -> None:
        self.ready_flags[pid] = ready

    async def check_queue(self, pid: str) -> None:
        drv = self.drivers.get(pid)
        if not drv or not self.ready_flags.get(pid):
            return
        if drv.status.state not in (PrinterState.IDLE, PrinterState.FINISHED, PrinterState.CANCELLED):
            return
        for q in self.db.list_queue():
            if q["status"] == "queued" and q["auto_start"] and q["printer_id"] == pid:
                await self.start_queue_item(q["id"])
                break

    async def start_queue_item(self, qid: int, printer_id: Optional[str] = None) -> str:
        q = next((x for x in self.db.list_queue() if x["id"] == qid), None)
        if not q:
            raise KeyError(qid)
        pid = printer_id or q["printer_id"]
        if not pid:
            raise DriverError("Выберите принтер для задания")
        self.db.update_queue(qid, status="uploading", printer_id=pid, message="Загрузка файла…")
        self.ready_flags[pid] = False
        opts = q["options"] or {}
        return await self.send_file(pid, q["library_id"], True, q["plate"] or 1, opts,
                                    opts.get("material_id"), queue_id=qid)


def _same_file(remote: str, reported: str) -> bool:
    def norm(s: str) -> str:
        s = os.path.basename(s.replace("\\", "/")).lower()
        return re.sub(r"(\.gcode)?(\.3mf)?$|\.(gcode|goo|ctb|pwmx|pm\w+)$", "", s)
    return norm(remote) == norm(reported) or norm(remote) in norm(reported) or norm(reported) in norm(remote)


def summarize_analysis(analysis: dict[str, Any], plate: int = 1) -> dict[str, Any]:
    """Сводка «время/материал» из сохранённого анализа файла библиотеки."""
    if not analysis:
        return {}
    kind = analysis.get("kind")
    if kind == "gcode":
        g = analysis.get("gcode", {})
        return {"time_s": g.get("time_s"), "material_g": g.get("filament_g"), "material_m": (g.get("filament_mm") or 0) / 1000}
    if kind == "3mf":
        plates = analysis.get("threemf", {}).get("plates", [])
        p = next((x for x in plates if x.get("index") == plate), plates[0] if plates else None)
        if p:
            return {"time_s": p.get("prediction_s"), "material_g": p.get("weight_g"), "gcode_md5": p.get("gcode_md5")}
    if kind == "resin":
        r = analysis.get("resin", {})
        return {"time_s": r.get("print_time_s"), "material_ml": r.get("volume_ml"), "material_g": r.get("weight_g")}
    if kind == "mesh":
        est = analysis.get("estimate", {})
        return {"time_s": est.get("time_s"), "material_g": est.get("filament_g") or est.get("resin_g"),
                "material_ml": est.get("resin_ml")}
    return {}
