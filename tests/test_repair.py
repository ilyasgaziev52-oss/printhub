"""Ремонт модели не должен её портить: «веер» — только для плоских дыр, защита от больших выдуманных заплаток."""
import numpy as np
import trimesh

from printhub.analysis import mesh as M


def tower_with_corner_hole() -> trimesh.Trimesh:
    """«Здание»: проём заходит на две стены сразу (контур дыры изогнут под 90°) — как фасад на модели гостиницы."""
    b = trimesh.creation.box(extents=[20, 20, 60]).subdivide().subdivide().subdivide()
    c = b.triangles_center
    hole = (c[:, 2] > 10) & (c[:, 2] < 40) & (((c[:, 0] > 9.9) & (c[:, 1] > -5)) | ((c[:, 1] > 9.9) & (c[:, 0] > -5)))
    return trimesh.Trimesh(b.vertices, b.faces[~hole], process=False)


def test_fan_skips_bent_hole():
    m = tower_with_corner_hole()
    m.merge_vertices()
    assert not m.is_watertight
    filled = M.fill_holes_fan(m)
    assert filled == 0                       # изогнутая дыра — не «веером» (иначе треугольники через всю модель)


def test_fan_fills_flat_hole():
    cyl = trimesh.creation.cylinder(radius=8, height=60, sections=48)
    top = np.where(cyl.face_normals[:, 2] > 0.99)[0][:5]                     # снят кусок плоского торца
    m = trimesh.Trimesh(cyl.vertices, np.delete(cyl.faces, top, axis=0), process=False)
    m.merge_vertices()
    assert M.fill_holes_fan(m) == 1 and m.is_watertight


def test_repair_never_invents_much_surface():
    m = tower_with_corner_hole()
    fixed, res = M.repair_mesh(m)
    assert fixed.area <= m.area * 1.5 + 1e-6
    # заплатка лежит в пределах исходной модели — ничего не торчит наружу
    assert np.all(fixed.bounds[0] >= m.bounds[0] - 0.5) and np.all(fixed.bounds[1] <= m.bounds[1] + 0.5)


def test_meters_and_double_faces_like_sketchup():
    """Файл из SketchUp: модель в метрах, каждая грань записана дважды (лицом и изнанкой).
    Загрузка переводит в мм до склейки вершин, ремонт убирает дубли — модель цельная, без «выдуманной» поверхности."""
    tower = trimesh.util.concatenate([trimesh.creation.box(extents=[0.03, 0.04, 0.1]).apply_translation([0, 0, 0.05]),
                                      trimesh.creation.box(extents=[0.05, 0.06, 0.01]).apply_translation([0, 0, 0.005])])
    doubled = trimesh.Trimesh(np.vstack([tower.vertices, tower.vertices]),
                              np.vstack([tower.faces, tower.faces[:, ::-1] + len(tower.vertices)]), process=False)
    m = M.load_mesh(doubled.export(file_type="stl"), file_type="stl")
    assert abs(m.extents[2] - 100) < 1e-3                                                # метры → мм
    assert any(i.code == "units_fixed" for i in M.analyze_mesh(m).issues)
    fixed, res = M.repair_mesh(m)
    assert fixed.is_watertight and not res.destructive
    assert abs(fixed.area - m.area / 2) < 1.0                                           # ушли только дубли
