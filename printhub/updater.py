"""Автообновление: проверка новой версии, загрузка установщика, проверка SHA-256, тихая установка.

Источник обновлений — релизы GitHub (последний релиз, файл PrintHub-Setup-*.exe) или
собственный JSON-манифест: {"version": "0.3.0", "url": "https://…/PrintHub-Setup-0.3.0.exe",
"sha256": "…", "notes": "…"}.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import __version__
from . import build_info

log = logging.getLogger("printhub.updater")

# установщик по-прежнему называется PrintHub-Setup-*.exe (так его находят уже установленные копии);
# на будущее понимаем и SEBE3D-Setup-*.exe
ASSET_RE = re.compile(r"^(PrintHub|SEBE[-_ ]?3D)[-_]Setup[-_].*\.exe$", re.I)
CHECK_INTERVAL = 3600            # фоновая проверка раз в час
KICK_MIN_AGE = 600               # при открытии программы — не чаще раза в 10 минут
FIRST_CHECK_DELAY = 60


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", (v or "").lstrip("vV").split("-")[0])
    return tuple(int(n) for n in nums[:4]) or (0,)


def install_root() -> Optional[Path]:
    """Папка установки, если PrintHub установлен установщиком (рядом лежит installed.json)."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "installed.json").is_file() else None


class Updater:
    def __init__(self, data_dir: Path, get_settings: Callable[[], dict[str, Any]],
                 is_busy: Callable[[], bool], broadcast: Optional[Callable[[dict[str, Any]], Any]] = None):
        self.dir = Path(data_dir) / "updates"
        self.get_settings = get_settings
        self.is_busy = is_busy
        self.broadcast = broadcast
        self.request_shutdown: Optional[Callable[[], None]] = None  # задаёт desktop.py
        self.state: dict[str, Any] = {
            "current": __version__, "latest": None, "status": "idle", "notes": "", "error": "",
            "progress": 0.0, "last_check": None, "installed": install_root() is not None,
            "source": self.source_description(),
        }
        self._task: Optional[asyncio.Task] = None
        self._kick: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._asset: dict[str, Any] = {}

    # ------------------------------------------------------------------ источник
    def source(self) -> tuple[str, str]:
        s = self.get_settings()
        url = (s.get("update_url") or os.environ.get("PRINTHUB_UPDATE_URL") or build_info.UPDATE_URL or "").strip()
        if url:
            return "manifest", url
        repo = (s.get("update_repo") or os.environ.get("PRINTHUB_UPDATE_REPO") or build_info.UPDATE_REPO or "").strip()
        if repo:
            return "github", repo
        return "", ""

    def source_description(self) -> str:
        kind, val = self.source()
        if kind == "github":
            return f"GitHub: {val}"
        if kind == "manifest":
            return val
        return "не настроен"

    # ------------------------------------------------------------------ состояние
    async def _set(self, **kw: Any) -> None:
        self.state.update(kw)
        if self.broadcast:
            r = self.broadcast({"type": "update", "update": dict(self.state)})
            if asyncio.iscoroutine(r):
                await r

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def check_and_apply(self) -> None:
        """Проверить и, если включено автообновление, скачать и установить (когда принтеры не печатают)."""
        try:
            await self.check()
            if self.state["status"] == "available" and self.auto_enabled() and self.state["installed"]:
                await self.download()
                await self.install_when_safe()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.warning("обновление: %s", e)

    def kick(self, min_age: float = KICK_MIN_AGE) -> None:
        """Проверка при открытии программы в браузере: если последняя была давно — проверить сейчас (в фоне)."""
        busy = self.state.get("status") in ("checking", "downloading", "ready", "waiting", "installing")
        if busy or (self._kick and not self._kick.done()):
            return
        if time.time() - (self.state.get("last_check") or 0) < min_age:
            return
        self._kick = asyncio.create_task(self.check_and_apply())

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_CHECK_DELAY)
        while True:
            if not (self._kick and not self._kick.done()):
                await self.check_and_apply()
            await asyncio.sleep(CHECK_INTERVAL)

    def auto_enabled(self) -> bool:
        return bool(self.get_settings().get("auto_update", True))

    # ------------------------------------------------------------------ проверка
    async def check(self) -> dict[str, Any]:
        async with self._lock:
            kind, val = self.source()
            self.state["source"] = self.source_description()
            if not kind:
                await self._set(status="disabled", error="Источник обновлений не настроен")
                return self.state
            await self._set(status="checking", error="")
            try:
                async with httpx.AsyncClient(timeout=20, follow_redirects=True,
                                             headers={"User-Agent": f"PrintHub/{__version__}"}) as c:
                    if kind == "github":
                        r = await c.get(f"https://api.github.com/repos/{val}/releases/latest",
                                        headers={"Accept": "application/vnd.github+json"})
                        if r.status_code == 404:
                            raise RuntimeError("В репозитории пока нет релизов")
                        r.raise_for_status()
                        rel = r.json()
                        assets = rel.get("assets") or []
                        exe = next((a for a in assets if ASSET_RE.match(a.get("name", ""))), None)
                        if not exe:
                            raise RuntimeError("В последнем релизе нет файла PrintHub-Setup-*.exe")
                        sha = ""
                        digest = str(exe.get("digest") or "")
                        if digest.startswith("sha256:"):
                            sha = digest.split(":", 1)[1]
                        else:
                            side = next((a for a in assets if a.get("name") == exe["name"] + ".sha256"), None)
                            if side:
                                t = await c.get(side["browser_download_url"])
                                t.raise_for_status()
                                sha = t.text.split()[0]
                        info = {"version": str(rel.get("tag_name", "")).lstrip("vV"), "url": exe["browser_download_url"],
                                "sha256": sha, "size": exe.get("size"), "notes": rel.get("body") or "",
                                "name": exe["name"]}
                    else:
                        r = await c.get(val)
                        r.raise_for_status()
                        m = r.json()
                        info = {"version": str(m.get("version", "")).lstrip("vV"), "url": m["url"],
                                "sha256": m.get("sha256", ""), "size": m.get("size"), "notes": m.get("notes", ""),
                                "name": os.path.basename(str(m["url"]).split("?")[0]) or "PrintHub-Setup.exe"}
            except Exception as e:  # noqa: BLE001
                await self._set(status="error", error=f"Не удалось проверить обновления: {e}", last_check=time.time())
                return self.state
            if not info["sha256"] or not re.fullmatch(r"[0-9a-fA-F]{64}", info["sha256"]):
                await self._set(status="error", error="У обновления нет контрольной суммы SHA-256 — установка запрещена",
                                last_check=time.time(), latest=info["version"])
                return self.state
            self._asset = info
            newer = parse_version(info["version"]) > parse_version(__version__)
            await self._set(latest=info["version"], notes=info["notes"][:4000], last_check=time.time(),
                            status="available" if newer else "up_to_date")
            return self.state

    # ------------------------------------------------------------------ загрузка
    def _target(self) -> Path:
        name = re.sub(r"[^\w.\-]", "_", self._asset.get("name") or f"PrintHub-Setup-{self._asset.get('version')}.exe")
        return self.dir / name

    async def download(self) -> Path:
        if not self._asset:
            raise RuntimeError("Сначала проверьте обновления")
        self.dir.mkdir(parents=True, exist_ok=True)
        target = self._target()
        want = self._asset["sha256"].lower()
        if target.exists() and _sha256(target) == want:
            await self._set(status="ready", progress=100.0)
            return target
        part = target.with_suffix(target.suffix + ".part")
        await self._set(status="downloading", progress=0.0, error="")
        h = hashlib.sha256()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30, read=120), follow_redirects=True,
                                         headers={"User-Agent": f"PrintHub/{__version__}"}) as c:
                async with c.stream("GET", self._asset["url"]) as r:
                    r.raise_for_status()
                    total = int(r.headers.get("content-length") or self._asset.get("size") or 0)
                    got = 0
                    last = 0.0
                    with open(part, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            f.write(chunk)
                            h.update(chunk)
                            got += len(chunk)
                            if total and time.monotonic() - last > 0.5:
                                last = time.monotonic()
                                await self._set(progress=round(100 * got / total, 1))
        except Exception as e:  # noqa: BLE001
            await self._set(status="error", error=f"Ошибка загрузки обновления: {e}")
            raise
        if h.hexdigest() != want:
            part.unlink(missing_ok=True)
            await self._set(status="error", error="Контрольная сумма обновления не совпала — файл удалён")
            raise RuntimeError("SHA-256 не совпадает")
        os.replace(part, target)
        for old in self.dir.glob("PrintHub*Setup*.exe"):
            if old != target:
                old.unlink(missing_ok=True)
        await self._set(status="ready", progress=100.0)
        return target

    # ------------------------------------------------------------------ установка
    async def install_when_safe(self, max_wait: float = 3600) -> None:
        deadline = time.monotonic() + max_wait
        while self.is_busy() and time.monotonic() < deadline:
            await self._set(status="waiting", error="Ждём окончания передачи файлов на принтеры")
            await asyncio.sleep(15)
        await self.install()

    async def install(self) -> None:
        root = install_root()
        if root is None or sys.platform != "win32":
            await self._set(error="Автоустановка работает только в установленной версии для Windows")
            raise RuntimeError("не установленная версия")
        if self.request_shutdown is None:
            raise RuntimeError("перезапуск недоступен в этом режиме")
        target = self._target()
        if not target.exists() or _sha256(target) != self._asset.get("sha256", "").lower():
            target = await self.download()
        await self._set(status="installing", error="")
        log.info("запуск установщика %s", target)
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([str(target), "/S", "/UPDATE"], close_fds=True, creationflags=flags)
        await asyncio.sleep(1.0)
        self.request_shutdown()


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(installer: Path, version: str, url: str, notes: str = "") -> dict[str, Any]:
    """Утилита для сборки: манифест для собственного сервера обновлений."""
    return {"version": version, "url": url, "sha256": _sha256(installer), "size": installer.stat().st_size,
            "notes": notes}


if __name__ == "__main__":  # python -m printhub.updater <installer.exe> <version> <url>
    print(json.dumps(write_manifest(Path(sys.argv[1]), sys.argv[2], sys.argv[3]), ensure_ascii=False, indent=1))
