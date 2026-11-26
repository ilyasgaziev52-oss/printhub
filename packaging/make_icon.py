"""Значок SEBE 3D (printhub.ico) в айдентике SEBE: белая плашка, знак «S» и под ним «3D» той же лентой
с шариками на концах, переход от бирюзового к зелёному (см. sebe_mark.py). На 16–24 px — только «S».
Готовый значок лежит в packaging/sebe3d.ico — сборка берёт его.
Перерисовать: python packaging/make_icon.py packaging/sebe3d.ico
Ещё пишет SVG знака для интерфейса: python packaging/make_icon.py --svg printhub/web/logo-s.svg
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
from sebe_mark import BALL, WIDTH, _centerline  # noqa: E402
from sebe_mark import draw_3d, draw_mark, svg_3d  # noqa: E402

TILE = (255, 255, 255, 255)


def draw(size: int) -> Image.Image:
    k = 4
    S = size * k
    s = S / 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=56 * s, fill=TILE)
    if size <= 24:                               # мелко: только знак «S», крупно
        m = draw_mark(int(256 * 0.86 * s))
        off = int((S - m.size[0]) / 2)
        im.paste(m, (off, off + int(2 * s)), m)
    else:                                        # «S» сверху, «3D» лентой снизу
        m = draw_mark(int(256 * 0.42 * s))
        t = draw_3d(int(256 * 0.36 * s))
        im.paste(m, (int((S - m.size[0]) / 2), int(256 * 0.08 * s)), m)
        im.paste(t, (int((S - t.size[0]) / 2), int(256 * 0.52 * s)), t)
    return im.resize((size, size), Image.LANCZOS)


def svg_mark() -> str:
    """Знак S в SVG: лента (средняя линия + круглые концы), шарики, градиент сверху вниз бирюзовый → зелёный."""
    pts = _centerline(160) * 100
    d = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts[30:-30])
    (x0, y0), (x1, y1) = pts[0], pts[-1]
    neck1 = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts[:32])
    neck2 = "M" + " L".join(f"{x:.1f} {y:.1f}" for x, y in pts[-32:])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
            '<defs><linearGradient id="g" x1="0" y1="10" x2="0" y2="90" gradientUnits="userSpaceOnUse">'
            '<stop offset="0" stop-color="#22CBD3"/><stop offset=".42" stop-color="#00A9C2"/>'
            '<stop offset=".62" stop-color="#7AD94C"/><stop offset="1" stop-color="#2FAE4C"/></linearGradient></defs>'
            f'<g fill="none" stroke="url(#g)" stroke-linecap="round" stroke-linejoin="round">'
            f'<path d="{d}" stroke-width="{WIDTH * 100:.1f}"/>'
            f'<path d="{neck1}" stroke-width="7.5"/><path d="{neck2}" stroke-width="7.5"/></g>'
            f'<circle cx="{x0:.1f}" cy="{y0:.1f}" r="{BALL * 100:.1f}" fill="#2ACFD2"/>'
            f'<circle cx="{x1:.1f}" cy="{y1:.1f}" r="{BALL * 100:.1f}" fill="#3CC85A"/></svg>')


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--svg":
        Path(sys.argv[2]).write_text(svg_mark(), encoding="utf-8")
        raise SystemExit(0)
    if len(sys.argv) > 2 and sys.argv[1] == "--svg3d":
        Path(sys.argv[2]).write_text(svg_3d(), encoding="utf-8")
        raise SystemExit(0)
    out = sys.argv[1] if len(sys.argv) > 1 else "printhub.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw(n) for n in sizes]
    imgs[-1].save(out, format="ICO", sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
    imgs[-1].save(out.rsplit(".", 1)[0] + ".png")
