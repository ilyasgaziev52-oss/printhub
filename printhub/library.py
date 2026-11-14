"""Библиотека файлов: приём, определение типа, анализ."""
from __future__ import annotations

import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any, Optional

from .analysis.lazy import mesh as meshmod
from .analysis.gcode import GcodeAnalyzer
from .analysis.threemf import analyze_3mf

log = logging.getLogger("printhub.library")

GCODE_EXT = (".gcode", ".gco", ".g", ".gc")
RESIN_EXT = (".goo", ".ctb", ".cbddlp", ".photon", ".pwmx", ".pwma", ".pwms", ".pwmo", ".pws", ".pw0",
             ".pwx", ".pwmb", ".pm3", ".pm3m", ".pm3r", ".pm4n", ".pm4u", ".pm5", ".pm5s", ".pm7", ".pm7m",
             ".pwc", ".pwsz", ".prz", ".jxs", ".zip", ".sl1", ".sl1s")
MESH_EXT = (".stl", ".obj", ".3mf", ".ply", ".off", ".glb", ".gltf")


def safe_filename(name: str) -> str:
    name = os.path.basename(name.replace("\\", "/")).strip()
    name = re.sub(r"[^\w\-. ()\[\]]+", "_", name, flags=re.UNICODE)
    return name[:180] or "file"


def detect_kind(path: str, filename: str) -> str:
    low = filename.lower()
    if low.endswith(GCODE_EXT):
        return "gcode"
    if low.endswith(".3mf"):
        try:
            with zipfile.ZipFile(path) as z:
                if any(re.fullmatch(r"Metadata/plate_\d+\.gcode", n) for n in z.namelist()):
                    return "3mf"
        except zipfile.BadZipFile:
            return "other"
        return "mesh"
    if low.endswith(MESH_EXT):
        return "mesh"
    if low.endswith(RESIN_EXT) and not low.endswith(".zip"):
        return "resin"
    return "other"


def analyze_path(path: str, filename: str, build_volume: Optional[list[float]] = None,
                 technology: str = "fdm") -> tuple[dict[str, Any], str]:
    """Возвращает (analysis, thumbnail_b64)."""
    kind = detect_kind(path, filename)
    analysis: dict[str, Any] = {"kind": kind, "analyzed_at": time.time()}
    thumb = ""
    try:
        if kind == "gcode":
            ga = GcodeAnalyzer().analyze_file(path)
            thumb = ga.thumbnail_png_b64
            d = ga.to_dict()
            d.pop("thumbnail_png_b64", None)
            analysis["gcode"] = d
            analysis["technology"] = "fdm"
        elif kind == "3mf":
            info = analyze_3mf(path)
            d = info.to_dict()
            for p in d["plates"]:
                if p.get("thumbnail_b64") and not thumb:
                    thumb = p["thumbnail_b64"]
                p.pop("thumbnail_b64", None)
            analysis["threemf"] = d
            analysis["technology"] = "fdm"
        elif kind == "mesh":
            m = meshmod.load_mesh(path, file_type=Path(filename).suffix.lstrip(".").lower())
            rep = meshmod.analyze_mesh(m, build_volume)
            analysis["mesh"] = rep.to_dict()
            analysis["estimate_fdm"] = meshmod.estimate_fdm_from_mesh(m, meshmod.FdmEstimateParams())
            analysis["estimate_resin"] = meshmod.estimate_resin_from_mesh(m, meshmod.ResinEstimateParams())
            analysis["estimate"] = analysis["estimate_resin"] if technology == "resin" else analysis["estimate_fdm"]
        elif kind == "resin":
            from .analysis.resin_files import read_resin_file
            info = read_resin_file(path)
            thumb = info.pop("thumbnail_b64", "") or ""
            analysis["resin"] = info
            analysis["technology"] = "resin"
    except Exception as e:  # noqa: BLE001
        log.exception("анализ %s", filename)
        analysis["error"] = f"{type(e).__name__}: {e}"
    return analysis, thumb


def store_upload(tmp_path: str, filename: str, library_dir: Path) -> dict[str, Any]:
    lid = uuid.uuid4().hex[:12]
    fname = safe_filename(filename)
    dest_dir = library_dir / lid
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / fname
    shutil.move(tmp_path, dest)
    return {"id": lid, "filename": fname, "stored_path": str(dest), "size": dest.stat().st_size,
            "uploaded_at": time.time()}
