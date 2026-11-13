"""Чтение заголовков файлов фотополимерной печати (временная заглушка)."""
from __future__ import annotations

import os
from typing import Any


def read_resin_file(path: str) -> dict[str, Any]:
    return {"format": os.path.splitext(path)[1].lstrip(".").lower(), "size": os.path.getsize(path)}
