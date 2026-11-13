"""SQLite-хранилище: принтеры, настройки, материалы, библиотека файлов, история, очередь."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .models import PrinterConfig

SCHEMA = """
CREATE TABLE IF NOT EXISTS printers (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS materials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    technology TEXT NOT NULL DEFAULT 'fdm',
    type TEXT NOT NULL DEFAULT 'PLA',
    color TEXT DEFAULT '',
    price REAL DEFAULT 0,          -- за кг (FDM) или за литр (смола)
    density REAL DEFAULT 1.24,     -- г/см³
    diameter REAL DEFAULT 1.75,
    remaining_g REAL DEFAULT 1000,
    notes TEXT DEFAULT '',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS library (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    stored_path TEXT NOT NULL,
    kind TEXT NOT NULL,            -- gcode / 3mf / mesh / resin / other
    technology TEXT DEFAULT '',
    size INTEGER,
    uploaded_at REAL,
    analysis TEXT,
    thumbnail TEXT,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id TEXT,
    printer_name TEXT,
    filename TEXT,
    library_id TEXT,
    started_at REAL,
    ended_at REAL,
    status TEXT,                  -- printing / finished / cancelled / error
    duration_s REAL,
    material_g REAL,
    material_ml REAL,
    material_id INTEGER,
    cost REAL,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    printer_id TEXT,
    library_id TEXT NOT NULL,
    plate INTEGER DEFAULT 1,
    options TEXT DEFAULT '{}',
    status TEXT DEFAULT 'queued', -- queued / uploading / started / failed / done
    position REAL,
    auto_start INTEGER DEFAULT 0,
    message TEXT DEFAULT '',
    created_at REAL
);
"""

DEFAULT_SETTINGS: dict[str, Any] = {
    "currency": "₽",
    "electricity_price": 6.0,          # за кВт·ч
    "labor_per_job": 0.0,
    "markup_pct": 0.0,
    "failure_pct": 5.0,                # запас на неудачные печати
    "depreciation_per_hour": 0.0,
    "telegram_token": "",
    "telegram_chat_id": "",
    "notify_events": ["finished", "error", "paused", "offline"],
    "slicer_path": "",
    "slicer_profiles_dir": "",
    "prusaslicer_path": "",
    "uvtools_path": "",
    "resin_profiles_dir": "",
    "auth_password": "",
    "discovery_subnet": "",            # например 192.168.1.0/24; пусто — автоопределение
    "camera_fps": 2,
}


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    # ---------------------------------------------------------- утилиты
    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql: str, args: tuple = ()) -> Optional[dict[str, Any]]:
        with self._lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    # ---------------------------------------------------------- принтеры
    def list_printers(self) -> list[PrinterConfig]:
        out = []
        for r in self._all("SELECT data FROM printers ORDER BY rowid"):
            d = json.loads(r["data"])
            known = PrinterConfig.__dataclass_fields__.keys()
            out.append(PrinterConfig(**{k: v for k, v in d.items() if k in known}))
        return out

    def get_printer(self, pid: str) -> Optional[PrinterConfig]:
        for p in self.list_printers():
            if p.id == pid:
                return p
        return None

    def save_printer(self, cfg: PrinterConfig) -> None:
        self._exec("INSERT INTO printers(id, data) VALUES(?, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                   (cfg.id, json.dumps(cfg.to_dict(), ensure_ascii=False)))

    def delete_printer(self, pid: str) -> None:
        self._exec("DELETE FROM printers WHERE id=?", (pid,))

    # ---------------------------------------------------------- настройки
    def get_settings(self) -> dict[str, Any]:
        s = dict(DEFAULT_SETTINGS)
        for r in self._all("SELECT key, value FROM settings"):
            try:
                s[r["key"]] = json.loads(r["value"])
            except ValueError:
                pass
        return s

    def update_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        for k, v in values.items():
            if k in DEFAULT_SETTINGS:
                self._exec("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (k, json.dumps(v, ensure_ascii=False)))
        return self.get_settings()

    # ---------------------------------------------------------- материалы
    def list_materials(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM materials ORDER BY technology, type, name")

    def get_material(self, mid: int) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM materials WHERE id=?", (mid,))

    def save_material(self, m: dict[str, Any]) -> dict[str, Any]:
        cols = ("name", "technology", "type", "color", "price", "density", "diameter", "remaining_g", "notes")
        vals = tuple(m.get(c) for c in cols)
        if m.get("id"):
            self._exec(f"UPDATE materials SET {', '.join(c + '=?' for c in cols)} WHERE id=?", vals + (m["id"],))
            mid = int(m["id"])
        else:
            cur = self._exec(f"INSERT INTO materials({', '.join(cols)}, created_at) VALUES({', '.join('?' * len(cols))}, ?)",
                             vals + (time.time(),))
            mid = int(cur.lastrowid)
        return self.get_material(mid) or {}

    def consume_material(self, mid: int, grams: float) -> None:
        self._exec("UPDATE materials SET remaining_g = MAX(remaining_g - ?, 0) WHERE id=?", (grams, mid))

    def delete_material(self, mid: int) -> None:
        self._exec("DELETE FROM materials WHERE id=?", (mid,))

    def seed_materials(self) -> None:
        if self._one("SELECT id FROM materials LIMIT 1"):
            return
        for m in (
            {"name": "PLA базовый", "technology": "fdm", "type": "PLA", "color": "#f2f2f2", "price": 1500, "density": 1.24, "diameter": 1.75, "remaining_g": 1000},
            {"name": "PETG", "technology": "fdm", "type": "PETG", "color": "#3a7bd5", "price": 1700, "density": 1.27, "diameter": 1.75, "remaining_g": 1000},
            {"name": "ABS", "technology": "fdm", "type": "ABS", "color": "#222222", "price": 1600, "density": 1.04, "diameter": 1.75, "remaining_g": 1000},
            {"name": "Смола стандартная", "technology": "resin", "type": "Standard", "color": "#9aa0a6", "price": 3500, "density": 1.10, "diameter": 0, "remaining_g": 1000},
            {"name": "Смола ABS-like", "technology": "resin", "type": "ABS-like", "color": "#5f6368", "price": 4500, "density": 1.12, "diameter": 0, "remaining_g": 1000},
        ):
            self.save_material(m)

    # ---------------------------------------------------------- библиотека
    def add_library_item(self, item: dict[str, Any]) -> None:
        self._exec("INSERT OR REPLACE INTO library(id, filename, stored_path, kind, technology, size, uploaded_at, analysis, thumbnail, notes) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (item["id"], item["filename"], item["stored_path"], item["kind"], item.get("technology", ""),
                    item.get("size"), item.get("uploaded_at", time.time()),
                    json.dumps(item.get("analysis") or {}, ensure_ascii=False), item.get("thumbnail", ""),
                    item.get("notes", "")))

    def update_library_analysis(self, lid: str, analysis: dict[str, Any], thumbnail: Optional[str] = None) -> None:
        if thumbnail is None:
            self._exec("UPDATE library SET analysis=? WHERE id=?", (json.dumps(analysis, ensure_ascii=False), lid))
        else:
            self._exec("UPDATE library SET analysis=?, thumbnail=? WHERE id=?",
                       (json.dumps(analysis, ensure_ascii=False), thumbnail, lid))

    def list_library(self) -> list[dict[str, Any]]:
        rows = self._all("SELECT id, filename, kind, technology, size, uploaded_at, analysis, notes, "
                         "length(thumbnail) > 0 AS has_thumb FROM library ORDER BY uploaded_at DESC")
        for r in rows:
            r["analysis"] = json.loads(r["analysis"] or "{}")
        return rows

    def get_library_item(self, lid: str, with_thumb: bool = False) -> Optional[dict[str, Any]]:
        r = self._one("SELECT * FROM library WHERE id=?", (lid,))
        if r:
            r["analysis"] = json.loads(r["analysis"] or "{}")
            if not with_thumb:
                r.pop("thumbnail", None)
        return r

    def delete_library_item(self, lid: str) -> Optional[dict[str, Any]]:
        r = self.get_library_item(lid)
        self._exec("DELETE FROM library WHERE id=?", (lid,))
        return r

    # ---------------------------------------------------------- история
    def start_job(self, printer_id: str, printer_name: str, filename: str, library_id: str = "",
                  material_id: Optional[int] = None) -> int:
        cur = self._exec("INSERT INTO jobs(printer_id, printer_name, filename, library_id, started_at, status, material_id) "
                         "VALUES(?,?,?,?,?,?,?)",
                         (printer_id, printer_name, filename, library_id, time.time(), "printing", material_id))
        return int(cur.lastrowid)

    def finish_job(self, job_id: int, status: str, duration_s: Optional[float] = None,
                   material_g: Optional[float] = None, material_ml: Optional[float] = None,
                   cost: Optional[float] = None) -> None:
        job = self._one("SELECT * FROM jobs WHERE id=?", (job_id,))
        if not job:
            return
        now = time.time()
        dur = duration_s if duration_s is not None else now - (job["started_at"] or now)
        self._exec("UPDATE jobs SET ended_at=?, status=?, duration_s=?, material_g=COALESCE(?, material_g), "
                   "material_ml=COALESCE(?, material_ml), cost=COALESCE(?, cost) WHERE id=?",
                   (now, status, dur, material_g, material_ml, cost, job_id))

    def open_job(self, printer_id: str) -> Optional[dict[str, Any]]:
        return self._one("SELECT * FROM jobs WHERE printer_id=? AND status='printing' ORDER BY id DESC LIMIT 1",
                         (printer_id,))

    def list_jobs(self, limit: int = 200, printer_id: Optional[str] = None) -> list[dict[str, Any]]:
        if printer_id:
            return self._all("SELECT * FROM jobs WHERE printer_id=? ORDER BY id DESC LIMIT ?", (printer_id, limit))
        return self._all("SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,))

    def stats(self) -> dict[str, Any]:
        per_printer = self._all(
            "SELECT printer_id, printer_name, COUNT(*) AS jobs, "
            "SUM(CASE WHEN status='finished' THEN 1 ELSE 0 END) AS finished, "
            "SUM(CASE WHEN status IN ('error','cancelled') THEN 1 ELSE 0 END) AS failed, "
            "COALESCE(SUM(duration_s),0) AS hours_s, COALESCE(SUM(material_g),0) AS material_g, "
            "COALESCE(SUM(material_ml),0) AS material_ml, COALESCE(SUM(cost),0) AS cost "
            "FROM jobs GROUP BY printer_id ORDER BY jobs DESC")
        total = self._one(
            "SELECT COUNT(*) AS jobs, SUM(CASE WHEN status='finished' THEN 1 ELSE 0 END) AS finished, "
            "COALESCE(SUM(duration_s),0) AS hours_s, COALESCE(SUM(material_g),0) AS material_g, "
            "COALESCE(SUM(material_ml),0) AS material_ml, COALESCE(SUM(cost),0) AS cost FROM jobs") or {}
        return {"total": total, "per_printer": per_printer}

    # ---------------------------------------------------------- очередь
    def list_queue(self, include_done: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM queue" + ("" if include_done else " WHERE status NOT IN ('done')") + " ORDER BY position, id"
        rows = self._all(sql)
        for r in rows:
            r["options"] = json.loads(r["options"] or "{}")
            r["auto_start"] = bool(r["auto_start"])
        return rows

    def add_queue(self, library_id: str, printer_id: Optional[str], plate: int = 1,
                  options: Optional[dict[str, Any]] = None, auto_start: bool = False) -> int:
        pos = (self._one("SELECT MAX(position) AS p FROM queue") or {}).get("p") or 0
        cur = self._exec("INSERT INTO queue(printer_id, library_id, plate, options, status, position, auto_start, created_at) "
                         "VALUES(?,?,?,?,?,?,?,?)",
                         (printer_id, library_id, plate, json.dumps(options or {}), "queued", pos + 1,
                          1 if auto_start else 0, time.time()))
        return int(cur.lastrowid)

    def update_queue(self, qid: int, **fields: Any) -> None:
        if not fields:
            return
        if "options" in fields:
            fields["options"] = json.dumps(fields["options"])
        if "auto_start" in fields:
            fields["auto_start"] = 1 if fields["auto_start"] else 0
        cols = ", ".join(f"{k}=?" for k in fields)
        self._exec(f"UPDATE queue SET {cols} WHERE id=?", tuple(fields.values()) + (qid,))

    def delete_queue(self, qid: int) -> None:
        self._exec("DELETE FROM queue WHERE id=?", (qid,))
