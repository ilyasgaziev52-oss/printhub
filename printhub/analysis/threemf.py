"""Разбор 3MF-проектов (Bambu Studio / OrcaSlicer / Elegoo / Anycubic Slicer Next).

Нарезанный .gcode.3mf содержит Metadata/plate_N.gcode, Metadata/slice_info.config
(время и вес по каждой пластине), миниатюры Metadata/plate_N.png.
"""
from __future__ import annotations

import base64
import io
import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from .gcode import GcodeAnalyzer


@dataclass
class PlateFilament:
    id: str = ""
    type: str = ""
    color: str = ""
    used_m: float = 0.0
    used_g: float = 0.0
    tray_info_idx: str = ""


@dataclass
class PlateInfo:
    index: int
    gcode_path: str = ""
    gcode_md5: str = ""
    prediction_s: Optional[float] = None
    weight_g: Optional[float] = None
    filaments: list[PlateFilament] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    thumbnail_b64: str = ""
    printer_model_id: str = ""
    nozzle_diameter: Optional[float] = None
    support_used: Optional[bool] = None
    gcode_analysis: Optional[dict[str, Any]] = None


@dataclass
class ThreeMFInfo:
    is_sliced: bool = False
    plates: list[PlateInfo] = field(default_factory=list)
    printer_model: str = ""
    application: str = ""
    has_model: bool = False
    settings: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(v: Optional[str]) -> Optional[float]:
    try:
        return float(v) if v not in (None, "") else None
    except ValueError:
        return None


def parse_slice_info(xml_text: str) -> list[PlateInfo]:
    plates: list[PlateInfo] = []
    root = ET.fromstring(xml_text)
    for pel in root.iter("plate"):
        meta = {m.get("key"): m.get("value") for m in pel.findall("metadata")}
        try:
            idx = int(meta.get("index", len(plates) + 1))
        except ValueError:
            idx = len(plates) + 1
        p = PlateInfo(index=idx)
        p.prediction_s = _f(meta.get("prediction"))
        p.weight_g = _f(meta.get("weight"))
        p.printer_model_id = meta.get("printer_model_id", "") or ""
        p.nozzle_diameter = _f(meta.get("nozzle_diameters") or meta.get("nozzle_diameter"))
        if meta.get("support_used") is not None:
            p.support_used = meta.get("support_used", "").lower() == "true"
        for fel in pel.findall("filament"):
            p.filaments.append(PlateFilament(
                id=fel.get("id", ""), type=fel.get("type", ""), color=fel.get("color", ""),
                used_m=_f(fel.get("used_m")) or 0.0, used_g=_f(fel.get("used_g")) or 0.0,
                tray_info_idx=fel.get("tray_info_idx", ""),
            ))
        for oel in pel.findall("object"):
            if oel.get("name"):
                p.objects.append(oel.get("name", ""))
        plates.append(p)
    return plates


def analyze_3mf(path_or_bytes: str | bytes, analyze_gcode: bool = True) -> ThreeMFInfo:
    src = io.BytesIO(path_or_bytes) if isinstance(path_or_bytes, (bytes, bytearray)) else path_or_bytes
    info = ThreeMFInfo()
    with zipfile.ZipFile(src) as z:
        names = set(z.namelist())
        info.has_model = any(n.lower().startswith("3d/") and n.lower().endswith(".model") for n in names)
        gcodes = sorted((n for n in names if re.fullmatch(r"Metadata/plate_\d+\.gcode", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        info.is_sliced = bool(gcodes)

        plates: dict[int, PlateInfo] = {}
        if "Metadata/slice_info.config" in names:
            try:
                for p in parse_slice_info(z.read("Metadata/slice_info.config").decode("utf-8", "replace")):
                    plates[p.index] = p
            except ET.ParseError as e:
                info.warnings.append(f"slice_info.config повреждён: {e}")
        for g in gcodes:
            idx = int(re.findall(r"\d+", g)[-1])
            p = plates.setdefault(idx, PlateInfo(index=idx))
            p.gcode_path = g
            md5_name = g + ".md5"
            if md5_name in names:
                p.gcode_md5 = z.read(md5_name).decode("ascii", "replace").strip()
            if analyze_gcode:
                with z.open(g) as fh:
                    text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
                    ga = GcodeAnalyzer().analyze_stream(text)
                d = ga.to_dict()
                d.pop("thumbnail_png_b64", None)
                p.gcode_analysis = d
                if p.prediction_s is None:
                    p.prediction_s = ga.time_s
                if p.weight_g is None:
                    p.weight_g = ga.filament_g
        for p in plates.values():
            thumb = f"Metadata/plate_{p.index}.png"
            if thumb in names:
                p.thumbnail_b64 = base64.b64encode(z.read(thumb)).decode()

        if "Metadata/project_settings.config" in names:
            try:
                settings = json.loads(z.read("Metadata/project_settings.config"))
                keep = ("printer_model", "printer_settings_id", "filament_type", "filament_colour",
                        "nozzle_diameter", "layer_height", "sparse_infill_density", "print_settings_id",
                        "filament_settings_id", "curr_bed_type", "printable_area", "printable_height")
                info.settings = {k: settings[k] for k in keep if k in settings}
                info.printer_model = str(settings.get("printer_model", ""))
            except (ValueError, KeyError):
                info.warnings.append("project_settings.config не читается")
        if "3D/3dmodel.model" in names and not info.application:
            head = z.read("3D/3dmodel.model")[:4000].decode("utf-8", "replace")
            m = re.search(r'name="Application"[^>]*>([^<]+)<', head)
            if m:
                info.application = m.group(1)
        info.plates = [plates[k] for k in sorted(plates)]
    return info
