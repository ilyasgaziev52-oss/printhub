"""Ленивая загрузка тяжёлых модулей (numpy, scipy, trimesh): сервер стартует быстрее,
а 3D-анализ подгружается при первом обращении."""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import Any, Optional


class LazyModule:
    def __init__(self, name: str):
        self._name = name
        self._mod: Optional[ModuleType] = None

    def __getattr__(self, attr: str) -> Any:
        if self._mod is None:
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)


mesh = LazyModule("printhub.analysis.mesh")
