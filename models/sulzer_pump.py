"""Фигурка «насос Sulzer на площадке» для 3D-печати (по мотивам сувенира iGateway).

Модель строится из точных примитивов (manifold3d), поэтому она сразу замкнутая и пригодна для печати.
Каждый цвет — отдельная деталь. Печать в два захода (под AMS lite на 4 цвета):
  часть 1 — подставка (коричневый), надписи (белый), площадка с трубой (серый), ограждение (жёлтый);
  часть 2 — насос (красный), надпись SULZER (белый), шланг (чёрный); ставится на штырь площадки и приклеивается.

Запуск: python models/sulzer_pump.py <папка_вывода>
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np
import trimesh
from fontTools.pens.basePen import BasePen
from fontTools.ttLib import TTFont
from manifold3d import CrossSection, FillRule, JoinType, Manifold, Mesh

SEG = 96
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# ---------------------------------------------------------------- основные размеры, мм
BASE_W, BASE_D, BASE_H, BASE_R = 100.0, 52.0, 18.0, 14.0
PLAT_W, PLAT_D, PLAT_H, PLAT_R = 88.0, 40.0, 3.0, 6.0
Z0 = BASE_H + PLAT_H                      # верх площадки
PUMP = (8.0, 0.0)                          # центр насоса (x, y)
PIPE_X = -30.0
HOSE_X = 32.0
EMBOSS = 0.8                               # высота рельефа надписей


# ---------------------------------------------------------------- примитивы
def cyl(r: float, h: float, x: float = 0, y: float = 0, z: float = 0, seg: int = SEG) -> Manifold:
    return Manifold.cylinder(h, r, r, seg).translate((x, y, z))


def rod_x(r: float, x0: float, x1: float, y: float, z: float) -> Manifold:
    return Manifold.cylinder(x1 - x0, r, r, 48).rotate((0, 90, 0)).translate((x0, y, z))


def rod_y(r: float, y0: float, y1: float, x: float, z: float) -> Manifold:
    return Manifold.cylinder(y1 - y0, r, r, 48).rotate((-90, 0, 0)).translate((x, y0, z))


def quarter_torus(R: float, r: float) -> Manifold:
    """Четверть тора в плоскости XY: от направления +X (угол 0) до +Y (угол 90°)."""
    return Manifold.revolve(CrossSection.circle(r, 48).translate((R, 0)), SEG, 90)


def stadium(w: float, d: float, rad: float) -> CrossSection:
    return CrossSection.square((w - 2 * rad, d - 2 * rad), True).offset(rad, JoinType.Round, 2.0, SEG)


# ---------------------------------------------------------------- текст
class _FlatPen(BasePen):
    def __init__(self, glyph_set, steps: int = 10):
        super().__init__(glyph_set)
        self.contours: list[list[tuple[float, float]]] = []
        self.cur: list[tuple[float, float]] = []
        self.steps = steps

    def _moveTo(self, p):
        self.cur = [p]

    def _lineTo(self, p):
        self.cur.append(p)

    def _curveToOne(self, p1, p2, p3):
        p0 = self.cur[-1]
        for i in range(1, self.steps + 1):
            t = i / self.steps
            a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t ** 2, t ** 3
            self.cur.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))

    def _qCurveToOne(self, p1, p2):
        p0 = self.cur[-1]
        for i in range(1, self.steps + 1):
            t = i / self.steps
            a, b, c = (1 - t) ** 2, 2 * (1 - t) * t, t ** 2
            self.cur.append((a * p0[0] + b * p1[0] + c * p2[0], a * p0[1] + b * p1[1] + c * p2[1]))

    def _closePath(self):
        pts = self.cur
        if len(pts) > 1 and pts[0] == pts[-1]:
            pts = pts[:-1]
        if len(pts) >= 3:
            self.contours.append(pts)
        self.cur = []

    _endPath = _closePath


def text_section(text: str, cap_height: float, spacing: float = 0.0) -> CrossSection:
    """Контуры текста по центру (x), базовая линия на y=0, высота заглавных = cap_height."""
    font = TTFont(FONT)
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    cap = getattr(font["OS/2"], "sCapHeight", 0) or font["head"].unitsPerEm * 0.73
    k = cap_height / cap
    contours = []
    x = 0.0
    for ch in text:
        name = cmap[ord(ch)]
        pen = _FlatPen(gs)
        gs[name].draw(pen)
        for c in pen.contours:
            contours.append(np.array([(x + px * k, py * k) for px, py in c], dtype=float))
        x += font["hmtx"][name][0] * k + spacing
    cs = CrossSection(contours, FillRule.EvenOdd)
    (x0, _y0, x1, _y1) = cs.bounds()
    return cs.translate((-(x0 + x1) / 2, 0))


def swirl_section(size: float) -> CrossSection:
    """Логотип-завиток (спираль), диаметр ≈ size."""
    width = size * 0.11
    pitch = size * 0.21
    pts_l, pts_r = [], []
    turns = 1.7
    n = 160
    r0 = size * 0.12
    for i in range(n + 1):
        th = turns * 2 * math.pi * i / n
        r = r0 + pitch * th / (2 * math.pi)
        x, y = r * math.cos(th), r * math.sin(th)
        dx = pitch / (2 * math.pi) * math.cos(th) - r * math.sin(th)
        dy = pitch / (2 * math.pi) * math.sin(th) + r * math.cos(th)
        L = math.hypot(dx, dy)
        nx, ny = dy / L, -dx / L
        pts_l.append((x + nx * width / 2, y + ny * width / 2))
        pts_r.append((x - nx * width / 2, y - ny * width / 2))
    poly = np.array(pts_l + pts_r[::-1])
    cs = CrossSection([poly], FillRule.NonZero)
    if cs.area() < 0:
        cs = CrossSection([poly[::-1]], FillRule.NonZero)
    return cs.rotate(200)


def on_front_face(cs: CrossSection, front_y: float, z: float, depth: float, inset: float = 0.3) -> Manifold:
    """Рельеф на вертикальной грани, обращённой к −Y; inset — сколько уходит внутрь детали."""
    m = Manifold.extrude(cs, depth + inset).rotate((90, 0, 0))  # локальная z → −Y, локальная y → Z
    return m.translate((0, front_y + inset, z))


def wrap_on_cylinder(cs: CrossSection, center: tuple[float, float], radius: float, z: float,
                     depth: float, inset: float = 0.4) -> Manifold:
    """Рельефная надпись, обёрнутая вокруг вертикального цилиндра (лицом к −Y)."""
    flat = Manifold.extrude(cs, depth + inset).rotate((90, 0, 0)).translate((0, -(radius - inset), z))
    flat = flat.refine_to_length(0.6)
    cx, cy = center

    def warp(v: np.ndarray) -> np.ndarray:
        v = np.array(v, dtype=float)
        rho = -v[:, 1]
        ang = v[:, 0] / radius
        out = np.empty_like(v)
        out[:, 0] = cx + rho * np.sin(ang)
        out[:, 1] = cy - rho * np.cos(ang)
        out[:, 2] = v[:, 2]
        return out
    return flat.warp_batch(warp)


# ---------------------------------------------------------------- детали
def build() -> dict[str, Manifold]:
    parts: dict[str, Manifold] = {}

    # подставка и надписи
    base = Manifold.extrude(stadium(BASE_W, BASE_D, BASE_R), BASE_H)
    front = -BASE_D / 2
    text = on_front_face(text_section("iGateway", 6.2, 0.15), front, 3.0, EMBOSS)
    logo = on_front_face(swirl_section(7.5), front, 12.6, EMBOSS)
    signs = text + logo
    parts["1_подставка_коричневый"] = base - signs
    parts["1_надпись_белый"] = signs

    # площадка + штырь под насос + труба (серый)
    plat = Manifold.extrude(stadium(PLAT_W, PLAT_D, PLAT_R), PLAT_H).translate((0, 0, BASE_H))
    peg = cyl(3.4, 2.0, PUMP[0], PUMP[1], Z0, 48)
    pipe = cyl(6.0, 2.0, PIPE_X, 0, Z0)                                  # фланец у основания
    pipe += cyl(3.8, 44 - Z0, PIPE_X, 0, Z0 + 1)                         # вертикальная труба
    pipe += cyl(4.8, 2.5, PIPE_X, 0, 32)                                 # муфта
    elbow = quarter_torus(6.0, 3.8).rotate((90, 0, 0)).mirror((1, 0, 0)).translate((PIPE_X + 6, 0, 44))
    pipe_end = PUMP[0] - 10.0 - 0.3                                      # торец у корпуса насоса
    pipe += elbow + rod_x(3.8, PIPE_X + 6 - 0.01, pipe_end, 0, 50)
    pipe += rod_x(5.0, -17.0, -15.2, 0, 50)                              # фланец на горизонтали
    pipe += rod_x(4.6, pipe_end - 1.5, pipe_end, 0, 50)                  # фланец у насоса
    parts["1_площадка_и_труба_серый"] = plat + peg + pipe

    # ограждение (жёлтый): по бокам и сзади
    rail = Manifold()
    top, mid = Z0 + 15.0, Z0 + 8.0
    posts = [(sx * 40, y) for sx in (-1, 1) for y in (-15, 0, 15)] + [(-20, 15), (0, 15), (20, 15)]
    for x, y in posts:
        rail += cyl(1.3, 15.0 + 1.0, x, y, Z0, 32)
    for sx in (-1, 1):
        for z in (top, mid):
            rail += rod_y(1.1, -15, 15, sx * 40, z)
    for z in (top, mid):
        rail += rod_x(1.1, -40, 40, 15, z)
    parts["1_ограждение_жёлтый"] = rail

    # насос (красный) + надпись SULZER (белый)
    px, py = PUMP
    pump = cyl(12.5, 3.0, px, py, Z0)
    pump += cyl(10.5, 7.0, px, py, Z0 + 3)
    pump += cyl(11.5, 2.0, px, py, Z0 + 10)
    pump += cyl(10.0, 20.0, px, py, Z0 + 12, 128)
    pump += cyl(11.0, 2.0, px, py, Z0 + 32)
    pump += cyl(9.0, 4.0, px, py, Z0 + 34)
    pump += cyl(7.0, 2.5, px, py, Z0 + 38)
    eye_z = Z0 + 40.5 + 4.2 - 0.6
    eye = Manifold.revolve(CrossSection.circle(1.5, 32).translate((4.2, 0)), 64).rotate((90, 0, 0)).translate((px, py, eye_z))
    pump += eye + cyl(2.6, 1.5, px, py, Z0 + 40.3, 32)
    # болты на фланце
    for k in range(8):
        a = 2 * math.pi * k / 8
        pump += cyl(0.9, 1.0, px + 11.3 * math.cos(a), py + 11.3 * math.sin(a), Z0 + 3, 16)
    pump -= cyl(3.7, 2.4, px, py, Z0 - 0.01, 48)                        # гнездо под штырь площадки
    label = wrap_on_cylinder(text_section("SULZER", 3.6, 0.15), (px, py), 10.03, Z0 + 20.0, EMBOSS)
    parts["2_насос_красный"] = pump - label
    parts["2_надпись_SULZER_белый"] = label

    # шланг (чёрный): из верхней части насоса — дуга — вниз к площадке
    hz = Z0 + 36.0
    hose = rod_x(2.2, px + 6.0, HOSE_X - 8.0, py, hz)
    bend = quarter_torus(8.0, 2.2).rotate((90, 0, 0)).rotate((0, -90, 0)).mirror((1, 0, 0)).translate((HOSE_X - 8.0, py, hz - 8.0))
    hose += bend + cyl(2.2, hz - 8.0 - Z0 + 0.01, HOSE_X, py, Z0)
    hose += cyl(3.0, 2.0, HOSE_X, py, Z0, 32)                            # хомут у площадки
    parts["2_шланг_чёрный"] = hose - pump - label

    return parts


COLORS = {
    "коричневый": (110, 62, 40), "белый": (245, 245, 245), "серый": (165, 170, 175),
    "жёлтый": (245, 200, 20), "красный": (210, 30, 35), "чёрный": (30, 30, 32),
}


def to_trimesh(m: Manifold, name: str) -> trimesh.Trimesh:
    mesh: Mesh = m.to_mesh()
    t = trimesh.Trimesh(np.asarray(mesh.vert_properties)[:, :3], np.asarray(mesh.tri_verts), process=True)
    color = next((c for k, c in COLORS.items() if k in name), (200, 200, 200))
    t.visual.face_colors = list(color) + [255]
    return t


def write_3mf(path: Path, assemblies: list[tuple[str, list[tuple[str, trimesh.Trimesh]], tuple[float, float]]],
              title: str) -> None:
    """3MF по стандарту: каждая сборка — один объект из частей (components), части = цвета.
    Bambu Studio / OrcaSlicer / PrusaSlicer открывают такую сборку как «объект с несколькими частями»."""
    import zipfile
    from xml.sax.saxutils import quoteattr

    objs: list[str] = []
    build_items: list[str] = []
    oid = 0
    for asm_name, parts, (ox, oy) in assemblies:
        zmin = min(t.bounds[0][2] for _, t in parts)
        comp_ids = []
        for part_name, t in parts:
            oid += 1
            comp_ids.append(oid)
            v = t.vertices - np.array([0.0, 0.0, zmin])
            vx = "".join(f'<vertex x="{a:.4f}" y="{b:.4f}" z="{c:.4f}"/>' for a, b, c in v)
            tr = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in t.faces)
            objs.append(f'<object id="{oid}" type="model" name={quoteattr(part_name)}><mesh><vertices>{vx}</vertices>'
                        f'<triangles>{tr}</triangles></mesh></object>')
        oid += 1
        comps = "".join(f'<component objectid="{c}"/>' for c in comp_ids)
        objs.append(f'<object id="{oid}" type="model" name={quoteattr(asm_name)}><components>{comps}</components></object>')
        build_items.append(f'<item objectid="{oid}" transform="1 0 0 0 1 0 0 0 1 {ox:.3f} {oy:.3f} 0"/>')
    model = ('<?xml version="1.0" encoding="UTF-8"?>\n'
             '<model unit="millimeter" xml:lang="ru-RU" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
             f'<metadata name="Title">{title}</metadata><metadata name="Application">PrintHub</metadata>'
             f'<resources>{"".join(objs)}</resources><build>{"".join(build_items)}</build></model>')
    ctypes = ('<?xml version="1.0" encoding="UTF-8"?>\n<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/></Types>')
    rels = ('<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
            'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", ctypes)
        z.writestr("_rels/.rels", rels)
        z.writestr("3D/3dmodel.model", model)


PART_NAMES = {
    "1_подставка_коричневый": "Подставка (коричневый)", "1_надпись_белый": "Надпись iGateway (белый)",
    "1_площадка_и_труба_серый": "Площадка и труба (серый)", "1_ограждение_жёлтый": "Ограждение (жёлтый)",
    "2_насос_красный": "Насос (красный)", "2_надпись_SULZER_белый": "Надпись SULZER (белый)",
    "2_шланг_чёрный": "Шланг (чёрный)",
}


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    parts = build()
    meshes = {name: to_trimesh(m, name) for name, m in parts.items()}
    for name, t in meshes.items():
        assert t.is_watertight, f"{name}: не замкнута"
        print(f"{name:32s} {t.extents.round(1)} мм  объём {t.volume / 1000:.1f} см³")
    if "--stl" in sys.argv:
        (out / "stl").mkdir(exist_ok=True)
        for name, t in meshes.items():
            t.export(out / "stl" / f"{name}.stl")
    # две модели рядом на столе 256×256: подставка слева, насос справа
    part1 = [(PART_NAMES[n], t) for n, t in meshes.items() if n.startswith("1_")]
    part2 = [(PART_NAMES[n], t.copy().apply_translation((-PUMP[0], -PUMP[1], 0))) for n, t in meshes.items() if n.startswith("2_")]
    write_3mf(out / "Sulzer_iGateway.3mf", [
        ("Sulzer - подставка", part1, (100.0, 128.0)),
        ("Sulzer - насос", part2, (205.0, 128.0)),
    ], "Sulzer iGateway")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "out_sulzer")
