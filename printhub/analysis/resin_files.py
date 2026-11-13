"""Чтение заголовков файлов фотополимерной печати: время, слои, объём смолы, превью.

Поддерживается:
- Elegoo .goo (V3 и V5, официальная спецификация, big-endian)
- Chitubox .ctb / .cbddlp (незашифрованные версии; зашифрованный CTB v5 — только базовые сведения)
- Anycubic бинарный формат «ANYCUBIC» (.pwmx, .pwma, .pwmb, .pwms, .pm3, .pm3m, .pm3r, .pm4n, .pm5, .pm5s, .px6s…)
- Anycubic ZIP-форматы (.pm7, .pm7m, .pwsz, .pm4u, .pp1)
"""
from __future__ import annotations

import base64
import json
import os
import struct
import zipfile
import zlib
from typing import Any, Optional


# ---------------------------------------------------------------- PNG без Pillow
def _png(width: int, height: int, rgb: bytes) -> bytes:
    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b""))


def rgb565_to_png_b64(data: bytes, width: int, height: int, big_endian: bool) -> str:
    n = width * height
    if len(data) < n * 2 or n <= 0:
        return ""
    fmt = (">" if big_endian else "<") + f"{n}H"
    px = struct.unpack(fmt, data[:n * 2])
    out = bytearray(n * 3)
    for i, v in enumerate(px):
        r, g, b = (v >> 11) & 0x1F, (v >> 5) & 0x3F, v & 0x1F
        out[i * 3] = (r << 3) | (r >> 2)
        out[i * 3 + 1] = (g << 2) | (g >> 4)
        out[i * 3 + 2] = (b << 3) | (b >> 2)
    return base64.b64encode(_png(width, height, bytes(out))).decode()


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("utf-8", "replace").strip()


# ---------------------------------------------------------------- Elegoo GOO
GOO_MAGIC = b"\x07\x00\x00\x00\x44\x4C\x50\x00"


def read_goo(path: str) -> dict[str, Any]:
    with open(path, "rb") as f:
        h = f.read(195477)
    if h[4:12] != GOO_MAGIC:
        raise ValueError("не похоже на .goo")
    r: dict[str, Any] = {"format": "goo", "version": _cstr(h[0:4]), "software": _cstr(h[12:44]),
                         "software_version": _cstr(h[44:68]), "created": _cstr(h[68:92]),
                         "machine": _cstr(h[92:124]), "resin_profile": _cstr(h[156:188])}
    layers, rx, ry = struct.unpack_from(">IHH", h, 195310)
    mirror_x, mirror_y = struct.unpack_from(">BB", h, 195318)
    px, py, pz, lh, exp = struct.unpack_from(">fffff", h, 195320)
    bottom_exp, = struct.unpack_from(">f", h, 195369)
    bottom_layers, = struct.unpack_from(">I", h, 195373)
    lift_h, lift_speed = struct.unpack_from(">ff", h, 195377 + 8)
    print_time, vol_mm3, weight, price = struct.unpack_from(">Ifff", h, 195446)
    unit = _cstr(h[195462:195470])
    r.update(layers=layers, resolution=[rx, ry], platform_mm=[round(px, 2), round(py, 2), round(pz, 2)],
             layer_height_mm=round(lh, 4), exposure_s=round(exp, 3), bottom_exposure_s=round(bottom_exp, 3),
             bottom_layers=bottom_layers, lift_height_mm=round(lift_h, 2), lift_speed_mm_min=round(lift_speed, 1),
             print_time_s=print_time, volume_ml=round(vol_mm3 / 1000.0, 2), weight_g=round(weight, 2),
             price=round(price, 2), price_unit=unit, height_mm=round(layers * lh, 2))
    r["thumbnail_b64"] = rgb565_to_png_b64(h[27108:27108 + 168200], 290, 290, big_endian=True)
    return r


# ---------------------------------------------------------------- Chitubox CTB
CTB_MAGICS = {0x12FD0019: "cbddlp", 0x12FD0086: "ctb", 0x12FD0106: "ctb v4", 0xFF220810: "gktwo",
              0x12FD0107: "ctb (зашифрованный)"}


def read_ctb(path: str) -> dict[str, Any]:
    with open(path, "rb") as f:
        h = f.read(112)
        magic, = struct.unpack_from("<I", h, 0)
        if magic not in CTB_MAGICS:
            raise ValueError("не похоже на .ctb")
        r: dict[str, Any] = {"format": CTB_MAGICS[magic]}
        if magic == 0x12FD0107:
            r["note"] = ("Зашифрованный CTB: параметры не читаются. Время и расход покажет принтер при печати "
                         "(или сохраните файл в Chitubox как .goo / незашифрованный формат).")
            return r
        version, = struct.unpack_from("<I", h, 4)
        bx, by, bz = struct.unpack_from("<fff", h, 8)
        total_h, lh, exp, bexp, off = struct.unpack_from("<fffff", h, 28)
        bottom_layers, rx, ry, prev_large, layers_def, layers, prev_small, print_time = struct.unpack_from("<8I", h, 48)
        pp_off, pp_size = struct.unpack_from("<II", h, 84)
        r.update(version=version, platform_mm=[round(bx, 2), round(by, 2), round(bz, 2)], height_mm=round(total_h, 2),
                 layer_height_mm=round(lh, 4), exposure_s=round(exp, 3), bottom_exposure_s=round(bexp, 3),
                 bottom_layers=bottom_layers, resolution=[rx, ry], layers=layers, print_time_s=print_time)
        if pp_off and pp_size >= 32:
            f.seek(pp_off)
            pp = f.read(32)
            if len(pp) == 32:
                _, _, lift_h, lift_s, _, vol, weight, cost = struct.unpack("<8f", pp)
                r.update(volume_ml=round(vol, 2), weight_g=round(weight, 2), price=round(cost, 2),
                         lift_height_mm=round(lift_h, 2))
        if prev_large:
            f.seek(prev_large)
            pw, ph, img_off, img_len = struct.unpack("<4I", f.read(16))
            if 0 < pw <= 1000 and 0 < ph <= 1000 and img_len < 4_000_000:
                f.seek(img_off)
                r["thumbnail_b64"] = _ctb_rle_preview(f.read(img_len), pw, ph)
    return r


def _ctb_rle_preview(data: bytes, w: int, h: int) -> str:
    """RLE-превью Chitubox (RGB15 с флагом повтора)."""
    out = bytearray()
    i = 0
    n = w * h
    while i + 1 < len(data) and len(out) < n * 3:
        v = data[i] | (data[i + 1] << 8)
        i += 2
        rep = 1
        if v & 0x20:
            if i + 1 >= len(data):
                break
            rep += (data[i] | (data[i + 1] << 8)) & 0x0FFF
            i += 2
        r, g, b = ((v >> 11) & 0x1F) << 3, ((v >> 6) & 0x1F) << 3, (v & 0x1F) << 3
        out.extend(bytes([r, g, b]) * rep)
    if len(out) < n * 3:
        return ""
    return base64.b64encode(_png(w, h, bytes(out[:n * 3]))).decode()


# ---------------------------------------------------------------- Anycubic бинарный
def read_anycubic(path: str) -> dict[str, Any]:
    with open(path, "rb") as f:
        fm = f.read(64)
        if fm[:8] != b"ANYCUBIC":
            raise ValueError("не похоже на файл Anycubic")
        ver, ntables, hdr, soft, preview, _ct, ldef, _extra = struct.unpack_from("<8I", fm, 12)
        mach = struct.unpack_from("<I", fm, 44)[0] if ver >= 516 else 0
        f.seek(hdr)
        h = f.read(112)
        px, lh, exp, off, bexp, bl, lift_h, lift_s, retr, vol = struct.unpack_from("<10f", h, 16)
        aa, rx, ry = struct.unpack_from("<3I", h, 56)
        weight, price = struct.unpack_from("<2f", h, 68)
        cur = h[76:80].rstrip(b"\0").decode("utf-8", "replace")
        ptime = struct.unpack_from("<I", h, 84)[0] if len(h) >= 88 else 0
        f.seek(ldef + 16)
        layers = struct.unpack("<I", f.read(4))[0]
        machine = ""
        if mach:
            f.seek(mach + 16)
            machine = _cstr(f.read(96))
        thumb = ""
        if preview:
            f.seek(preview + 16)
            pw, _x, ph = struct.unpack("<III", f.read(12))
            if 0 < pw <= 1024 and 0 < ph <= 1024:
                thumb = rgb565_to_png_b64(f.read(pw * ph * 2), pw, ph, big_endian=False)
    return {"format": f"anycubic v{ver}", "version": ver, "machine": machine, "layer_height_mm": round(lh, 4),
            "exposure_s": round(exp, 3), "bottom_exposure_s": round(bexp, 3), "bottom_layers": int(bl),
            "lift_height_mm": round(lift_h, 2), "volume_ml": round(vol, 2), "weight_g": round(weight, 2),
            "price": round(price, 2), "price_unit": cur, "print_time_s": ptime, "resolution": [rx, ry],
            "pixel_um": round(px, 2), "layers": layers, "height_mm": round(layers * lh, 2), "thumbnail_b64": thumb}


# ---------------------------------------------------------------- Anycubic ZIP
def read_anycubic_zip(path: str) -> dict[str, Any]:
    r: dict[str, Any] = {"format": os.path.splitext(path)[1].lstrip(".") + " (zip)"}
    with zipfile.ZipFile(path) as z:
        names = set(z.namelist())
        if "print_info.json" in names:
            pi = json.loads(z.read("print_info.json"))
            r.update(layers=pi.get("model_layers_count"), print_time_s=pi.get("print_time"),
                     volume_ml=pi.get("volume"), weight_g=pi.get("weight"), price=pi.get("cost"),
                     price_unit=pi.get("currency"))
        if "anycubic_photon_resins.pwsp" in names:
            try:
                rs = json.loads(z.read("anycubic_photon_resins.pwsp"))
                mt = rs.get("machine_type") or {}
                r["machine"] = mt.get("name", "")
                r["resolution"] = [mt.get("res_x"), mt.get("res_y")]
                ext = rs.get("machine_extern") or {}
                active = (ext.get("active_resins") or [None])[0]
                for res in (ext.get("user_resins") or []) + (ext.get("factory_resins") or []):
                    if (res.get("property") or {}).get("name") == active:
                        sp = res.get("slicepara") or {}
                        r.update(resin=active, layer_height_mm=sp.get("zthick"), exposure_s=sp.get("exposure_time"),
                                 bottom_exposure_s=sp.get("bott_time"), bottom_layers=sp.get("bott_layers"),
                                 lift_height_mm=sp.get("zup_height"))
                        break
            except ValueError:
                pass
        if not r.get("layers") and "layers_controller.conf" in names:
            try:
                r["layers"] = json.loads(z.read("layers_controller.conf")).get("count")
            except ValueError:
                pass
        if r.get("layers") and r.get("layer_height_mm"):
            r["height_mm"] = round(float(r["layers"]) * float(r["layer_height_mm"]), 2)
        prev = sorted(n for n in names if n.startswith("preview_images/") and n.endswith(".png"))
        if prev:
            r["thumbnail_b64"] = base64.b64encode(z.read(prev[-1])).decode()
    return r


def read_resin_file(path: str) -> dict[str, Any]:
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        head = f.read(16)
    try:
        if head[4:12] == GOO_MAGIC:
            r = read_goo(path)
        elif head[:8] == b"ANYCUBIC":
            r = read_anycubic(path)
        elif head[:2] == b"PK":
            r = read_anycubic_zip(path)
        elif len(head) >= 4 and struct.unpack_from("<I", head, 0)[0] in CTB_MAGICS:
            r = read_ctb(path)
        else:
            r = {"format": ext.lstrip("."), "note": "Формат не распознан — доступны только базовые сведения"}
    except (struct.error, ValueError, OSError, zipfile.BadZipFile) as e:
        r = {"format": ext.lstrip("."), "error": f"Не удалось прочитать заголовок: {e}"}
    r["size"] = os.path.getsize(path)
    return r
