"""Фигурки iGateway (Ingersoll Rand, Enerpac/Norbar, шина, Epiroc) — готовые проекты Bambu Studio для A1.

Каждая фигурка — набор объектов из цветных частей. Объекты разложены по пластинам так, чтобы на одной
пластине было не больше 4 цветов (AMS lite), а мелкие детали, которые иначе меняли бы цвет на каждом слое,
печатались отдельно. Детали садятся на штыри/в гнёзда и приклеиваются.

Запуск:  python models/igateway_figurines.py [папка] [имя ...]
С переменной BAMBU_CLI (путь к bambu-studio) сразу собирает проекты .3mf и проверяет их нарезкой.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import numpy as np
import trimesh
from manifold3d import CrossSection, JoinType, Manifold

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sulzer_pump import (capsule, cyl, on_front_face, tube, rod_x, rod_y, stadium, swirl_section,  # noqa: E402
                         text_section, wrap_on_cylinder, write_3mf)

HEX = {"brown": "#6E3E28", "white": "#F5F5F5", "grey": "#A5AAB0", "yellow": "#F5C814",
       "red": "#D21E23", "black": "#1E1E20", "blue": "#1F5FC8"}
RU = {"brown": "коричневый", "white": "белый", "grey": "серый", "yellow": "жёлтый",
      "red": "красный", "black": "чёрный", "blue": "синий"}
EMB = 0.8
BASE_H = 18.0


# ---------------------------------------------------------------- общие элементы
def back_sign(text: str, w: float, d: float, r: float = 14.0) -> Manifold:
    """Рельефная надпись на задней стороне подставки (на той же высоте, что iGateway спереди — без лишних смен цвета)."""
    cs = fit_text(text, 4.6, w - 2 * r - 8)
    return on_front_face(cs, -d / 2, 3.6, EMB).rotate((0, 0, 180))


def base(w: float, d: float, r: float = 14.0, back: str | None = None) -> tuple[Manifold, Manifold]:
    """Коричневая подставка с белыми логотипом и надписью iGateway спереди и названием бренда сзади."""
    b = Manifold.extrude(stadium(w, d, r), BASE_H)
    front = -d / 2
    signs = on_front_face(text_section("iGateway", 6.2, 0.15), front, 3.0, EMB)
    signs += on_front_face(swirl_section(7.5), front, 12.6, EMB)
    if back:
        signs += back_sign(back, w, d, r)
    return b - signs, signs


def on_cylinder_back(cs: CrossSection, center: tuple[float, float], radius: float, z: float, depth: float) -> Manifold:
    """Рельеф на задней (+Y) стороне вертикального цилиндра."""
    cx, cy = center
    return wrap_on_cylinder(cs, center, radius, z, depth).translate((-cx, -cy, 0)).rotate((0, 0, 180)).translate((cx, cy, 0))


def nameplate(w: float, h: float) -> CrossSection:
    """Табличка с тремя прорезями-«строчками»."""
    cs = CrossSection.square((w, h), True).translate((0, h / 2))
    for k in range(3):
        cs -= CrossSection.square((w - 2.4, 0.45), True).translate((0, h * (0.28 + 0.22 * k)))
    return cs


def platform(w: float = 88.0, d: float = 40.0) -> Manifold:
    return Manifold.extrude(stadium(w, d, 6.0), 3.0).translate((0, 0, BASE_H))


def fit_text(text: str, cap: float, max_w: float) -> CrossSection:
    cs = text_section(text, cap, 0.12)
    x0, _, x1, _ = cs.bounds()
    if x1 - x0 > max_w:
        cs = text_section(text, cap * max_w / (x1 - x0), 0.12)
    return cs


def box(x0, x1, y0, y1, z0, z1) -> Manifold:
    return Manifold.cube((x1 - x0, y1 - y0, z1 - z0)).translate((x0, y0, z0))


def rocks(spec: list[tuple[float, float, float, float]], clip: Manifold, seed: int) -> Manifold:
    """Камни: выпуклые оболочки случайных точек; (x, y, размер, высота)."""
    rng = np.random.default_rng(seed)
    m = Manifold()
    for x, y, s, h in spec:
        pts = []
        for k in range(7):  # плоское дно на подставке
            a = 2 * math.pi * k / 7 + rng.uniform(-0.3, 0.3)
            rr = s / 2 * rng.uniform(0.75, 1.0)
            pts.append((x + rr * math.cos(a), y + rr * 0.8 * math.sin(a), BASE_H))
        for _ in range(9):
            a = rng.uniform(0, 2 * math.pi)
            rr = s / 2 * rng.uniform(0.2, 0.85)
            pts.append((x + rr * math.cos(a), y + rr * 0.8 * math.sin(a), BASE_H + h * rng.uniform(0.45, 1.0)))
        m += Manifold.hull_points(np.array(pts))
    return m ^ clip


def rock_pile(rx: float, ry: float, peak: float, seed: int, n: int = 26,
              size: tuple[float, float] = (10, 14)) -> list[tuple[float, float, float, float]]:
    """Россыпь камней в эллипсе rx×ry: в центре выше и крупнее, к краям мельче."""
    rng = np.random.default_rng(seed)
    out = []
    while len(out) < n:
        x, y = rng.uniform(-rx, rx), rng.uniform(-ry, ry)
        e = (x / rx) ** 2 + (y / ry) ** 2
        if e > 1:
            continue
        k = 1 - 0.6 * math.sqrt(e)
        out.append((x, y, rng.uniform(*size) * (0.6 + 0.4 * k), peak * k * rng.uniform(0.7, 1.0)))
    return out


def railing(posts: list[tuple[float, float]], runs: list[tuple[tuple[float, float], tuple[float, float]]],
            z0: float, sink: float = 1.2) -> tuple[Manifold, Manifold]:
    """Ограждение (отдельная деталь): стойки уходят на sink мм в отверстия площадки. Возвращает (ограждение, отверстия)."""
    rail, holes = Manifold(), Manifold()
    for x, y in posts:
        rail += cyl(1.3, 16.0 + sink, x, y, z0 - sink, 32)
        holes += cyl(1.5, sink + 0.01, x, y, z0 - sink, 32)
    for (xa, ya), (xb, yb) in runs:
        for z in (z0 + 15.0, z0 + 8.0):
            rail += rod_y(1.1, min(ya, yb), max(ya, yb), xa, z) if xa == xb else rod_x(1.1, min(xa, xb), max(xa, xb), ya, z)
    return rail, holes


# ---------------------------------------------------------------- Ingersoll Rand: компрессор на площадке
def ingersoll() -> dict:
    Z0 = BASE_H + 3
    b, signs = base(100, 52, back="INGERSOLL RAND")
    plat = platform()
    pegs = cyl(2.6, 2.0, -30, 0, Z0, 32) + cyl(2.6, 2.0, 4, 0, Z0, 32)
    tank = cyl(9.0, 28.0, 29, 2, Z0, 96) + Manifold.sphere(9.0, 96).scale((1, 1, 0.5)).translate((29, 2, Z0 + 28))
    tank += cyl(3.4, 1.5, 29, 2, Z0 + 33.5, 48) + cyl(2.6, 3.99, 29, 2, Z0 + 31, 48)
    tank += cyl(9.6, 1.6, 29, 2, Z0 + 6, 96) + cyl(9.6, 1.6, 29, 2, Z0 + 22, 96)       # обручи
    rail_l, holes_l = railing([(-42, -16), (-42, -4), (-42, 8)], [((-42, -16), (-42, 8))], Z0)
    rail_r, holes_r = railing([(41, -14), (41, 0), (41, 14)], [((41, -14), (41, 14))], Z0)

    # компрессор (отдельный объект)
    skid = Manifold.extrude(stadium(52, 22, 1.0), 3.0).translate((-12, 0, Z0))
    skid -= cyl(2.9, 2.4, -30, 0, Z0 - 0.01, 32) + cyl(2.9, 2.4, 4, 0, Z0 - 0.01, 32)
    bz0, bz1, fy = Z0 + 3, Z0 + 33, -13.0
    body = Manifold.extrude(stadium(56, 26, 2.0), 30.0).translate((-12, 0, bz0))
    for gx in (-24, 6):                                                          # стыки панелей
        body -= box(gx - 0.4, gx + 0.4, fy - 1, fy + 0.6, bz0 + 2, bz1 - 2)
    for k in range(7):                                                           # вентиляционная решётка
        z = bz0 + 6 + k * 2.6
        body -= box(8.5, 13.5, fy - 1, fy + 0.6, z, z + 1.0)
    for k in range(6):                                                           # решётка сбоку
        z = bz0 + 6 + k * 3.0
        body -= box(16 - 0.6, 17, -8, 8, z, z + 1.2)
    # задняя сторона (y = +13): стыки панелей, дверца с ручкой, табличка, выход воздуха с краном, решётка, заклёпки
    by = 13.0
    for gx in (-24, 6):
        body -= box(gx - 0.4, gx + 0.4, by - 0.6, by + 1, bz0 + 2, bz1 - 2)
    door = box(-37, -27, by - 0.5, by + 1, bz0 + 4, bz1 - 4) - box(-36.2, -27.8, by - 1, by + 2, bz0 + 4.8, bz1 - 4.8)
    body -= door
    body += box(-29.6, -28.6, by - 0.3, by + 1.4, bz0 + 11, bz0 + 19)            # ручка дверцы
    body += box(-30.0, -28.2, by - 0.3, by + 0.7, bz0 + 10.4, bz0 + 11.2) + box(-30.0, -28.2, by - 0.3, by + 0.7, bz0 + 18.8, bz0 + 19.6)
    plate = box(-16, -4, by - 0.3, by + 0.7, bz0 + 19, bz0 + 25)                  # табличка
    for k in range(3):
        plate -= box(-14.5, -5.5, by + 0.4, by + 1, bz0 + 20.3 + k * 1.5, bz0 + 20.8 + k * 1.5)
    body += plate
    ox, oz = -10.0, bz0 + 8.0                                                     # выход сжатого воздуха с краном
    body += rod_y(3.2, by - 0.5, by + 1.5, ox, oz) + rod_y(1.7, by + 1.4, by + 5.0, ox, oz)
    body += Manifold.cylinder(1.8, 2.4, 2.4, 6).rotate((-90, 0, 0)).translate((ox, by + 3.0, oz))   # гайка
    body += rod_y(0.9, by + 4.9, by + 6.2, ox, oz) + box(ox - 0.7, ox + 0.7, by + 3.4, by + 4.4, oz, oz + 5)  # кран
    for k in range(7):                                                            # решётка справа
        z = bz0 + 6 + k * 2.6
        body -= box(8.5, 13.5, by - 0.6, by + 1, z, z + 1.0)
    for x in (-38.5, -25.5, -22.5, 4.5, 7.5, 14.5):                               # заклёпки по углам панелей
        for z in (bz0 + 2.5, bz1 - 2.5):
            body += rod_y(0.6, by - 0.2, by + 0.5, x, z)
    pipes = tube([(-34, -5, bz1 - 1), (-34, -5, Z0 + 40), (-20, -5, Z0 + 40), (-20, -5, bz1 - 1)], 2.4, 4)
    pipes += tube([(-6, 2, bz1 - 1), (-6, 2, Z0 + 50), (29, 2, Z0 + 50), (29, 2, Z0 + 40)], 2.6, 4)
    pipes += cyl(2.6, 5.01, 29, 2, Z0 + 35, 48)                                  # спуск к баку, ровный торец
    blue = body + pipes
    label = on_front_face(fit_text("Ingersoll Rand", 3.0, 27.0), fy, Z0 + 19, EMB)
    screen = box(-36.5, -27.5, fy - 0.8, fy + 0.3, Z0 + 20, Z0 + 27)          # светлый экран, как на фото
    btn_r = rod_y(1.4, fy - 0.9, fy + 0.3, -34, Z0 + 14)
    btn_w = rod_y(1.4, fy - 0.9, fy + 0.3, -30, Z0 + 14)
    blue -= label + screen + btn_r + btn_w
    return {
        "title": "Ingersoll Rand iGateway",
        "colors": ["brown", "white", "grey", "yellow", "blue", "black", "red"],
        "objects": [
            # ограждения печатаются первыми, лёжа (2.6 мм высотой), подставка — последней
            {"name": "Ingersoll Rand — ограждение слева", "plate": 1, "offset": (-50, 40), "print_rot": [(0, 90, 0)],
             "parts": [("Ограждение", rail_l, "yellow")]},
            {"name": "Ingersoll Rand — ограждение справа", "plate": 1, "offset": (50, 40), "print_rot": [(0, 90, 0)],
             "parts": [("Ограждение", rail_r, "yellow")]},
            {"name": "Ingersoll Rand — подставка", "plate": 1, "offset": (0, -97), "parts": [
                ("Подставка", b, "brown"), ("Надпись iGateway", signs, "white"),
                ("Площадка и бак", plat + pegs + tank - holes_l - holes_r, "grey")]},
            {"name": "Ingersoll Rand — компрессор", "plate": 2, "parts": [
                ("Корпус и трубы", blue, "blue"), ("Надпись, экран, кнопка", label + screen + btn_w, "white"),
                ("Рама", skid, "black"), ("Кнопка", btn_r, "red")]},
        ],
        "plate_names": {"1": "Подставка и ограждения", "2": "Компрессор"},
        "note": "ограждения — стойками в отверстия площадки; компрессор — рамой на два штыря, труба на штуцер бака",
    }


# ---------------------------------------------------------------- Enerpac + Norbar: домкрат, гайковёрт, шуруповёрт
def enerpac() -> dict:
    Z0 = BASE_H + 3
    b, signs = base(100, 52, back="ENERPAC  NORBAR")
    plat = platform()
    JX, JY = -16.0, 3.0
    peg = cyl(3.4, 2.0, JX, JY, Z0, 48)

    # гайковёрт Norbar — по замерам с фото. Строится в своей системе координат (центр отверстия x=0, лицевая
    # грань y=-wt/2), потом поворачивается лицом влево-вперёд (виден левый торец с окошком), как на фото.
    wt, rf = 14.0, 2.6                                    # толщина и радиус скругления кромок
    # точки сняты с фото (мм от центра отверстия): верх и низ хвоста идут под наклоном вниз-вправо.
    # Форма = две выпуклые части, каждая — оболочка «дисков» со скруглённой кромкой (радиус rf) на обеих гранях.
    def rdisc(cx: float, cz: float, r: float, y: float) -> Manifold:
        sec = CrossSection.batch_hull([CrossSection.circle(rf, 32).translate((r - rf, 0)),
                                       CrossSection.square((0.01, 2 * rf)).translate((0, -rf))])
        return Manifold.revolve(sec, 96).rotate((90, 0, 0)).translate((cx, y, cz))

    def rslab(circles: list[tuple[float, float, float]]) -> Manifold:
        ye = wt / 2 - rf
        return Manifold.batch_hull([rdisc(cx, cz, r, y) for cx, cz, r in circles for y in (-ye, ye)])

    C_RING, C_CORNER = (0, 9.6, 9.6), (-4.1, 22.2, 5.5)
    w_local = rslab([C_RING, C_CORNER]) + rslab([C_CORNER, (20, 22.4, 3.5), (34, 6.5, 2.5), (13.7, 14.0, 3.7)])
    hole = Manifold.cylinder(wt + 2, 4.6, 4.6, 64).rotate((90, 0, 0)).translate((0, wt / 2 + 1, 9.6))
    # фаски по краям отверстия
    hole += Manifold.cylinder(1.61, 4.6, 6.2, 64).rotate((90, 0, 0)).translate((0, -wt / 2 + 1.6, 9.6))
    hole += Manifold.cylinder(1.61, 4.6, 6.2, 64).rotate((-90, 0, 0)).translate((0, wt / 2 - 1.6, 9.6))
    w_local = (w_local - hole).translate((0, 0, -1.0)) - box(-50, 50, -50, 50, -10, -0.3)   # ровное дно, утоплено на 0.3
    w_local -= box(-10.0, -8.4, -4.0, 4.0, 18.5, 24.5)    # окошко на левом торце
    ncs = fit_text("NORBAR", 3.1, 18.8).rotate(-14).translate((11.3, 0))
    n_local = on_front_face(ncs, -wt / 2, 18.3, 0.45, inset=0.4)
    w_local -= n_local
    # задняя грань гайковёрта: рамка-канавка и два болта
    w_local -= box(3, 17, wt / 2 - 0.5, wt / 2 + 1, 15, 23) - box(3.8, 16.2, wt / 2 - 1, wt / 2 + 2, 15.8, 22.2)
    for bx, bz in ((6, 19), (14, 19)):
        w_local += rod_y(1.0, wt / 2 - 0.3, wt / 2 + 0.6, bx, bz)
    blk_local = Manifold.extrude(stadium(18, wt - 3, 2.0), 10.0).translate((24.0, 0, 0)) - w_local - n_local

    def place_w(m: Manifold) -> Manifold:
        return m.rotate((0, 0, 15)).translate((5.0, -10.0, Z0))
    wrench, norbar, wblock = place_w(w_local), place_w(n_local), place_w(blk_local)

    # шуруповёрт (чёрный): корпус, рукоятка, батарея; серая ступенчатая насадка смотрит влево над гайковёртом
    DY, DZ = 12.5, Z0 + 31
    nozzle = rod_x(4.2, 17, 21, DY, DZ) + rod_x(2.4, 10, 17.1, DY, DZ) + rod_x(1.3, 6.5, 10.1, DY, DZ)
    drill = capsule((23, DY, DZ), (38, DY, DZ), 6.0, 48)
    drill += capsule((36, DY, DZ - 3), (35, DY, Z0 + 8), 4.0, 32)
    drill += Manifold.extrude(stadium(14, 11, 2.0), 7.0).translate((36, DY, Z0))
    drill -= nozzle

    # домкрат Enerpac (отдельный объект): жёлтый штуцер слева (к нему шланг), трубка справа уходит вниз к площадке
    jack = cyl(11.0, 3.0, JX, JY, Z0, 96) + cyl(10.0, 30.0, JX, JY, Z0 + 3, 128) + cyl(10.5, 2.0, JX, JY, Z0 + 33, 96)
    jack += rod_x(2.6, JX - 14.5, JX - 9.0, JY, Z0 + 7) + rod_x(3.1, JX - 13.0, JX - 11.0, JY, Z0 + 7)
    jack += tube([(JX + 8, JY + 4, Z0 + 29), (JX + 13, 9, Z0 + 29.5), (0, 11, Z0 + 26), (0, 11, Z0 + 8)], 1.9, 6)
    jack += cyl(1.9, 8.01, 0, 11, Z0, 32) + cyl(2.7, 1.2, 0, 11, Z0, 32)
    jack -= cyl(3.7, 2.4, JX, JY, Z0 - 0.01, 48)
    ram = cyl(7.5, 5.0, JX, JY, Z0 + 35, 96) + cyl(8.2, 3.5, JX, JY, Z0 + 40, 96)
    ram -= cyl(2.5, 1.0, JX, JY, Z0 + 42.51, 48)
    txt = wrap_on_cylinder(fit_text("ENERPAC.", 3.0, 22.0), (JX, JY), 10.03, Z0 + 21, EMB)
    jack -= txt
    jack += on_cylinder_back(nameplate(10.0, 6.0), (JX, JY), 9.9, Z0 + 12, 0.7)      # табличка сзади
    jack += rod_y(2.0, JY + 9.5, JY + 12.0, JX, Z0 + 26) + rod_y(2.6, JY + 11.4, JY + 12.6, JX, Z0 + 26)  # штуцер сзади

    # шланг: из штуцера влево, петлёй по площадке вперёд, за кольцом гайковёрта — к чёрному упору под его хвостом
    hose = tube([(JX - 14.5, JY, Z0 + 7), (-34, 4, Z0 + 7), (-40, -4, Z0 + 4), (-35, -14.5, Z0 + 2), (-22, -16.5, Z0 + 2),
                 (-11, -13, Z0 + 2), (-6, -3, Z0 + 2), (6, 1.5, Z0 + 2), (15, 3.5, Z0 + 2), (24, 0.0, Z0 + 2)], 2.1, 6)
    hose = (hose - (plat + wrench + jack + ram)) + wblock
    return {
        "title": "Enerpac Norbar iGateway",
        "colors": ["brown", "white", "grey", "black", "yellow"],
        "objects": [
            {"name": "Enerpac — подставка", "plate": 1, "parts": [
                ("Подставка", b, "brown"), ("Надпись iGateway", signs, "white"),
                ("Площадка и гайковёрт", plat + peg + wrench, "grey"), ("Надпись NORBAR", norbar, "black")]},
            # мелкие детали печатаются первыми (ниже 25 мм), домкрат — последним
            {"name": "Enerpac — шланг и упор", "plate": 2, "offset": (-75, -75), "parts": [("Шланг и упор", hose, "black")]},
            {"name": "Enerpac — шуруповёрт", "plate": 2, "offset": (-80, 60), "print_rot": [(90, 0, 0)], "parts": [
                ("Шуруповёрт", drill, "black")]},
            # насадка отдельно: иначе на её высоте цвет менялся бы на каждом слое (≈15 г в отход)
            {"name": "Enerpac — насадка", "plate": 2, "offset": (75, 75), "print_rot": [(0, -90, 0)], "parts": [
                ("Насадка", nozzle, "grey")]},
            {"name": "Enerpac — домкрат", "plate": 2, "offset": (75, -60), "parts": [
                ("Домкрат", jack, "yellow"), ("Шток и надпись ENERPAC", ram + txt, "black")]},
        ],
        "plate_names": {"1": "Подставка", "2": "Домкрат, шланг, шуруповёрт"},
        "note": "домкрат — гнездом на штырь площадки; шланг с упором — упор под хвост гайковёрта, шланг к штуцеру домкрата; шуруповёрт — батареей на площадку",
    }


# ---------------------------------------------------------------- шина на камнях
def tire_local(R: float = 23.0, r_in: float = 9.0, w: float = 24.0) -> Manifold:
    """Шина, ось — Z, лежит на боку (так и печатается: без поддержек)."""
    rad = 3.5
    sec = CrossSection.square((R - 3.0 - r_in - 2 * rad, w - 2 * rad), True).offset(rad, JoinType.Round, 2.0, 48)
    sec = sec.translate(((r_in + R - 3.0) / 2, 0))
    body = Manifold.revolve(sec, 128)
    lugs = Manifold()
    n = 16
    for k in range(n):
        for side, off in ((1, 0.0), (-1, 0.5)):
            lug = Manifold.cube((5.0, 6.0, 10.0), True)
            lug = lug.rotate((side * 22, 0, 0)).translate((R - 2.6, 0, side * 5.6))
            lugs += lug.rotate((0, 0, 360 * (k + off) / n))
    lugs ^= Manifold.cylinder(w, R + 1.0, R + 1.0, 128).translate((0, 0, -w / 2))  # срез по боковинам
    t = body + lugs
    rim = Manifold.cylinder(w - 6, r_in + 2.2, r_in + 2.2, 96).translate((0, 0, -(w - 6) / 2)) - \
        Manifold.cylinder(w, r_in, r_in, 96).translate((0, 0, -w / 2 - 1))
    t += rim
    # боковины: радиальные прорези и кольцевая канавка (на обеих сторонах; углублённые — шина ровно лежит при печати)
    for zs in (w / 2 - 0.5, -w / 2 - 0.5):
        for k in range(20):
            t -= Manifold.cube((3.2, 1.0, 1.0)).translate((13.0, -0.5, zs)).rotate((0, 0, 360 * k / 20))
        t -= (Manifold.cylinder(1.0, 17.4, 17.4, 96) - Manifold.cylinder(1.0, 16.8, 16.8, 96)).translate((0, 0, zs))
    return t.translate((0, 0, w / 2))


def tire() -> dict:
    W, D = 92.0, 46.0
    b, signs = base(W, D, back="MINING TIRE")
    t_local = tire_local()
    # в сборе: стоит на ребре, ось смотрит вперёд-влево
    placed = t_local.translate((0, 0, -12)).rotate((90, 0, 0)).rotate((0, 0, -25)).translate((0, 3, BASE_H + 23 - 2.5))
    clip = Manifold.extrude(stadium(W - 2, D - 2, 13), 20).translate((0, 0, BASE_H - 0.01))
    st = rocks(rock_pile(40, 19, 7.0, 7, size=(11, 15)), clip, 7)
    st -= placed
    b -= placed
    return {
        "title": "Tire iGateway",
        "colors": ["brown", "white", "grey", "black"],
        "objects": [
            # обе детали ниже 25 мм — печатаются по очереди на одной пластине, шина лёжа на боку
            {"name": "Шина — подставка", "plate": 1, "offset": (0, -75), "parts": [
                ("Подставка", b, "brown"), ("Надпись iGateway", signs, "white"), ("Камни", st, "grey")]},
            {"name": "Шина", "plate": 1, "offset": (0, 70), "print_rot": [(0, 0, 25), (-90, 0, 0)],
             "parts": [("Шина", placed, "black")]},
        ],
        "plate_names": {"1": "Подставка и шина"},
        "note": "шина ставится на ребро в выемку среди камней",
    }


# ---------------------------------------------------------------- Epiroc: гидромолот в камнях
def epiroc() -> dict:
    W, D = 92.0, 46.0
    b, signs = base(W, D, back="EPIROC")
    zb = BASE_H
    chisel = cyl(3.2, 14.0, 0, 0, zb, 64)
    chisel += cyl(4.4, 2.0, 0, 0, zb + 14, 64) + cyl(2.4, 3.0, 0, 0, zb + 16, 48)
    clip = Manifold.extrude(stadium(W - 2, D - 2, 13), 20).translate((0, 0, zb - 0.01))
    st = rocks(rock_pile(38, 18, 8.0, 11, size=(10, 14)) + [(0, 0, 14, 8.5)], clip, 11)
    st -= chisel

    # молот (отдельный объект), низ = верх уступа зубила
    z1 = zb + 16
    collar = Manifold.extrude(stadium(16, 14, 4.0), 3.0).translate((0, 0, z1))      # чёрный держатель пики, как на фото
    collar -= cyl(2.7, 3.4, 0, 0, z1 - 0.01, 48)
    body = Manifold.extrude(stadium(22, 18, 2.5), 28.0).translate((0, 0, z1 + 3))
    # задняя сторона (y = +9): рамка панели, болты по углам, табличка, рёбра внизу
    body -= box(-7.5, 7.5, 8.5, 10, z1 + 6, z1 + 27) - box(-6.7, 6.7, 8, 11, z1 + 6.8, z1 + 26.2)
    for x in (-6.0, 6.0):
        for z in (z1 + 8.0, z1 + 25.0):
            body += rod_y(1.0, 8.8, 9.8, x, z)
    body += box(-4, 4, 8.8, 9.6, z1 + 15, z1 + 19.5)
    for x in (-3.0, 0.0, 3.0):
        body += box(x - 0.6, x + 0.6, 8.8, 10.0, z1 + 3.5, z1 + 6.0)
    zt = z1 + 31
    prof = CrossSection.batch_hull([CrossSection.square((22, 4)).translate((-11, 0)),
                                    CrossSection.circle(4.5, 64).translate((-5.5, 8)),
                                    CrossSection.circle(4.5, 64).translate((5.5, 8))])
    prof -= CrossSection.circle(1.8, 32).translate((-5.5, 8)) + CrossSection.circle(1.8, 32).translate((5.5, 8))
    head = Manifold.extrude(prof, 16).rotate((90, 0, 0)).translate((0, 8, zt))
    head -= box(-12, 12, -2.5, 2.5, zt + 5, zt + 14)                            # проушины
    hoses = Manifold()
    for y in (-3.2, 3.2):
        hoses += tube([(10, y, z1 + 25), (16, y, z1 + 26), (21, y, z1 + 20), (21.5, y, z1 + 11), (17, y, z1 + 6),
                       (10, y, z1 + 6)], 1.8, 6)
    hoses += box(20, 23.5, -3.6, 3.6, z1 + 13, z1 + 17)                       # хомут, связывает шланги
    hoses -= body + collar
    txt = on_front_face(fit_text("Epiroc", 3.6, 15.0), -9.0, z1 + 15, EMB)
    body -= txt
    return {
        "title": "Epiroc iGateway",
        "colors": ["brown", "white", "grey", "black", "yellow"],
        "objects": [
            {"name": "Epiroc — подставка", "plate": 1, "parts": [
                ("Подставка", b, "brown"), ("Надпись iGateway", signs, "white"), ("Камни", st, "grey"),
                ("Пика", chisel, "black")]},
            # шланги и молот на одной пластине, но печатаются по очереди («по объектам»):
            # сначала шланги (ниже 25 мм — проходят под балкой A1), потом молот — без смены цвета на каждом слое
            {"name": "Epiroc — шланги", "plate": 2, "offset": (-55, 0), "parts": [("Шланги", hoses, "black")]},
            {"name": "Epiroc — молот", "plate": 2, "offset": (55, 0), "parts": [
                ("Корпус", body, "yellow"), ("Оголовок, держатель, надпись", head + collar + txt, "black")]},
        ],
        "plate_names": {"1": "Подставка", "2": "Молот и шланги"},
        "note": "молот надевается гнездом на штырь пики, шланги приклеиваются к боку молота",
    }


# ---------------------------------------------------------------- Sulzer (геометрия — models/sulzer_pump.py)
def sulzer() -> dict:
    import sulzer_pump as S
    p = S.build()
    back = back_sign("SULZER", 100, 52)
    p["1_подставка_коричневый"] = p["1_подставка_коричневый"] - back
    p["1_надпись_белый"] = p["1_надпись_белый"] + back
    rail, holes = railing([(sx * 40, y) for sx in (-1, 1) for y in (-15, 0, 15)] + [(-20, 15), (0, 15), (20, 15)],
                          [((-40, -15), (-40, 15)), ((40, -15), (40, 15)), ((-40, 15), (40, 15))], S.Z0)
    return {
        "title": "Sulzer iGateway",
        "colors": ["brown", "white", "grey", "yellow", "red", "black"],
        "objects": [
            {"name": "Sulzer — ограждение", "plate": 1, "offset": (0, 22), "parts": [("Ограждение", rail, "yellow")]},
            {"name": "Sulzer — подставка", "plate": 1, "offset": (0, -97), "parts": [
                ("Подставка", p["1_подставка_коричневый"], "brown"), ("Надпись iGateway", p["1_надпись_белый"], "white"),
                ("Площадка и труба", p["1_площадка_и_труба_серый"] - holes, "grey")]},
            {"name": "Sulzer — шланг", "plate": 2, "offset": (-55, 0), "print_rot": [(90, 0, 0)],
             "parts": [("Шланг", p["2_шланг_чёрный"], "black")]},
            {"name": "Sulzer — насос", "plate": 2, "offset": (55, 0), "parts": [
                ("Насос", p["2_насос_красный"], "red"), ("Надпись SULZER", p["2_надпись_SULZER_белый"], "white")]},
        ],
        "plate_names": {"1": "Подставка и ограждение", "2": "Насос и шланг"},
        "note": "ограждение — стойками в отверстия площадки; насос — гнездом на штырь; шланг — к насосу и площадке",
    }


FIGURINES = {"Sulzer": sulzer, "IngersollRand": ingersoll, "Enerpac_Norbar": enerpac, "Tire": tire, "Epiroc": epiroc}


def to_mesh(m: Manifold) -> trimesh.Trimesh:
    mm = m.to_mesh()
    return trimesh.Trimesh(np.asarray(mm.vert_properties)[:, :3], np.asarray(mm.tri_verts), process=False)


def check_assembly(name: str, fig: dict) -> list[str]:
    """Проверка сборки: детали разных объектов не пересекаются, и каждая отдельная деталь чем-то опирается/касается
    остальных (есть где клеить). Детали — в положении сборки."""
    objs = [(o["name"], sum((p[1] for p in o["parts"]), Manifold())) for o in fig["objects"]]
    msgs = []
    for i, (na, a) in enumerate(objs):
        for nb, b in objs[i + 1:]:
            ov = (a ^ b).volume()
            assert ov < 0.5, f"{name}: «{na}» и «{nb}» пересекаются в сборе ({ov:.2f} мм³)"
    dirs = [(0, 0, -1), (0, 0, 1), (1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0)]
    for i, (na, a) in enumerate(objs):
        others = sum((m for j, (_, m) in enumerate(objs) if j != i), Manifold())
        touch = max((a.translate(tuple(0.25 * np.array(d))) ^ others).volume() for d in dirs)
        assert touch > 0.01, f"{name}: «{na}» ни на что не опирается в сборе"
        msgs.append(f"{na}: контакт {touch:.1f} мм³")
    return msgs


def make(name: str, out: Path) -> tuple[Path, dict]:
    fig = FIGURINES[name]()
    colors = fig["colors"]
    if len(fig["objects"]) > 1:
        for m in check_assembly(name, fig):
            print("   ✓", m)
    assemblies, spec_objects = [], []
    for i, o in enumerate(fig["objects"]):
        mans = [p[1] for p in o["parts"]]
        for a in range(len(mans)):
            for c in range(a + 1, len(mans)):
                ov = (mans[a] ^ mans[c]).volume()
                assert ov < 0.5, f"{name}/{o['name']}: части {a} и {c} пересекаются ({ov:.2f} мм³)"
        for rot in o.get("print_rot", []):          # положение для печати
            mans = [m.rotate(rot) for m in mans]
        meshes = [(f"{pn} — {RU[col]}", to_mesh(m)) for (pn, _, col), m in zip(o["parts"], mans)]
        for pn, t in meshes:
            assert t.is_watertight and t.volume > 0, f"{name}/{pn}: не замкнута"
        lo = np.min([t.bounds[0] for _, t in meshes], axis=0)
        hi = np.max([t.bounds[1] for _, t in meshes], axis=0)
        for _, t in meshes:                          # центр объекта — в начале координат, позицию задаёт offset
            t.apply_translation((-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, 0))
        assemblies.append((o["name"], meshes, (100.0 + 60 * i, 128.0)))
        spec_objects.append({"name": o["name"], "plate": o["plate"], "offset": o.get("offset", (0, 0)), "height": float(hi[2] - lo[2]),
                             "parts": [{"name": pn, "filament": colors.index(col) + 1} for (pn, _), (_, _, col) in zip(meshes, o["parts"])]})
        print(f"  {o['name']}: {np.round(hi - lo, 1)} мм, частей {len(meshes)}")
    out.mkdir(parents=True, exist_ok=True)
    generic = out / f"{name}_parts.3mf"
    write_3mf(generic, assemblies, fig["title"])
    spec = {"bed": [256, 256], "bed_type": "Textured PEI Plate", "supports": "tree(auto)",
            "colors": [HEX[c] for c in colors], "plate_names": fig["plate_names"], "objects": spec_objects,
            "print_sequence": "by object"}
    return generic, spec


def main() -> None:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "dist/figurines")
    names = sys.argv[2:] or list(FIGURINES)
    cli = os.environ.get("BAMBU_CLI")
    for name in names:
        print(name)
        generic, spec = make(name, out / "work")
        (out / "work" / f"{name}.json").write_text(json.dumps(spec, ensure_ascii=False, indent=1))
        if cli:
            from bambu_project import build
            rep = build(generic, out / f"{name}_A1.3mf", spec, cli.split(), "Bambu Lab A1 0.4 nozzle",
                        "0.16mm Optimal @BBL A1", "Bambu PLA Basic @BBL A1")
            for p in rep:
                print(f"  пластина {p['plate']}: {p['time_s'] // 3600} ч {p['time_s'] % 3600 // 60} мин, {p['weight_g']} г, высота {p['max_z']} мм —",
                      ", ".join(f"#{i} {c} {g} г" for i, c, g in p["filaments"]))




# ---------------------------------------------------------------- одноцветная версия: каждая фигурка — одна цельная деталь
def solid(fig: dict) -> Manifold:
    """Все части фигурки в положении сборки, слитые в одно тело; внутренние пустоты (зазоры штырь—гнездо) заполнены."""
    m = sum((p[1] for o in fig["objects"] for p in o["parts"]), Manifold())
    comps = m.decompose()
    outer = [c for c in comps if c.volume() > 0]
    return sum(outer, Manifold())


def make_mono(names: list[str], out: Path, color: str = "grey") -> tuple[Path, dict]:
    assemblies, spec_objects = [], []
    for i, name in enumerate(names):
        fig = FIGURINES[name]()
        body = solid(fig)
        n = len([c for c in body.decompose() if c.volume() > 0])
        t = to_mesh(body)
        assert t.is_watertight, f"{name}: не замкнута"
        lo, hi = t.bounds
        t.apply_translation((-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]))
        title = fig["title"].replace(" iGateway", "")
        print(f"  {title}: {np.round(hi - lo, 1)} мм, {t.volume / 1000:.1f} см³, тел: {n}")
        assemblies.append((title, [(title, t)], (128.0, 128.0)))
        spec_objects.append({"name": title, "plate": i + 1, "height": float(hi[2] - lo[2]),
                             "parts": [{"name": title, "filament": 1}]})
    out.mkdir(parents=True, exist_ok=True)
    generic = out / "iGateway_mono_parts.3mf"
    write_3mf(generic, assemblies, "iGateway")
    spec = {"bed": [256, 256], "bed_type": "Textured PEI Plate", "supports": "tree(auto)", "colors": [HEX[color]],
            "plate_names": {str(i + 1): FIGURINES[n]()["title"].replace(" iGateway", "") for i, n in enumerate(names)},
            "objects": spec_objects}
    return generic, spec


def main_mono() -> None:
    out = Path(sys.argv[2] if len(sys.argv) > 2 else "dist/figurines")
    names = list(FIGURINES)
    generic, spec = make_mono(names, out / "work")
    cli = os.environ.get("BAMBU_CLI")
    if cli:
        from bambu_project import build
        rep = build(generic, out / "iGateway_all_grey_A1.3mf", spec, cli.split(), "Bambu Lab A1 0.4 nozzle",
                    "0.16mm Optimal @BBL A1", "Bambu PLA Basic @BBL A1")
        for p in rep:
            print(f"  пластина {p['plate']}: {p['time_s'] // 3600} ч {p['time_s'] % 3600 // 60} мин, {p['weight_g']} г, высота {p['max_z']} мм")


if __name__ == "__main__":
    # python models/igateway_figurines.py --mono [папка]  — все фигурки цельными, одним серым пластиком, в одном проекте
    main_mono() if "--mono" in sys.argv else main()
