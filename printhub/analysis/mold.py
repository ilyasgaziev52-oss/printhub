"""Опалубки и формы для литья по 3D-модели.

Два режима:
  * ``silicone_box`` — опалубка под силикон вокруг мастер-модели: дно с отметкой, куда ставить модель,
    и две половины стенок, которые стягиваются болтами M3. Считает объём и массу силикона.
  * ``two_part`` — готовая разъёмная форма-негатив из двух половин: плоскость разъёма по самому широкому
    сечению (или заданная), замки-полусферы, литник с воронкой, воздушники, отверстия под болты,
    пазы для разъёма. Предупреждает о поднутрениях, из-за которых отливка застрянет в жёсткой форме.

Результат — 3MF, где каждая деталь отдельным объектом уже разложена на столе для печати
(обе половины формы полостью вверх, без поддержек внутри полости).
"""
from __future__ import annotations

import io
import math
import zipfile
from dataclasses import asdict, dataclass, field
from typing import Any, Optional
from xml.sax.saxutils import quoteattr

import numpy as np
import trimesh
from manifold3d import CrossSection, JoinType, Manifold, Mesh

# плотность заливочных материалов, г/см³ (для подсказки, сколько покупать)
CAST_MATERIALS = {
    "silicone": ("Силикон", 1.10),
    "plaster": ("Гипс", 1.20),
    "concrete": ("Бетон / цементная смесь", 2.30),
    "epoxy": ("Эпоксидная смола", 1.15),
    "polyurethane": ("Полиуретан (жидкий пластик)", 1.05),
    "wax": ("Воск", 0.95),
    "soap": ("Мыльная основа", 1.06),
}


# ------------------------------------------------------------------ преобразования
def to_manifold(m: trimesh.Trimesh) -> Manifold:
    mesh = Mesh(vert_properties=np.asarray(m.vertices, dtype=np.float32),
                tri_verts=np.asarray(m.faces, dtype=np.uint32))
    mesh.merge()
    man = Manifold(mesh)
    if man.is_empty() or man.volume() <= 0:
        raise ValueError("Модель не замкнутая — сначала исправьте её («Подготовить к печати» или «Исправить»), "
                         "потом стройте опалубку")
    return man


def to_trimesh(m: Manifold) -> trimesh.Trimesh:
    mm = m.to_mesh()
    return trimesh.Trimesh(np.asarray(mm.vert_properties)[:, :3], np.asarray(mm.tri_verts), process=False)


def box(x0: float, x1: float, y0: float, y1: float, z0: float, z1: float) -> Manifold:
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def rrect(w: float, d: float, r: float) -> CrossSection:
    r = max(0.1, min(r, w / 2 - 0.05, d / 2 - 0.05))
    return CrossSection.square((w - 2 * r, d - 2 * r), True).offset(r, JoinType.Round, 2.0, 32)


def rod(axis: str, r: float, a0: float, a1: float, p: float, q: float) -> Manifold:
    """Цилиндр вдоль оси axis от a0 до a1; (p, q) — координаты в двух других осях."""
    c = Manifold.cylinder(a1 - a0, r, r, 32)
    if axis == "x":
        return c.rotate((0, 90, 0)).translate((a0, p, q))
    if axis == "y":
        return c.rotate((-90, 0, 0)).translate((p, a0, q))
    return c.translate((p, q, a0))


def centered(model: Manifold) -> Manifold:
    """Модель по центру XY, низ на z=0."""
    x0, y0, z0, x1, y1, _ = model.bounding_box()
    return model.translate((-(x0 + x1) / 2, -(y0 + y1) / 2, -z0))


# ------------------------------------------------------------------ результат
@dataclass
class MoldPart:
    name: str
    mesh: trimesh.Trimesh
    size_mm: list[float] = field(default_factory=list)


@dataclass
class MoldResult:
    kind: str
    parts: list[MoldPart]
    info: dict[str, Any]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "info": self.info, "warnings": self.warnings,
                "parts": [{"name": p.name, "size_mm": p.size_mm} for p in self.parts]}


def _part(name: str, m: Manifold) -> MoldPart:
    # убрать вырожденные (нулевой площади) треугольники, которые оставляют булевы операции:
    # иначе после записи в файл они схлопываются и слайсер/проверка видят «незамкнутую» деталь
    m = m.simplify(1e-3)
    t = to_trimesh(m)
    return MoldPart(name, t, [round(float(v), 1) for v in t.extents])


def layout(parts: list[MoldPart], gap: float = 8.0) -> list[tuple[MoldPart, tuple[float, float]]]:
    """Раскладка деталей в ряд на столе (низ каждой — на z=0), центр ряда — в начале координат."""
    placed, x = [], 0.0
    for p in parts:
        lo, hi = p.mesh.bounds
        p.mesh.apply_translation((-lo[0] + x, -(lo[1] + hi[1]) / 2, -lo[2]))
        placed.append((p, (x + (hi[0] - lo[0]) / 2, 0.0)))
        x += (hi[0] - lo[0]) + gap
    shift = -(x - gap) / 2
    for p, _ in placed:
        p.mesh.apply_translation((shift, 0, 0))
    return placed


def to_3mf(parts: list[MoldPart], title: str, bed_center: tuple[float, float] = (128.0, 128.0)) -> bytes:
    """3MF по базовой спецификации: каждая деталь — отдельный объект, разложены на столе."""
    layout(parts)
    objs, items = [], []
    for i, p in enumerate(parts, start=1):
        v = "".join(f'<vertex x="{a:.5f}" y="{b:.5f}" z="{c:.5f}"/>' for a, b, c in p.mesh.vertices)
        t = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in p.mesh.faces)
        objs.append(f'<object id="{i}" type="model" name={quoteattr(p.name)}><mesh><vertices>{v}</vertices>'
                    f'<triangles>{t}</triangles></mesh></object>')
        items.append(f'<item objectid="{i}" transform="1 0 0 0 1 0 0 0 1 {bed_center[0]:.3f} {bed_center[1]:.3f} 0"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="ru-RU" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<metadata name="Title">{title}</metadata><metadata name="Application">PrintHub</metadata>'
             f'<resources>{"".join(objs)}</resources><build>{"".join(items)}</build></model>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                   '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
        z.writestr("_rels/.rels",
                   '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                   'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
        z.writestr("3D/3dmodel.model", model)
    return buf.getvalue()


# ------------------------------------------------------------------ опалубка под силикон
@dataclass
class SiliconeBoxOptions:
    gap: float = 8.0           # зазор от модели до стенки, мм (толщина силикона по бокам)
    over: float = 8.0          # слой силикона над моделью, мм
    wall: float = 2.4          # толщина стенки опалубки
    base: float = 3.0          # толщина дна
    fastening: str = "clips"   # чем стягивать половины стенок: clips — прищепки/зажимы, bolts — болты M3, none
    bolts: Optional[bool] = None  # старый параметр: True → болты, False → без креплений
    with_base: bool = False    # печатать дно; по умолчанию нет — стенки ставятся на ровный стол/доску
    density: float = 1.10      # плотность силикона, г/см³


FASTEN_TXT = {"clips": "прищепками или канцелярскими зажимами", "bolts": "болтами M3", "none": ""}


def silicone_box(model_tm: trimesh.Trimesh, o: SiliconeBoxOptions | None = None) -> MoldResult:
    o = o or SiliconeBoxOptions()
    if o.bolts is not None:
        o.fastening = "bolts" if o.bolts else "none"
    if o.fastening not in FASTEN_TXT:
        o.fastening = "clips"
    model = centered(to_manifold(model_tm))
    x0, y0, _, x1, y1, h = model.bounding_box()
    iw, idp = (x1 - x0) + 2 * o.gap, (y1 - y0) + 2 * o.gap              # внутренний размер короба
    fill_h = h + o.over                                                 # уровень заливки
    H = fill_h + 3.0                                                    # стенки чуть выше уровня заливки
    r_in = min(4.0, o.gap)
    inner = rrect(iw, idp, r_in)
    outer = inner.offset(o.wall, JoinType.Round, 2.0, 32)
    groove_d, cl = (1.6 if o.with_base else 0.0), 0.25

    # стенки: кольцо, нижние 1.6 мм входят в паз дна
    ring = Manifold.extrude(outer - inner, H + groove_d).translate((0, 0, -groove_d))
    ow, od = iw + 2 * o.wall, idp + 2 * o.wall
    halves = []
    ear_holes = []
    # «ушки» на стыках половин (x=0): под прищепки — тонкие гладкие (вместе 6 мм), под болты — толще и с отверстиями
    ear_w, ear_l = (3.0, 10.0) if o.fastening == "clips" else (6.0, 9.0)
    if o.fastening != "none":
        for sy in (-1, 1):
            yb = sy * od / 2
            ys = (yb - sy * o.wall * 0.6, yb + sy * ear_l)            # заходит в стенку — одно целое с ней
            ring += box(-ear_w, ear_w, min(ys), max(ys), (-groove_d + 0.01) if groove_d else 0.0, H)
            if o.fastening == "bolts":
                for zf in (0.25, 0.75):
                    ear_holes.append(rod("x", 1.75, -ear_w - 1, ear_w + 1, yb + sy * ear_l / 2, H * zf))
    for h_ in ear_holes:
        ring -= h_
    big = 10 * (ow + od + H)
    halves.append(ring ^ box(-big, 0, -big, big, -big, big))
    halves.append(ring ^ box(0, big, -big, big, -big, big))

    parts = [_part("Стенка — половина 1", halves[0]), _part("Стенка — половина 2", halves[1])]
    steps_place = ["Поставьте мастер-модель на ровную гладкую поверхность (стекло, пластик, ламинат) и приклейте "
                   "термоклеем, чтобы не всплыла",
                   "Поставьте вокруг половины стенок" + (f", стяните {FASTEN_TXT[o.fastening]} за ушки" if o.fastening != "none" else "") +
                   "; низ стенок промажьте пластилином или термоклеем, чтобы силикон не подтекал"]
    if not o.with_base:
        return _silicone_result(o, inner, model, iw, idp, H, fill_h, parts, steps_place)
    # дно: плита с пазом под стенки и отметкой контура модели
    margin = 5.0 + (ear_l if o.fastening != "none" else 0.0)
    plate = Manifold.extrude(rrect(ow + 2 * margin, od + 2 * margin, r_in + o.wall + margin), o.base).translate((0, 0, -o.base))
    slot = Manifold.extrude(outer.offset(cl, JoinType.Round, 2.0, 32) - inner.offset(-cl, JoinType.Round, 2.0, 32),
                            groove_d + 0.01).translate((0, 0, -groove_d))
    plate -= slot
    if o.fastening != "none":                                           # вырезы в дне под ушки
        for sy in (-1, 1):
            yb = sy * od / 2
            ys = (yb, yb + sy * ear_l)
            plate -= box(-ear_w - cl, ear_w + cl, min(ys) - cl, max(ys) + cl, -groove_d, 0.01)
    foot = model.project()                                              # контур модели — куда ставить мастер
    if foot.area() > 1:
        mark = (foot.offset(0.6, JoinType.Round, 2.0, 32) - foot.offset(-0.6, JoinType.Round, 2.0, 32))
        plate -= Manifold.extrude(mark, 0.5).translate((0, 0, -0.45))
    plate += box(-1, 1, -idp / 2 + 1.5, -idp / 2 + 5, 0, 0.6)           # стрелка «перед»

    parts.insert(0, _part("Дно опалубки", plate))
    steps_place = ["Приклейте мастер-модель к дну по отметке (термоклей или двусторонний скотч), стрелка — перед модели",
                   "Вставьте половины стенок в паз дна" + (f", стяните {FASTEN_TXT[o.fastening]} за ушки" if o.fastening != "none" else "")]
    return _silicone_result(o, inner, model, iw, idp, H, fill_h, parts, steps_place)


def _silicone_result(o: "SiliconeBoxOptions", inner: CrossSection, model: Manifold, iw: float, idp: float, H: float,
                     fill_h: float, parts: list[MoldPart], steps_place: list[str]) -> MoldResult:
    cavity_cm3 = (inner.area() * fill_h - model.volume()) / 1000.0
    warnings = []
    if o.gap < 5:
        warnings.append("Зазор меньше 5 мм — силиконовая форма будет тонкой и может порваться при извлечении")
    info = {
        "inner_mm": [round(iw, 1), round(idp, 1), round(H, 1)],
        "fill_height_mm": round(fill_h, 1),
        "silicone_cm3": round(cavity_cm3, 1),
        "silicone_g": round(cavity_cm3 * o.density, 0),
        "silicone_g_with_reserve": round(cavity_cm3 * o.density * 1.1, 0),
        "bolts": {"clips": "4 прищепки или канцелярских зажима (по 2 на каждый стык)",
                  "bolts": "4 болта M3×16 с гайками", "none": ""}[o.fastening],
        "steps": [
            "Напечатайте " + ("дно и " if o.with_base else "") + "две половины стенок, мастер-модель — отдельно",
            *steps_place,
            "Промажьте стенки и модель разделителем (вазелин или спрей-разделитель)",
            f"Залейте силикон до уровня {fill_h:.0f} мм от дна — модель будет покрыта на {o.over:.0f} мм",
            "После отверждения разберите опалубку и извлеките модель из силикона",
        ],
    }
    return MoldResult("silicone_box", parts, info, warnings)


# ------------------------------------------------------------------ разъёмная форма
@dataclass
class TwoPartOptions:
    wall: float = 10.0               # толщина формы вокруг модели
    split: Optional[float] = None    # высота разъёма от низа модели, мм; None — по самому широкому сечению
    sprue_d: float = 6.0             # диаметр литника
    vents: bool = True               # воздушники от верхних точек модели
    keys: bool = True                # замки-полусферы
    bolts: bool = False              # отверстия под болты M4 (обычно хватает резинок или струбцин)
    clearance: float = 0.25          # зазор в замках
    density: float = 1.20            # плотность заливки, г/см³


def widest_split(model: Manifold, h: float) -> float:
    """Высота с наибольшей площадью горизонтального сечения — по ней разъём даёт меньше всего поднутрений."""
    best_z, best_a = h / 2, -1.0
    for z in np.linspace(h * 0.06, h * 0.94, 45):
        a = model.slice(float(z)).area()
        if a > best_a + 1e-6:
            best_z, best_a = float(z), a
    return best_z


def undercut_area(tm: trimesh.Trimesh, split: float) -> float:
    """Площадь поверхности, «прячущейся» за жёсткой формой: снизу от разъёма — грани, смотрящие вверх,
    сверху — смотрящие вниз (на изделии). Такие места не дадут вынуть отливку. Возвращает см²."""
    c = tm.triangles_center[:, 2]
    nz = tm.face_normals[:, 2]
    a = tm.area_faces
    bad = ((c < split - 0.3) & (nz > 0.15)) | ((c > split + 0.3) & (nz < -0.15))
    return float(a[bad].sum()) / 100.0


def _peaks(tm: trimesh.Trimesh, split: float, cells: int = 4) -> list[np.ndarray]:
    """Локальные вершины модели (кандидаты для литника и воздушников), сверху вниз."""
    v = tm.vertices
    lo, hi = v[:, :2].min(0), v[:, :2].max(0)
    span = np.maximum(hi - lo, 1e-6)
    ij = np.minimum(((v[:, :2] - lo) / span * cells).astype(int), cells - 1)
    best: dict[tuple[int, int], np.ndarray] = {}
    for k, (i, j) in enumerate(map(tuple, ij)):
        if (i, j) not in best or v[k, 2] > best[(i, j)][2]:
            best[(i, j)] = v[k]
    out = []
    for (i, j), p in best.items():
        nb = [best.get((i + di, j + dj)) for di in (-1, 0, 1) for dj in (-1, 0, 1) if (di or dj)]
        if p[2] > split + 2 and all(q is None or q[2] <= p[2] + 1e-6 for q in nb):
            out.append(p)
    return sorted(out, key=lambda p: -p[2])


def two_part(model_tm: trimesh.Trimesh, o: TwoPartOptions | None = None) -> MoldResult:
    o = o or TwoPartOptions()
    model = centered(to_manifold(model_tm))
    x0, y0, _, x1, y1, h = model.bounding_box()
    tm = to_trimesh(model)
    split = float(o.split) if o.split is not None else widest_split(model, h)
    split = min(max(split, 1.0), h - 1.0)
    W, D = (x1 - x0) + 2 * o.wall, (y1 - y0) + 2 * o.wall
    z_lo, z_hi = -o.wall, h + o.wall
    block = Manifold.extrude(rrect(W, D, 3.0), z_hi - z_lo).translate((0, 0, z_lo))
    cav = block - model

    # литник с воронкой — от самой высокой точки модели; воздушники — от остальных вершин
    peaks = _peaks(tm, split)
    top = peaks[0] if peaks else tm.vertices[np.argmax(tm.vertices[:, 2])]
    sr = o.sprue_d / 2
    channels = rod("z", sr, float(top[2]) - 1.0, z_hi + 1, float(top[0]), float(top[1]))
    funnel = Manifold.cylinder(6.0, sr, sr * 2.2, 48).translate((float(top[0]), float(top[1]), z_hi - 5.99))
    channels += funnel
    vents = []
    if o.vents:
        for p in peaks[1:]:
            if np.linalg.norm(p[:2] - top[:2]) > 8 and len(vents) < 4:
                vents.append(p)
                channels += rod("z", 1.0, float(p[2]) - 0.5, z_hi + 1, float(p[0]), float(p[1]))
    cav -= channels

    big = 10 * (W + D + h)
    bottom = cav ^ box(-big, big, -big, big, -big, split)
    upper = cav ^ box(-big, big, -big, big, split, big)

    # замки по углам: полусферы на нижней половине, гнёзда в верхней
    kx, ky = W / 2 - o.wall / 2, D / 2 - o.wall / 2
    if o.keys and o.wall >= 7:
        kr = min(4.0, o.wall / 2 - 1.0)
        for sx in (-1, 1):
            for sy in (-1, 1):
                s = Manifold.sphere(kr, 32).translate((sx * kx, sy * ky, split))
                bottom += s ^ box(-big, big, -big, big, split, big)
                upper -= Manifold.sphere(kr + o.clearance, 32).translate((sx * kx, sy * ky, split))
    # болты M4 насквозь: посередине длинных сторон
    bolt_txt = ""
    if o.bolts and o.wall >= 8:
        n = 0
        for sx in ((-0.25, 0.25) if W > 60 else (0.0,)):
            for sy in (-1, 1):
                hole = rod("z", 2.2, z_lo - 1, z_hi + 1, sx * W, sy * ky)
                bottom -= hole
                upper -= hole
                n += 1
        L = int(math.ceil((z_hi - z_lo + 8) / 5) * 5)
        bolt_txt = (f"{n} болта M4×{L} с гайками" if L <= 60 else f"{n} шпильки M4 длиной {L} мм с гайками") + \
            " — или просто струбцины / резинки"
    # пазы для разъёма отвёрткой — на двух торцах по линии разъёма
    for sx in (-1, 1):
        notch = box(sx * W / 2 - 3, sx * W / 2 + 3, -5, 5, split - 1.0, split + 1.0)
        bottom -= notch
        upper -= notch
    # верхняя половина печатается разъёмом вверх — полость открыта, поддержки внутри не нужны
    upper = upper.rotate((180, 0, 0))

    und = undercut_area(tm, split)
    total = float(tm.area) / 100.0
    warnings = []
    if und > max(0.5, total * 0.02):
        warnings.append(f"Поднутрения {und:.1f} см² ({und / total * 100:.0f}% поверхности): отливка может застрять "
                        "в жёсткой форме. Поверните модель, задайте другую высоту разъёма или сделайте силиконовую форму.")
    vol_cm3 = model.volume() / 1000.0
    info = {
        "split_mm": round(split, 1),
        "split_auto": o.split is None,
        "mold_mm": [round(W, 1), round(D, 1), round(z_hi - z_lo, 1)],
        "cast_cm3": round(vol_cm3, 1),
        "cast_g": round(vol_cm3 * o.density, 0),
        "cast_g_with_reserve": round(vol_cm3 * o.density * 1.15, 0),
        "undercut_cm2": round(und, 1),
        "vents": len(vents),
        "bolts": bolt_txt,
        "steps": [
            "Напечатайте обе половины (они уже лежат полостью вверх)",
            "Обработайте полость разделителем (вазелин, воск или спрей-разделитель)",
            "Сложите половины по замкам и стяните " + ("болтами, резинками или струбцинами" if bolt_txt
                                                       else "резинками, струбцинами или прищепками"),
            f"Заливайте через воронку литника до появления материала в воздушниках ({len(vents)} шт.)"
            if vents else "Заливайте через воронку литника медленно, постукивая по форме, чтобы вышел воздух",
            "После застывания разнимите форму отвёрткой через пазы на торцах и срежьте литник",
        ],
    }
    parts = [_part("Форма — низ", bottom), _part("Форма — верх", upper)]
    return MoldResult("two_part", parts, info, warnings)


def size_warnings(model_tm: trimesh.Trimesh) -> list[str]:
    """Подозрительно маленькая или плоская модель — частая ошибка единиц (файл в сантиметрах/метрах/дюймах)."""
    ext = [float(v) for v in model_tm.extents]
    txt = " × ".join(f"{v:.1f}" for v in ext)
    if max(ext) < 10:
        return [f"Модель очень маленькая: {txt} мм. Если в жизни она больше — файл, скорее всего, сохранён "
                "в сантиметрах или метрах: увеличьте её в «Подготовке» (масштаб ×10 или ×1000) и сделайте опалубку заново."]
    if min(ext) < 1.0:
        return [f"Модель почти плоская: {txt} мм — проверьте масштаб и толщину."]
    return []


def make_mold(model_tm: trimesh.Trimesh, kind: str, options: dict[str, Any] | None = None) -> MoldResult:
    res = _make_mold(model_tm, kind, options)
    res.warnings = size_warnings(model_tm) + res.warnings
    return res


def _make_mold(model_tm: trimesh.Trimesh, kind: str, options: dict[str, Any] | None = None) -> MoldResult:
    options = dict(options or {})
    mat = options.pop("material", None)
    if mat in CAST_MATERIALS:
        options["density"] = CAST_MATERIALS[mat][1]
    if kind == "silicone_box":
        fields_ = SiliconeBoxOptions.__dataclass_fields__
        return silicone_box(model_tm, SiliconeBoxOptions(**{k: v for k, v in options.items() if k in fields_}))
    if kind == "two_part":
        fields_ = TwoPartOptions.__dataclass_fields__
        o = {k: v for k, v in options.items() if k in fields_}
        if o.get("split") in ("", "auto"):
            o["split"] = None
        return two_part(model_tm, TwoPartOptions(**o))
    raise ValueError(f"Неизвестный тип опалубки: {kind}")


__all__ = ["CAST_MATERIALS", "SiliconeBoxOptions", "TwoPartOptions", "MoldResult", "make_mold",
           "silicone_box", "two_part", "to_3mf", "asdict"]
