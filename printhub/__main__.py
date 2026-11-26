"""Запуск: python -m printhub [--host 0.0.0.0] [--port 8080] [--data ./data]"""
from __future__ import annotations

import argparse
import logging
import os
import webbrowser
import threading


def main() -> None:
    ap = argparse.ArgumentParser(prog="printhub", description="SEBE 3D — управление 3D-принтерами в локальной сети")
    ap.add_argument("--host", default=os.environ.get("PRINTHUB_HOST", "0.0.0.0"),
                    help="адрес для веб-интерфейса (0.0.0.0 — доступен со всех устройств в сети)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PRINTHUB_PORT", "8080")))
    ap.add_argument("--data", default=os.environ.get("PRINTHUB_DATA", "data"), help="папка для базы и файлов")
    ap.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    ap.add_argument("--demo", action="store_true", help="демо-режим: два эмулированных принтера и примеры файлов")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not args.debug:
        for noisy in ("httpx", "websockets", "paho"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
    import uvicorn

    from .app import create_app
    from .discovery import local_ipv4

    data_dir = args.data if not args.demo or args.data != "data" else "data-demo"
    app = create_app(data_dir, demo=args.demo)
    ip = local_ipv4() or "127.0.0.1"
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    print(f"\n  SEBE 3D запущен: {url}" + ("  (демо-режим)" if args.demo else ""))
    if args.host in ("0.0.0.0", "::"):
        print(f"  С телефона/других ПК в сети: http://{ip}:{args.port}\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
