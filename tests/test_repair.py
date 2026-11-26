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
