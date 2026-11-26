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
from manifold3d import CrossSection, FillRule, JoinType, Manifold, Mesh, OpType

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
    comps = man.decompose()
    if len(comps) > 1:                      # несколько тел (башня, подиум, рёбра…) — сливаем в одно
        man = Manifold.batch_boolean(comps, OpType.Add)
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


def teardrop_x(r: float, a0: float, a1: float, y: float, z: float) -> Manifold:
    """Горизонтальное отверстие вдоль x каплей — острым углом вверх: печатается без поддержек."""
    tip = Manifold.cube((a1 - a0, 2 * r, 2 * r), True).rotate((45, 0, 0)) ^ box(-1e4, 1e4, -1e4, 1e4, 0.0, 1e4)
    return rod("x", r, a0, a1, y, z) + tip.translate(((a0 + a1) / 2, y, z))


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
    overhang_cm2: float = 0.0          # сколько печатается «в воздухе» (нужны поддержки), см²
    overhang_z: list[float] = field(default_factory=list)   # на каких высотах, мм от стола


@dataclass
class MoldResult:
    kind: str
    parts: list[MoldPart]
    info: dict[str, Any]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "info": self.info, "warnings": self.warnings,
                "parts": [{"name": p.name, "size_mm": p.size_mm, "overhang_cm2": p.overhang_cm2,
                           "needs_support": p.overhang_cm2 >= SUPPORT_MIN_CM2, "overhang_z": p.overhang_z}
                          for p in self.parts]}


def _part(name: str, m: Manifold) -> MoldPart:
    # убрать вырожденные (нулевой площади) треугольники, которые оставляют булевы операции:
    # иначе после записи в файл они схлопываются и слайсер/проверка видят «незамкнутую» деталь
    m = m.simplify(1e-3)
    t = to_trimesh(m)
    ov, zs = print_overhang(m)
    return MoldPart(name, t, [round(float(v), 1) for v in t.extents], ov, zs)


SUPPORT_MIN_CM2 = 0.5     # меньше — отдельные пятнышки, принтер их перекидывает мостиком без поддержек


def print_overhang(m: Manifold, layer: float = 0.2, reach: float = 0.3) -> tuple[float, list[float]]:
    """Проверка как в слайсере: деталь режется на слои, «в воздухе» — то, что выступает над предыдущим слоем
    дальше reach мм (0.2–0.3 мм на слой 0.2 мм — скос около 45°, держится без поддержек).
    Возвращает площадь без опоры, см², и высоты (мм от стола) самых больших таких мест."""
    bb = m.bounding_box()
    z0, z1 = bb[2], bb[5]
    if z1 - z0 > 150:                       # высокие детали — слоем потолще, проверка та же по углу
        layer, reach = 0.4, 0.5
    tot, spots = 0.0, []
    prev = None
    z = z0 + layer / 2
    while z < z1:
        cs = m.slice(z)
        if prev is not None:
            a = (cs - prev.offset(reach, JoinType.Round, 2.0, 12)).area()
            if a > 0.5:
                tot += a
                spots.append((a, z - z0))
        prev = cs
        z += layer
    spots.sort(reverse=True)
    return round(tot / 100.0, 2), sorted(round(zz, 1) for _, zz in spots[:3])


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
    gap: float = 7.0           # отступ от модели до стенки, мм (толщина силикона по бокам)
    shape: str = "contour"     # contour — стенки по форме модели (меньше силикона); box — прямоугольный короб
    over: float = 8.0          # слой силикона над моделью, мм
    wall: float = 2.4          # толщина стенки опалубки
    base: float = 3.0          # толщина дна
    fastening: str = "clips"   # чем стягивать половины стенок: clips — прищепки/зажимы, bolts — болты M3, none
    bolts: Optional[bool] = None  # старый параметр: True → болты, False → без креплений
    with_base: bool = False    # печатать дно; по умолчанию нет — стенки ставятся на ровный стол/доску
    density: float = 1.10      # плотность силикона, г/см³


LOCK_D, LOCK_W, LOCK_CL = 1.6, 2.4, 0.2   # шип на стыке половин: выступ, ширина, зазор в пазу, мм


def _lock(yc: float, z0: float, z1: float) -> tuple[Manifold, Manifold]:
    """Шип-паз по высоте стыка (плоскость x = 0): шип растёт из половины 1 (x < 0) в паз половины 2."""
    tongue = box(-0.5, LOCK_D, yc - LOCK_W / 2, yc + LOCK_W / 2, z0, z1)
    groove = box(-0.5, LOCK_D + LOCK_CL, yc - LOCK_W / 2 - LOCK_CL, yc + LOCK_W / 2 + LOCK_CL, z0 - 0.01, z1 + 0.01)
    return tongue, groove


def _apply_locks(halves: list[Manifold], locks: list[tuple[Manifold, Manifold]]) -> list[Manifold]:
    if not locks:
        return halves
    t = Manifold.batch_boolean([a for a, _ in locks], OpType.Add)
    g = Manifold.batch_boolean([b for _, b in locks], OpType.Add)
    return [halves[0] + t, halves[1] - g]


FASTEN_TXT = {"clips": "прищепками или канцелярскими зажимами", "bolts": "болтами M3", "none": ""}


def silicone_box(model_tm: trimesh.Trimesh, o: SiliconeBoxOptions | None = None) -> MoldResult:
    o = o or SiliconeBoxOptions()
    if o.bolts is not None:
        o.fastening = "bolts" if o.bolts else "none"
    if o.fastening not in FASTEN_TXT:
        o.fastening = "clips"
    if o.shape == "contour":
        return contour_box(model_tm, o)
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
    # «ушки» на стыках половин (x=0): под прищепки — гладкие (вместе 8 мм), с шипом и пазом, под болты — толще и с отверстиями
    ear_w, ear_l = (4.0, 10.0) if o.fastening == "clips" else (6.0, 9.0)
    locks = []
    if o.fastening != "none":
        for sy in (-1, 1):
            yb = sy * od / 2
            ys = (yb - sy * o.wall * 0.6, yb + sy * ear_l)            # заходит в стенку — одно целое с ней
            ring += box(-ear_w, ear_w, min(ys), max(ys), (-groove_d + 0.01) if groove_d else 0.0, H)
            locks.append(_lock(yb + sy * ear_l / 2, (-groove_d + 0.01) if groove_d else 0.0, H))
            if o.fastening == "bolts":
                for zf in (0.25, 0.75):
                    ear_holes.append(teardrop_x(1.75, -ear_w - 1, ear_w + 1, yb + sy * ear_l / 2, H * zf))
    for h_ in ear_holes:
        ring -= h_
    big = 10 * (ow + od + H)
    halves.append(ring ^ box(-big, 0, -big, big, -big, big))
    halves.append(ring ^ box(0, big, -big, big, -big, big))
    halves = _apply_locks(halves, locks)
    halves = [h_ - Manifold.batch_boolean(ear_holes, OpType.Add) for h_ in halves] if ear_holes else halves

    parts = [_part("Стенка — половина 1", halves[0]), _part("Стенка — половина 2", halves[1])]
    steps_place = ["Поставьте мастер-модель на ровную гладкую поверхность (стекло, пластик, ламинат) и приклейте "
                   "термоклеем, чтобы не всплыла",
                   "Поставьте вокруг половины стенок, вставив шип одной в паз другой" + (f", стяните {FASTEN_TXT[o.fastening]} за ушки" if o.fastening != "none" else "") +
                   "; низ стенок промажьте пластилином или термоклеем, чтобы силикон не подтекал"]
    box_cm3 = (inner.area() * fill_h - model.volume()) / 1000.0
    if not o.with_base:
        return _silicone_result(o, box_cm3, [iw, idp], H, fill_h, parts, steps_place)
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
                   "Вставьте половины стенок в паз дна, шип одной — в паз другой" + (f", стяните {FASTEN_TXT[o.fastening]} за ушки" if o.fastening != "none" else "")]
    return _silicone_result(o, box_cm3, [iw, idp], H, fill_h, parts, steps_place)


def _silicone_result(o: "SiliconeBoxOptions", cavity_cm3: float, inner_mm: list[float], H: float,
                     fill_h: float, parts: list[MoldPart], steps_place: list[str],
                     extra: Optional[dict[str, Any]] = None) -> MoldResult:
    warnings = []
    if o.gap < 5:
        warnings.append("Отступ меньше 5 мм — силиконовая форма будет тонкой и может порваться при извлечении")
    info = {
        "shape": o.shape,
        "inner_mm": [round(v, 1) for v in inner_mm] + [round(H, 1)],
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
    info.update(extra or {})
    return MoldResult("silicone_box", parts, info, warnings)


# ---------------------------------------------------------------- опалубка по контуру модели
def _outer_only(cs: CrossSection) -> CrossSection:
    """Только внешние контуры (внутренние «дворики» заполняем — силикон всё равно туда затечёт)."""
    polys = [np.asarray(p) for p in cs.to_polygons()]
    outer = []
    for p in polys:
        if len(p) < 3:
            continue
        x, y = p[:, 0], p[:, 1]
        if 0.5 * float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) > 0:
            outer.append(p)
    return CrossSection(outer, FillRule.NonZero) if outer else cs


def _bands(model: Manifold, h: float, H: float, dz: float, gap: float) -> tuple[list[float], list[CrossSection]]:
    """Контур полости по высоте: силуэт модели в каждом слое dz + отступ gap; переходы между слоями не круче 45°."""
    zs = [i * dz for i in range(int(math.ceil(H / dz)))]
    big = 1e4
    raw: list[Optional[CrossSection]] = []
    for za in zs:
        if za >= h:
            raw.append(None)
            continue
        band = model ^ box(-big, big, -big, big, za - 1e-3, min(za + dz, h) + 1e-3)
        fp = band.project() if not band.is_empty() else CrossSection()
        raw.append(fp if fp.area() > 1e-6 else None)
    # над моделью (слой силикона сверху) и в «пустых» слоях — контур ближайшего слоя с моделью
    last = None
    for i in range(len(raw)):
        if raw[i] is None:
            raw[i] = last
        else:
            last = raw[i]
    first = next((c for c in raw if c is not None), None)
    raw = [c if c is not None else first for c in raw]
    # сглаживание, чтобы стенка не повторяла окна и балконы:
    # по высоте — объединяем соседние слои в пределах ±6 мм; в плане — «перешагиваем» выемки уже 12 мм
    k = max(1, int(round(6.0 / dz)))
    raw = [CrossSection.batch_boolean(raw[max(0, i - k): i + k + 1], OpType.Add).simplify(0.05) for i in range(len(raw))]
    R = 6.0
    C = [_outer_only(c.offset(gap + R, JoinType.Round, 2.0, 16).offset(-R, JoinType.Round, 2.0, 16)).simplify(0.1)
         for c in raw]
    # не круче 45°: соседние слои отличаются не больше чем на dz (только наращиваем)
    # после каждого шага упрощаем контур, иначе число точек растёт лавинообразно
    for _ in range(2):
        for i in range(1, len(C)):
            C[i] = (C[i] + C[i - 1].offset(-dz, JoinType.Round, 2.0, 12)).simplify(0.05)
        for i in range(len(C) - 2, -1, -1):
            C[i] = (C[i] + C[i + 1].offset(-dz, JoinType.Round, 2.0, 12)).simplify(0.05)
    return zs, [c.simplify(0.05) for c in C]


SUB = 0.25   # шаг скоса стенки по высоте, мм — не больше слоя печати, чтобы слайсер не видел уступов


def _morph(a: CrossSection, b: CrossSection, t: float, d: float) -> CrossSection:
    """Промежуточный контур между a (t=0) и b (t=1), если они отличаются не больше чем на d:
    растёт от a не больше t·d и убывает к b — шаг между соседними t не больше (Δt)·d."""
    u = a + b
    return (u ^ a.offset(t * d, JoinType.Round, 2.0, 12) ^ b.offset((1 - t) * d, JoinType.Round, 2.0, 12)).simplify(0.02)


def contour_box(model_tm: trimesh.Trimesh, o: "SiliconeBoxOptions") -> MoldResult:
    model = centered(to_manifold(model_tm))
    x0, y0, _, x1, y1, h = model.bounding_box()
    fill_h = h + o.over
    H = fill_h + 3.0
    dz = 1.0 if H <= 120 else 2.0
    zs, C = _bands(model, h, H, dz, o.gap)
    groove_d, cl = (1.6 if o.with_base else 0.0), 0.25
    ear_w, ear_l = (4.0, 10.0) if o.fastening == "clips" else (6.0, 9.0)
    big = 1e4
    rings, ears, locks, allsegs = [], [], [], []
    strip = CrossSection.square((2 * ear_w, 2 * big), True)
    for i, (za, c) in enumerate(zip(zs, C)):
        top = min(za + dz, H)
        z0 = za - (groove_d if i == 0 else 0.0)
        outer = c.offset(o.wall, JoinType.Round, 2.0, 24)
        # переход к следующему слою — не ступенькой dz (слайсер видит плоский уступ и ставит поддержки),
        # а мелкими шагами ≤ SUB мм: настоящий скос 45°, печатается без поддержек
        segs = [(z0, top, c, outer)]
        nxt = C[i + 1] if i + 1 < len(C) else None
        if nxt is not None and top - za > dz - 1e-6 and (c - nxt).area() + (nxt - c).area() > 0.5:
            n = max(1, int(math.ceil(dz / SUB)))
            segs = [(z0, za + dz / n, c, outer)]
            for k in range(1, n):
                sc = _morph(c, nxt, k / n, dz)
                segs.append((za + k * dz / n, za + (k + 1) * dz / n, sc, sc.offset(o.wall, JoinType.Round, 2.0, 24)))
        allsegs.extend(segs)
    # ушки — ровной вертикальной полосой с прямым краем (удобно ставить прищепки, шип идёт прямо),
    # от стенки, где бы она ни была на этой высоте, до общего края
    edge = {}
    for zb, zt, sc, ou in allsegs:
        rings.append(Manifold.extrude(ou - sc, zt - zb + 0.01).translate((0, 0, zb)))
        sb = (ou ^ strip).bounds()
        edge[-1] = min(edge.get(-1, 0.0), sb[1])
        edge[1] = max(edge.get(1, 0.0), sb[3])
        if o.fastening != "none":
            for yb, sy in ((sb[1], -1), (sb[3], 1)):
                ears.append((sy, yb - sy * o.wall * 0.6, max(zb, 0.0), zt))
    if o.fastening != "none":
        ears = [box(-ear_w, ear_w, min(y0_, edge[sy] + sy * ear_l), max(y0_, edge[sy] + sy * ear_l), zb, zt + 0.01)
                for sy, y0_, zb, zt in ears]
        for sy in (-1, 1):
            locks.append(_lock(edge[sy] + sy * ear_l / 2, 0.0, H + 0.01))
    walls = Manifold.batch_boolean(rings + ears, OpType.Add)
    halves = [walls ^ box(-big, 0, -big, big, -big, big), walls ^ box(0, big, -big, big, -big, big)]
    halves = _apply_locks(halves, locks)
    if o.fastening == "bolts":                      # отверстия под болты на ¼ и ¾ высоты (насквозь, и через шип)
        for zf in (0.25, 0.75):
            z = H * zf
            for sy in (-1, 1):
                hole = teardrop_x(1.75, -ear_w - 1, ear_w + 1, edge[sy] + sy * ear_l / 2, z)
                halves = [h_ - hole for h_ in halves]
    parts = [_part("Стенка — половина 1", halves[0]), _part("Стенка — половина 2", halves[1])]

    vol = sum(c.area() * max(0.0, min(dz, fill_h - za)) for za, c in zip(zs, C))
    cavity_cm3 = (vol - model.volume()) / 1000.0
    iw, idp = (x1 - x0) + 2 * o.gap, (y1 - y0) + 2 * o.gap
    box_cm3 = (rrect(iw, idp, min(4.0, o.gap)).area() * fill_h - model.volume()) / 1000.0
    cb = C[0].bounds()
    fasten = f", стяните {FASTEN_TXT[o.fastening]} за ушки" if o.fastening != "none" else ""
    steps_place = ["Поставьте мастер-модель на ровную гладкую поверхность (стекло, пластик, ламинат) и приклейте "
                   "термоклеем, чтобы не всплыла",
                   "Поставьте вокруг половины стенок, вставив шип одной в паз другой" + fasten +
                   "; низ стенок промажьте пластилином или термоклеем, чтобы силикон не подтекал"]
    if o.with_base:
        foot = C[0]
        plate = Manifold.extrude(foot.offset(o.wall + 6.0, JoinType.Round, 2.0, 24), o.base).translate((0, 0, -o.base))
        slot = Manifold.extrude(foot.offset(o.wall + cl, JoinType.Round, 2.0, 24) - foot.offset(-cl, JoinType.Round, 2.0, 24),
                                groove_d + 0.01).translate((0, 0, -groove_d))
        plate -= slot
        mp = model.project()
        if mp.area() > 1:
            plate -= Manifold.extrude(mp.offset(0.6, JoinType.Round, 2.0, 24) - mp.offset(-0.6, JoinType.Round, 2.0, 24),
                                      0.5).translate((0, 0, -0.45))
        parts.insert(0, _part("Дно опалубки", plate))
        steps_place = ["Приклейте мастер-модель к дну по отметке (термоклей или двусторонний скотч)",
                       "Вставьте половины стенок в паз дна, шип одной — в паз другой" + fasten]
    extra = {"box_silicone_g": round(box_cm3 * o.density, 0),
             "saved_pct": round(max(0.0, (1 - cavity_cm3 / box_cm3)) * 100) if box_cm3 > 0 else 0}
    return _silicone_result(o, cavity_cm3, [cb[2] - cb[0], cb[3] - cb[1]], H, fill_h, parts, steps_place, extra)


# ------------------------------------------------------------------ разъёмная форма
@dataclass
class TwoPartOptions:
    wall: float = 10.0               # толщина формы вокруг модели
    split: Optional[float] = None    # высота разъёма от низа модели, мм (горизонтальный разъём); None — авто
    axis: str = "auto"               # направление разъёма: auto — с наименьшими поднутрениями; z — горизонтальный;
                                     # vertical — вертикальный (x или y, что лучше)
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


# направления разъёма: поворот модели, после которого плоскость разъёма горизонтальна (ось → z),
# матрица этого поворота и куда после него смотрит «верх» модели (для литника)
SPLIT_AXES = {
    "z": ((0, 0, 0), np.eye(3), np.array([0.0, 0.0, 1.0]), "horizontal"),
    "x": ((0, 90, 0), np.array([[0, 0, 1], [0, 1, 0], [-1, 0, 0]], float), np.array([1.0, 0.0, 0.0]), "vertical"),
    "y": ((-90, 0, 0), np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float), np.array([0.0, 1.0, 0.0]), "vertical"),
}


def _oriented(model: Manifold, axis: str) -> tuple[Manifold, np.ndarray, np.ndarray]:
    """Модель в системе, где разъём горизонтален; возвращает (модель, матрица, сдвиг) для пересчёта точек."""
    rot, R, _, _ = SPLIT_AXES[axis]
    m = model.rotate(rot) if axis != "z" else model
    bb = m.bounding_box()
    shift = np.array([-(bb[0] + bb[3]) / 2, -(bb[1] + bb[4]) / 2, -bb[2]])
    return m.translate(tuple(shift)), R, shift


def _choose_axis(model: Manifold, o: "TwoPartOptions") -> tuple[str, float, float, dict[str, float]]:
    """Перебор направлений: для каждого — разъём по самому широкому сечению и площадь поднутрений.
    Горизонтальный предпочитаем при почти равных поднутрениях (его проще заливать)."""
    want = o.axis if o.axis in SPLIT_AXES else "auto"
    cands = ["z"] if (want == "z" or o.split is not None) else (["x", "y"] if want == "vertical"
                                                                 else [want] if want in ("x", "y") else ["z", "x", "y"])
    scores: dict[str, float] = {}
    best: Optional[tuple[str, float, float]] = None
    for ax in cands:
        m, _, _ = _oriented(model, ax)
        h = m.bounding_box()[5]
        sp = float(o.split) if (o.split is not None and ax == "z") else widest_split(m, h)
        sp = min(max(sp, 1.0), h - 1.0)
        und = undercut_area(to_trimesh(m), sp)
        scores[ax] = round(und, 2)
        if best is None or und < best[2] - max(0.3, best[2] * 0.15):
            best = (ax, sp, und)
    assert best is not None
    return best[0], best[1], best[2], scores


def two_part(model_tm: trimesh.Trimesh, o: TwoPartOptions | None = None) -> MoldResult:
    o = o or TwoPartOptions()
    model0 = centered(to_manifold(model_tm))
    tm0 = to_trimesh(model0)
    h0 = model0.bounding_box()[5]
    axis, split, und, scores = _choose_axis(model0, o)
    model, R, shift = _oriented(model0, axis)
    up = SPLIT_AXES[axis][2]                           # «верх» модели в системе формы
    k_up = int(np.argmax(np.abs(up)))
    to_mold = lambda pt: R @ np.asarray(pt, float) + shift   # noqa: E731
    x0, y0, _, x1, y1, h = model.bounding_box()
    W, D = (x1 - x0) + 2 * o.wall, (y1 - y0) + 2 * o.wall
    z_lo, z_hi = -o.wall, h + o.wall
    block = Manifold.extrude(rrect(W, D, 3.0), z_hi - z_lo).translate((0, 0, z_lo))
    cav = block - model
    lim_hi = np.array([W / 2, D / 2, z_hi])

    # литник с воронкой — от самой высокой точки модели (как её будут заливать), воздушники — от других вершин
    peaks = _peaks(tm0, split if axis == "z" else h0 * 0.45)
    top0 = peaks[0] if peaks else tm0.vertices[np.argmax(tm0.vertices[:, 2])]
    sr = o.sprue_d / 2
    names = "xyz"

    vmold = tm0.vertices @ R.T + shift

    def entry(p0: np.ndarray) -> np.ndarray:
        """Точка входа канала в системе формы. При вертикальном разъёме канал кладём прямо на линию разъёма
        (половина канала в каждой половине формы — легко вынуть и почистить), если верх модели её пересекает."""
        pt = to_mold(p0)
        if k_up == 2:
            return pt
        near = vmold[(np.abs(vmold[:, k_up] - pt[k_up]) < 1.5) & (np.linalg.norm(vmold[:, :2] - pt[:2], axis=1) < 25)]
        if len(near) and near[:, 2].min() <= split <= near[:, 2].max():
            on = near[np.argmin(np.abs(near[:, 2] - split))]
            pt = np.array([on[0], on[1], split])
        return pt

    def channel(pt: np.ndarray, r: float) -> Manifold:
        a0, a1 = pt[k_up] - 1.0, lim_hi[k_up] + 1.0
        q = [pt[t] for t in range(3) if t != k_up]
        return rod(names[k_up], r, float(a0), float(a1), float(q[0]), float(q[1]))

    tp = entry(top0)
    channels = channel(tp, sr)
    cone = Manifold.cylinder(6.0, sr, sr * 2.2, 48)
    if k_up == 0:
        funnel = cone.rotate((0, 90, 0)).translate((lim_hi[0] - 5.99, float(tp[1]), float(tp[2])))
    elif k_up == 1:
        funnel = cone.rotate((-90, 0, 0)).translate((float(tp[0]), lim_hi[1] - 5.99, float(tp[2])))
    else:
        funnel = cone.translate((float(tp[0]), float(tp[1]), z_hi - 5.99))
    channels += funnel
    vents = []
    if o.vents:
        for pk in peaks[1:]:
            if np.linalg.norm(pk[:2] - top0[:2]) > 8 and len(vents) < 4:
                vents.append(pk)
                channels += channel(entry(pk), 1.0)
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
                sph = Manifold.sphere(kr, 32).translate((sx * kx, sy * ky, split))
                bottom += sph ^ box(-big, big, -big, big, split - 0.3, big)       # чуть утоплена — срастается с формой
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
    # пазы для разъёма отвёрткой — на двух торцах по линии разъёма (не там, где выходит литник)
    for sgn in (-1, 1):
        if k_up == 0:
            notch = box(-5, 5, sgn * D / 2 - 3, sgn * D / 2 + 3, split - 1.0, split + 1.0)
        else:
            notch = box(sgn * W / 2 - 3, sgn * W / 2 + 3, -5, 5, split - 1.0, split + 1.0)
        bottom -= notch
        upper -= notch
    # верхняя половина печатается разъёмом вверх — полость открыта, поддержки внутри не нужны
    upper = upper.rotate((180, 0, 0))

    total = float(tm0.area) / 100.0
    warnings = []
    if und > max(0.5, total * 0.02):
        warnings.append(f"Поднутрения {und:.1f} см² ({und / total * 100:.0f}% поверхности) при лучшем направлении разъёма: "
                        "отливка может застрять в жёсткой форме. Для такой модели лучше силиконовая форма.")
    vertical = SPLIT_AXES[axis][3] == "vertical"
    desc = (f"вертикальный, поперёк оси {axis.upper()} — модель режется вдоль высоты" if vertical
            else f"горизонтальный, на высоте {split:.1f} мм")
    vol_cm3 = model.volume() / 1000.0
    info = {
        "split_mm": round(split, 1),
        "split_auto": o.split is None,
        "split_axis": axis,
        "split_desc": desc,
        "undercut_by_axis": scores,
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
            ("Поставьте форму воронкой вверх (модель стоит вертикально) и заливайте" if vertical else "Заливайте") +
            (f" через воронку литника до появления материала в воздушниках ({len(vents)} шт.)" if vents
             else " через воронку литника медленно, постукивая по форме, чтобы вышел воздух"),
            "После застывания разнимите форму отвёрткой через пазы по линии разъёма и срежьте литник",
        ],
    }
    parts = [_part("Форма — половина 1" if vertical else "Форма — низ", bottom),
             _part("Форма — половина 2" if vertical else "Форма — верх", upper)]
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
