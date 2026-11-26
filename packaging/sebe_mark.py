"""Знак SEBE: лента в форме «S» с шариками на концах, градиент вдоль ленты — бирюзовый сверху, зелёный снизу.
Перерисован по логотипу компании, чтобы значок был чётким на любом размере."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

# точки средней линии ленты (доли от размера знака): шарик сверху-справа → шарик снизу-слева
PATH = [(0.84, 0.17), (0.72, 0.20), (0.52, 0.225), (0.34, 0.24), (0.23, 0.33), (0.26, 0.44), (0.44, 0.50),
        (0.62, 0.55), (0.76, 0.62), (0.77, 0.73), (0.64, 0.80), (0.42, 0.81), (0.26, 0.80), (0.16, 0.78)]
# цвета вдоль ленты (t, RGB): бирюзовый → тёмная бирюза в изгибе → зелёный → салатовый → зелёный
STOPS = [(0.00, (42, 207, 210)), (0.10, (0, 195, 207)), (0.33, (0, 169, 194)), (0.50, (25, 183, 143)),
         (0.60, (108, 216, 78)), (0.72, (142, 224, 74)), (0.86, (47, 174, 76)), (1.00, (60, 200, 90))]
WIDTH = 0.165          # толщина ленты
NECK = 0.075           # толщина «шейки» у шариков
BALL = 0.085           # радиус шариков на концах


def _spline(pts: list[tuple[float, float]], n: int = 400) -> np.ndarray:
    P = np.array(pts, float)
    P = np.vstack([P[0] * 2 - P[1], P, P[-1] * 2 - P[-2]])
    out = []
    per = max(2, n // (len(P) - 3))
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for k in range(per):
            t = k / per
            out.append(0.5 * (2 * p1 + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t * t
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t ** 3))
    out.append(P[-2])
    return np.array(out)


def _color(t: float) -> tuple[int, int, int]:
    for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
        if t <= t1:
            k = (t - t0) / (t1 - t0) if t1 > t0 else 0
            return tuple(int(round(a + (b - a) * k)) for a, b in zip(c0, c1))
    return STOPS[-1][1]


def _centerline(n: int = 600) -> np.ndarray:
    """Средняя линия «S» из прямых и полуокружностей (как в логотипе): шарик сверху-справа →
    верхняя перекладина → левый изгиб → средняя перекладина → правый изгиб → нижняя перекладина → шарик."""
    yt, R, xl, xr = 0.20, 0.155, 0.34, 0.66
    pts: list[tuple[float, float]] = []

    def line(a, b, k):
        for i in range(k):
            t = i / k
            pts.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))

    def arc(c, th0, th1, k):
        for i in range(k):
            th = np.radians(th0 + (th1 - th0) * i / k)
            pts.append((c[0] + R * np.cos(th), c[1] + R * np.sin(th)))
    line((0.85, 0.165), (0.62, yt), 40)                 # шейка к верхнему шарику
    line((0.64, yt), (xl, yt), 60)
    arc((xl, yt + R), 270, 90, 90)                      # левый изгиб
    line((xl, yt + 2 * R), (xr, yt + 2 * R), 60)
    arc((xr, yt + 3 * R), -90, 90, 90)                  # правый изгиб
    line((xr, yt + 4 * R), (0.36, yt + 4 * R), 60)
    line((0.38, yt + 4 * R), (0.15, 0.835), 40)        # шейка к нижнему шарику
    pts.append((0.15, 0.835))
    return np.array(pts)


def draw_mark(size: int) -> Image.Image:
    """Знак S на прозрачном фоне, size×size."""
    k = 4
    S = size * k
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    pts = _centerline() * S
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    L = np.concatenate([[0], np.cumsum(seg)])
    for p, l in zip(pts, L):
        t = l / L[-1]
        e = min(t, 1 - t)                                  # расстояние до ближнего конца
        w = NECK + (WIDTH - NECK) * float(np.clip((e - 0.035) / 0.075, 0, 1)) ** 0.7
        r = w * S / 2
        c = _color(t)
        d.ellipse((p[0] - r, p[1] - r, p[0] + r, p[1] + r), fill=c + (255,))
    for p, t in ((pts[0], 0.0), (pts[-1], 1.0)):        # шарики на концах — чуть светлее
        c = _color(t)
        rb = BALL * S
        d.ellipse((p[0] - rb, p[1] - rb, p[0] + rb, p[1] + rb), fill=c + (255,))
    return im.resize((size, size), Image.LANCZOS)


def svg_path_d(scale: float = 100.0) -> str:
    """Средняя линия ленты для SVG (Catmull-Rom → набор отрезков)."""
    pts = _centerline(200) * scale
    return "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts)
