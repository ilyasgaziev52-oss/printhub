"""Реестр драйверов принтеров."""
from __future__ import annotations

import importlib
import logging
from typing import Optional

from .base import PrinterDriver

log = logging.getLogger("printhub.drivers")

_MODULES = {
    "bambu": ("bambu", "BambuDriver"),
    "elegoo_sdcp": ("elegoo", "ElegooSdcpDriver"),
    "elegoo_cc2": ("elegoo_cc2", "ElegooCC2Driver"),
    "anycubic_photon": ("anycubic_photon", "AnycubicPhotonDriver"),
    "anycubic_lan": ("anycubic_lan", "AnycubicLanDriver"),
    "moonraker": ("moonraker", "MoonrakerDriver"),
    "octoprint": ("octoprint", "OctoPrintDriver"),
}

_cache: dict[str, type[PrinterDriver]] = {}


def get_driver_class(kind: str) -> Optional[type[PrinterDriver]]:
    if kind in _cache:
        return _cache[kind]
    spec = _MODULES.get(kind)
    if not spec:
        return None
    try:
        mod = importlib.import_module(f"{__name__}.{spec[0]}")
        cls = getattr(mod, spec[1])
    except Exception as e:  # noqa: BLE001
        log.warning("драйвер %s недоступен: %s", kind, e)
        return None
    _cache[kind] = cls
    return cls


def all_driver_classes() -> list[type[PrinterDriver]]:
    out = []
    for kind in _MODULES:
        cls = get_driver_class(kind)
        if cls:
            out.append(cls)
    return out
