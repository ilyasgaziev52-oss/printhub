"""Значок SEBE 3D (printhub.ico) в айдентике интерфейса: тёмная плашка как шапка программы,
«SEBE» белым, «3D» фирменным синим #2F6FED. На мелких размерах (16–24 px) — только крупное «3D».

Готовый значок лежит в packaging/sebe3d.ico — сборка берёт его (на сборочном сервере нет шрифта Inter).
Перерисовать: python packaging/make_icon.py packaging/sebe3d.ico
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = (47, 111, 237, 255)
DARK = (20, 25, 34, 255)
WHITE = (255, 255, 255, 255)
FONT_DIRS = ["/usr/share/fonts/opentype/inter", "C:/Windows/Fonts"]


def font(names: list[str], px: int) -> ImageFont.FreeTypeFont:
    for d in FONT_DIRS:
        for n in names:
            p = Path(d) / n
            if p.exists():
                return ImageFont.truetype(str(p), px)
    return ImageFont.truetype("DejaVuSans-Bold.ttf", px)


def _center(d: ImageDraw.ImageDraw, cx: float, y: float, txt: str, f: ImageFont.FreeTypeFont, fill) -> None:
    l, t, r, _ = d.textbbox((0, 0), txt, font=f)
    d.text((cx - (l + r) / 2, y - t), txt, font=f, fill=fill)


def draw(size: int) -> Image.Image:
    k = 4                                    # рисуем крупнее и уменьшаем — ровные края
    S = size * k
    s = S / 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=56 * s, fill=DARK)
    if size <= 24:                           # мелко: только «3D», крупно
        _center(d, 128 * s, 62 * s, "3D", font(["Inter-Black.otf", "seguibl.ttf", "arialbd.ttf"], int(150 * s)), ACCENT)
    else:
        _center(d, 128 * s, 46 * s, "SEBE", font(["Inter-ExtraBold.otf", "seguibl.ttf", "arialbd.ttf"], int(50 * s)), WHITE)
        _center(d, 128 * s, 108 * s, "3D", font(["Inter-Black.otf", "seguibl.ttf", "arialbd.ttf"], int(118 * s)), ACCENT)
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "printhub.ico"
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw(n) for n in sizes]
    imgs[-1].save(out, format="ICO", sizes=[(n, n) for n in sizes], append_images=imgs[:-1])
    imgs[-1].save(out.rsplit(".", 1)[0] + ".png")
