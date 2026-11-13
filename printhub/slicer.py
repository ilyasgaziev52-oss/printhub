"""Интеграция со слайсерами семейства Slic3r → Bambu Studio → OrcaSlicer (CLI).

OrcaSlicer умеет резать для Bambu Lab, Elegoo и Anycubic; Elegoo Slicer и Anycubic Slicer Next —
это форки OrcaSlicer с тем же CLI.
"""
from __future__ import annotations

import asyncio
import json
import os
import platform
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

SLICER_NAMES = {
    "orca-slicer": "OrcaSlicer", "orcaslicer": "OrcaSlicer", "bambu-studio": "Bambu Studio",
    "bambustudio": "Bambu Studio", "elegooslicer": "Elegoo Slicer", "elegoo-slicer": "Elegoo Slicer",
    "anycubicslicernext": "Anycubic Slicer Next",
}


def _candidates() -> list[Path]:
    sysname = platform.system()
    out: list[Path] = []
    if sysname == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            if not base:
                continue
            b = Path(base)
            out += [b / "OrcaSlicer" / "orca-slicer.exe", b / "Bambu Studio" / "bambu-studio.exe",
                    b / "BambuStudio" / "bambu-studio.exe", b / "ElegooSlicer" / "elegoo-slicer.exe",
                    b / "ElegooSlicer" / "ElegooSlicer.exe", b / "AnycubicSlicerNext" / "AnycubicSlicerNext.exe"]
    elif sysname == "Darwin":
        for app, exe in (("OrcaSlicer", "OrcaSlicer"), ("BambuStudio", "BambuStudio"),
                         ("ElegooSlicer", "ElegooSlicer"), ("AnycubicSlicerNext", "AnycubicSlicerNext")):
            out.append(Path(f"/Applications/{app}.app/Contents/MacOS/{exe}"))
    else:
        for name in ("orca-slicer", "OrcaSlicer", "bambu-studio", "elegoo-slicer", "AnycubicSlicerNext"):
            w = shutil.which(name)
            if w:
                out.append(Path(w))
        out += [Path("/opt/OrcaSlicer/bin/orca-slicer"), Path("/usr/bin/orca-slicer")]
        home = Path.home()
        for d in (home / "Applications", home / "Downloads", home):
            if d.is_dir():
                out += sorted(d.glob("OrcaSlicer*.AppImage")) + sorted(d.glob("Bambu*Studio*.AppImage"))
    return out


def _name_for(path: Path) -> str:
    stem = path.stem.lower().replace("_", "-").replace(" ", "-")
    for key, name in SLICER_NAMES.items():
        if stem.startswith(key):
            return name
    if "orca" in stem:
        return "OrcaSlicer"
    if "bambu" in stem:
        return "Bambu Studio"
    return path.stem


def find_slicers(custom: str = "") -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen = set()
    cands = ([Path(custom)] if custom else []) + _candidates()
    for c in cands:
        try:
            if c.is_file() and str(c) not in seen:
                seen.add(str(c))
                found.append({"name": _name_for(c), "path": str(c)})
        except OSError:
            continue
    return found


def profiles_root(slicer_path: str, override: str = "") -> Optional[Path]:
    if override:
        p = Path(override)
        return p if p.is_dir() else None
    if not slicer_path:
        sl = find_slicers()
        if not sl:
            return None
        slicer_path = sl[0]["path"]
    exe = Path(slicer_path)
    for cand in (exe.parent / "resources" / "profiles", exe.parent.parent / "Resources" / "profiles",
                 exe.parent.parent / "share" / "OrcaSlicer" / "profiles", exe.parent / "profiles"):
        if cand.is_dir():
            return cand
    return None


def list_profiles(slicer_path: str = "", override_dir: str = "", vendor: str = "") -> dict[str, Any]:
    root = profiles_root(slicer_path, override_dir)
    res: dict[str, Any] = {"dir": str(root or ""), "machines": [], "processes": [], "filaments": []}
    if not root:
        res["error"] = "Папка профилей слайсера не найдена"
        return res
    vendors = [root / vendor] if vendor else [d for d in root.iterdir() if d.is_dir()]
    wanted = {"BBL", "Elegoo", "Anycubic", "OrcaFilamentLibrary"}
    for vdir in vendors:
        if not vendor and vdir.name not in wanted:
            continue
        for sub, key in (("machine", "machines"), ("process", "processes"), ("filament", "filaments")):
            d = vdir / sub
            if not d.is_dir():
                continue
            for f in d.rglob("*.json"):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if str(data.get("instantiation", "true")).lower() != "true":
                    continue
                if data.get("type") == "machine_model":
                    continue
                res[key].append({"name": data.get("name", f.stem), "path": str(f), "vendor": vdir.name})
    for k in ("machines", "processes", "filaments"):
        res[k].sort(key=lambda x: (x["vendor"], x["name"]))
    return res


def _resolve_profile(path: str, root: Optional[Path]) -> dict[str, Any]:
    """Сворачивает цепочку inherits в один словарь (CLI нужны «плоские» профили)."""
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    parent_name = data.get("inherits")
    depth = 0
    while parent_name and depth < 10:
        depth += 1
        parent_file = None
        for d in [p.parent] + ([root] if root else []):
            if d is None:
                continue
            hits = list(Path(d).rglob(f"{parent_name}.json"))
            if hits:
                parent_file = hits[0]
                break
        if not parent_file:
            break
        parent = json.loads(parent_file.read_text(encoding="utf-8"))
        merged = dict(parent)
        merged.update(data)
        merged["inherits"] = parent.get("inherits", "")
        data = merged
        parent_name = data.get("inherits")
        p = parent_file
    data.pop("inherits", None)
    data["from"] = "User"
    return data


async def slice_with_orca(slicer_path: str, model_path: str, *, machine: str, process: str,
                          filaments: list[str], work_dir: str, arrange: bool = True, orient: bool = False,
                          profiles_dir: str = "", plate: int = 0, timeout: float = 900) -> tuple[str, str]:
    """Режет модель, возвращает (путь к .gcode.3mf, лог)."""
    if not slicer_path:
        sl = find_slicers()
        if not sl:
            raise ValueError("Слайсер не найден. Установите OrcaSlicer и укажите путь в настройках.")
        slicer_path = sl[0]["path"]
    if not machine or not process:
        raise ValueError("Выберите профиль принтера и профиль печати")
    root = profiles_root(slicer_path, profiles_dir)
    job = Path(work_dir) / uuid.uuid4().hex[:8]
    job.mkdir(parents=True, exist_ok=True)

    def flat(src: str, name: str) -> str:
        data = _resolve_profile(src, root)
        out = job / name
        out.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        return str(out)

    m_json = flat(machine, "machine.json")
    p_json = flat(process, "process.json")
    f_jsons = [flat(f, f"filament_{i}.json") for i, f in enumerate(filaments)] if filaments else []
    out_name = "result.gcode.3mf"
    model_abs = str(Path(model_path).resolve())
    cmd = [slicer_path, "--slice", str(plate), "--load-settings", f"{m_json};{p_json}",
           "--outputdir", str(job), "--export-3mf", out_name]
    if f_jsons:
        cmd += ["--load-filaments", ";".join(f_jsons)]
    if arrange:
        cmd += ["--arrange", "1"]
    if orient:
        cmd += ["--orient", "1"]
    cmd.append(model_abs)
    env = dict(os.environ)
    if platform.system() == "Linux" and not env.get("DISPLAY"):
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(job), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ValueError("Слайсер не уложился в отведённое время")
    log_text = out.decode("utf-8", "replace")
    result = job / out_name
    if proc.returncode != 0 or not result.exists():
        hits = sorted(job.glob("*.3mf"))
        if hits and proc.returncode == 0:
            result = hits[0]
        else:
            raise ValueError(f"Слайсер завершился с кодом {proc.returncode}:\n{log_text[-1500:]}")
    return str(result), log_text
