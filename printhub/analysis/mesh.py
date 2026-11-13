"""Подготовка и исправление 3D-моделей (STL / OBJ / 3MF / PLY).

Анализ: размеры, объём, замкнутость, дыры, неманифолдные рёбра, вывернутые нормали,
отдельные тела, нависания, помещается ли в область печати, подозрение на неверные единицы.
Ремонт: склейка вершин, удаление вырожденных/дублирующих граней, исправление нормалей,
заделка дыр, PyMeshFix для тяжёлых случаев, удаление мусорных осколков, объединение тел.
Преобразования: масштаб, поворот, «положить на грань», центрирование, зеркалирование.
"""
from __future__ import annotations

import io
import math
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

import numpy as np
import trimesh

try:  # тяжёлый ремонт
    import pymeshfix  # type: ignore
except Exception:  # noqa: BLE001
    pymeshfix = None

try:  # булевы операции / объединение тел
    import manifold3d  # type: ignore
except Exception:  # noqa: BLE001
    manifold3d = None


SUPPORTED_MESH_EXT = (".stl", ".obj", ".3mf", ".ply", ".off", ".glb", ".gltf")


def load_mesh(path_or_bytes: str | bytes, file_type: Optional[str] = None) -> trimesh.Trimesh:
    """Загружает файл и сводит сцену к одному мешу (в мм)."""
    if isinstance(path_or_bytes, (bytes, bytearray)):
        if not file_type:
            raise ValueError("file_type обязателен для bytes")
        obj = trimesh.load(io.BytesIO(path_or_bytes), file_type=file_type.lstrip("."), force=None,
                           process=False)
    else:
        obj = trimesh.load(path_or_bytes, file_type=file_type, process=False)
    if isinstance(obj, trimesh.Scene):
        geoms = [g for g in obj.dump(concatenate=False) if isinstance(g, trimesh.Trimesh)] \
            if hasattr(obj, "dump") else list(obj.geometry.values())
        if not geoms:
            raise ValueError("В файле нет треугольной геометрии")
        mesh = trimesh.util.concatenate(geoms)
    elif isinstance(obj, trimesh.Trimesh):
        mesh = obj
    else:
        raise ValueError(f"Неподдерживаемый тип геометрии: {type(obj).__name__}")
    if len(mesh.faces) == 0:
        raise ValueError("Пустая модель")
    return mesh


@dataclass
class MeshIssue:
    code: str
    severity: str  # info / warning / error
    text: str
    count: Optional[int] = None


@dataclass
class MeshReport:
    vertices: int = 0
    faces: int = 0
    size_mm: list[float] = field(default_factory=list)
    bbox_min: list[float] = field(default_factory=list)
    bbox_max: list[float] = field(default_factory=list)
    volume_cm3: float = 0.0
    area_cm2: float = 0.0
    watertight: bool = False
    winding_consistent: bool = False
    bodies: int = 1
    boundary_edges: int = 0
    holes: int = 0
    non_manifold_edges: int = 0
    degenerate_faces: int = 0
    duplicate_faces: int = 0
    inverted: bool = False
    overhang_area_cm2: float = 0.0
    overhang_pct: float = 0.0
    bed_contact_cm2: float = 0.0
    fits_build_volume: Optional[bool] = None
    fits_after_rotation: Optional[bool] = None
    units_hint: str = ""
    printable: bool = True
    issues: list[MeshIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _edge_stats(mesh: trimesh.Trimesh) -> tuple[int, int, int]:
    """(boundary_edges, non_manifold_edges, holes) по уникальным рёбрам."""
    edges = np.sort(mesh.edges, axis=1)
    if len(edges) == 0:
        return 0, 0, 0
    _, counts = np.unique(edges, axis=0, return_counts=True)
    boundary = int(np.sum(counts == 1))
    non_manifold = int(np.sum(counts > 2))
    holes = 0
    if boundary:
        try:
            bnd = edges[trimesh.grouping.group_rows(edges, require_count=1)]
            import networkx as nx  # зависимость trimesh
            g = nx.Graph()
            g.add_edges_from(map(tuple, bnd))
            holes = nx.number_connected_components(g)
        except Exception:  # noqa: BLE001
            holes = 0
    return boundary, non_manifold, holes


def overhang_stats(mesh: trimesh.Trimesh, angle_deg: float = 45.0) -> tuple[float, float]:
    """Площадь граней, которым нужны поддержки (мм²), и площадь контакта со столом (мм²)."""
    normals = mesh.face_normals
    areas = mesh.area_faces
    zmin = mesh.bounds[0][2]
    tri_z = mesh.triangles[:, :, 2]
    on_bed = np.all(np.abs(tri_z - zmin) < 0.05, axis=1) & (normals[:, 2] < -0.99)
    thr = -math.sin(math.radians(angle_deg))
    over = (normals[:, 2] < thr) & ~on_bed
    return float(areas[over].sum()), float(areas[on_bed].sum())


def analyze_mesh(mesh: trimesh.Trimesh, build_volume: Optional[list[float]] = None,
                 overhang_angle: float = 45.0) -> MeshReport:
    r = MeshReport()
    r.vertices = int(len(mesh.vertices))
    r.faces = int(len(mesh.faces))
    b = mesh.bounds
    r.bbox_min = [round(float(v), 3) for v in b[0]]
    r.bbox_max = [round(float(v), 3) for v in b[1]]
    size = (b[1] - b[0]).astype(float)
    r.size_mm = [round(float(v), 3) for v in size]
    r.area_cm2 = round(float(mesh.area) / 100.0, 3)
    r.watertight = bool(mesh.is_watertight)
    r.winding_consistent = bool(mesh.is_winding_consistent)
    try:
        r.bodies = int(mesh.body_count)
    except Exception:  # noqa: BLE001
        r.bodies = len(mesh.split(only_watertight=False))
    r.boundary_edges, r.non_manifold_edges, r.holes = _edge_stats(mesh)
    r.degenerate_faces = int(len(mesh.faces) - int(np.count_nonzero(mesh.nondegenerate_faces())))
    r.duplicate_faces = int(len(mesh.faces) - int(np.count_nonzero(mesh.unique_faces())))
    vol = float(mesh.volume) if r.watertight or r.winding_consistent else float(abs(mesh.volume))
    r.inverted = bool(r.watertight and vol < 0)
    r.volume_cm3 = round(abs(vol) / 1000.0, 4)
    over, contact = overhang_stats(mesh, overhang_angle)
    r.overhang_area_cm2 = round(over / 100.0, 3)
    r.overhang_pct = round(100.0 * over / max(float(mesh.area), 1e-9), 2)
    r.bed_contact_cm2 = round(contact / 100.0, 3)

    mx = float(size.max())
    if mx < 10:
        r.units_hint = "Модель очень маленькая (<10 мм) — возможно, она в сантиметрах или дюймах: попробуйте ×10 или ×25.4."
    elif mx > 2000:
        r.units_hint = "Модель больше 2 м — возможно, единицы не мм: попробуйте ×0.1 или ×0.001."

    if build_volume and all(v > 0 for v in build_volume):
        bv = np.array(build_volume, dtype=float)
        r.fits_build_volume = bool(np.all(size <= bv + 1e-6))
        if not r.fits_build_volume:
            r.fits_after_rotation = bool(np.all(np.sort(size) <= np.sort(bv) + 1e-6))

    iss = r.issues
    if r.degenerate_faces:
        iss.append(MeshIssue("degenerate", "warning", "Вырожденные (нулевой площади) грани", r.degenerate_faces))
    if r.duplicate_faces:
        iss.append(MeshIssue("duplicate", "warning", "Дублирующиеся грани", r.duplicate_faces))
    if r.holes:
        iss.append(MeshIssue("holes", "error", "Дыры в поверхности (модель не замкнута)", r.holes))
    elif not r.watertight:
        iss.append(MeshIssue("not_watertight", "error", "Поверхность не замкнута"))
    if r.non_manifold_edges:
        iss.append(MeshIssue("non_manifold", "error", "Неманифолдные рёбра (общие для 3+ граней)", r.non_manifold_edges))
    if not r.winding_consistent:
        iss.append(MeshIssue("winding", "warning", "Несогласованные нормали (часть граней вывернута)"))
    if r.inverted:
        iss.append(MeshIssue("inverted", "error", "Модель вывернута наизнанку (отрицательный объём)"))
    if r.bodies > 1:
        iss.append(MeshIssue("bodies", "info", "Несколько отдельных тел", r.bodies))
    if r.units_hint:
        iss.append(MeshIssue("units", "warning", r.units_hint))
    if r.fits_build_volume is False:
        txt = "Не помещается в область печати"
        if r.fits_after_rotation:
            txt += " (поместится, если повернуть)"
        iss.append(MeshIssue("too_big", "error", txt))
    if r.overhang_pct > 5:
        iss.append(MeshIssue("overhangs", "info", f"Нависания > {overhang_angle:.0f}°: {r.overhang_pct}% поверхности — нужны поддержки"))
    if r.bed_contact_cm2 < 0.5 and r.size_mm[2] > 20:
        iss.append(MeshIssue("small_contact", "warning", "Маленькая площадь контакта со столом — попробуйте «Положить на грань» или кайму (brim)"))
    r.printable = not any(i.severity == "error" for i in iss)
    return r


# ---------------------------------------------------------------- ремонт

@dataclass
class RepairResult:
    steps: list[str] = field(default_factory=list)
    before: Optional[MeshReport] = None
    after: Optional[MeshReport] = None


def repair_mesh(mesh: trimesh.Trimesh, *, aggressive: bool = False, remove_debris: bool = True,
                merge_bodies: bool = False, build_volume: Optional[list[float]] = None
                ) -> tuple[trimesh.Trimesh, RepairResult]:
    res = RepairResult(before=analyze_mesh(mesh, build_volume))
    m = mesh.copy()

    nv = len(m.vertices)
    m.merge_vertices(digits_vertex=5)
    if len(m.vertices) != nv:
        res.steps.append(f"Склеено вершин: {nv - len(m.vertices)}")

    nf = len(m.faces)
    m.update_faces(m.nondegenerate_faces())
    m.update_faces(m.unique_faces())
    m.remove_unreferenced_vertices()
    if len(m.faces) != nf:
        res.steps.append(f"Удалено вырожденных и дублирующихся граней: {nf - len(m.faces)}")

    if remove_debris:
        parts = m.split(only_watertight=False, repair=False)
        if len(parts) > 1:
            sizes = np.array([p.area for p in parts])
            keep = [p for p, a in zip(parts, sizes) if a >= sizes.max() * 0.001 and len(p.faces) >= 4]
            if len(keep) < len(parts):
                res.steps.append(f"Удалено мелких осколков: {len(parts) - len(keep)}")
                m = trimesh.util.concatenate(keep)

    trimesh.repair.fix_normals(m, multibody=True)
    holes0 = _edge_stats(m)[2]
    if not m.is_watertight:
        trimesh.repair.fill_holes(m)
        holes1 = _edge_stats(m)[2]
        if holes1 < holes0:
            res.steps.append(f"Заделано дыр: {holes0 - holes1}")

    if (not m.is_watertight or _edge_stats(m)[1]) and pymeshfix is not None:
        try:
            parts = m.split(only_watertight=False, repair=False) if not aggressive else [m]
            fixed = []
            for p in parts:
                if p.is_watertight and p.is_winding_consistent:
                    fixed.append(p)
                    continue
                mf = pymeshfix.MeshFix(np.asarray(p.vertices, dtype=float), np.asarray(p.faces, dtype=np.int32))
                mf.repair(joincomp=aggressive, remove_smallest_components=False)
                pts, fcs = np.asarray(mf.points), np.asarray(mf.faces)
                if len(fcs):
                    fixed.append(trimesh.Trimesh(pts, fcs, process=True))
            if fixed:
                m = trimesh.util.concatenate(fixed)
                res.steps.append("Глубокий ремонт (MeshFix): заделка дыр, удаление самопересечений")
        except Exception as e:  # noqa: BLE001
            res.steps.append(f"MeshFix не справился: {e}")

    trimesh.repair.fix_normals(m, multibody=True)
    if m.is_watertight and m.volume < 0:
        m.invert()
        res.steps.append("Вывернутая модель развёрнута")

    if merge_bodies:
        merged = union_bodies(m)
        if merged is not None:
            m = merged
            res.steps.append("Пересекающиеся тела объединены в одно")

    res.after = analyze_mesh(m, build_volume)
    b, a = res.before, res.after
    if b.non_manifold_edges and a.non_manifold_edges < b.non_manifold_edges:
        res.steps.append(f"Исправлено неманифолдных рёбер: {b.non_manifold_edges - a.non_manifold_edges}")
    if not b.winding_consistent and a.winding_consistent:
        res.steps.append("Нормали согласованы")
    if not res.steps:
        res.steps.append("Ошибок не найдено — модель не изменена")
    return m, res


def union_bodies(mesh: trimesh.Trimesh) -> Optional[trimesh.Trimesh]:
    if manifold3d is None:
        return None
    parts = mesh.split(only_watertight=False)
    mans = []
    for p in parts:
        if not p.is_watertight:
            return None
        mm = manifold3d.Manifold(manifold3d.Mesh(
            vert_properties=np.asarray(p.vertices, dtype=np.float32),
            tri_verts=np.asarray(p.faces, dtype=np.uint32)))
        if mm.status() != manifold3d.Error.NoError:
            return None
        mans.append(mm)
    if not mans:
        return None
    out = manifold3d.Manifold.batch_boolean(mans, manifold3d.OpType.Add) if len(mans) > 1 else mans[0]
    om = out.to_mesh()
    return trimesh.Trimesh(np.asarray(om.vert_properties)[:, :3], np.asarray(om.tri_verts), process=True)


# ---------------------------------------------------------------- преобразования

def place_on_bed(mesh: trimesh.Trimesh, bed_size: Optional[list[float]] = None) -> trimesh.Trimesh:
    """Опускает на z=0 и центрирует по XY (в центр стола, если размер известен, иначе в 0,0)."""
    b = mesh.bounds
    cx, cy = (b[0][0] + b[1][0]) / 2, (b[0][1] + b[1][1]) / 2
    tx = (bed_size[0] / 2 if bed_size and bed_size[0] else 0.0) - cx
    ty = (bed_size[1] / 2 if bed_size and bed_size[1] else 0.0) - cy
    mesh.apply_translation([tx, ty, -b[0][2]])
    return mesh


def scale_mesh(mesh: trimesh.Trimesh, factor: float | list[float]) -> trimesh.Trimesh:
    f = np.array(factor if isinstance(factor, (list, tuple)) else [factor] * 3, dtype=float)
    if np.any(f <= 0):
        raise ValueError("Масштаб должен быть > 0")
    mesh.apply_transform(np.diag([f[0], f[1], f[2], 1.0]))
    return mesh


def scale_to_size(mesh: trimesh.Trimesh, axis: int, size_mm: float) -> trimesh.Trimesh:
    cur = float(mesh.extents[axis])
    if cur <= 0:
        raise ValueError("Нулевой размер по оси")
    return scale_mesh(mesh, size_mm / cur)


def rotate_mesh(mesh: trimesh.Trimesh, x: float = 0, y: float = 0, z: float = 0) -> trimesh.Trimesh:
    c = mesh.bounds.mean(axis=0)
    for ang, axis in ((x, [1, 0, 0]), (y, [0, 1, 0]), (z, [0, 0, 1])):
        if ang:
            mesh.apply_transform(trimesh.transformations.rotation_matrix(math.radians(ang), axis, c))
    return mesh


def mirror_mesh(mesh: trimesh.Trimesh, axis: int) -> trimesh.Trimesh:
    s = [1.0, 1.0, 1.0]
    s[axis] = -1.0
    mesh.apply_transform(np.diag(s + [1.0]))
    mesh.invert()  # после зеркалирования нормали надо развернуть
    return mesh


@dataclass
class Orientation:
    matrix: list[list[float]]
    contact_cm2: float
    overhang_cm2: float
    height_mm: float
    score: float
    label: str = ""


def _align_to_down(normal: np.ndarray) -> np.ndarray:
    n = normal / np.linalg.norm(normal)
    down = np.array([0.0, 0.0, -1.0])
    v = np.cross(n, down)
    c = float(np.dot(n, down))
    if np.linalg.norm(v) < 1e-9:
        if c > 0:
            return np.eye(4)
        return trimesh.transformations.rotation_matrix(math.pi, [1, 0, 0])
    angle = math.acos(max(-1.0, min(1.0, c)))
    return trimesh.transformations.rotation_matrix(angle, v)


def suggest_orientations(mesh: trimesh.Trimesh, goal: str = "balanced", top: int = 6,
                         overhang_angle: float = 45.0) -> list[Orientation]:
    """Варианты ориентации: кладём модель на крупнейшие грани выпуклой оболочки.

    goal: balanced — баланс поддержек и контакта; supports — минимум поддержек;
          contact — максимум площади на столе; height — минимальная высота (быстрее).
    """
    hull = mesh.convex_hull
    facets_area = hull.facets_area
    order = np.argsort(facets_area)[::-1][:24]
    candidates: list[Orientation] = []
    seen: list[np.ndarray] = []
    for idx in order:
        normal = hull.facets_normal[idx]
        if any(np.dot(normal, s) > 0.999 for s in seen):
            continue
        seen.append(normal)
        T = _align_to_down(normal)
        m = mesh.copy()
        m.apply_transform(T)
        m.apply_translation([0, 0, -m.bounds[0][2]])
        over, contact = overhang_stats(m, overhang_angle)
        h = float(m.extents[2])
        candidates.append(Orientation(T.tolist(), round(contact / 100, 3), round(over / 100, 3), round(h, 2), 0.0))
    if not candidates:
        return []
    max_over = max(c.overhang_cm2 for c in candidates) or 1.0
    max_contact = max(c.contact_cm2 for c in candidates) or 1.0
    max_h = max(c.height_mm for c in candidates) or 1.0
    w = {"balanced": (1.0, 0.6, 0.3), "supports": (1.0, 0.15, 0.05),
         "contact": (0.3, 1.0, 0.1), "height": (0.3, 0.2, 1.0)}.get(goal, (1.0, 0.6, 0.3))
    for c in candidates:
        c.score = round(w[0] * (1 - c.overhang_cm2 / max_over) + w[1] * (c.contact_cm2 / max_contact)
                        + w[2] * (1 - c.height_mm / max_h), 4)
    candidates.sort(key=lambda c: c.score, reverse=True)
    for i, c in enumerate(candidates[:top]):
        c.label = f"Вариант {i + 1}: контакт {c.contact_cm2} см², нависания {c.overhang_cm2} см², высота {c.height_mm} мм"
    return candidates[:top]


def lay_flat(mesh: trimesh.Trimesh, goal: str = "balanced") -> trimesh.Trimesh:
    opts = suggest_orientations(mesh, goal=goal, top=1)
    if opts:
        mesh.apply_transform(np.array(opts[0].matrix))
    return mesh


def export_mesh(mesh: trimesh.Trimesh, fmt: str = "stl") -> bytes:
    fmt = fmt.lower().lstrip(".")
    data = mesh.export(file_type=fmt)
    return data if isinstance(data, bytes) else data.encode()


# ---------------------------------------------------------------- оценки без слайсинга

@dataclass
class FdmEstimateParams:
    density: float = 1.24          # г/см³
    infill_pct: float = 15.0
    wall_mm: float = 1.2           # суммарная толщина периметров
    top_bottom_mm: float = 1.0
    volumetric_flow: float = 12.0  # мм³/с — средняя реальная скорость экструзии
    overhead_pct: float = 25.0     # перемещения, ретракты, разгон
    support_factor: float = 0.35   # доля объёма поддержек от площади нависаний × 1 мм


@dataclass
class ResinEstimateParams:
    density: float = 1.10          # г/мл
    layer_height_mm: float = 0.05
    exposure_s: float = 2.5
    bottom_exposure_s: float = 25.0
    bottom_layers: int = 5
    lift_cycle_s: float = 5.0      # подъём + опускание + пауза на слой
    supports_pct: float = 15.0     # добавка на поддержки
    lift_height_mm: float = 0.0    # высота подъёма платформы над моделью при печати на поддержках
    hollow: bool = False
    wall_mm: float = 2.0           # толщина стенки при полой печати


def estimate_fdm_from_mesh(mesh: trimesh.Trimesh, p: FdmEstimateParams) -> dict[str, Any]:
    vol = abs(float(mesh.volume)) if mesh.is_watertight else float(mesh.convex_hull.volume) * 0.6
    area = float(mesh.area)
    shell = min(vol, area * (p.wall_mm + p.top_bottom_mm) / 2.0)
    inner = max(vol - shell, 0.0)
    over, _ = overhang_stats(mesh)
    support = over * p.support_factor * 3.0
    plastic = shell + inner * p.infill_pct / 100.0 + support  # мм³
    grams = plastic / 1000.0 * p.density
    length_m = plastic / (math.pi * (1.75 / 2) ** 2) / 1000.0
    time_s = plastic / max(p.volumetric_flow, 0.1) * (1 + p.overhead_pct / 100.0)
    return {
        "method": "mesh-heuristic",
        "plastic_cm3": round(plastic / 1000, 2),
        "filament_g": round(grams, 1),
        "filament_m": round(length_m, 2),
        "support_cm3": round(support / 1000, 2),
        "time_s": round(time_s),
        "note": "Приблизительно, ±30%. Точные значения — после нарезки в слайсере.",
    }


def estimate_resin_from_mesh(mesh: trimesh.Trimesh, p: ResinEstimateParams) -> dict[str, Any]:
    vol_mm3 = abs(float(mesh.volume)) if mesh.is_watertight else float(mesh.convex_hull.volume) * 0.7
    if p.hollow:
        shell = float(mesh.area) * p.wall_mm
        vol_mm3 = min(vol_mm3, shell)
    ml = vol_mm3 / 1000.0 * (1 + p.supports_pct / 100.0)
    height = float(mesh.extents[2]) + p.lift_height_mm
    layers = max(int(math.ceil(height / max(p.layer_height_mm, 0.001))), 1)
    bl = min(p.bottom_layers, layers)
    time_s = bl * (p.bottom_exposure_s + p.lift_cycle_s) + (layers - bl) * (p.exposure_s + p.lift_cycle_s)
    return {
        "method": "mesh-heuristic",
        "resin_ml": round(ml, 2),
        "resin_g": round(ml * p.density, 1),
        "layers": layers,
        "height_mm": round(height, 2),
        "time_s": round(time_s),
        "note": "Время фотополимерной печати зависит только от высоты, а не от количества моделей на платформе.",
    }
