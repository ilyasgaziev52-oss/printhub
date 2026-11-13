"""Эмуляторы принтеров (Elegoo SDCP, Klipper/Moonraker) — для тестов и демо-режима (--demo).

В режиме simulate печать «идёт»: растут прогресс, слои и температуры, затем задание завершается.
"""
from __future__ import annotations

import asyncio
import json
import socket
import time
from typing import Any

import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def serve_app(app: Any, port: int) -> tuple[uvicorn.Server, asyncio.Task]:
    cfg = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="off")
    server = uvicorn.Server(cfg)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    return server, task


# ---------------------------------------------------------------- Elegoo SDCP (Centauri Carbon)
class SdcpPrinter:
    MBID = "2c9018510147017000002c0000000000"

    SIM_TOTAL = 900.0   # длительность «печати», с (в ускоренном времени)
    SIM_SPEED = 5.0     # во сколько раз быстрее реального времени

    def __init__(self, ws_port: int = 0, http_port: int = 0, simulate: bool = False) -> None:
        self.ws_port = ws_port or free_port()
        self.http_port = http_port or free_port()
        self.simulate = simulate
        self.received: list[dict[str, Any]] = []
        self.uploaded: dict[str, bytearray] = {}
        self.status: dict[str, Any] = {
            "CurrentStatus": [0], "TempOfHotbed": 25.0, "TempOfNozzle": 26.0, "TempOfBox": 24.0,
            "TempTargetHotbed": 0, "TempTargetNozzle": 0, "TempTargetBox": 0,
            "CurrentFanSpeed": {"ModelFan": 0, "AuxiliaryFan": 0, "BoxFan": 0},
            "LightStatus": {"SecondLight": 0, "RgbLight": [0, 0, 0]},
            "PrintInfo": {"Status": 0, "CurrentLayer": 0, "TotalLayer": 0, "CurrentTicks": 0, "TotalTicks": 0,
                          "Filename": "", "TaskId": "", "PrintSpeedPct": 100, "Progress": 0},
        }
        self.clients: set[Any] = set()

    def attrs(self) -> dict[str, Any]:
        return {"Attributes": {"Name": "Centauri Carbon", "MachineName": "Centauri Carbon", "BrandName": "ELEGOO",
                               "ProtocolVersion": "V3.0.0", "FirmwareVersion": "V1.1.29", "MainboardID": self.MBID,
                               "Capabilities": ["FILE_TRANSFER", "PRINT_CONTROL"],
                               "SupportFileType": ["gcode"]},
                "MainboardID": self.MBID, "TimeStamp": int(time.time()), "Topic": f"sdcp/attributes/{self.MBID}"}

    async def push_status(self) -> None:
        msg = json.dumps({"Status": self.status, "MainboardID": self.MBID, "TimeStamp": int(time.time()),
                          "Topic": f"sdcp/status/{self.MBID}"})
        for c in list(self.clients):
            try:
                await c.send(msg)
            except Exception:  # noqa: BLE001
                self.clients.discard(c)

    async def handler(self, ws: Any) -> None:
        self.clients.add(ws)
        try:
            async for raw in ws:
                if raw == "ping":
                    await ws.send("pong")
                    continue
                msg = json.loads(raw)
                d = msg["Data"]
                self.received.append(d)
                cmd = d["Cmd"]
                result: dict[str, Any] = {"Ack": 0}
                if cmd == 258:
                    result["FileList"] = [{"name": f"/local/{n}", "type": 1, "FileSize": len(b), "CreateTime": 1700000000}
                                          for n, b in self.uploaded.items()]
                elif cmd == 128:
                    self.status["CurrentStatus"] = [1]
                    if self.simulate:
                        self.status["PrintInfo"].update(Status=16, Filename=d["Data"]["Filename"], TotalLayer=150,
                                                        CurrentLayer=0, CurrentTicks=0.0, TotalTicks=self.SIM_TOTAL, Progress=0)
                    else:
                        self.status["PrintInfo"].update(Status=13, Filename=d["Data"]["Filename"], TotalLayer=100,
                                                        CurrentLayer=10, CurrentTicks=60.0, TotalTicks=600.0, Progress=10)
                elif cmd == 129:
                    self.status["PrintInfo"]["Status"] = 6
                elif cmd == 131:
                    self.status["PrintInfo"]["Status"] = 13
                elif cmd == 130:
                    self.status["CurrentStatus"] = [0]
                    self.status["PrintInfo"]["Status"] = 8
                elif cmd == 403 and "LightStatus" in d["Data"]:
                    self.status["LightStatus"] = d["Data"]["LightStatus"]
                await ws.send(json.dumps({"Id": "x", "Data": {"Cmd": cmd, "Data": result, "RequestID": d["RequestID"],
                                                               "MainboardID": self.MBID, "TimeStamp": 0},
                                          "Topic": f"sdcp/response/{self.MBID}"}))
                if cmd == 1:
                    await ws.send(json.dumps(self.attrs()))
                if cmd in (0, 128, 129, 130, 131, 403):
                    await self.push_status()
        finally:
            self.clients.discard(ws)

    async def upload(self, request: Request) -> JSONResponse:
        form = await request.form()
        f = form["File"]
        data = await f.read()  # type: ignore[union-attr]
        off = int(form["Offset"])  # type: ignore[arg-type]
        buf = self.uploaded.setdefault(f.filename, bytearray())  # type: ignore[union-attr]
        if off != len(buf):
            return JSONResponse({"code": "111111", "messages": [{"field": "common_field", "message": -2}], "success": False})
        buf.extend(data)
        return JSONResponse({"code": "000000", "messages": None, "data": {}, "success": True})

    async def _simulate(self) -> None:
        while True:
            await asyncio.sleep(1)
            st, pi = self.status, self.status["PrintInfo"]
            printing = st["CurrentStatus"] == [1] and pi["Status"] in (13, 16)
            target_n, target_b = (220.0, 60.0) if printing else (0.0, 0.0)
            st["TempTargetNozzle"], st["TempTargetHotbed"] = target_n, target_b
            st["TempOfNozzle"] = round(st["TempOfNozzle"] + ((target_n or 25) - st["TempOfNozzle"]) * 0.3, 1)
            st["TempOfHotbed"] = round(st["TempOfHotbed"] + ((target_b or 24) - st["TempOfHotbed"]) * 0.2, 1)
            if printing:
                if pi["Status"] == 16 and st["TempOfNozzle"] > 200:
                    pi["Status"] = 13
                if pi["Status"] == 13:
                    st["CurrentFanSpeed"]["ModelFan"] = 100
                    pi["CurrentTicks"] = min(pi["CurrentTicks"] + self.SIM_SPEED, pi["TotalTicks"])
                    frac = pi["CurrentTicks"] / pi["TotalTicks"]
                    pi["Progress"] = int(frac * 100)
                    pi["CurrentLayer"] = int(frac * pi["TotalLayer"])
                    if frac >= 1:
                        st["CurrentStatus"] = [0]
                        pi["Status"] = 9
                        st["CurrentFanSpeed"]["ModelFan"] = 0
            await self.push_status()

    async def start(self) -> None:
        self.ws_server = await websockets.serve(self.handler, "127.0.0.1", self.ws_port)
        app = Starlette(routes=[Route("/uploadFile/upload", self.upload, methods=["POST"])])
        self.http_server, self.http_task = await serve_app(app, self.http_port)
        self._sim = asyncio.create_task(self._simulate()) if self.simulate else None

    async def stop(self) -> None:
        if self._sim:
            self._sim.cancel()
        self.ws_server.close()
        self.http_server.should_exit = True
        await self.http_task


# ---------------------------------------------------------------- Moonraker
class MoonrakerPrinter:
    SIM_TOTAL = 1200.0
    SIM_SPEED = 6.0

    def __init__(self, port: int = 0, simulate: bool = False) -> None:
        self.port = port or free_port()
        self.simulate = simulate
        self.calls: list[str] = []
        self.files: dict[str, bytes] = {}
        self.state: dict[str, Any] = {
            "print_stats": {"state": "standby", "filename": "", "print_duration": 0, "info": {"current_layer": None, "total_layer": None}},
            "display_status": {"progress": 0},
            "virtual_sdcard": {"progress": 0},
            "extruder": {"temperature": 25.0, "target": 0},
            "heater_bed": {"temperature": 24.0, "target": 0},
            "fan": {"speed": 0},
            "gcode_move": {"speed_factor": 1.0},
            "webhooks": {"state": "ready"},
        }
        self.sockets: set[WebSocket] = set()

    async def notify(self, diff: dict[str, Any]) -> None:
        for k, v in diff.items():
            self.state.setdefault(k, {}).update(v)
        for ws in list(self.sockets):
            try:
                await ws.send_text(json.dumps({"jsonrpc": "2.0", "method": "notify_status_update", "params": [diff, 0]}))
            except Exception:  # noqa: BLE001
                self.sockets.discard(ws)

    def app(self) -> Starlette:
        async def server_info(r: Request) -> JSONResponse:
            return JSONResponse({"result": {"klippy_state": "ready", "moonraker_version": "v0.9.3"}})

        async def printer_info(r: Request) -> JSONResponse:
            return JSONResponse({"result": {"state": "ready", "hostname": "neptune4", "software_version": "v0.12.0"}})

        async def objects_list(r: Request) -> JSONResponse:
            return JSONResponse({"result": {"objects": list(self.state.keys())}})

        async def files_list(r: Request) -> JSONResponse:
            return JSONResponse({"result": [{"path": n, "size": len(b), "modified": 1700000000.0} for n, b in self.files.items()]})

        async def upload(r: Request) -> JSONResponse:
            form = await r.form()
            f = form["file"]
            self.files[f.filename] = await f.read()  # type: ignore[union-attr]
            return JSONResponse({"result": {"item": {"path": f.filename}}})  # type: ignore[union-attr]

        async def metadata(r: Request) -> JSONResponse:
            return JSONResponse({"result": {"estimated_time": self.SIM_TOTAL if self.simulate else 600}})

        async def webcams(r: Request) -> JSONResponse:
            return JSONResponse({"result": {"webcams": []}})

        async def start(r: Request) -> JSONResponse:
            self.calls.append("start:" + r.query_params["filename"])
            if self.simulate:
                await self.notify({"print_stats": {"state": "printing", "filename": r.query_params["filename"], "print_duration": 0,
                                                   "info": {"current_layer": 0, "total_layer": 200}},
                                   "display_status": {"progress": 0.0}})
            else:
                await self.notify({"print_stats": {"state": "printing", "filename": r.query_params["filename"], "print_duration": 30},
                                   "display_status": {"progress": 0.05}})
            return JSONResponse({"result": "ok"})

        async def pause(r: Request) -> JSONResponse:
            self.calls.append("pause")
            await self.notify({"print_stats": {"state": "paused"}})
            return JSONResponse({"result": "ok"})

        async def resume(r: Request) -> JSONResponse:
            self.calls.append("resume")
            await self.notify({"print_stats": {"state": "printing"}})
            return JSONResponse({"result": "ok"})

        async def cancel(r: Request) -> JSONResponse:
            self.calls.append("cancel")
            await self.notify({"print_stats": {"state": "cancelled"}})
            return JSONResponse({"result": "ok"})

        async def gcode(r: Request) -> JSONResponse:
            self.calls.append("gcode:" + r.query_params["script"])
            return JSONResponse({"result": "ok"})

        async def ws_endpoint(ws: WebSocket) -> None:
            await ws.accept()
            self.sockets.add(ws)
            try:
                while True:
                    msg = json.loads(await ws.receive_text())
                    if msg.get("method") == "printer.objects.subscribe":
                        objs = msg["params"]["objects"]
                        await ws.send_text(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {
                            "eventtime": 0, "status": {k: v for k, v in self.state.items() if k in objs}}}))
            except Exception:  # noqa: BLE001
                self.sockets.discard(ws)

        return Starlette(routes=[
            Route("/server/info", server_info), Route("/printer/info", printer_info),
            Route("/printer/objects/list", objects_list), Route("/server/files/list", files_list),
            Route("/server/files/upload", upload, methods=["POST"]), Route("/server/files/metadata", metadata),
            Route("/server/webcams/list", webcams),
            Route("/printer/print/start", start, methods=["POST"]), Route("/printer/print/pause", pause, methods=["POST"]),
            Route("/printer/print/resume", resume, methods=["POST"]), Route("/printer/print/cancel", cancel, methods=["POST"]),
            Route("/printer/gcode/script", gcode, methods=["POST"]), WebSocketRoute("/websocket", ws_endpoint),
        ])

    async def _simulate(self) -> None:
        while True:
            await asyncio.sleep(1)
            ps = self.state["print_stats"]
            printing = ps["state"] == "printing"
            ext, bed = self.state["extruder"], self.state["heater_bed"]
            ext["target"], bed["target"] = (210.0, 60.0) if printing else (0.0, 0.0)
            ext["temperature"] = round(ext["temperature"] + ((ext["target"] or 25) - ext["temperature"]) * 0.3, 1)
            bed["temperature"] = round(bed["temperature"] + ((bed["target"] or 24) - bed["temperature"]) * 0.2, 1)
            diff: dict[str, Any] = {"extruder": dict(ext), "heater_bed": dict(bed)}
            if printing and ext["temperature"] > 190:
                ps["print_duration"] = min(ps["print_duration"] + self.SIM_SPEED, self.SIM_TOTAL)
                frac = ps["print_duration"] / self.SIM_TOTAL
                ps["info"] = {"current_layer": int(frac * 200), "total_layer": 200}
                diff["print_stats"] = {"print_duration": ps["print_duration"], "info": ps["info"]}
                diff["display_status"] = {"progress": round(frac, 4)}
                diff["fan"] = {"speed": 1.0}
                if frac >= 1:
                    diff["print_stats"]["state"] = "complete"
                    diff["fan"] = {"speed": 0.0}
            await self.notify(diff)

    async def start(self) -> None:
        self.server, self.task = await serve_app(self.app(), self.port)
        self._sim = asyncio.create_task(self._simulate()) if self.simulate else None

    async def stop(self) -> None:
        if self._sim:
            self._sim.cancel()
        self.server.should_exit = True
        await self.task
