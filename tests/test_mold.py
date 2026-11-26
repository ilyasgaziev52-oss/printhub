"""Опалубка под силикон и разъёмная форма для литья."""
import math

import httpx
import pytest
import trimesh

from printhub.analysis import mold
from printhub.app import create_app


def vase() -> trimesh.Trimesh:
    """Цилиндр с шаром сверху: самое широкое место — экватор шара на высоте 40 мм."""
    return trimesh.boolean.union([trimesh.creation.cylinder(radius=15, height=30).apply_translation([0, 0, 15]),
                                  trimesh.creation.icosphere(subdivisions=3, radius=18).apply_translation([0, 0, 40])],
                                 engine="manifold")


def test_silicone_box():
    cyl = trimesh.creation.cylinder(radius=10, height=40, sections=64).apply_translation([5, 7, 20])
    r = mold.make_mold(cyl, "silicone_box", {"gap": 8, "over": 8, "shape": "box"})
    assert [p.name for p in r.parts] == ["Стенка — половина 1", "Стенка — половина 2"]   # без дна — ставится на стол
    for p in r.parts:
        assert p.mesh.is_watertight and len(p.mesh.split(only_watertight=False)) == 1, p.name
        assert p.mesh.bounds[0][2] == pytest.approx(0, abs=1e-3)                         # ровный низ
    rb = mold.make_mold(cyl, "silicone_box", {"gap": 8, "over": 8, "with_base": True, "shape": "box"})
    assert [p.name for p in rb.parts] == ["Дно опалубки", "Стенка — половина 1", "Стенка — половина 2"]
    assert all(p.mesh.is_watertight for p in rb.parts)
    i = r.info
    assert i["inner_mm"][:2] == [36.0, 36.0] and i["fill_height_mm"] == 48.0
    # короб 36×36 со скруглением 4 мм, высота заливки 48, минус цилиндр
    inner_area = 36 * 36 - (4 - math.pi) * 4 ** 2
    expect = (inner_area * 48 - math.pi * 10 ** 2 * 40) / 1000
    assert i["silicone_cm3"] == pytest.approx(expect, rel=0.02)
    assert i["silicone_g"] == pytest.approx(expect * 1.1, abs=2)


def test_two_part_auto_split_and_channels():
    r = mold.make_mold(vase(), "two_part", {"material": "plaster"})
    i = r.info
    assert i["split_auto"] and i["split_mm"] == pytest.approx(40, abs=1.5)   # по экватору шара
    assert not r.warnings and i["undercut_cm2"] < 0.5
    assert i["cast_g"] == pytest.approx(i["cast_cm3"] * 1.2, abs=1)
    lo, up = r.parts
    for p in (lo, up):
        assert p.mesh.is_watertight and len(p.mesh.split(only_watertight=False)) == 1
    # объём полости в двух половинах = объём модели + литник (форма — блок минус модель)
    block = i["mold_mm"][0] * i["mold_mm"][1] * i["mold_mm"][2]
    assert lo.mesh.volume + up.mesh.volume < block - vase().volume * 0.95


def test_two_part_warns_about_undercuts():
    # «гриб» с разъёмом посередине ножки: шляпка снизу нависает над нижней половиной — застрянет
    mush = trimesh.boolean.union([trimesh.creation.cylinder(radius=5, height=30).apply_translation([0, 0, 15]),
                                  trimesh.creation.cylinder(radius=20, height=6).apply_translation([0, 0, 33])],
                                 engine="manifold")
    r = mold.make_mold(mush, "two_part", {"split": 10})
    assert r.warnings and "Поднутрения" in r.warnings[0]
    assert mold.make_mold(mush, "two_part", {}).info["split_mm"] > 30            # авто — по шляпке, без поднутрений


def test_open_mesh_is_rejected():
    cyl = trimesh.creation.cylinder(radius=10, height=20)
    broken = trimesh.Trimesh(cyl.vertices, cyl.faces[4:], process=False)
    with pytest.raises(ValueError, match="не замкнутая"):
        mold.make_mold(broken, "silicone_box")


async def test_mold_endpoint(tmp_path):
    app = create_app(str(tmp_path / "data"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t", timeout=120) as c:
        item = (await c.post("/api/library", files={"file": ("vase.stl", vase().export(file_type="stl"), "model/stl")})).json()
        mats = (await c.get("/api/mold/materials")).json()
        assert any(m["id"] == "plaster" for m in mats)
        r = (await c.post(f"/api/library/{item['id']}/mold", json={"kind": "two_part", "options": {"material": "epoxy"}})).json()
        assert r["filename"] == "vase_forma.3mf" and len(r["parts"]) == 2 and r["info"]["cast_g"] > 0
        lib = {x["id"]: x for x in (await c.get("/api/library")).json()}
        assert lib[r["item_id"]]["kind"] == "mesh"
        r2 = (await c.post(f"/api/library/{item['id']}/mold", json={"kind": "silicone_box"})).json()
        assert r2["filename"] == "vase_opalubka.3mf" and r2["info"]["silicone_g"] > 0
        bad = await c.post(f"/api/library/{item['id']}/mold", json={"kind": "nope"})
        assert bad.status_code == 400


def test_silicone_box_fastening():
    """По умолчанию — ушки под прищепки (тонкие, без отверстий); болты — толще и с отверстиями; можно без креплений."""
    cyl = trimesh.creation.cylinder(radius=10, height=30, sections=64).apply_translation([0, 0, 15])
    clips = mold.make_mold(cyl, "silicone_box", {})
    bolts = mold.make_mold(cyl, "silicone_box", {"fastening": "bolts"})
    plain = mold.make_mold(cyl, "silicone_box", {"fastening": "none"})
    assert "прищепк" in clips.info["bolts"] and "M3" in bolts.info["bolts"] and plain.info["bolts"] == ""
    v = lambda r: r.parts[0].mesh.volume                            # noqa: E731  ушки под болты толще, чем под прищепки
    assert v(bolts) > v(clips) > v(plain)
    assert clips.parts[0].mesh.extents[1] > plain.parts[0].mesh.extents[1] + 15          # ушки торчат с двух сторон
    for r in (clips, bolts, plain):
        assert all(p.mesh.is_watertight and len(p.mesh.split(only_watertight=False)) == 1 for p in r.parts)
    assert any("прищепк" in s for s in clips.info["steps"])
    assert mold.make_mold(cyl, "silicone_box", {"bolts": True}).info["bolts"].startswith("4 болта")   # старый параметр


def test_tiny_model_warns():
    tiny = trimesh.creation.box(extents=[3, 2, 0.5])               # модель «в сантиметрах»
    r = mold.make_mold(tiny, "silicone_box")
    assert r.warnings and "очень маленькая" in r.warnings[0]
    assert not mold.make_mold(vase(), "silicone_box").warnings


def test_contour_mold_saves_silicone():
    """«По контуру»: стенки на отступе от модели и по высоте — силикона заметно меньше, чем в коробе."""
    tower = trimesh.boolean.union([trimesh.creation.box(extents=[60, 60, 10]).apply_translation([0, 0, 5]),
                                   trimesh.creation.cylinder(radius=15, height=80, sections=64).apply_translation([0, 0, 50])],
                                  engine="manifold")
    r = mold.make_mold(tower, "silicone_box", {"gap": 7})
    i = r.info
    assert i["shape"] == "contour" and i["saved_pct"] >= 40 and i["silicone_g"] < i["box_silicone_g"]
    for p in r.parts:
        assert p.mesh.is_watertight and len(p.mesh.split(only_watertight=False)) == 1
    # объём силикона ≈ «модель + отступ 7 мм» по слоям минус модель — в разумных пределах
    assert 0.2 * i["box_silicone_g"] < i["silicone_g"] < 0.7 * i["box_silicone_g"]
