"""Кнопка «Подготовить к печати»: ремонт → ориентация → размещение → нарезка с автоподбором профилей."""
import json
import stat
import sys

import httpx
import pytest
import trimesh

from printhub import slicer
from printhub.app import create_app

FAKE_ORCA = """#!{py}
import sys, json, zipfile, os
a = sys.argv[1:]
out = a[a.index('--outputdir') + 1]; name = a[a.index('--export-3mf') + 1]
settings = a[a.index('--load-settings') + 1].split(';')
m = json.load(open(settings[0]))
assert m.get('printable_height') == '265', m
proc = json.load(open(settings[1]))
open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'last_process.json'), 'w').write(json.dumps(proc))
with zipfile.ZipFile(os.path.join(out, name), 'w') as z:
    z.writestr('Metadata/plate_1.gcode', '; total estimated time: 42m 0s\\nG1 X1 E1\\n')
    z.writestr('Metadata/slice_info.config', '<config><plate><metadata key="index" value="1"/><metadata key="prediction" value="2520"/><metadata key="weight" value="12.5"/></plate></config>')
"""


def make_profiles(root):
    m = root / "Elegoo" / "machine"
    p = root / "Elegoo" / "process"
    f = root / "Elegoo" / "filament"
    for d in (m, p, f):
        d.mkdir(parents=True)
    (m / "fdm_elegoo_common.json").write_text(json.dumps({"name": "fdm_elegoo_common", "type": "machine", "instantiation": "false",
                                                           "printable_height": "250"}))
    (m / "Elegoo Neptune 4 Pro 0.4 nozzle.json").write_text(json.dumps({"name": "Elegoo Neptune 4 Pro 0.4 nozzle", "type": "machine",
                                                                         "inherits": "fdm_elegoo_common", "printable_height": "265"}))
    (m / "Elegoo Neptune 4 Pro 0.6 nozzle.json").write_text(json.dumps({"name": "Elegoo Neptune 4 Pro 0.6 nozzle", "type": "machine"}))
    (p / "0.16mm Optimal @Elegoo N4Pro.json").write_text(json.dumps({"name": "0.16mm Optimal @Elegoo N4Pro", "type": "process",
                                                                      "compatible_printers": ["Elegoo Neptune 4 Pro 0.4 nozzle"]}))
    (p / "0.20mm Standard @Elegoo N4Pro.json").write_text(json.dumps({"name": "0.20mm Standard @Elegoo N4Pro", "type": "process",
                                                                       "compatible_printers": ["Elegoo Neptune 4 Pro 0.4 nozzle"]}))
    (f / "Elegoo PETG @N4Pro.json").write_text(json.dumps({"name": "Elegoo PETG @N4Pro", "type": "filament",
                                                            "compatible_printers": ["Elegoo Neptune 4 Pro 0.4 nozzle"]}))
    (f / "Elegoo PLA Basic @N4Pro.json").write_text(json.dumps({"name": "Elegoo PLA Basic @N4Pro", "type": "filament",
                                                                 "compatible_printers": ["Elegoo Neptune 4 Pro 0.4 nozzle"]}))


def test_auto_profiles(tmp_path):
    make_profiles(tmp_path)
    r = slicer.auto_profiles("", str(tmp_path), "Elegoo Neptune 4 Pro")
    assert r["machine"]["name"] == "Elegoo Neptune 4 Pro 0.4 nozzle"
    assert r["process"]["name"] == "0.20mm Standard @Elegoo N4Pro"
    assert r["filament"]["name"] == "Elegoo PLA Basic @N4Pro" and r["bed_type"] == ""
    assert slicer.auto_profiles("", str(tmp_path), "Elegoo Neptune 4 Pro", "PETG")["filament"]["name"] == "Elegoo PETG @N4Pro"
    assert "error" in slicer.auto_profiles("", str(tmp_path), "Bambu Lab A1")


@pytest.mark.skipif(sys.platform == "win32", reason="скрипт-заглушка для POSIX")
async def test_prepare_endpoint(tmp_path):
    make_profiles(tmp_path / "profiles")
    fake = tmp_path / "orca-slicer"
    fake.write_text(FAKE_ORCA.format(py=sys.executable))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    app = create_app(str(tmp_path / "data"))
    app.state.db.update_settings({"slicer_path": str(fake), "slicer_profiles_dir": str(tmp_path / "profiles")})
    from printhub.models import PrinterConfig
    app.state.db.save_printer(PrinterConfig(id="n4", name="N4", kind="moonraker", host="127.0.0.1", port=1,
                                            model="Elegoo Neptune 4 Pro", build_volume=[225, 225, 265]))
    # лежащий цилиндр с дырой: должен исправиться и встать на торец
    cyl = trimesh.creation.cylinder(radius=8, height=60, sections=48)
    cyl.apply_transform(trimesh.transformations.rotation_matrix(1.5708, [1, 0, 0]))
    broken = trimesh.Trimesh(cyl.vertices, cyl.faces[3:], process=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=120) as c:
        item = (await c.post("/api/library", files={"file": ("tube.stl", broken.export(file_type="stl"), "model/stl")})).json()
        r = (await c.post(f"/api/library/{item['id']}/prepare", json={"printer_id": "n4", "options": {}})).json()
        names = [s["step"] for s in r["steps"]]
        assert "Исправление модели" in names and "Ориентация" in names, r["steps"]
        assert r["printable"] and r["report"]["size_mm"][2] == pytest.approx(60, abs=0.5)
        assert r["sliced_item_id"] and r["summary"]["time_s"] == 2520 and r["summary"]["material_g"] == 12.5
        assert all(s["ok"] for s in r["steps"]), r["steps"]
        sup = next(s for s in r["steps"] if s["step"] == "Поддержки")
        assert "не нужны" in sup["detail"]  # цилиндр встал на торец — нависаний нет
        assert json.loads((tmp_path / "last_process.json").read_text())["enable_support"] == "0"
        # «гриб»: широкая шляпка на тонкой ножке — нужны поддержки
        mush = trimesh.util.concatenate([trimesh.creation.cylinder(radius=4, height=30).apply_translation([0, 0, 15]),
                                         trimesh.creation.cylinder(radius=20, height=6).apply_translation([0, 0, 33])])
        item2 = (await c.post("/api/library", files={"file": ("mush.stl", mush.export(file_type="stl"), "model/stl")})).json()
        r3 = (await c.post(f"/api/library/{item2['id']}/prepare", json={"printer_id": "n4", "options": {"orient": "none"}})).json()
        sup = next(s for s in r3["steps"] if s["step"] == "Поддержки")
        assert "включены: древовидные" in sup["detail"], r3["steps"]
        proc = json.loads((tmp_path / "last_process.json").read_text())
        assert proc["enable_support"] == "1" and proc["support_type"] == "tree(auto)"
        r4 = (await c.post(f"/api/library/{item2['id']}/prepare", json={"printer_id": "n4", "options": {
            "orient": "none", "supports": "on", "support_type": "normal", "support_plate_only": True}})).json()
        proc = json.loads((tmp_path / "last_process.json").read_text())
        assert proc["support_type"] == "normal(auto)" and proc["support_on_build_plate_only"] == "1", r4["steps"]
        # без принтера — только подготовка, без нарезки
        r2 = (await c.post(f"/api/library/{item['id']}/prepare", json={"options": {"slice": False}})).json()
        assert r2["sliced_item_id"] is None and r2["printable"]
