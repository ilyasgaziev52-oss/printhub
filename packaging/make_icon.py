"""Рисует значок PrintHub (printhub.ico) — синий квадрат со стилизованным 3D-принтером."""
import sys

from PIL import Image, ImageDraw


def draw(size: int) -> Image.Image:
    s = size / 256
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8 * s, 8 * s, 248 * s, 248 * s), radius=52 * s, fill=(47, 111, 237, 255))
    w = max(int(18 * s), 1)
    # рама принтера
    d.line((60 * s, 196 * s, 196 * s, 196 * s), fill="white", width=w)
    d.line((76 * s, 196 * s, 76 * s, 76 * s), fill="white", width=w)
    d.line((180 * s, 196 * s, 180 * s, 76 * s), fill="white", width=w)
    d.line((70 * s, 76 * s, 186 * s, 76 * s), fill="white", width=w)
    # печатающая головка и деталь
    d.rounded_rectangle((108 * s, 92 * s, 148 * s, 124 * s), radius=6 * s, fill="white")
    d.polygon([(118 * s, 124 * s), (138 * s, 124 * s), (128 * s, 138 * s)], fill="white")
    d.rounded_rectangle((104 * s, 156 * s, 152 * s, 186 * s), radius=6 * s, fill=(255, 196, 61, 255))
    return im


if __name__ == "__main__":
    out = sys.argv[1] if len(sys.argv) > 1 else "printhub.ico"
    big = draw(256)
    big.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    big.save(out.replace(".ico", ".png"))
