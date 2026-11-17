"""Готовый проект Bambu Studio (.3mf) из многочастной модели: цвета частей, пластины, проверка нарезкой.

Схема:
  1. Bambu Studio (CLI) открывает обычный 3MF (сборки из частей) и сохраняет его как свой проект
     с профилями принтера, печати и филаментов — это гарантирует «родной» формат.
  2. В проект дописываются: филамент каждой части, цвета филаментов, таблица промывки,
     раскладка объектов по пластинам, тип стола и поддержки.
  3. Контроль: Bambu Studio нарезает все пластины; если нарезка не прошла — проект не годится.

Нужен Bambu Studio с CLI (путь в BAMBU_CLI). Пример — в конце файла.
"""
from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

PROFILES = Path(os.environ.get("BAMBU_PROFILES", "/app/share/BambuStudio/profiles/BBL"))
GAP = 0.2  # LOGICAL_PART_PLATE_GAP в Bambu Studio


def _cols(n: int) -> int:
    v = math.sqrt(n)
    r = round(v)
    return r + 1 if v > r else r


def plate_center(index: int, n_plates: int, bed: tuple[float, float]) -> tuple[float, float]:
    cols = _cols(n_plates)
    row, col = divmod(index, cols)
    return col * bed[0] * (1 + GAP) + bed[0] / 2, -row * bed[1] * (1 + GAP) + bed[1] / 2


def _lum(c: str) -> float:
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def flatten_profiles(machine: str, process: str, filament: str, out: Path) -> tuple[str, str, str]:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from printhub.slicer import resolve_profile
    paths = []
    for sub, name, fn in (("machine", machine, "machine.json"), ("process", process, "process.json"),
                          ("filament", filament, "filament.json")):
        d = resolve_profile(str(PROFILES / sub / f"{name}.json"), PROFILES)
        d["from"] = "system"
        d.pop("is_custom_defined", None)
        p = out / fn
        p.write_text(json.dumps(d, ensure_ascii=False, indent=1))
        paths.append(str(p))
    return paths[0], paths[1], paths[2]


def finalize(exported: Path, dst: Path, spec: dict[str, Any]) -> None:
    """spec: colors, objects=[{plate, parts=[{name, filament}]}], bed, bed_type, supports."""
    colors: list[str] = spec["colors"]
    objects: list[dict[str, Any]] = spec["objects"]
    n_plates = max(o["plate"] for o in objects)
    bed = tuple(spec.get("bed", (256, 256)))
    zin = zipfile.ZipFile(exported)
    files = {i.filename: zin.read(i.filename) for i in zin.infolist()}

    ms = files["Metadata/model_settings.config"].decode()
    obj_blocks = re.findall(r'<object id="(\d+)">(.*?)</object>', ms, re.S)
    assert len(obj_blocks) == len(objects), f"объектов в проекте {len(obj_blocks)}, в описании {len(objects)}"
    # Bambu Studio не всегда сохраняет объекты в исходном порядке — сопоставляем по имени объекта
    by_name = {re.search(r'<metadata key="name" value="([^"]*)"/>', body).group(1): (oid, body) for oid, body in obj_blocks}
    if all(o.get("name") in by_name for o in objects) and len(by_name) == len(objects):
        obj_blocks = [by_name[o["name"]] for o in objects]
    new_ms = ms
    for (oid, body), o in zip(obj_blocks, objects):
        parts = re.findall(r'<part id="(\d+)"(.*?)</part>', body, re.S)
        assert len(parts) == len(o["parts"]), f"объект {oid}: частей {len(parts)}, в описании {len(o['parts'])}"
        new_body = body
        for (pid, pbody), pspec in zip(parts, o["parts"]):
            fixed = re.sub(r'<metadata key="name" value="[^"]*"/>',
                           f'<metadata key="name" value="{pspec["name"]}"/>\n      <metadata key="extruder" value="{pspec["filament"]}"/>',
                           pbody, count=1)
            new_body = new_body.replace(f'<part id="{pid}"{pbody}</part>', f'<part id="{pid}"{fixed}</part>')
        new_body = re.sub(r'<metadata key="extruder" value="\d+"/>', f'<metadata key="extruder" value="{o["parts"][0]["filament"]}"/>', new_body, count=1)
        if o.get("name"):
            new_body = re.sub(r'<metadata key="name" value="[^"]*"/>', f'<metadata key="name" value="{o["name"]}"/>', new_body, count=1)
        new_ms = new_ms.replace(f'<object id="{oid}">{body}</object>', f'<object id="{oid}">{new_body}</object>')
        o["_oid"] = oid
    plates = ""
    for pl in range(1, n_plates + 1):
        insts = "".join(f'\n    <model_instance>\n      <metadata key="object_id" value="{o["_oid"]}"/>\n'
                        f'      <metadata key="instance_id" value="0"/>\n      <metadata key="identify_id" value="{100 + int(o["_oid"])}"/>\n'
                        f'    </model_instance>' for o in objects if o["plate"] == pl)
        name = spec.get("plate_names", {}).get(str(pl), "")
        plates += (f'  <plate>\n    <metadata key="plater_id" value="{pl}"/>\n    <metadata key="plater_name" value="{name}"/>\n'
                   f'    <metadata key="locked" value="false"/>\n    <metadata key="filament_map_mode" value="Auto For Flush"/>\n'
                   f'    <metadata key="gcode_file" value=""/>{insts}\n  </plate>\n')
    new_ms = re.sub(r'  <plate>.*?</plate>\n', '', new_ms, flags=re.S)
    new_ms = new_ms.replace("  <assemble>", plates + "  <assemble>", 1)
    files["Metadata/model_settings.config"] = new_ms.encode()

    mm = files["3D/3dmodel.model"].decode()
    for o in objects:
        x, y = plate_center(o["plate"] - 1, n_plates, bed)
        x += o.get("offset", (0, 0))[0]
        y += o.get("offset", (0, 0))[1]
        # меняем только X и Y; Z оставляем как сохранил Bambu Studio (у объекта из одной части там половина высоты)
        mm = re.sub(rf'(<item objectid="{o["_oid"]}"[^>]*transform="1 0 0 0 1 0 0 0 1 )\S+ \S+ (\S+)"', rf'\g<1>{x:g} {y:g} \g<2>"', mm)
    files["3D/3dmodel.model"] = mm.encode()

    ps = json.loads(files["Metadata/project_settings.config"])
    n = len(colors)
    ps["filament_colour"] = colors
    ps["filament_map"] = ["1"] * n
    ps["flush_volumes_matrix"] = [str(0 if i == j else int(round(200 + 560 * max(0.0, _lum(colors[j]) - _lum(colors[i])))))
                                  for i in range(n) for j in range(n)]
    ps["curr_bed_type"] = spec.get("bed_type", "Textured PEI Plate")
    if spec.get("supports"):
        ps["enable_support"] = "1"
        ps["support_type"] = spec["supports"]
    if spec.get("print_sequence"):
        ps["print_sequence"] = spec["print_sequence"]   # «by object»: детали пластины печатаются по очереди
    ps["flush_into_infill"] = "1"   # промывка при смене цвета уходит в заполнение, а не в отходы
    files["Metadata/project_settings.config"] = json.dumps(ps, ensure_ascii=False, indent=4).encode()

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)


def slice_report(cli: list[str], project: Path, workdir: Path) -> list[dict[str, Any]]:
    out = workdir / "slice_out"
    out.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cli + ["--slice", "0", "--outputdir", str(out), "--export-3mf", "sliced.gcode.3mf", str(project)],
                       capture_output=True, text=True, timeout=1800)
    res = json.loads((out / "result.json").read_text()) if (out / "result.json").exists() else {}
    if r.returncode != 0 or not (out / "sliced.gcode.3mf").exists():
        raise RuntimeError(f"Bambu Studio не нарезал проект: {res.get('error_string') or r.stdout[-800:]}")
    zf = zipfile.ZipFile(out / "sliced.gcode.3mf")
    s = zf.read("Metadata/slice_info.config").decode()
    max_z = {}
    for n in zf.namelist():
        mt = re.fullmatch(r"Metadata/plate_(\d+)\.gcode", n)
        if mt:
            head = zf.open(n).read(4000).decode(errors="ignore")
            mz = re.search(r"; max_z_height: ([\d.]+)", head)
            max_z[int(mt.group(1))] = float(mz.group(1)) if mz else None
    plates = []
    for p in re.findall(r"<plate>.*?</plate>", s, re.S):
        g = lambda k: (re.search(rf'key="{k}" value="([^"]*)"', p) or [None, None])[1]  # noqa: E731
        fil = [dict(re.findall(r'(\w+)="([^"]*)"', f)) for f in re.findall(r"<filament [^>]*/>", p)]
        plates.append({"plate": int(g("index")), "time_s": int(g("prediction")), "weight_g": float(g("weight")),
                       "max_z": max_z.get(int(g("index"))),
                       "filaments": [(f["id"], f["color"], float(f["used_g"])) for f in fil]})
    return plates


def build(generic_3mf: Path, dst: Path, spec: dict[str, Any], cli: list[str], printer: str, process: str, filament: str) -> list[dict[str, Any]]:
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        m, p, f = flatten_profiles(printer, process, filament, tdp)
        n = len(spec["colors"])
        r = subprocess.run(cli + ["--load-settings", f"{m};{p}", "--load-filaments", ";".join([f] * n), "--arrange", "0",
                                  "--outputdir", str(tdp), "--export-3mf", "exported.3mf", str(generic_3mf)],
                           capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not (tdp / "exported.3mf").exists():
            raise RuntimeError("Bambu Studio не смог сохранить проект:\n" + r.stdout[-1500:])
        finalize(tdp / "exported.3mf", dst, spec)
        rep = slice_report(cli, dst, tdp)
        # контроль: высота нарезки каждой пластины = высоте самой высокой детали на ней (ничего не ушло под стол)
        for p in rep:
            hs = [o["height"] for o in spec["objects"] if o["plate"] == p["plate"] and "height" in o]
            if hs and p["max_z"] is not None:
                assert abs(p["max_z"] - max(hs)) < 0.35, f"пластина {p['plate']}: нарезано до {p['max_z']} мм, а деталь {max(hs):.1f} мм"
        return rep


if __name__ == "__main__":
    # python models/bambu_project.py <обычный.3mf> <проект.3mf> <spec.json>   (BAMBU_CLI="путь к bambu-studio")
    spec = json.loads(Path(sys.argv[3]).read_text())
    cli = os.environ.get("BAMBU_CLI", "bambu-studio").split()
    rep = build(Path(sys.argv[1]), Path(sys.argv[2]), spec, cli, spec.get("printer", "Bambu Lab A1 0.4 nozzle"),
                spec.get("process", "0.16mm Optimal @BBL A1"), spec.get("filament", "Bambu PLA Basic @BBL A1"))
    for p in rep:
        print(f"пластина {p['plate']}: {p['time_s'] // 3600} ч {p['time_s'] % 3600 // 60} мин, {p['weight_g']} г —",
              ", ".join(f"#{i} {c} {g} г" for i, c, g in p["filaments"]))
