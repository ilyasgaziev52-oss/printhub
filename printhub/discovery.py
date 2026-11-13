"""Поиск принтеров в локальной сети.

1. Широковещательные протоколы производителей (Bambu SSDP, Elegoo SDCP UDP, …) —
   реализованы в classmethod discover() каждого драйвера.
2. Сканирование подсети /24 по характерным портам с последующей проверкой probe().
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Optional

from .drivers import all_driver_classes, get_driver_class
from .models import DiscoveredPrinter

log = logging.getLogger("printhub.discovery")

# порт -> драйверы, которые стоит проверить, если порт открыт
PORT_HINTS: dict[int, list[str]] = {
    8883: ["bambu"],
    3030: ["elegoo_sdcp"],
    7125: ["moonraker"],
    1883: ["elegoo_cc2"],
    6000: ["anycubic_photon"],
    9883: ["anycubic_lan"],
    80: ["octoprint", "moonraker"],
}


def local_ipv4() -> Optional[str]:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # пакет не отправляется
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def default_subnet() -> Optional[str]:
    ip = local_ipv4()
    if not ip or ip.startswith("127."):
        return None
    return str(ipaddress.ip_network(f"{ip}/24", strict=False))


async def _port_open(host: str, port: int, timeout: float) -> bool:
    try:
        fut = asyncio.open_connection(host, port)
        _, w = await asyncio.wait_for(fut, timeout)
        w.close()
        try:
            await w.wait_closed()
        except Exception:  # noqa: BLE001
            pass
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def scan_subnet(subnet: Optional[str] = None, timeout: float = 0.6,
                      concurrency: int = 300) -> list[DiscoveredPrinter]:
    subnet = subnet or default_subnet()
    if not subnet:
        return []
    net = ipaddress.ip_network(subnet, strict=False)
    if net.num_addresses > 1024:
        raise ValueError("Слишком большая подсеть для сканирования (максимум /22)")
    sem = asyncio.Semaphore(concurrency)
    found: list[DiscoveredPrinter] = []
    me = local_ipv4()

    async def check(host: str, port: int) -> None:
        async with sem:
            if not await _port_open(host, port, timeout):
                return
        for kind in PORT_HINTS[port]:
            cls = get_driver_class(kind)
            if not cls:
                continue
            try:
                d = await cls.probe(host)
            except Exception as e:  # noqa: BLE001
                log.debug("probe %s %s: %s", kind, host, e)
                d = None
            if d:
                found.append(d)

    tasks = [check(str(h), p) for h in net.hosts() if str(h) != me for p in PORT_HINTS]
    await asyncio.gather(*tasks)
    return found


async def discover_all(scan: bool = True, subnet: Optional[str] = None,
                       timeout: float = 4.0) -> list[DiscoveredPrinter]:
    jobs = [cls.discover(timeout) for cls in all_driver_classes()]
    if scan:
        jobs.append(scan_subnet(subnet))
    results = await asyncio.gather(*jobs, return_exceptions=True)
    merged: dict[tuple[str, str], DiscoveredPrinter] = {}
    for r in results:
        if isinstance(r, BaseException):
            log.warning("ошибка поиска: %s", r)
            continue
        for d in r:
            key = (d.kind, d.host)
            prev = merged.get(key)
            # сведения из широковещательного ответа богаче, чем у сканера
            if prev is None or (not prev.serial and d.serial) or (not prev.model and d.model):
                if prev:
                    for f in ("name", "model", "serial", "firmware"):
                        if not getattr(d, f) and getattr(prev, f):
                            setattr(d, f, getattr(prev, f))
                merged[key] = d
    # Если один и тот же хост найден и как bambu, и как что-то ещё — оставляем самое специфичное
    return sorted(merged.values(), key=lambda d: (d.kind, d.host))
