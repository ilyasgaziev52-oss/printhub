"""Общие модели данных: конфигурация принтера, унифицированный статус, файлы."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class Technology(str, Enum):
    FDM = "fdm"
    RESIN = "resin"


class PrinterState(str, Enum):
    OFFLINE = "offline"      # нет связи
    IDLE = "idle"            # готов
    PREPARING = "preparing"  # нагрев, калибровка, загрузка файла
    PRINTING = "printing"
    PAUSED = "paused"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    ERROR = "error"
    BUSY = "busy"            # занят чем-то другим (сервис, обновление)
    UNKNOWN = "unknown"


ACTIVE_STATES = {PrinterState.PREPARING, PrinterState.PRINTING, PrinterState.PAUSED}


@dataclass
class PrinterConfig:
    """То, что хранится в БД для каждого добавленного принтера."""
    id: str
    name: str
    kind: str                       # ключ драйвера: bambu, elegoo_sdcp, moonraker, octoprint, anycubic_photon
    host: str
    model: str = ""
    serial: str = ""                # Bambu: серийный номер; Elegoo: MainboardID
    access_code: str = ""           # Bambu: LAN access code
    api_key: str = ""               # OctoPrint / Moonraker (если включена авторизация)
    port: int = 0                   # 0 = порт по умолчанию драйвера
    technology: str = Technology.FDM.value
    build_volume: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    power_w: float = 0.0            # средняя потребляемая мощность для расчёта стоимости
    enabled: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, hide_secrets: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if hide_secrets:
            for k in ("access_code", "api_key"):
                if d.get(k):
                    d[k] = "••••"
        return d


@dataclass
class Temperature:
    current: Optional[float] = None
    target: Optional[float] = None


@dataclass
class MaterialSlot:
    """Слот материала: катушка AMS, внешняя катушка, ванна со смолой."""
    id: str
    name: str = ""
    type: str = ""          # PLA, PETG, Resin…
    color: str = ""         # #RRGGBB
    remain_pct: Optional[float] = None
    active: bool = False


@dataclass
class PrinterStatus:
    state: PrinterState = PrinterState.OFFLINE
    state_raw: str = ""                 # исходный статус принтера, для отладки
    progress: Optional[float] = None    # 0..100
    layer: Optional[int] = None
    total_layers: Optional[int] = None
    elapsed_s: Optional[float] = None
    remaining_s: Optional[float] = None
    filename: str = ""
    temps: dict[str, Temperature] = field(default_factory=dict)
    fans: dict[str, float] = field(default_factory=dict)  # 0..100 %
    speed_pct: Optional[float] = None
    speed_level: str = ""
    light: Optional[bool] = None
    error: str = ""
    materials: list[MaterialSlot] = field(default_factory=list)
    wifi_signal: str = ""
    firmware: str = ""
    message: str = ""                   # пояснение для пользователя (например «включите режим разработчика»)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["state"] = self.state.value
        return d


@dataclass
class FileEntry:
    name: str
    path: str
    size: Optional[int] = None
    modified: Optional[float] = None
    is_dir: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class DiscoveredPrinter:
    kind: str
    host: str
    name: str = ""
    model: str = ""
    serial: str = ""
    technology: str = Technology.FDM.value
    firmware: str = ""
    port: int = 0
    notes: str = ""                 # что ещё нужно для подключения
    needs: list[str] = field(default_factory=list)  # поля, которые надо ввести: access_code, api_key
    extra: dict[str, Any] = field(default_factory=dict)

    def key(self) -> str:
        return f"{self.kind}:{self.host}:{self.serial}"


@dataclass
class Capabilities:
    upload: bool = False
    start: bool = False
    pause: bool = False
    resume: bool = False
    stop: bool = False
    files: bool = False
    delete: bool = False
    camera: bool = False
    light: bool = False
    gcode: bool = False
    speed: bool = False

    def to_dict(self) -> dict[str, bool]:
        return asdict(self)
