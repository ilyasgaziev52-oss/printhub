"""Запуск: python -m printhub [--host 0.0.0.0] [--port 8080] [--data ./data]"""
from __future__ import annotations

import argparse
import logging
import os
import webbrowser
import threading


def main() -> None:
    ap = argparse.ArgumentParser(prog="printhub", description="PrintHub — управление 3D-принтерами в локальной сети")
    ap.add_argument("--host", default=os.environ.get("PRINTHUB_HOST", "0.0.0.0"),
                    help="адрес для веб-интерфейса (0.0.0.0 — доступен со всех устройств в сети)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PRINTHUB_PORT", "8080")))
    ap.add_argument("--data", default=os.environ.get("PRINTHUB_DATA", "data"), help="папка для базы и файлов")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    import uvicorn

    from .app import create_app
    from .discovery import local_ipv4

    app = create_app(args.data)
    ip = local_ipv4() or "127.0.0.1"
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"\n  PrintHub запущен: {url}")
    if args.host in ("0.0.0.0", "::"):
        print(f"  С телефона/других ПК в сети: http://{ip}:{args.port}\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
