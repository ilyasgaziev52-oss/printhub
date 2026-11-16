"""HTTP API и веб-интерфейс PrintHub."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import Body, FastAPI, File, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .analysis.lazy import mesh as meshmod
from .analysis.cost import calc_cost
from .db import Database
from .discovery import default_subnet, discover_all
from .drivers import all_driver_classes, get_driver_class
from .drivers.base import DriverError, NotSupported
from .library import analyze_path, detect_kind, safe_filename, store_upload
from .manager import PrinterManager, summarize_analysis
from .models import PrinterConfig
from .notify import send_telegram
from . import slicer as slicermod

log = logging.getLogger("printhub.app")


def session_token(password: str) -> str:
    import hashlib
    return hashlib.sha256(("printhub:" + password).encode()).hexdigest()
WEB_DIR = Path(__file__).parent / "web"


def create_app(data_dir: Optional[str] = None, demo: bool = False) -> FastAPI:
    data = Path(data_dir or os.environ.get("PRINTHUB_DATA", Path.cwd() / "data")).resolve()
    data.mkdir(parents=True, exist_ok=True)
    db = Database(data / "printhub.sqlite3")
    db.seed_materials()
    mgr = PrinterManager(db, data)

    demo_obj = None
    if demo:
        from .demo import Demo
        demo_obj = Demo(db, data)

    from .updater import Updater

    updater = Updater(data, db.get_settings,
                      is_busy=lambda: any(t.get("status") in ("uploading", "starting") for t in mgr.transfers.values()),
                      broadcast=mgr.broadcast)

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        if demo_obj:
            await demo_obj.start()
        await mgr.start()
        if not demo:
            updater.start()
        yield
        await updater.stop()
        await mgr.stop()
        if demo_obj:
            await demo_obj.stop()

    app = FastAPI(title="PrintHub", version=__version__, lifespan=lifespan)
    app.state.db = db
    app.state.mgr = mgr
    app.state.updater = updater

    # ------------------------------------------------------------------ авторизация (опционально)
    app.state.shutdown_token = ""
    app.state.request_shutdown = None

    @app.post("/api/system/shutdown", include_in_schema=False)
    async def system_shutdown(request: Request) -> dict[str, Any]:
        tok = app.state.shutdown_token
        host = request.client.host if request.client else ""
        if not tok or host not in ("127.0.0.1", "::1") or \
                not secrets.compare_digest(request.headers.get("x-shutdown-token", ""), tok):
            raise HTTPException(403, "Запрещено")
        if app.state.request_shutdown:
            app.state.request_shutdown()
        return {"ok": True}

    @app.middleware("http")
    async def auth_mw(request: Request, call_next):  # noqa: ANN001, ANN202
        pwd = db.get_settings().get("auth_password") or ""
        if pwd and request.url.path != "/api/system/shutdown":
            hdr = request.headers.get("authorization", "")
            ok = False
            if hdr.lower().startswith("basic "):
                try:
                    _, _, given = base64.b64decode(hdr[6:]).decode().partition(":")
                    ok = secrets.compare_digest(given, pwd)
                except Exception:  # noqa: BLE001
                    ok = False
            if not ok and secrets.compare_digest(request.cookies.get("ph_session", ""), session_token(pwd)):
                ok = True
            if not ok:
                return Response("Требуется пароль", status_code=401,
                                headers={"WWW-Authenticate": 'Basic realm="PrintHub"'})
            resp = await call_next(request)
            resp.set_cookie("ph_session", session_token(pwd), httponly=True, samesite="strict")
            return resp
        return await call_next(request)

    def err(e: Exception) -> HTTPException:
        if isinstance(e, KeyError):
            return HTTPException(404, "Не найдено")
        if isinstance(e, NotSupported):
            return HTTPException(400, str(e))
        if isinstance(e, (DriverError, ValueError)):
            return HTTPException(400, str(e))
        log.exception("ошибка запроса")
        return HTTPException(500, f"{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ общее
    @app.get("/api/info")
    async def info() -> dict[str, Any]:
        s = db.get_settings()
        return {"version": __version__, "data_dir": str(data), "subnet": default_subnet(),
                "update": dict(updater.state),
                "slicers": slicermod.find_slicers(s.get("slicer_path", "")),
                "prusaslicer": slicermod.find_prusaslicer(s.get("prusaslicer_path", "")),
                "uvtools": slicermod.find_uvtools(s.get("uvtools_path", ""))}

    @app.get("/api/drivers")
    async def drivers() -> list[dict[str, Any]]:
        return [c.describe() for c in all_driver_classes()]

    # ------------------------------------------------------------------ принтеры
    @app.get("/api/printers")
    async def printers() -> list[dict[str, Any]]:
        return mgr.all_snapshots()

    def cfg_from_body(body: dict[str, Any], pid: Optional[str] = None) -> PrinterConfig:
        known = PrinterConfig.__dataclass_fields__.keys()
        d = {k: v for k, v in body.items() if k in known}
        d["id"] = pid or d.get("id") or uuid.uuid4().hex[:8]
        if not d.get("kind") or not get_driver_class(d["kind"]):
            raise HTTPException(400, "Неизвестный тип принтера")
        if not d.get("host"):
            raise HTTPException(400, "Укажите IP-адрес принтера")
        d.setdefault("name", d["host"])
        cls = get_driver_class(d["kind"])
        if cls and "technology" not in body:
            d["technology"] = cls.technology
        bv = d.get("build_volume")
        if isinstance(bv, list):
            d["build_volume"] = [float(x or 0) for x in (bv + [0, 0, 0])[:3]]
        return PrinterConfig(**d)

    @app.post("/api/printers")
    async def add_printer(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        cfg = cfg_from_body(body)
        db.save_printer(cfg)
        await mgr.add_driver(cfg)
        return mgr.snapshot(cfg.id)

    @app.put("/api/printers/{pid}")
    async def update_printer(pid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        old = db.get_printer(pid)
        if not old:
            raise HTTPException(404, "Принтер не найден")
        merged = old.to_dict()
        for k, v in body.items():
            if k in ("access_code", "api_key") and v == "••••":
                continue  # секрет не меняли
            merged[k] = v
        cfg = cfg_from_body(merged, pid)
        db.save_printer(cfg)
        if cfg.enabled:
            await mgr.add_driver(cfg)
        else:
            await mgr.remove_driver(pid)
        return mgr.snapshot(pid)

    @app.delete("/api/printers/{pid}")
    async def delete_printer(pid: str) -> dict[str, Any]:
        await mgr.remove_driver(pid)
        db.delete_printer(pid)
        await mgr.broadcast({"type": "printers_changed"})
        return {"ok": True}

    def with_stored_secrets(body: dict[str, Any]) -> dict[str, Any]:
        """В окне настроек секреты показаны точками — подставляем сохранённые значения."""
        body = dict(body)
        old = db.get_printer(body.get("id", "")) if body.get("id") else None
        for k in ("access_code", "api_key"):
            if body.get(k) == "••••":
                body[k] = getattr(old, k) if old else ""
        return body

    @app.post("/api/printers/diagnose")
    async def diagnose_printer(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        cfg = cfg_from_body(with_stored_secrets(body), "diag")
        drv = mgr.make_driver(cfg)
        drv.on_update = None
        try:
            steps = await asyncio.wait_for(drv.diagnose(), 60)
        except asyncio.TimeoutError:
            steps = [{"step": "Диагностика", "ok": False, "detail": "не уложилась в 60 секунд"}]
        except Exception as e:  # noqa: BLE001
            steps = [{"step": "Диагностика", "ok": False, "detail": str(e) or type(e).__name__}]
        finally:
            await drv.close()
        return {"steps": steps, "ok": all(s["ok"] for s in steps)}

    @app.post("/api/printers/test")
    async def test_printer(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        cfg = cfg_from_body(with_stored_secrets(body), "test")
        drv = mgr.make_driver(cfg)
        drv.on_update = None
        try:
            return await asyncio.wait_for(drv.test_connection(), 20)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "Нет ответа за 20 секунд"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": str(e) or type(e).__name__}
        finally:
            await drv.close()

    @app.get("/api/discover")
    async def discover(scan: bool = True, subnet: str = "") -> dict[str, Any]:
        s = subnet or db.get_settings().get("discovery_subnet") or None
        t = time.time()
        found = await discover_all(scan=scan, subnet=s)
        known = {(p.kind, p.host) for p in db.list_printers()}
        items = []
        for d in found:
            x = d.__dict__.copy()
            x["already_added"] = (d.kind, d.host) in known
            items.append(x)
        return {"subnet": s or default_subnet(), "took_s": round(time.time() - t, 1), "printers": items}

    @app.post("/api/printers/{pid}/action")
    async def action(pid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            drv = mgr.get(pid)
            a = body.get("action")
            if a == "pause":
                await drv.pause()
            elif a == "resume":
                await drv.resume()
            elif a == "stop":
                await drv.cancel()
            elif a == "light":
                await drv.set_light(bool(body.get("value")))
            elif a == "speed":
                await drv.set_speed(str(body.get("value")))
            elif a == "gcode":
                await drv.send_gcode(str(body.get("value", "")))
            elif a == "ready":
                mgr.set_ready(pid, bool(body.get("value", True)))
                await mgr.check_queue(pid)
            else:
                raise HTTPException(400, "Неизвестное действие")
            return {"ok": True}
        except HTTPException:
            raise
        except Exception as e:  # noqa: BLE001
            raise err(e) from e

    @app.get("/api/printers/{pid}/files")
    async def printer_files(pid: str) -> list[dict[str, Any]]:
        try:
            files = await asyncio.wait_for(mgr.get(pid).list_files(), 30)
            return [f.__dict__ for f in files]
        except asyncio.TimeoutError as e:
            raise HTTPException(504, "Принтер не ответил") from e
        except Exception as e:  # noqa: BLE001
            raise err(e) from e

    @app.delete("/api/printers/{pid}/files")
    async def printer_delete_file(pid: str, path: str = Query(...)) -> dict[str, Any]:
        try:
            await mgr.get(pid).delete_file(path)
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            raise err(e) from e

    @app.post("/api/printers/{pid}/print")
    async def printer_print(pid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            await mgr.get(pid).start_print(body["path"], body.get("options") or {})
            return {"ok": True}
        except Exception as e:  # noqa: BLE001
            raise err(e) from e

    @app.get("/api/printers/{pid}/camera.mjpg")
    async def camera_mjpg(pid: str) -> StreamingResponse:
        try:
            hub = mgr.camera(pid)
        except KeyError as e:
            raise HTTPException(404, "Принтер не найден") from e
        if not mgr.get(pid).capabilities().camera:
            raise HTTPException(400, "У этого принтера нет доступной камеры")
        fps = float(db.get_settings().get("camera_fps") or 2)

        async def gen():  # noqa: ANN202
            last = 0.0
            async for frame in hub.frames():
                now = time.monotonic()
                if now - last < 1.0 / max(fps, 0.2):
                    continue
                last = now
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(frame)).encode()
                       + b"\r\n\r\n" + frame + b"\r\n")
        return StreamingResponse(gen(), media_type="multipart/x-mixed-replace; boundary=frame",
                                 headers={"Cache-Control": "no-store"})

    @app.get("/api/printers/{pid}/camera.jpg")
    async def camera_jpg(pid: str) -> Response:
        try:
            hub = mgr.camera(pid)
        except KeyError as e:
            raise HTTPException(404, "Принтер не найден") from e
        frame = await hub.snapshot()
        if not frame:
            raise HTTPException(503, hub.error or "Камера не отвечает")
        return Response(frame, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    # ------------------------------------------------------------------ библиотека
    @app.get("/api/library")
    async def library() -> list[dict[str, Any]]:
        items = db.list_library()
        for it in items:
            it["summary"] = summarize_analysis(it["analysis"])
            a = it["analysis"]
            it["analysis"] = {k: a[k] for k in ("kind", "technology", "error") if k in a}
            if "threemf" in a:
                it["plates"] = len(a["threemf"].get("plates", []))
        return items

    @app.post("/api/library")
    async def library_upload(file: UploadFile = File(...), printer_id: str = Query("")) -> dict[str, Any]:
        fd, tmp = tempfile.mkstemp(dir=data)
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(file.file, out, 1 << 20)
        item = store_upload(tmp, file.filename or "file", mgr.library_dir)
        cfg = db.get_printer(printer_id) if printer_id else None
        bv = cfg.build_volume if cfg else None
        tech = cfg.technology if cfg else "fdm"
        analysis, thumb = await asyncio.to_thread(analyze_path, item["stored_path"], item["filename"], bv, tech)
        item.update(kind=analysis["kind"], technology=analysis.get("technology", ""), analysis=analysis,
                    thumbnail=thumb)
        db.add_library_item(item)
        await mgr.broadcast({"type": "library_changed"})
        res = db.get_library_item(item["id"]) or {}
        res["summary"] = summarize_analysis(res.get("analysis") or {})
        return res

    @app.get("/api/library/{lid}")
    async def library_item(lid: str) -> dict[str, Any]:
        it = db.get_library_item(lid)
        if not it:
            raise HTTPException(404, "Файл не найден")
        it.pop("stored_path", None)
        it["summary"] = summarize_analysis(it["analysis"])
        return it

    @app.delete("/api/library/{lid}")
    async def library_delete(lid: str) -> dict[str, Any]:
        it = db.delete_library_item(lid)
        if it:
            shutil.rmtree(Path(it["stored_path"]).parent, ignore_errors=True)
        await mgr.broadcast({"type": "library_changed"})
        return {"ok": True}

    @app.get("/api/library/{lid}/download")
    async def library_download(lid: str) -> FileResponse:
        it = db.get_library_item(lid)
        if not it:
            raise HTTPException(404, "Файл не найден")
        return FileResponse(it["stored_path"], filename=it["filename"])

    @app.get("/api/library/{lid}/thumbnail")
    async def library_thumb(lid: str) -> Response:
        it = db.get_library_item(lid, with_thumb=True)
        if not it or not it.get("thumbnail"):
            raise HTTPException(404, "Нет миниатюры")
        raw = base64.b64decode(it["thumbnail"])
        mime = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png"
        return Response(raw, media_type=mime, headers={"Cache-Control": "max-age=3600"})

    @app.post("/api/library/{lid}/thumbnail")
    async def library_set_thumb(lid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        it = db.get_library_item(lid)
        if not it:
            raise HTTPException(404, "Файл не найден")
        data_url = str(body.get("data_url", ""))
        b64 = data_url.split(",", 1)[1] if "," in data_url else data_url
        if len(b64) > 3_000_000:
            raise HTTPException(400, "Слишком большая миниатюра")
        db.update_library_analysis(lid, it["analysis"], b64)
        return {"ok": True}

    @app.post("/api/library/{lid}/reanalyze")
    async def library_reanalyze(lid: str, printer_id: str = Query("")) -> dict[str, Any]:
        it = db.get_library_item(lid)
        if not it:
            raise HTTPException(404, "Файл не найден")
        cfg = db.get_printer(printer_id) if printer_id else None
        analysis, thumb = await asyncio.to_thread(analyze_path, it["stored_path"], it["filename"],
                                                  cfg.build_volume if cfg else None,
                                                  cfg.technology if cfg else "fdm")
        db.update_library_analysis(lid, analysis, thumb or None)
        return await library_item(lid)

    @app.post("/api/library/{lid}/send")
    async def library_send(lid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        try:
            tid = await mgr.send_file(body["printer_id"], lid, bool(body.get("start", True)),
                                      int(body.get("plate") or 1), body.get("options") or {},
                                      body.get("material_id"))
            return {"transfer_id": tid}
        except Exception as e:  # noqa: BLE001
            raise err(e) from e

    @app.get("/api/transfers")
    async def transfers() -> list[dict[str, Any]]:
        return sorted(mgr.transfers.values(), key=lambda t: t["id"])[-50:]

    # ------------------------------------------------------------------ подготовка модели
    def load_item_mesh(lid: str) -> tuple[dict[str, Any], Any]:
        it = db.get_library_item(lid)
        if not it:
            raise HTTPException(404, "Файл не найден")
        if it["kind"] != "mesh":
            raise HTTPException(400, "Это не 3D-модель")
        m = meshmod.load_mesh(it["stored_path"], file_type=Path(it["filename"]).suffix.lstrip(".").lower())
        return it, m

    def build_volume_for(printer_id: str) -> Optional[list[float]]:
        cfg = db.get_printer(printer_id) if printer_id else None
        return cfg.build_volume if cfg and any(cfg.build_volume) else None

    @app.get("/api/library/{lid}/mesh.stl")
    async def mesh_stl(lid: str) -> Response:
        it, m = await asyncio.to_thread(load_item_mesh, lid)
        data_ = await asyncio.to_thread(meshmod.export_mesh, m, "stl")
        return Response(data_, media_type="model/stl")

    @app.post("/api/library/{lid}/mesh/analyze")
    async def mesh_analyze(lid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        it, m = await asyncio.to_thread(load_item_mesh, lid)
        bv = body.get("build_volume") or build_volume_for(body.get("printer_id", ""))
        rep = await asyncio.to_thread(meshmod.analyze_mesh, m, bv, float(body.get("overhang_angle", 45)))
        return rep.to_dict()

    @app.get("/api/library/{lid}/mesh/orientations")
    async def mesh_orientations(lid: str, goal: str = "balanced") -> list[dict[str, Any]]:
        it, m = await asyncio.to_thread(load_item_mesh, lid)
        opts = await asyncio.to_thread(meshmod.suggest_orientations, m, goal)
        return [o.__dict__ for o in opts]

    def save_mesh_version(it: dict[str, Any], m: Any, suffix: str, replace: bool,
                          bv: Optional[list[float]]) -> dict[str, Any]:
        stem = Path(it["filename"]).stem
        fmt = "3mf" if it["filename"].lower().endswith(".3mf") else "stl"
        data_ = meshmod.export_mesh(m, fmt)
        if replace:
            Path(it["stored_path"]).write_bytes(data_)
            new = dict(it)
            new["size"] = len(data_)
        else:
            fd, tmp = tempfile.mkstemp(dir=data)
            with os.fdopen(fd, "wb") as f:
                f.write(data_)
            new = store_upload(tmp, f"{stem}{suffix}.{fmt}", mgr.library_dir)
            new["kind"] = "mesh"
        analysis, _ = analyze_path(new["stored_path"], new["filename"], bv)
        new["analysis"] = analysis
        new["kind"] = "mesh"
        new["thumbnail"] = ""
        db.add_library_item(new)
        return new

    @app.post("/api/library/{lid}/mesh/repair")
    async def mesh_repair(lid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        it, m = await asyncio.to_thread(load_item_mesh, lid)
        bv = build_volume_for(body.get("printer_id", ""))
        fixed, res = await asyncio.to_thread(meshmod.repair_mesh, m, aggressive=bool(body.get("aggressive")),
                                             remove_debris=bool(body.get("remove_debris", True)),
                                             merge_bodies=bool(body.get("merge_bodies")), build_volume=bv)
        new = await asyncio.to_thread(save_mesh_version, it, fixed, "_fixed", bool(body.get("replace")), bv)
        await mgr.broadcast({"type": "library_changed"})
        return {"steps": res.steps, "before": res.before.to_dict() if res.before else None,
                "after": res.after.to_dict() if res.after else None, "item_id": new["id"]}

    @app.post("/api/library/{lid}/mesh/transform")
    async def mesh_transform(lid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        it, m = await asyncio.to_thread(load_item_mesh, lid)
        printer_id = body.get("printer_id", "")
        cfg = db.get_printer(printer_id) if printer_id else None
        import numpy as np

        def apply_ops() -> Any:
            for op in body.get("ops", []):
                kind = op.get("op")
                if kind == "scale":
                    meshmod.scale_mesh(m, op.get("value", 1.0))
                elif kind == "scale_to":
                    meshmod.scale_to_size(m, int(op.get("axis", 2)), float(op["size"]))
                elif kind == "rotate":
                    meshmod.rotate_mesh(m, float(op.get("x", 0)), float(op.get("y", 0)), float(op.get("z", 0)))
                elif kind == "lay_flat":
                    meshmod.lay_flat(m, op.get("goal", "balanced"))
                elif kind == "orientation":
                    m.apply_transform(np.array(op["matrix"], dtype=float))
                elif kind == "mirror":
                    meshmod.mirror_mesh(m, int(op.get("axis", 0)))
                elif kind == "center":
                    pass
                else:
                    raise ValueError(f"Неизвестная операция {kind}")
            meshmod.place_on_bed(m, cfg.build_volume[:2] if cfg and any(cfg.build_volume) else None)
            return m

        try:
            await asyncio.to_thread(apply_ops)
        except Exception as e:  # noqa: BLE001
            raise err(e) from e
        new = await asyncio.to_thread(save_mesh_version, it, m, "_edit", bool(body.get("replace")),
                                      cfg.build_volume if cfg else None)
        await mgr.broadcast({"type": "library_changed"})
        return {"item_id": new["id"], "mesh": new["analysis"].get("mesh")}

    # ------------------------------------------------------------------ слайсер
    @app.get("/api/slicer/profiles")
    async def slicer_profiles(vendor: str = "") -> dict[str, Any]:
        s = db.get_settings()
        return await asyncio.to_thread(slicermod.list_profiles, s.get("slicer_path", ""),
                                       s.get("slicer_profiles_dir", ""), vendor)

    @app.post("/api/library/{lid}/slice")
    async def slice_item(lid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        it = db.get_library_item(lid)
        if not it:
            raise HTTPException(404, "Файл не найден")
        s = db.get_settings()
        try:
            out_path, log_text = await slicermod.slice_with_orca(
                s.get("slicer_path", ""), it["stored_path"], machine=body.get("machine", ""),
                process=body.get("process", ""), filaments=body.get("filaments") or [],
                work_dir=str(data / "slicing"), arrange=bool(body.get("arrange", True)),
                orient=bool(body.get("orient", False)), profiles_dir=s.get("slicer_profiles_dir", ""),
                bed_type=body.get("bed_type", ""), plate=int(body.get("plate") or 0),
                process_overrides=slicermod.support_overrides(
                    body.get("supports") or "on", 0.0, body.get("support_type") or "tree",
                    bool(body.get("support_plate_only")))[0] if body.get("supports") else None)
        except Exception as e:  # noqa: BLE001
            raise err(e) from e
        stem = Path(it["filename"]).stem
        new = store_upload(out_path, f"{stem}.gcode.3mf", mgr.library_dir)
        analysis, thumb = await asyncio.to_thread(analyze_path, new["stored_path"], new["filename"])
        new.update(kind=analysis["kind"], technology="fdm", analysis=analysis, thumbnail=thumb)
        db.add_library_item(new)
        await mgr.broadcast({"type": "library_changed"})
        return {"item_id": new["id"], "summary": summarize_analysis(analysis), "log": log_text[-4000:]}

    @app.post("/api/library/{lid}/prepare")
    async def prepare_item(lid: str, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Одна кнопка: ремонт → ориентация → размещение → нарезка под выбранный принтер."""
        it, m = await asyncio.to_thread(load_item_mesh, lid)
        cfg = db.get_printer(body.get("printer_id", "")) if body.get("printer_id") else None
        bv = cfg.build_volume if cfg and any(cfg.build_volume) else None
        o = body.get("options") or {}
        popts = meshmod.PrepareOptions(**{k: v for k, v in o.items() if k in meshmod.PrepareOptions.__dataclass_fields__})
        try:
            m2, steps, rep = await asyncio.to_thread(meshmod.auto_prepare, m, bv, popts)
        except Exception as e:  # noqa: BLE001
            raise err(e) from e
        new = await asyncio.to_thread(save_mesh_version, it, m2, "_ready", False, bv)
        tech = cfg.technology if cfg else "fdm"
        if tech == "resin":
            dens = {"light": "лёгкие", "medium": "средние", "heavy": "тяжёлые"}.get(o.get("resin_supports") or "medium", "средние")
            steps.append({"step": "Поддержки", "ok": True, "detail": f"для смолы нужны всегда — плотность: {dens}"})
            sup_ov = {}
        else:
            sup_ov, sup_text = slicermod.support_overrides(
                o.get("supports") or "auto", rep.overhang_area_cm2, o.get("support_type") or "tree",
                bool(o.get("support_plate_only")), o.get("support_angle"))
            steps.append({"step": "Поддержки", "ok": True, "detail": sup_text})
        result: dict[str, Any] = {"steps": steps, "mesh_item_id": new["id"], "report": rep.to_dict(),
                                  "printable": rep.printable, "sliced_item_id": None, "summary": None}
        await mgr.broadcast({"type": "library_changed"})
        if not rep.printable or o.get("slice") is False:
            return result
        s = db.get_settings()
        model = (cfg.model if cfg else "") or ""
        try:
            if tech == "resin":
                uv = slicermod.find_uvtools(s.get("uvtools_path", ""))
                ps = slicermod.find_prusaslicer(s.get("prusaslicer_path", ""))
                if not (uv and ps):
                    steps.append({"step": "Нарезка", "ok": False, "detail": "для смолы установите PrusaSlicer и UVtools "
                                  "(или нарежьте модель в Chitubox / Photon Workshop)"})
                    return result
                prof = await asyncio.to_thread(slicermod.auto_resin_profiles, uv, s.get("resin_profiles_dir", ""), model,
                                               o.get("resin_supports") or "medium")
                if o.get("resin_printer"):
                    prof = {"printer": {"path": o["resin_printer"], "name": Path(o["resin_printer"]).stem},
                            "print": {"path": o.get("resin_print") or (prof.get("print") or {}).get("path", ""), "name": ""}}
                if prof.get("error"):
                    steps.append({"step": "Нарезка", "ok": False, "detail": prof["error"]})
                    return result
                out, _log = await slicermod.slice_resin(ps, uv, new["stored_path"], printer_ini=prof["printer"]["path"],
                                                        print_ini=prof["print"]["path"], work_dir=str(data / "slicing"),
                                                        exposure_s=o.get("exposure_s"), bottom_exposure_s=o.get("bottom_exposure_s"),
                                                        bottom_layers=o.get("bottom_layers"))
                fname = Path(out).name
            else:
                if not slicermod.find_slicers(s.get("slicer_path", "")):
                    steps.append({"step": "Нарезка", "ok": False, "detail": "слайсер не найден — установите OrcaSlicer "
                                  "(или нарежьте модель в своём слайсере и загрузите G-code / 3MF)"})
                    return result
                prof = await asyncio.to_thread(slicermod.auto_profiles, s.get("slicer_path", ""),
                                               s.get("slicer_profiles_dir", ""), model, o.get("material") or "PLA")
                machine = o.get("machine") or (prof.get("machine") or {}).get("path")
                process = o.get("process") or (prof.get("process") or {}).get("path")
                filament = o.get("filament") or (prof.get("filament") or {}).get("path")
                if not (machine and process):
                    steps.append({"step": "Нарезка", "ok": False, "detail": prof.get("error") or "профили не выбраны"})
                    return result
                out, _log = await slicermod.slice_with_orca(
                    s.get("slicer_path", ""), new["stored_path"], machine=machine, process=process,
                    filaments=[filament] if filament else [], work_dir=str(data / "slicing"), arrange=False,
                    orient=False, profiles_dir=s.get("slicer_profiles_dir", ""),
                    bed_type=o.get("bed_type") if o.get("bed_type") is not None else prof.get("bed_type", ""),
                    process_overrides=sup_ov)
                fname = f"{Path(it['filename']).stem}.gcode.3mf"
                steps.append({"step": "Профили", "ok": True, "detail": " · ".join(
                    Path(p).stem for p in (machine, process, filament) if p)})
        except Exception as e:  # noqa: BLE001
            steps.append({"step": "Нарезка", "ok": False, "detail": str(e)[:1500]})
            return result
        sliced = store_upload(out, fname, mgr.library_dir)
        analysis, thumb = await asyncio.to_thread(analyze_path, sliced["stored_path"], sliced["filename"])
        sliced.update(kind=analysis["kind"], technology=tech, analysis=analysis, thumbnail=thumb)
        db.add_library_item(sliced)
        summ = summarize_analysis(analysis)
        steps.append({"step": "Нарезка", "ok": True, "detail": "файл для печати готов"})
        result.update(sliced_item_id=sliced["id"], summary=summ)
        await mgr.broadcast({"type": "library_changed"})
        return result

    @app.get("/api/slicer/resin-profiles")
    async def resin_profiles() -> dict[str, Any]:
        s = db.get_settings()
        return await asyncio.to_thread(slicermod.resin_profiles,
                                       slicermod.find_uvtools(s.get("uvtools_path", "")), s.get("resin_profiles_dir", ""))

    @app.post("/api/library/{lid}/slice-resin")
    async def slice_resin_item(lid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        it = db.get_library_item(lid)
        if not it:
            raise HTTPException(404, "Файл не найден")
        s = db.get_settings()
        try:
            out_path, log_text = await slicermod.slice_resin(
                s.get("prusaslicer_path", ""), s.get("uvtools_path", ""), it["stored_path"],
                printer_ini=body.get("printer", ""), print_ini=body.get("print", ""),
                work_dir=str(data / "slicing"), exposure_s=body.get("exposure_s"),
                bottom_exposure_s=body.get("bottom_exposure_s"), bottom_layers=body.get("bottom_layers"),
                target=body.get("target") or "auto")
        except Exception as e:  # noqa: BLE001
            raise err(e) from e
        new = store_upload(out_path, Path(out_path).name, mgr.library_dir)
        analysis, thumb = await asyncio.to_thread(analyze_path, new["stored_path"], new["filename"])
        new.update(kind=analysis["kind"], technology="resin", analysis=analysis, thumbnail=thumb)
        db.add_library_item(new)
        await mgr.broadcast({"type": "library_changed"})
        return {"item_id": new["id"], "summary": summarize_analysis(analysis), "log": log_text[-4000:]}

    # ------------------------------------------------------------------ расчёт стоимости
    @app.post("/api/estimate")
    async def estimate(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        settings = db.get_settings()
        material = db.get_material(int(body["material_id"])) if body.get("material_id") else None
        cfg = db.get_printer(body.get("printer_id", "")) if body.get("printer_id") else None
        tech = body.get("technology") or (cfg.technology if cfg else (material or {}).get("technology") or "fdm")
        result: dict[str, Any] = {"technology": tech}
        time_s = body.get("time_s")
        mat_g = body.get("material_g")
        mat_ml = body.get("material_ml")
        if body.get("library_id"):
            it = db.get_library_item(body["library_id"])
            if not it:
                raise HTTPException(404, "Файл не найден")
            if it["kind"] == "mesh":
                m = await asyncio.to_thread(meshmod.load_mesh, it["stored_path"],
                                            Path(it["filename"]).suffix.lstrip(".").lower())
                if tech == "resin":
                    rp = meshmod.ResinEstimateParams(**{k: v for k, v in (body.get("resin") or {}).items()
                                                        if k in meshmod.ResinEstimateParams.__dataclass_fields__})
                    if material and material.get("density"):
                        rp.density = float(material["density"])
                    est = meshmod.estimate_resin_from_mesh(m, rp)
                    mat_ml, mat_g = est["resin_ml"], est["resin_g"]
                else:
                    fp = meshmod.FdmEstimateParams(**{k: v for k, v in (body.get("fdm") or {}).items()
                                                      if k in meshmod.FdmEstimateParams.__dataclass_fields__})
                    if material and material.get("density"):
                        fp.density = float(material["density"])
                    est = meshmod.estimate_fdm_from_mesh(m, fp)
                    mat_g = est["filament_g"]
                time_s = est["time_s"]
                result["estimate"] = est
            else:
                summ = summarize_analysis(it["analysis"], int(body.get("plate") or 1))
                time_s = time_s or summ.get("time_s")
                mat_g = mat_g if mat_g is not None else summ.get("material_g")
                mat_ml = mat_ml if mat_ml is not None else summ.get("material_ml")
                result["estimate"] = summ
        if time_s is None:
            raise HTTPException(400, "Нет данных о времени печати")
        result["cost"] = calc_cost(technology=tech, time_s=float(time_s), material_g=mat_g, material_ml=mat_ml,
                                   material=material, settings=settings, power_w=cfg.power_w if cfg else 0,
                                   quantity=int(body.get("quantity") or 1))
        result["time_s"] = time_s
        return result

    # ------------------------------------------------------------------ материалы
    @app.get("/api/materials")
    async def materials() -> list[dict[str, Any]]:
        return db.list_materials()

    @app.post("/api/materials")
    async def save_material(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not body.get("name"):
            raise HTTPException(400, "Укажите название")
        return db.save_material(body)

    @app.delete("/api/materials/{mid}")
    async def delete_material(mid: int) -> dict[str, Any]:
        db.delete_material(mid)
        return {"ok": True}

    # ------------------------------------------------------------------ история и статистика
    @app.get("/api/jobs")
    async def jobs(limit: int = 200, printer_id: str = "") -> list[dict[str, Any]]:
        return db.list_jobs(limit, printer_id or None)

    @app.get("/api/stats")
    async def stats() -> dict[str, Any]:
        return db.stats()

    # ------------------------------------------------------------------ очередь
    @app.get("/api/queue")
    async def queue() -> list[dict[str, Any]]:
        items = db.list_queue(include_done=False)
        lib = {i["id"]: i for i in db.list_library()}
        for q in items:
            li = lib.get(q["library_id"])
            q["filename"] = li["filename"] if li else "(файл удалён)"
            q["summary"] = summarize_analysis(li["analysis"], q["plate"] or 1) if li else {}
        return items

    @app.post("/api/queue")
    async def queue_add(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        if not db.get_library_item(body.get("library_id", "")):
            raise HTTPException(404, "Файл не найден")
        qid = db.add_queue(body["library_id"], body.get("printer_id") or None, int(body.get("plate") or 1),
                           body.get("options") or {}, bool(body.get("auto_start")))
        await mgr.broadcast({"type": "queue_changed"})
        if body.get("printer_id"):
            await mgr.check_queue(body["printer_id"])
        return {"id": qid}

    @app.put("/api/queue/{qid}")
    async def queue_update(qid: int, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        allowed = {k: v for k, v in body.items() if k in ("printer_id", "plate", "options", "auto_start", "position", "status")}
        db.update_queue(qid, **allowed)
        await mgr.broadcast({"type": "queue_changed"})
        return {"ok": True}

    @app.delete("/api/queue/{qid}")
    async def queue_delete(qid: int) -> dict[str, Any]:
        db.delete_queue(qid)
        await mgr.broadcast({"type": "queue_changed"})
        return {"ok": True}

    @app.post("/api/queue/{qid}/start")
    async def queue_start(qid: int, body: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        try:
            tid = await mgr.start_queue_item(qid, body.get("printer_id"))
            return {"transfer_id": tid}
        except Exception as e:  # noqa: BLE001
            db.update_queue(qid, status="queued")
            raise err(e) from e

    # ------------------------------------------------------------------ обновления
    @app.get("/api/update")
    async def update_state() -> dict[str, Any]:
        return dict(updater.state)

    @app.post("/api/update/check")
    async def update_check() -> dict[str, Any]:
        return dict(await updater.check())

    @app.post("/api/update/install")
    async def update_install() -> dict[str, Any]:
        if updater.state.get("status") not in ("available", "ready", "waiting", "error"):
            await updater.check()
        if updater.state.get("status") == "up_to_date":
            return dict(updater.state)
        if not updater.state.get("installed"):
            raise HTTPException(400, "Автоустановка работает в версии, установленной через PrintHub-Setup.exe")
        try:
            await updater.download()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, str(e)) from e
        asyncio.create_task(updater.install_when_safe())
        return dict(updater.state)

    # ------------------------------------------------------------------ настройки
    @app.get("/api/settings")
    async def get_settings() -> dict[str, Any]:
        s = db.get_settings()
        for k in ("telegram_token", "auth_password"):
            if s.get(k):
                s[k] = "••••"
        return s

    @app.put("/api/settings")
    async def put_settings(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        body = {k: v for k, v in body.items() if v != "••••"}
        db.update_settings(body)
        return await get_settings()

    @app.post("/api/settings/test-telegram")
    async def test_telegram() -> dict[str, Any]:
        s = db.get_settings()
        ok = await send_telegram(s.get("telegram_token", ""), s.get("telegram_chat_id", ""),
                                 "🖨 PrintHub: тестовое уведомление")
        return {"ok": ok}

    # ------------------------------------------------------------------ события
    @app.websocket("/api/ws")
    async def ws(websocket: WebSocket) -> None:
        pwd = db.get_settings().get("auth_password") or ""
        if pwd and not secrets.compare_digest(websocket.cookies.get("ph_session", ""), session_token(pwd)):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        q = mgr.subscribe()
        try:
            await websocket.send_text(json.dumps({"type": "hello", "printers": mgr.all_snapshots()},
                                                 ensure_ascii=False, default=str))
            while True:
                msg = await q.get()
                await websocket.send_text(json.dumps(msg, ensure_ascii=False, default=str))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            mgr.unsubscribe(q)

    # ------------------------------------------------------------------ статика
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
