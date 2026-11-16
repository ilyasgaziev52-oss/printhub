"""Нарезка через внешние слайсеры (CLI).

FDM: OrcaSlicer (подходит для Bambu Lab, Elegoo и Anycubic), а также Bambu Studio, Elegoo Slicer,
Anycubic Slicer Next — у всех общий CLI. Результат — .gcode.3mf (для Bambu отправляется целиком,
для остальных PrintHub извлекает G-code пластины).

Смола: PrusaSlicer (--export-sla → .sl1) + UVtoolsCmd (convert → .goo/.ctb/.pwmx/.pm4n/.pm5…).
Профили принтеров для PrusaSlicer поставляются вместе с UVtools (Assets/PrusaSlicer).
"""
from __future__ import annotations

import asyncio
import ctypes
import json
import os
import platform
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Optional

BED_TYPES = ["Textured PEI Plate", "Cool Plate", "Engineering Plate", "High Temp Plate",
             "Textured Cool Plate", "Supertack Plate"]

EXIT_CODES = {
    -1: "ошибка окружения", -2: "неверные параметры", -3: "файл не найден", -5: "профиль не найден или повреждён",
    -6: "модель не читается", -13: "не удалось сохранить 3MF", -15: "профиль принтера несовместим с проектом",
    -16: "профиль печати несовместим с проектом", -17: "профиль несовместим с проектом",
    -21: "не удалось расставить модели", -22: "не удалось ориентировать модели",
    -50: "на пластине нечего печатать", -51: "ошибка проверки проекта", -52: "модель выходит за пределы стола",
    -58: "превышен лимит времени", -59: "превышен лимит треугольников",
    -61: "тип стола не подходит к филаменту (выберите другой тип стола)",
    -62: "несовместимые температуры филаментов", -63: "столкновение с соплом/экструдером",
    -64: "столкновение объектов", -65: "неверные параметры режима «вазы»", -100: "ошибка нарезки",
    -101: "конфликт путей G-code", -102: "G-code выходит в непечатаемую область",
}

SLICER_NAMES = {
    "orca-slicer": "OrcaSlicer", "orcaslicer": "OrcaSlicer", "bambu-studio": "Bambu Studio",
    "bambustudio": "Bambu Studio", "elegooslicer": "Elegoo Slicer", "elegoo-slicer": "Elegoo Slicer",
    "anycubicslicernext": "Anycubic Slicer Next",
}


def normalize_rc(rc: int) -> int:
    if platform.system() == "Windows":
        return ctypes.c_int32(rc).value
    return rc - 256 if rc > 127 else rc


# ---------------------------------------------------------------- поиск программ
def _fdm_candidates() -> list[Path]:
    sysname = platform.system()
    out: list[Path] = []
    if sysname == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"), os.environ.get("ProgramFiles(x86)", "")):
            if not base:
                continue
            b = Path(base)
            out += [b / "OrcaSlicer" / "orca-slicer.exe", b / "Bambu Studio" / "bambu-studio.exe",
                    b / "BambuStudio" / "bambu-studio.exe", b / "ElegooSlicer" / "elegoo-slicer.exe",
                    b / "AnycubicSlicerNext" / "AnycubicSlicerNext.exe"]
    elif sysname == "Darwin":
        for app in ("OrcaSlicer", "BambuStudio", "ElegooSlicer"):
            out.append(Path(f"/Applications/{app}.app/Contents/MacOS/{app}"))
    else:
        for name in ("orca-slicer", "OrcaSlicer", "bambu-studio", "elegoo-slicer", "AnycubicSlicerNext"):
            w = shutil.which(name)
            if w:
                out.append(Path(w))
        out += [Path("/usr/bin/AnycubicSlicerNext")]
        home = Path.home()
        for d in (home / "Applications", home / "Downloads", home / "squashfs-root"):
            if d.is_dir():
                out += sorted(d.glob("OrcaSlicer*.AppImage")) + sorted(d.glob("Bambu*Studio*.AppImage"))
                out += sorted(d.glob("AppRun"))
    return out


def _name_for(path: Path) -> str:
    stem = path.stem.lower().replace("_", "-").replace(" ", "-")
    for key, name in SLICER_NAMES.items():
        if stem.startswith(key):
            return name
    if "orca" in stem or "orca" in str(path).lower():
        return "OrcaSlicer"
    if "bambu" in stem:
        return "Bambu Studio"
    return path.stem


def find_slicers(custom: str = "") -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    seen: set[str] = set()
    for c in ([Path(custom)] if custom else []) + _fdm_candidates():
        try:
            if c.is_file() and str(c) not in seen:
                seen.add(str(c))
                found.append({"name": _name_for(c), "path": str(c)})
        except OSError:
            continue
    return found


def find_prusaslicer(custom: str = "") -> str:
    cands = [custom] if custom else []
    if platform.system() == "Windows":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        cands += [rf"{pf}\Prusa3D\PrusaSlicer\prusa-slicer-console.exe", rf"{pf}\PrusaSlicer\prusa-slicer-console.exe"]
    elif platform.system() == "Darwin":
        cands += ["/Applications/Original Prusa Drivers/PrusaSlicer.app/Contents/MacOS/PrusaSlicer",
                  "/Applications/PrusaSlicer.app/Contents/MacOS/PrusaSlicer"]
    cands += [shutil.which("prusa-slicer") or "", shutil.which("PrusaSlicer") or ""]
    return next((c for c in cands if c and Path(c).is_file()), "")


def find_uvtools(custom: str = "") -> str:
    cands = [custom] if custom else []
    if platform.system() == "Windows":
        cands += [os.path.expandvars(r"%LOCALAPPDATA%\Programs\UVtools\UVtoolsCmd.exe"),
                  os.path.expandvars(r"%ProgramFiles%\UVtools\UVtoolsCmd.exe")]
    elif platform.system() == "Darwin":
        cands += ["/Applications/UVtools.app/Contents/MacOS/UVtoolsCmd"]
    cands += [shutil.which("UVtoolsCmd") or "", "/opt/uvtools/UVtoolsCmd", "/usr/bin/UVtoolsCmd"]
    return next((c for c in cands if c and Path(c).is_file()), "")


# ---------------------------------------------------------------- профили FDM
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
                 exe.parent.parent / "share" / "OrcaSlicer" / "profiles", exe.parent / "profiles",
                 exe.parent.parent / "share" / "AnycubicSlicerNext" / "resources" / "profiles"):
        if cand.is_dir():
            return cand
    return None


def list_profiles(slicer_path: str = "", override_dir: str = "", vendor: str = "") -> dict[str, Any]:
    root = profiles_root(slicer_path, override_dir)
    res: dict[str, Any] = {"dir": str(root or ""), "machines": [], "processes": [], "filaments": [],
                           "bed_types": BED_TYPES}
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
                if str(data.get("instantiation", "true")).lower() != "true" or data.get("type") == "machine_model":
                    continue
                if str(data.get("name", "")).startswith(("fdm_", "fdm-")):
                    continue
                res[key].append({"name": data.get("name", f.stem), "path": str(f), "vendor": vdir.name})
    for k in ("machines", "processes", "filaments"):
        res[k].sort(key=lambda x: (x["vendor"], x["name"]))
    if not res["machines"] and any(root.glob("*.opc")):
        res["error"] = ("В этой версии слайсера профили упакованы (.opc). Скачайте папку resources/profiles той же "
                        "версии с GitHub OrcaSlicer и укажите её в настройках «Папка профилей».")
    return res


def _find_by_name(name: str, dirs: list[Path]) -> Optional[Path]:
    for d in dirs:
        if not d or not d.is_dir():
            continue
        direct = list(d.rglob(f"{name}.json"))
        if direct:
            return direct[0]
    # имя файла может не совпадать с полем name
    for d in dirs:
        if not d or not d.is_dir():
            continue
        for f in d.rglob("*.json"):
            try:
                if json.loads(f.read_text(encoding="utf-8")).get("name") == name:
                    return f
            except (ValueError, OSError):
                continue
    return None


def resolve_profile(path: str, root: Optional[Path] = None) -> dict[str, Any]:
    """«Плоский» профиль: цепочка inherits и шаблоны include сливаются в один словарь."""
    p = Path(path)
    search = [p.parent, p.parent.parent] + ([root] if root else [])

    def load(fp: Path, depth: int = 0) -> dict[str, Any]:
        data = json.loads(fp.read_text(encoding="utf-8"))
        base: dict[str, Any] = {}
        parent = data.get("inherits")
        if parent and depth < 12:
            pf = _find_by_name(parent, [fp.parent] + search)
            if pf:
                base.update(load(pf, depth + 1))
        for inc in data.get("include", []) or []:
            inc_f = _find_by_name(inc, [fp.parent] + search)
            if inc_f:
                base.update(load(inc_f, depth + 1))
        base.update(data)
        return base

    data = load(p)
    data.pop("inherits", None)
    data.pop("include", None)
    data["from"] = "User"
    data.setdefault("is_custom_defined", "0")
    return data


async def slice_with_orca(slicer_path: str, model_path: str, *, machine: str, process: str,
                          filaments: list[str], work_dir: str, arrange: bool = True, orient: bool = False,
                          profiles_dir: str = "", plate: int = 0, bed_type: str = "",
                          timeout: float = 900) -> tuple[str, str]:
    """Режет модель, возвращает (путь к .gcode.3mf, лог)."""
    if not slicer_path:
        sl = find_slicers()
        if not sl:
            raise ValueError("Слайсер не найден. Установите OrcaSlicer и укажите путь в настройках.")
        slicer_path = sl[0]["path"]
    if not machine or not process:
        raise ValueError("Выберите профиль принтера и профиль печати")
    root = profiles_root(slicer_path, profiles_dir)
    job = (Path(work_dir) / uuid.uuid4().hex[:8]).resolve()
    out_dir = job / "out"
    datadir = Path(work_dir).resolve() / "orca-data"
    for d in (job, out_dir, datadir):
        d.mkdir(parents=True, exist_ok=True)

    def flat(src: str, name: str) -> str:
        out = job / name
        out.write_text(json.dumps(resolve_profile(src, root), ensure_ascii=False, indent=1), encoding="utf-8")
        return str(out)

    m_json = flat(machine, "machine.json")
    p_json = flat(process, "process.json")
    f_jsons = [flat(f, f"filament_{i}.json") for i, f in enumerate(filaments) if f]
    out_name = "result.gcode.3mf"
    cmd = [slicer_path, "--datadir", str(datadir), "--load-settings", f"{m_json};{p_json}"]
    if f_jsons:
        cmd += ["--load-filaments", ";".join(f_jsons)]
    if bed_type:
        cmd += ["--curr-bed-type", bed_type]
    cmd += ["--orient", "1" if orient else "0", "--arrange", "1" if arrange else "0",
            "--slice", str(plate), "--outputdir", str(out_dir), "--export-3mf", out_name,
            str(Path(model_path).resolve())]
    env = dict(os.environ)
    env.setdefault("LC_NUMERIC", "C")
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(job), stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT, env=env)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ValueError("Слайсер не уложился в отведённое время")
    log_text = out.decode("utf-8", "replace")
    rc = normalize_rc(proc.returncode or 0)
    result = out_dir / out_name
    if rc != 0 or not result.exists():
        reason = EXIT_CODES.get(rc, "")
        rj = out_dir / "result.json"
        if rj.exists():
            try:
                reason = json.loads(rj.read_text()).get("error_string") or reason
            except ValueError:
                pass
        raise ValueError(f"Слайсер завершился с кодом {rc}{': ' + reason if reason else ''}\n{log_text[-1500:]}")
    return str(result), log_text


# ---------------------------------------------------------------- смола
def resin_profiles(uvtools_path: str = "", override_dir: str = "") -> dict[str, Any]:
    """Профили PrusaSlicer для фотополимерных принтеров из комплекта UVtools."""
    roots: list[Path] = []
    if override_dir:
        roots.append(Path(override_dir))
    if uvtools_path:
        exe = Path(uvtools_path)
        roots += [exe.parent / "Assets" / "PrusaSlicer", exe.parent.parent / "Resources" / "Assets" / "PrusaSlicer"]
    root = next((r for r in roots if r.is_dir()), None)
    res: dict[str, Any] = {"dir": str(root or ""), "printers": [], "prints": []}
    if not root:
        res["error"] = "Профили PrusaSlicer из UVtools не найдены (папка Assets/PrusaSlicer)"
        return res
    for sub, key in (("printer", "printers"), ("sla_print", "prints")):
        d = root / sub
        if d.is_dir():
            for f in sorted(d.glob("*.ini")):
                name = f.stem
                if key == "printers" and not re.search(r"elegoo|anycubic|uniformation|phrozen|creality", name, re.I):
                    continue
                res[key].append({"name": name, "path": str(f)})
    return res


async def slice_resin(prusaslicer: str, uvtools: str, model_path: str, *, printer_ini: str, print_ini: str,
                      work_dir: str, exposure_s: Optional[float] = None, bottom_exposure_s: Optional[float] = None,
                      bottom_layers: Optional[int] = None, target: str = "auto",
                      timeout: float = 1800) -> tuple[str, str]:
    """STL → .sl1 (PrusaSlicer) → формат принтера (UVtools). Возвращает (путь, лог)."""
    prusaslicer = prusaslicer or find_prusaslicer()
    uvtools = uvtools or find_uvtools()
    if not prusaslicer:
        raise ValueError("PrusaSlicer не найден — установите его и укажите путь в настройках")
    if not uvtools:
        raise ValueError("UVtools не найден — установите его и укажите путь в настройках")
    job = (Path(work_dir) / uuid.uuid4().hex[:8]).resolve()
    job.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[{}]", "_", Path(model_path).stem)
    sl1 = job / f"{stem}.sl1"
    cmd = [prusaslicer, "--export-sla", "--load", printer_ini, "--load", print_ini]
    if exposure_s:
        cmd += ["--exposure-time", str(exposure_s)]
    if bottom_exposure_s:
        cmd += ["--initial-exposure-time", str(bottom_exposure_s)]
    if bottom_layers:
        cmd += ["--faded-layers", str(bottom_layers)]
    cmd += ["--output", str(sl1), str(Path(model_path).resolve())]
    logs = []
    for step in (cmd, [uvtools, "convert", str(sl1), target] + ([] if target == "auto" else [str(job / f"{stem}.{target.lower()}")])):
        proc = await asyncio.create_subprocess_exec(*step, cwd=str(job), stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise ValueError("Нарезка не уложилась в отведённое время")
        text = out.decode("utf-8", "replace")
        logs.append(text)
        if step is cmd and not sl1.exists():
            raise ValueError(f"PrusaSlicer не создал файл:\n{text[-1500:]}")
        if step[0] == uvtools and normalize_rc(proc.returncode or 0) not in (0,):
            raise ValueError(f"UVtools: ошибка конвертации:\n{text[-1500:]}")
    produced = [f for f in job.iterdir() if f.is_file() and f.suffix.lower() not in (".sl1", ".sl1s", ".ini")]
    if not produced:
        raise ValueError("UVtools не создал файл (проверьте FILEFORMAT_ в профиле принтера):\n" + logs[-1][-1500:])
    return str(produced[0]), "\n".join(logs)


# ---------------------------------------------------------------- автоподбор профилей
def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _compatible(path: str) -> list[str]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    comp = data.get("compatible_printers") or []
    return comp if isinstance(comp, list) else []


def auto_profiles(slicer_path: str, profiles_dir: str, printer_model: str,
                  material: str = "PLA") -> dict[str, Any]:
    """Подбирает профили принтера, печати и филамента по названию модели принтера."""
    prof = list_profiles(slicer_path, profiles_dir)
    if prof.get("error") and not prof["machines"]:
        return {"error": prof["error"]}
    model = _norm(printer_model)
    if not model:
        return {"error": "У принтера не указана модель — выберите профили вручную"}
    machine = None
    for m in prof["machines"]:  # точное совпадение «<модель> 0.4 nozzle»
        if _norm(m["name"]) == f"{model} 0 4 nozzle":
            machine = m
            break
    if machine is None:
        cands = [m for m in prof["machines"] if _norm(m["name"]).startswith(model + " ")
                 and re.fullmatch(r"0 4( nozzle)?", _norm(m["name"])[len(model):].strip())]
        machine = cands[0] if cands else None
    if machine is None:
        return {"error": f"Не найден профиль принтера для «{printer_model}» — выберите вручную"}
    mname = machine["name"]
    procs = [p for p in prof["processes"] if mname in _compatible(p["path"])]
    process = (next((p for p in procs if "0.20mm standard" in p["name"].lower()), None)
               or next((p for p in procs if "0.20mm" in p["name"].lower()), None)
               or (procs[0] if procs else None))
    if process is None:
        return {"error": f"Не найден профиль печати для «{mname}» — выберите вручную"}
    mat = material.upper()
    fils = [f for f in prof["filaments"] if mat in f["name"].upper()]
    compat = [f for f in fils if mname in _compatible(f["path"])]
    pick = compat or [f for f in fils if not _compatible(f["path"])]
    filament = (next((f for f in pick if "basic" in f["name"].lower() or "generic" in f["name"].lower()), None)
                or (pick[0] if pick else None))
    bed = "Textured PEI Plate" if printer_model.lower().startswith("bambu") else ""
    return {"machine": machine, "process": process, "filament": filament, "bed_type": bed}


def auto_resin_profiles(uvtools_path: str, override_dir: str, printer_model: str) -> dict[str, Any]:
    prof = resin_profiles(uvtools_path, override_dir)
    if prof.get("error"):
        return {"error": prof["error"]}
    words = set(_norm(printer_model).split())
    best, score = None, 0
    for p in prof["printers"]:
        s = len(words & set(_norm(p["name"]).split()))
        if s > score:
            best, score = p, s
    if not best or score < 2:
        return {"error": f"Не найден профиль смолы для «{printer_model}» — выберите вручную"}
    prints = prof["prints"]
    pr = (next((p for p in prints if "0.05" in p["name"] and "medium" in p["name"].lower()), None)
          or next((p for p in prints if "0.05" in p["name"]), None) or (prints[0] if prints else None))
    if not pr:
        return {"error": "Не найдены профили печати смолы"}
    return {"printer": best, "print": pr}
