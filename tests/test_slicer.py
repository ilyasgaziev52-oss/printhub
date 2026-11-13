import json
import os
import stat
import sys
import zipfile

import pytest

from printhub import slicer


def test_resolve_profile_inherits_and_include(tmp_path):
    m = tmp_path / "BBL" / "machine"
    m.mkdir(parents=True)
    (m / "fdm_machine_common.json").write_text(json.dumps({"name": "fdm_machine_common", "type": "machine",
                                                           "printable_height": "250", "nozzle_diameter": ["0.4"],
                                                           "instantiation": "false"}))
    (m / "fdm_bbl_3dp_001_common.json").write_text(json.dumps({"name": "fdm_bbl_3dp_001_common", "inherits": "fdm_machine_common",
                                                               "printable_height": "256", "instantiation": "false"}))
    (m / "tmpl.json").write_text(json.dumps({"name": "tmpl", "machine_start_gcode": "G28", "instantiation": "false"}))
    (m / "Bambu Lab P1S 0.4 nozzle.json").write_text(json.dumps({"name": "Bambu Lab P1S 0.4 nozzle", "from": "system",
                                                                  "inherits": "fdm_bbl_3dp_001_common", "include": ["tmpl"],
                                                                  "printer_model": "Bambu Lab P1S", "instantiation": "true"}))
    flat = slicer.resolve_profile(str(m / "Bambu Lab P1S 0.4 nozzle.json"), tmp_path)
    assert flat["printable_height"] == "256" and flat["nozzle_diameter"] == ["0.4"]
    assert flat["machine_start_gcode"] == "G28" and flat["printer_model"] == "Bambu Lab P1S"
    assert flat["from"] == "User" and "inherits" not in flat and "include" not in flat
    prof = slicer.list_profiles(override_dir=str(tmp_path))
    assert [p["name"] for p in prof["machines"]] == ["Bambu Lab P1S 0.4 nozzle"]


@pytest.mark.skipif(sys.platform == "win32", reason="скрипт-заглушка для POSIX")
async def test_slice_with_fake_orca(tmp_path):
    fake = tmp_path / "orca-slicer"
    fake.write_text(f"""#!{sys.executable}
import sys, json, zipfile, os
a = sys.argv[1:]
open(os.path.join(os.path.dirname(__file__), 'args.json'), 'w').write(json.dumps(a))
out = a[a.index('--outputdir') + 1]; name = a[a.index('--export-3mf') + 1]
assert os.path.isdir(a[a.index('--datadir') + 1])
with zipfile.ZipFile(os.path.join(out, name), 'w') as z:
    z.writestr('Metadata/plate_1.gcode', '; total estimated time: 10m 0s\\nG1 X1 E1\\n')
    z.writestr('Metadata/slice_info.config', '<config><plate><metadata key="index" value="1"/><metadata key="prediction" value="600"/><metadata key="weight" value="5.5"/></plate></config>')
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"name": "x", "type": "machine"}))
    model = tmp_path / "m.stl"
    model.write_text("solid x\nendsolid x\n")
    out, log = await slicer.slice_with_orca(str(fake), str(model), machine=str(prof), process=str(prof), filaments=[str(prof)],
                                            work_dir=str(tmp_path / "work"), bed_type="Textured PEI Plate")
    args = json.loads((tmp_path / "args.json").read_text())
    assert args[args.index("--curr-bed-type") + 1] == "Textured PEI Plate"
    assert args[args.index("--slice") + 1] == "0" and args[-1] == str(model.resolve())
    assert "--min-save" not in args
    with zipfile.ZipFile(out) as z:
        assert "Metadata/plate_1.gcode" in z.namelist()


@pytest.mark.skipif(sys.platform == "win32", reason="скрипт-заглушка для POSIX")
async def test_slice_error_code(tmp_path):
    fake = tmp_path / "orca-slicer"
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(256 - 61)\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    prof = tmp_path / "p.json"
    prof.write_text("{}")
    with pytest.raises(ValueError, match="тип стола"):
        await slicer.slice_with_orca(str(fake), str(prof), machine=str(prof), process=str(prof), filaments=[],
                                     work_dir=str(tmp_path / "w"))
