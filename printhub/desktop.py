"""Режим настольного приложения (установленная версия для Windows).

- сервер работает в фоне, в трее — значок с меню;
- повторный запуск не создаёт второй сервер, а открывает браузер;
- `--shutdown` останавливает запущенный экземпляр (нужно установщику перед обновлением);
- журнал пишется в %LOCALAPPDATA%\\PrintHub\\data\\printhub.log.
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Optional

APP_NAME = "PrintHub"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def data_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    d = Path(base) / APP_NAME / "data" if base else Path.home() / ".printhub"
    d.mkdir(parents=True, exist_ok=True)
    return d


def install_dir() -> Path:
    return Path(__file__).resolve().parents[2]


def message_box(text: str, title: str = APP_NAME, error: bool = True) -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)
            return
        except Exception:  # noqa: BLE001
            pass
    print(text, file=sys.stderr)


def setup_logging(d: Path) -> None:
    handler = logging.handlers.RotatingFileHandler(d / "printhub.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    for noisy in ("httpx", "websockets", "paho", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if sys.stderr is None or sys.stdout is None:  # pythonw: потоков вывода нет
        devnull = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or devnull
        sys.stderr = sys.stderr or devnull


# ---------------------------------------------------------------- один экземпляр
def instance_file(d: Path) -> Path:
    return d / "instance.json"


def read_instance(d: Path) -> Optional[dict[str, Any]]:
    try:
        return json.loads(instance_file(d).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def server_alive(port: int) -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/info", timeout=2) as r:
            return r.status in (200, 401)
    except Exception as e:  # noqa: BLE001
        return "401" in str(e)


def shutdown_running(d: Path, timeout: float = 20) -> bool:
    inst = read_instance(d)
    if not inst:
        return True
    pid = int(inst.get("pid") or 0)
    if not pid_alive(pid):
        return True
    try:
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{inst.get('port')}/api/system/shutdown", method="POST",
                                     headers={"X-Shutdown-Token": str(inst.get("token", ""))})
        urllib.request.urlopen(req, timeout=3).close()
    except Exception:  # noqa: BLE001
        pass
    end = time.time() + timeout
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.3)
    try:  # не завершился сам — завершаем принудительно
        if sys.platform == "win32":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
            if h:
                ctypes.windll.kernel32.TerminateProcess(h, 1)
                ctypes.windll.kernel32.CloseHandle(h)
        else:
            os.kill(pid, 9)
    except Exception:  # noqa: BLE001
        pass
    time.sleep(1)
    return not pid_alive(pid)


def pick_port(preferred: int) -> int:
    for port in list(range(preferred, preferred + 20)):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("0.0.0.0", port))
            return port
        except OSError:
            continue
        finally:
            s.close()
    raise RuntimeError(f"Порты {preferred}–{preferred + 19} заняты")


# ---------------------------------------------------------------- автозапуск
def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP_NAME)
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg
    exe = install_dir() / "PrintHub.exe"
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP_NAME, 0, winreg.REG_SZ, f'"{exe}" --no-browser')
        else:
            try:
                winreg.DeleteValue(k, APP_NAME)
            except OSError:
                pass


# ---------------------------------------------------------------- трей
def run_tray(url: str, lan_url: str, server: Any, app: Any, stop_event: threading.Event) -> bool:
    try:
        import pystray
        from PIL import Image, ImageDraw
    except Exception as e:  # noqa: BLE001
        logging.getLogger("printhub.desktop").warning("значок в трее недоступен: %s", e)
        return False

    ico = install_dir() / "printhub.ico"
    try:
        image = Image.open(ico) if ico.exists() else None
    except Exception:  # noqa: BLE001
        image = None
    if image is None:
        image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        dr = ImageDraw.Draw(image)
        dr.rounded_rectangle((4, 4, 60, 60), 12, fill=(47, 111, 237))
        dr.line((18, 44, 46, 44), fill="white", width=5)

    def open_ui(icon: Any = None, item: Any = None) -> None:
        webbrowser.open(url)

    def open_lan(icon: Any = None, item: Any = None) -> None:
        webbrowser.open(lan_url)

    def check_updates(icon: Any, item: Any) -> None:
        import asyncio
        upd = app.state.updater
        loop = getattr(server, "_printhub_loop", None)
        if loop is None:
            return
        fut = asyncio.run_coroutine_threadsafe(upd.check(), loop)
        try:
            st = fut.result(30)
        except Exception as e:  # noqa: BLE001
            icon.notify(f"Ошибка проверки: {e}", APP_NAME)
            return
        if st.get("status") == "available":
            icon.notify(f"Доступна версия {st.get('latest')}. Устанавливаю…", APP_NAME)
            asyncio.run_coroutine_threadsafe(_download_install(upd), loop)
        elif st.get("status") == "up_to_date":
            icon.notify(f"Установлена последняя версия ({st.get('current')})", APP_NAME)
        else:
            icon.notify(st.get("error") or "Проверка не удалась", APP_NAME)

    def toggle_autostart(icon: Any, item: Any) -> None:
        set_autostart(not autostart_enabled())

    def quit_app(icon: Any, item: Any) -> None:
        stop_event.set()
        server.should_exit = True
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Открыть PrintHub", open_ui, default=True),
        pystray.MenuItem(f"Для телефона: {lan_url}", open_lan),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Проверить обновления", check_updates),
        pystray.MenuItem("Запускать вместе с Windows", toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Выход", quit_app),
    )
    icon = pystray.Icon(APP_NAME, image, f"{APP_NAME} — {url}", menu)

    def watcher() -> None:
        stop_event.wait()
        try:
            icon.stop()
        except Exception:  # noqa: BLE001
            pass
    threading.Thread(target=watcher, daemon=True).start()
    try:
        icon.run()
    except Exception:  # noqa: BLE001
        logging.getLogger("printhub.desktop").exception("значок в трее завершился с ошибкой")
    return True


async def _download_install(upd: Any) -> None:
    await upd.download()
    await upd.install_when_safe()


# ---------------------------------------------------------------- main
def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="PrintHub")
    ap.add_argument("--shutdown", action="store_true", help="остановить запущенный PrintHub")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--no-tray", action="store_true")
    ap.add_argument("--port", type=int, default=0, help="порт (по умолчанию — прошлый или 8080)")
    ap.add_argument("--data", default="")
    args = ap.parse_args(argv)

    d = Path(args.data) if args.data else data_dir()
    d.mkdir(parents=True, exist_ok=True)
    setup_logging(d)
    log = logging.getLogger("printhub.desktop")

    if args.shutdown:
        ok = shutdown_running(d)
        log.info("остановка по запросу установщика: %s", "успешно" if ok else "не удалось")
        return 0 if ok else 1

    inst = read_instance(d)
    if inst and pid_alive(int(inst.get("pid") or 0)) and server_alive(int(inst.get("port") or 0)):
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{inst['port']}/")
        return 0

    import asyncio
    import secrets

    import uvicorn

    from . import __version__
    from .app import create_app
    from .discovery import local_ipv4

    last_port_file = d / "last_port.txt"
    preferred = args.port
    if not preferred:
        try:
            preferred = int(last_port_file.read_text().strip())
        except (OSError, ValueError):
            preferred = 8080
    try:
        port = pick_port(preferred)
        last_port_file.write_text(str(port))
        app = create_app(str(d))
    except Exception as e:  # noqa: BLE001
        log.exception("ошибка запуска")
        message_box(f"PrintHub не запустился:\n{e}\n\nПодробности: {d / 'printhub.log'}")
        return 1

    token = secrets.token_hex(16)
    stop_event = threading.Event()
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_config=None, log_level="warning")
    server = uvicorn.Server(config)

    def request_shutdown() -> None:
        stop_event.set()
        server.should_exit = True
    app.state.shutdown_token = token
    app.state.request_shutdown = request_shutdown
    app.state.updater.request_shutdown = request_shutdown

    def serve() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server._printhub_loop = loop  # type: ignore[attr-defined]
        try:
            loop.run_until_complete(server.serve())
        except Exception:  # noqa: BLE001
            log.exception("сервер остановился с ошибкой")
        finally:
            stop_event.set()

    th = threading.Thread(target=serve, name="printhub-server", daemon=True)
    th.start()
    for _ in range(300):
        if server.started or not th.is_alive():
            break
        time.sleep(0.1)
    if not server.started:
        message_box(f"PrintHub не запустился. Подробности: {d / 'printhub.log'}")
        return 1

    instance_file(d).write_text(json.dumps({"pid": os.getpid(), "port": port, "token": token,
                                            "version": __version__}), encoding="utf-8")
    url = f"http://127.0.0.1:{port}/"
    lan_url = f"http://{local_ipv4() or '127.0.0.1'}:{port}/"
    log.info("PrintHub %s запущен: %s (в сети: %s)", __version__, url, lan_url)
    if not args.no_browser:
        webbrowser.open(url)

    try:
        if not args.no_tray:
            run_tray(url, lan_url, server, app, stop_event)
        # сервер работает, пока не выбран «Выход» (или установщик не попросил остановиться),
        # даже если значок в трее создать не удалось
        while not stop_event.is_set():
            stop_event.wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.should_exit = True
        th.join(timeout=15)
        try:
            if (read_instance(d) or {}).get("pid") == os.getpid():
                instance_file(d).unlink()
        except OSError:
            pass
        log.info("PrintHub остановлен")
    return 0


if __name__ == "__main__":
    code = main()
    # Завершаем процесс принудительно: фоновые потоки (значок в трее, пул потоков asyncio)
    # не должны держать процесс живым — иначе файлы программы остаются занятыми и
    # установщик обновления не сможет их заменить.
    logging.shutdown()
    os._exit(code)
