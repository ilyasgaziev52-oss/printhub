import hashlib
import json

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from printhub import __version__
from printhub.emulators import free_port, serve_app
from printhub.updater import Updater, parse_version


def test_parse_version():
    assert parse_version("v0.2.10") > parse_version("0.2.9")
    assert parse_version("1.0.0") > parse_version("0.99.99")
    assert parse_version("0.2.0") == parse_version("0.2.0")


async def run_updater(tmp_path, manifest_extra=None, payload=b"MZ fake installer", github=False):
    port = free_port()
    sha = hashlib.sha256(payload).hexdigest()

    async def manifest(r):
        m = {"version": "99.0.0", "url": f"http://127.0.0.1:{port}/PrintHub-Setup-99.0.0.exe", "sha256": sha,
             "notes": "Новое"}
        m.update(manifest_extra or {})
        return JSONResponse(m)

    async def release(r):
        return JSONResponse({"tag_name": "v99.0.0", "body": "Новое", "assets": [
            {"name": "PrintHub-Setup-99.0.0.exe", "size": len(payload), "digest": f"sha256:{sha}",
             "browser_download_url": f"http://127.0.0.1:{port}/PrintHub-Setup-99.0.0.exe"}]})

    async def exe(r):
        return Response(payload, media_type="application/octet-stream")

    app = Starlette(routes=[Route("/manifest.json", manifest), Route("/PrintHub-Setup-99.0.0.exe", exe),
                            Route("/repos/u/p/releases/latest", release)])
    server, task = await serve_app(app, port)
    try:
        settings = {"update_url": f"http://127.0.0.1:{port}/manifest.json"}
        upd = Updater(tmp_path, lambda: settings, is_busy=lambda: False)
        st = await upd.check()
        result = dict(st)
        if st["status"] == "available":
            try:
                path = await upd.download()
                result["file"] = path.read_bytes()
            except RuntimeError:
                pass
            result["status_after"] = upd.state["status"]
        return result
    finally:
        server.should_exit = True
        await task


async def test_manifest_update(tmp_path):
    r = await run_updater(tmp_path)
    assert r["latest"] == "99.0.0" and r["current"] == __version__
    assert r["file"] == b"MZ fake installer" and r["status_after"] == "ready"


async def test_bad_checksum_rejected(tmp_path):
    r = await run_updater(tmp_path, manifest_extra={"sha256": "0" * 64})
    assert r["status_after"] == "error" and "file" not in r
    assert not list((tmp_path / "updates").glob("*.exe"))


async def test_missing_checksum_refused(tmp_path):
    r = await run_updater(tmp_path, manifest_extra={"sha256": ""})
    assert r["status"] == "error" and "SHA-256" in r["error"]


async def test_disabled_without_source(tmp_path, monkeypatch):
    from printhub import build_info
    monkeypatch.setattr(build_info, "UPDATE_REPO", "")
    monkeypatch.setattr(build_info, "UPDATE_URL", "")
    monkeypatch.delenv("PRINTHUB_UPDATE_REPO", raising=False)
    monkeypatch.delenv("PRINTHUB_UPDATE_URL", raising=False)
    upd = Updater(tmp_path, lambda: {}, is_busy=lambda: False)
    st = await upd.check()
    assert st["status"] == "disabled" and not st["installed"]


async def test_kick_checks_on_open(tmp_path, monkeypatch):
    """Открытие программы запускает проверку, если последняя была давно, и не дёргает сервер повторно."""
    import asyncio
    import time

    calls = []
    up = Updater(tmp_path, lambda: {"auto_update": False}, lambda: False)

    async def fake_check():
        calls.append(1)
        up.state.update(status="up_to_date", last_check=time.time())
        return up.state
    monkeypatch.setattr(up, "check", fake_check)
    up.kick()
    await asyncio.sleep(0.05)
    assert calls == [1]
    up.kick()                      # только что проверяли — второй раз не надо
    await asyncio.sleep(0.05)
    assert calls == [1]
    up.state["last_check"] = time.time() - 3600
    up.kick()
    await asyncio.sleep(0.05)
    assert calls == [1, 1]
