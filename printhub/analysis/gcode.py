"""Анализ G-code: время печати, расход филамента, слои, миниатюры.

Два источника данных:
1. Комментарии слайсера (OrcaSlicer, Bambu Studio, PrusaSlicer, Cura, Elegoo Slicer,
   Anycubic Slicer Next, Simplify3D, ideaMaker) — самые точные, если есть.
2. Собственная симуляция движения с планировщиком скоростей (ускорения,
   скорость на стыках как в Klipper/Marlin) — работает для любого файла.
"""
from __future__ import annotations

import base64
import io
import math
import re
from dataclasses import asdict, dataclass, field
from typing import IO, Any, Iterable, Optional

# Плотность (г/см³) для типов пластика — если слайсер её не записал.
FILAMENT_DENSITY = {
    "PLA": 1.24, "PLA+": 1.24, "PLA-CF": 1.29, "PETG": 1.27, "PETG-CF": 1.30, "PET": 1.38,
    "ABS": 1.04, "ASA": 1.07, "TPU": 1.21, "TPE": 1.20, "PA": 1.14, "PA-CF": 1.20, "PAHT-CF": 1.20,
    "NYLON": 1.14, "PC": 1.20, "PP": 0.90, "PVA": 1.23, "HIPS": 1.04, "PEEK": 1.30, "PEI": 1.27,
}
DEFAULT_DENSITY = 1.24

_DURATION_RE = re.compile(r"(?:(\d+)\s*d)?\s*(?:(\d+)\s*h)?\s*(?:(\d+)\s*m(?!m))?\s*(?:(\d+(?:\.\d+)?)\s*s)?", re.I)


def parse_duration(text: str) -> Optional[float]:
    """'1d 2h 3m 4s' / '2h 3m' / '45s' / '3723' -> секунды."""
    text = text.strip()
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    # Simplify3D: "1 hours 2 minutes"
    m = re.fullmatch(r"(?:(\d+)\s*hours?)?\s*(?:(\d+)\s*minutes?)?\s*(?:(\d+)\s*seconds?)?", text, re.I)
    if m and any(m.groups()):
        h, mi, s = (int(g) if g else 0 for g in m.groups())
        return h * 3600 + mi * 60 + s
    m = _DURATION_RE.fullmatch(text)
    if m and any(m.groups()):
        d, h, mi = (int(g) if g else 0 for g in m.groups()[:3])
        s = float(m.group(4)) if m.group(4) else 0.0
        return d * 86400 + h * 3600 + mi * 60 + s
    return None


def _floats(text: str) -> list[float]:
    out = []
    for part in re.split(r"[,;\s]+", text.strip()):
        try:
            out.append(float(part.rstrip("m")))
        except ValueError:
            pass
    return out


@dataclass
class MachineLimits:
    accel: float = 2000.0               # мм/с² для печати
    travel_accel: float = 0.0           # 0 = как accel
    max_velocity: float = 500.0         # мм/с
    max_accel_xy: float = 20000.0
    max_z_velocity: float = 15.0
    max_z_accel: float = 300.0
    max_e_velocity: float = 120.0
    square_corner_velocity: float = 5.0  # Klipper SCV; для Marlin эквивалент junction deviation


@dataclass
class SlicerInfo:
    slicer: str = ""
    version: str = ""
    printer_model: str = ""
    est_time_s: Optional[float] = None
    est_time_silent_s: Optional[float] = None
    filament_mm: list[float] = field(default_factory=list)
    filament_g: list[float] = field(default_factory=list)
    filament_cm3: list[float] = field(default_factory=list)
    filament_cost: Optional[float] = None
    filament_type: list[str] = field(default_factory=list)
    filament_density: list[float] = field(default_factory=list)
    filament_diameter: list[float] = field(default_factory=list)
    filament_colour: list[str] = field(default_factory=list)
    layer_count: Optional[int] = None
    layer_height: Optional[float] = None
    first_layer_height: Optional[float] = None
    nozzle_diameter: Optional[float] = None
    nozzle_temp: Optional[float] = None
    bed_temp: Optional[float] = None
    max_z: Optional[float] = None
    infill: str = ""
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class GcodeAnalysis:
    # Итоговые значения (лучшее из доступного)
    time_s: float = 0.0
    time_source: str = ""               # slicer / simulation
    filament_mm: float = 0.0
    filament_g: float = 0.0
    filament_cm3: float = 0.0
    per_tool_mm: dict[str, float] = field(default_factory=dict)
    layers: int = 0
    max_z: float = 0.0
    bbox_min: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    bbox_max: list[float] = field(default_factory=lambda: [0.0, 0.0, 0.0])
    # Симуляция
    sim_time_s: float = 0.0
    sim_heating_s: float = 0.0
    sim_moves: int = 0
    layer_times: list[float] = field(default_factory=list)  # накопленное время к началу каждого слоя
    tool_changes: int = 0
    lines: int = 0
    # Прочее
    slicer: SlicerInfo = field(default_factory=SlicerInfo)
    thumbnail_png_b64: str = ""
    thumbnail_mime: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self, with_layer_times: bool = False) -> dict[str, Any]:
        d = asdict(self)
        if not with_layer_times:
            d.pop("layer_times", None)
        d["slicer"].pop("raw", None)
        return d


# ---------------------------------------------------------------- заголовки слайсеров

_KV_RE = re.compile(r"^;\s*([A-Za-z0-9_ \[\]\^()/\-.#]+?)\s*[=:]\s*(.*)$")


def parse_slicer_comment(line: str, info: SlicerInfo) -> None:
    """Разбирает одну строку-комментарий и пополняет SlicerInfo."""
    s = line.strip()
    low = s.lower()

    # Идентификация слайсера
    if not info.slicer:
        m = re.match(r";\s*generated by\s+([A-Za-z][\w\- ]*?)\s+v?([\d][\w.\-+]*)", s, re.I)
        if m:
            info.slicer, info.version = m.group(1).strip(), m.group(2)
        elif low.startswith(";generated with cura_steamengine"):
            info.slicer, info.version = "Cura", s.split()[-1]
        elif re.match(r";\s*bambustudio\s+[\d.]+", low):
            info.slicer, info.version = "BambuStudio", s.split()[-1]
        elif low.startswith(";flavor:"):
            info.slicer = info.slicer or "Cura"
        elif "simplify3d" in low and "version" in low:
            info.slicer = "Simplify3D"
        elif low.startswith(";sliced by ideamaker") or low.startswith(";ideamaker"):
            info.slicer = "ideaMaker"

    # Cura / Elegoo (Cura-based) / Anycubic (старый)
    if low.startswith(";time:"):
        v = parse_duration(s.split(":", 1)[1])
        if v is not None and info.est_time_s is None:
            info.est_time_s = v
        return
    if low.startswith(";filament used:"):
        vals = _floats(s.split(":", 1)[1])
        if vals and not info.filament_mm:
            info.filament_mm = [v * 1000 for v in vals]  # Cura пишет метры
        return
    if low.startswith(";layer_count:"):
        try:
            info.layer_count = int(s.split(":", 1)[1])
        except ValueError:
            pass
        return
    if low.startswith(";layer height:"):
        vals = _floats(s.split(":", 1)[1])
        if vals:
            info.layer_height = vals[0]
        return
    if low.startswith(";print time:"):  # ideaMaker
        v = parse_duration(s.split(":", 1)[1])
        if v is not None and info.est_time_s is None:
            info.est_time_s = v
        return

    # Bambu Studio: "; model printing time: 1h 2m 3s; total estimated time: 1h 10m 5s"
    if "total estimated time:" in low:
        v = parse_duration(s.split("total estimated time:", 1)[1].split(";")[0])
        if v is not None:
            info.est_time_s = v
        return

    # Simplify3D
    if low.startswith(";   build time:"):
        v = parse_duration(s.split(":", 1)[1])
        if v is not None:
            info.est_time_s = v
        return
    if low.startswith(";   filament length:"):
        vals = _floats(s.split(":", 1)[1].split("mm")[0])
        if vals:
            info.filament_mm = vals[:1]
        return
    if low.startswith(";   plastic weight:"):
        vals = _floats(s.split(":", 1)[1].split("g")[0])
        if vals:
            info.filament_g = vals[:1]
        return

    m = _KV_RE.match(s)
    if not m:
        return
    key = m.group(1).strip().lower()
    val = m.group(2).strip()
    if len(info.raw) < 2000:
        info.raw.setdefault(key, val)

    if key.startswith("estimated printing time (normal mode)") or key == "estimated printing time":
        v = parse_duration(val)
        if v is not None:
            info.est_time_s = v
    elif key.startswith("estimated printing time (silent mode)"):
        info.est_time_silent_s = parse_duration(val)
    elif key in ("filament used [mm]", "total filament length [mm]"):
        info.filament_mm = _floats(val)
    elif key in ("filament used [g]", "total filament weight [g]"):
        info.filament_g = _floats(val)
    elif key == "total filament used [g]" and not info.filament_g:
        info.filament_g = _floats(val)
    elif key in ("filament used [cm3]", "total filament volume [cm^3]"):
        info.filament_cm3 = _floats(val)
    elif key in ("filament cost", "total filament cost"):
        vals = _floats(val)
        if vals:
            info.filament_cost = sum(vals)
    elif key in ("filament_type", "filament type"):
        info.filament_type = [t.strip().strip('"') for t in re.split(r"[;,]", val) if t.strip()]
    elif key in ("filament_density", "filament density"):
        info.filament_density = _floats(val)
    elif key in ("filament_diameter", "filament diameter"):
        info.filament_diameter = _floats(val)
    elif key in ("filament_colour", "filament_color", "extruder_colour"):
        if not info.filament_colour:
            info.filament_colour = [c.strip().strip('"') for c in re.split(r"[;,]", val) if c.strip()]
    elif key in ("total layer number", "total layers count", "total_layer_count"):
        try:
            info.layer_count = int(float(val))
        except ValueError:
            pass
    elif key == "layer_height":
        vals = _floats(val)
        if vals:
            info.layer_height = vals[0]
    elif key in ("first_layer_height", "initial_layer_print_height"):
        vals = _floats(val.rstrip("%"))
        if vals and "%" not in val:
            info.first_layer_height = vals[0]
    elif key == "nozzle_diameter":
        vals = _floats(val)
        if vals:
            info.nozzle_diameter = vals[0]
    elif key in ("nozzle_temperature", "temperature"):
        vals = _floats(val)
        if vals:
            info.nozzle_temp = vals[0]
    elif key in ("bed_temperature", "hot_plate_temp", "textured_plate_temp", "cool_plate_temp"):
        vals = _floats(val)
        if vals and info.bed_temp is None:
            info.bed_temp = vals[0]
    elif key in ("max_z_height", "max_layer_z"):
        vals = _floats(val)
        if vals:
            info.max_z = vals[0]
    elif key in ("printer_model", "printer_settings_id") and not info.printer_model:
        info.printer_model = val.strip('"')
    elif key in ("sparse_infill_density", "fill_density", "infill_sparse_density"):
        info.infill = val


# ---------------------------------------------------------------- планировщик движения

class _Planner:
    """Упрощённый look-ahead планировщик (трапециевидный профиль, скорость на стыках как в Klipper)."""

    FLUSH_AT = 4000
    KEEP = 200

    def __init__(self, limits: MachineLimits):
        self.lim = limits
        self.buf: list[list[float]] = []  # [dist, ux, uy, uz, v_max, accel, v_junction_max]
        self.prev_unit: Optional[tuple[float, float, float]] = None
        self.start_v = 0.0
        self.time = 0.0

    def junction_deviation(self, accel: float) -> float:
        scv = self.lim.square_corner_velocity
        return scv * scv * (math.sqrt(2.0) - 1.0) / max(accel, 1.0)

    def add(self, dx: float, dy: float, dz: float, de: float, feed_mm_s: float, accel: float) -> None:
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        if dist < 1e-9:
            if abs(de) > 1e-9:  # только экструдер: ретракт/подача, стоп до и после
                self.flush(final=True)
                v = min(feed_mm_s, self.lim.max_e_velocity)
                self.time += abs(de) / max(v, 0.1)
            return
        ux, uy, uz = dx / dist, dy / dist, dz / dist
        v = min(feed_mm_s, self.lim.max_velocity)
        a = accel
        if abs(dz) > 1e-9:
            zr = abs(uz)
            v = min(v, self.lim.max_z_velocity / zr)
            a = min(a, self.lim.max_z_accel / zr)
        if abs(de) > 1e-9:
            er = abs(de) / dist
            v = min(v, self.lim.max_e_velocity / er)
        v = max(v, 0.1)
        # скорость на стыке
        vj2 = 0.0
        if self.prev_unit is not None and self.buf:
            pux, puy, puz = self.prev_unit
            cos_theta = -(pux * ux + puy * uy + puz * uz)
            prev_v = self.buf[-1][4]
            if cos_theta < 0.999999:
                if cos_theta < -0.999999:
                    vj2 = min(prev_v, v) ** 2
                else:
                    sin_half = math.sqrt(0.5 * (1.0 - cos_theta))
                    r = self.junction_deviation(a) * sin_half / max(1.0 - sin_half, 1e-9)
                    vj2 = min(a * r, prev_v * prev_v, v * v)
        self.buf.append([dist, ux, uy, uz, v, a, vj2])
        self.prev_unit = (ux, uy, uz)
        if len(self.buf) >= self.FLUSH_AT:
            self.flush(final=False)

    def approx_time(self) -> float:
        """Время с учётом ещё не спланированных движений (грубо, без ускорений)."""
        return self.time + sum(m[0] / m[4] for m in self.buf)

    def flush(self, final: bool = True) -> None:
        buf = self.buf
        n = len(buf)
        if n == 0:
            self.prev_unit = None
            return
        # обратный проход: максимальная скорость входа, чтобы успеть затормозить
        start_max2 = [0.0] * n
        next_start2 = 0.0
        for i in range(n - 1, -1, -1):
            dist, _, _, _, v, a, vj2 = buf[i]
            lim2 = vj2 if i > 0 else self.start_v ** 2
            start_max2[i] = min(lim2, v * v, next_start2 + 2.0 * a * dist)
            next_start2 = start_max2[i]
        # прямой проход
        commit = n if final else n - self.KEEP
        v0_2 = start_max2[0]
        for i in range(commit):
            dist, _, _, _, v, a, _ = buf[i]
            v1_2 = start_max2[i + 1] if i + 1 < n else 0.0
            v1_2 = min(v1_2, v0_2 + 2.0 * a * dist)
            vc2 = min(v * v, (2.0 * a * dist + v0_2 + v1_2) / 2.0)
            vc = math.sqrt(max(vc2, 1e-12))
            v0 = math.sqrt(max(v0_2, 0.0))
            v1 = math.sqrt(max(v1_2, 0.0))
            d_acc = max((vc2 - v0_2) / (2.0 * a), 0.0)
            d_dec = max((vc2 - v1_2) / (2.0 * a), 0.0)
            cruise = max(dist - d_acc - d_dec, 0.0)
            self.time += (vc - v0) / a + (vc - v1) / a + cruise / vc
            v0_2 = v1_2
        if final:
            self.buf = []
            self.start_v = 0.0
            self.prev_unit = None
        else:
            self.buf = buf[commit:]
            self.start_v = math.sqrt(max(v0_2, 0.0))


# ---------------------------------------------------------------- основной анализ

def _parse_params(code_line: str) -> dict[str, float]:
    params: dict[str, float] = {}
    for word in code_line.split()[1:]:
        if len(word) < 2:
            continue
        k = word[0].upper()
        try:
            params[k] = float(word[1:])
        except ValueError:
            pass
    return params


def _klipper_params(code_line: str) -> dict[str, float]:
    out = {}
    for m in re.finditer(r"([A-Z_]+)=([\d.]+)", code_line.upper()):
        try:
            out[m.group(1)] = float(m.group(2))
        except ValueError:
            pass
    return out


class GcodeAnalyzer:
    def __init__(self, limits: Optional[MachineLimits] = None, simulate: bool = True,
                 heating_estimates: bool = True):
        self.lim = limits or MachineLimits()
        self.simulate = simulate
        self.heating_estimates = heating_estimates

    def analyze_file(self, path: str) -> GcodeAnalysis:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return self.analyze_stream(f)

    def analyze_bytes(self, data: bytes) -> GcodeAnalysis:
        return self.analyze_stream(io.StringIO(data.decode("utf-8", errors="replace")))

    def analyze_stream(self, lines: Iterable[str]) -> GcodeAnalysis:
        res = GcodeAnalysis()
        info = res.slicer
        lim = self.lim
        planner = _Planner(lim)

        absolute = True
        abs_e = True
        x = y = z = e = 0.0
        feed = 50.0  # мм/с
        accel = lim.accel
        travel_accel = 0.0
        tool = "0"
        per_tool: dict[str, float] = {}
        inch = False

        bb_min = [math.inf] * 3
        bb_max = [-math.inf] * 3
        last_layer_z = None
        layers = 0
        layer_times: list[float] = []
        heating = 0.0
        comment_layers = 0
        thumbs: list[tuple[int, str, list[str]]] = []
        thumb_cur: Optional[tuple[int, str, list[str]]] = None
        nlines = 0
        seen_m190 = False
        moves = 0

        for raw in lines:
            nlines += 1
            line = raw.strip()
            if not line:
                continue
            if line[0] == ";":
                # миниатюры
                if thumb_cur is not None:
                    low = line.lower()
                    if " end" in low and "thumbnail" in low:
                        thumbs.append(thumb_cur)
                        thumb_cur = None
                    else:
                        thumb_cur[2].append(line[1:].strip())
                    continue
                low = line.lower()
                if "thumbnail" in low and "begin" in low:
                    m = re.search(r"thumbnail(?:_(\w+))?\s+begin\s+(\d+)x(\d+)", low)
                    if m:
                        fmt = (m.group(1) or "png").lower()
                        thumb_cur = (int(m.group(2)) * int(m.group(3)), fmt, [])
                    continue
                if low.startswith(";layer_change") or low.startswith("; change_layer") or low.startswith(";layer:"):
                    comment_layers += 1
                parse_slicer_comment(line, info)
                continue

            code = line.split(";", 1)[0].strip()
            if not code:
                continue
            c0 = code[0].upper()
            word = code.split(None, 1)[0].upper()

            if c0 == "G":
                if word in ("G1", "G0", "G01", "G00", "G2", "G3", "G02", "G03"):
                    p = _parse_params(code)
                    if "F" in p:
                        feed = p["F"] / 60.0 * (25.4 if inch else 1.0)
                    scale = 25.4 if inch else 1.0
                    nx = (p["X"] * scale if absolute else x + p["X"] * scale) if "X" in p else x
                    ny = (p["Y"] * scale if absolute else y + p["Y"] * scale) if "Y" in p else y
                    nz = (p["Z"] * scale if absolute else z + p["Z"] * scale) if "Z" in p else z
                    de = 0.0
                    if "E" in p:
                        ev = p["E"] * scale
                        if abs_e:
                            de = ev - e
                            e = ev
                        else:
                            de = ev
                            e += ev
                    dx, dy, dz = nx - x, ny - y, nz - z
                    if word in ("G2", "G3", "G02", "G03"):
                        # длина дуги
                        if "I" in p or "J" in p:
                            ci, cj = p.get("I", 0.0) * scale, p.get("J", 0.0) * scale
                            r = math.hypot(ci, cj)
                            cx, cy = x + ci, y + cj
                            a0 = math.atan2(y - cy, x - cx)
                            a1 = math.atan2(ny - cy, nx - cx)
                            sweep = a1 - a0
                            cw = word in ("G2", "G02")
                            if cw and sweep >= 0:
                                sweep -= 2 * math.pi
                            elif not cw and sweep <= 0:
                                sweep += 2 * math.pi
                            arc_len = abs(sweep) * r
                            chord = math.hypot(dx, dy)
                            if chord > 1e-9 and arc_len > chord:
                                k = arc_len / chord
                                dx, dy = dx * k, dy * k
                    extruding = de > 1e-6
                    if self.simulate:
                        a_use = accel
                        if not extruding and travel_accel > 0:
                            a_use = travel_accel
                        planner.add(dx, dy, dz, de, feed, min(a_use, lim.max_accel_xy))
                    moves += 1
                    if de:
                        per_tool[tool] = per_tool.get(tool, 0.0) + de
                    if extruding and (dx or dy):
                        for i, val in enumerate((nx, ny, nz)):
                            if val < bb_min[i]:
                                bb_min[i] = val
                            if val > bb_max[i]:
                                bb_max[i] = val
                        if last_layer_z is None or nz > last_layer_z + 1e-4:
                            last_layer_z = nz
                            layers += 1
                            layer_times.append(planner.approx_time() + heating)
                    x, y, z = nx, ny, nz
                elif word in ("G90",):
                    absolute = True
                    abs_e = True
                elif word in ("G91",):
                    absolute = False
                    abs_e = False
                elif word == "G92":
                    p = _parse_params(code)
                    scale = 25.4 if inch else 1.0
                    if "E" in p:
                        e = p["E"] * scale
                    if "X" in p:
                        x = p["X"] * scale
                    if "Y" in p:
                        y = p["Y"] * scale
                    if "Z" in p:
                        z = p["Z"] * scale
                elif word == "G4":
                    p = _parse_params(code)
                    planner.flush()
                    if "S" in p:
                        planner.time += p["S"]
                    elif "P" in p:
                        planner.time += p["P"] / 1000.0
                elif word == "G28":
                    planner.flush()
                    planner.time += 12.0
                elif word in ("G29",):
                    planner.flush()
                    planner.time += 60.0
                elif word == "G20":
                    inch = True
                elif word == "G21":
                    inch = False
            elif c0 == "M":
                if word == "M82":
                    abs_e = True
                elif word == "M83":
                    abs_e = False
                elif word == "M204":
                    p = _parse_params(code)
                    if "S" in p:
                        accel = p["S"]
                    if "P" in p:
                        accel = p["P"]
                    if "T" in p:
                        travel_accel = p["T"]
                elif word == "M201":
                    p = _parse_params(code)
                    vals = [p[k] for k in ("X", "Y") if k in p]
                    if vals:
                        lim.max_accel_xy = min(vals)
                    if "Z" in p:
                        lim.max_z_accel = p["Z"]
                elif word == "M203":
                    p = _parse_params(code)
                    vals = [p[k] for k in ("X", "Y") if k in p]
                    if vals:
                        lim.max_velocity = min(vals)
                    if "Z" in p:
                        lim.max_z_velocity = p["Z"]
                    if "E" in p:
                        lim.max_e_velocity = p["E"]
                elif word in ("M109", "M190", "M191") and self.heating_estimates:
                    planner.flush()
                    # без модели нагревателя — грубая оценка
                    if word == "M190":
                        add = 0.0 if seen_m190 else 90.0
                        seen_m190 = True
                    elif word == "M191":
                        add = 120.0
                    else:
                        add = 30.0
                    heating += add
                elif word == "M400":
                    planner.flush()
            elif c0 == "T" and len(word) > 1 and word[1:].isdigit():
                if word[1:] != tool:
                    res.tool_changes += 1
                tool = word[1:]
            else:
                up = code.upper()
                if up.startswith("SET_VELOCITY_LIMIT"):
                    kp = _klipper_params(up)
                    if "ACCEL" in kp:
                        accel = kp["ACCEL"]
                    if "VELOCITY" in kp:
                        lim.max_velocity = kp["VELOCITY"]
                    if "SQUARE_CORNER_VELOCITY" in kp:
                        lim.square_corner_velocity = kp["SQUARE_CORNER_VELOCITY"]
                elif up.startswith("M1002") or up.startswith("M620") or up.startswith("M621"):
                    pass  # служебные команды Bambu

        planner.flush()
        res.lines = nlines
        res.sim_moves = moves
        res.sim_heating_s = heating
        res.sim_time_s = planner.time + heating
        res.layer_times = layer_times
        res.layers = info.layer_count or layers or comment_layers
        if bb_min[0] != math.inf:
            res.bbox_min = [round(v, 3) for v in bb_min]
            res.bbox_max = [round(v, 3) for v in bb_max]
            res.max_z = round(bb_max[2], 3)
        if info.max_z:
            res.max_z = info.max_z

        # Время: приоритет — оценка слайсера
        if info.est_time_s:
            res.time_s = info.est_time_s
            res.time_source = "slicer"
        else:
            res.time_s = res.sim_time_s
            res.time_source = "simulation"

        # Филамент
        res.per_tool_mm = {k: round(max(v, 0.0), 2) for k, v in per_tool.items()}
        sim_mm = sum(max(v, 0.0) for v in per_tool.values())
        diam = info.filament_diameter[0] if info.filament_diameter else 1.75
        dens = self._density(info)
        if info.filament_mm:
            res.filament_mm = sum(info.filament_mm)
        else:
            res.filament_mm = sim_mm
        if info.filament_cm3:
            res.filament_cm3 = sum(info.filament_cm3)
            # Bambu Studio пишет «[cm^3]», но фактически это мм³
            grams_hint = sum(info.filament_g) if info.filament_g else res.filament_mm * 0.003
            if grams_hint and res.filament_cm3 > 20 * grams_hint:
                res.filament_cm3 /= 1000.0
        else:
            res.filament_cm3 = res.filament_mm * math.pi * (diam / 2) ** 2 / 1000.0
        if info.filament_g:
            res.filament_g = sum(info.filament_g)
        else:
            res.filament_g = res.filament_cm3 * dens

        if info.filament_mm and sim_mm and abs(sim_mm - res.filament_mm) / max(res.filament_mm, 1) > 0.15:
            res.warnings.append("Расход по комментариям слайсера и по симуляции отличается более чем на 15%.")

        # Миниатюра: берём самую большую PNG/JPG
        best = None
        for area, fmt, chunks in thumbs:
            if fmt in ("png", "jpg", "jpeg") and (best is None or area > best[0]):
                best = (area, fmt, chunks)
        if best:
            data = "".join(best[2])
            try:
                base64.b64decode(data, validate=False)
                res.thumbnail_png_b64 = data
                res.thumbnail_mime = "image/png" if best[1] == "png" else "image/jpeg"
            except Exception:  # noqa: BLE001
                pass

        for k in ("filament_mm", "filament_g", "filament_cm3", "time_s", "sim_time_s"):
            setattr(res, k, round(getattr(res, k), 2))
        return res

    @staticmethod
    def _density(info: SlicerInfo) -> float:
        if info.filament_density and info.filament_density[0] > 0:
            return info.filament_density[0]
        if info.filament_type:
            return FILAMENT_DENSITY.get(info.filament_type[0].upper(), DEFAULT_DENSITY)
        return DEFAULT_DENSITY


def analyze_gcode(path_or_stream: str | IO[str], simulate: bool = True,
                  limits: Optional[MachineLimits] = None) -> GcodeAnalysis:
    an = GcodeAnalyzer(limits=limits, simulate=simulate)
    if isinstance(path_or_stream, str):
        return an.analyze_file(path_or_stream)
    return an.analyze_stream(path_or_stream)


def format_duration(seconds: Optional[float]) -> str:
    if seconds is None:
        return "—"
    s = int(round(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d} д")
    if h:
        parts.append(f"{h} ч")
    if m:
        parts.append(f"{m} мин")
    if not parts:
        parts.append(f"{s} с")
    return " ".join(parts)
