import base64
import json
import struct
import zipfile

from printhub.analysis.resin_files import GOO_MAGIC, read_resin_file, rgb565_to_png_b64


def make_goo(path):
    h = bytearray(195477)
    h[0:4] = b"V3.0"
    h[4:12] = GOO_MAGIC
    h[12:12 + 9] = b"CHITUBOX\0"
    h[92:92 + 16] = b"Saturn 4 Ultra\0\0"
    # превью 290x290: красный цвет в RGB565 big-endian
    red = struct.pack(">H", 0xF800)
    h[27108:27108 + 168200] = red * (290 * 290)
    struct.pack_into(">IHH", h, 195310, 2000, 11520, 5120)
    struct.pack_into(">fffff", h, 195320, 218.88, 122.88, 220.0, 0.05, 2.5)
    struct.pack_into(">f", h, 195369, 25.0)
    struct.pack_into(">I", h, 195373, 5)
    struct.pack_into(">Ifff", h, 195446, 7200, 45678.0, 50.2, 3.1)
    h[195462:195464] = b"$\0"
    with open(path, "wb") as f:
        f.write(bytes(h) + b"\0" * 32)


def test_goo(tmp_path):
    p = tmp_path / "a.goo"
    make_goo(p)
    r = read_resin_file(str(p))
    assert r["format"] == "goo" and r["machine"] == "Saturn 4 Ultra"
    assert r["layers"] == 2000 and r["resolution"] == [11520, 5120]
    assert r["print_time_s"] == 7200 and r["volume_ml"] == 45.68 and r["weight_g"] == 50.2
    assert r["layer_height_mm"] == 0.05 and r["bottom_layers"] == 5 and r["height_mm"] == 100.0
    png = base64.b64decode(r["thumbnail_b64"])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def make_anycubic(path):
    head_addr, ldef_addr, mach_addr, prev_addr = 64, 300, 400, 600
    buf = bytearray(600 + 32 + 4 * 3 * 2)
    buf[0:8] = b"ANYCUBIC"
    struct.pack_into("<8I", buf, 12, 516, 8, head_addr, 0, prev_addr, 0, ldef_addr, 0)
    struct.pack_into("<I", buf, 44, mach_addr)
    buf[head_addr:head_addr + 6] = b"HEADER"
    struct.pack_into("<10f", buf, head_addr + 16, 34.4, 0.05, 2.0, 0.5, 30.0, 6.0, 6.0, 2.0, 3.0, 12.5)
    struct.pack_into("<3I", buf, head_addr + 56, 1, 5760, 3600)
    struct.pack_into("<2f", buf, head_addr + 68, 13.75, 0.6)
    buf[head_addr + 76:head_addr + 80] = b"$\0\0\0"
    struct.pack_into("<I", buf, head_addr + 84, 3600)
    struct.pack_into("<I", buf, ldef_addr + 16, 1200)
    name = b"Photon Mono X 6K"
    buf[mach_addr + 16:mach_addr + 16 + len(name)] = name
    struct.pack_into("<III", buf, prev_addr + 16, 2, 0x78, 3)
    buf[prev_addr + 28:prev_addr + 28 + 12] = struct.pack("<6H", *([0x07E0] * 6))
    with open(path, "wb") as f:
        f.write(bytes(buf))


def test_anycubic_binary(tmp_path):
    p = tmp_path / "a.pwmx"
    make_anycubic(p)
    r = read_resin_file(str(p))
    assert r["version"] == 516 and r["machine"] == "Photon Mono X 6K"
    assert r["layers"] == 1200 and r["volume_ml"] == 12.5 and r["weight_g"] == 13.75
    assert r["print_time_s"] == 3600 and r["resolution"] == [5760, 3600] and r["bottom_layers"] == 6
    assert base64.b64decode(r["thumbnail_b64"])[:4] == b"\x89PNG"


def test_anycubic_zip(tmp_path):
    p = tmp_path / "a.pm7"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("print_info.json", json.dumps({"model_layers_count": 800, "print_time": 5400, "volume": 22.0, "weight": 24.2}))
        z.writestr("anycubic_photon_resins.pwsp", json.dumps({
            "machine_type": {"name": "Photon Mono M7", "res_x": 6480, "res_y": 3600},
            "machine_extern": {"active_resins": ["Standard"], "factory_resins": [
                {"property": {"name": "Standard"}, "slicepara": {"zthick": 0.05, "exposure_time": 2.0, "bott_layers": 4}}]}}))
    r = read_resin_file(str(p))
    assert r["layers"] == 800 and r["print_time_s"] == 5400 and r["machine"] == "Photon Mono M7"
    assert r["layer_height_mm"] == 0.05 and r["height_mm"] == 40.0


def test_encrypted_ctb(tmp_path):
    p = tmp_path / "a.ctb"
    p.write_bytes(struct.pack("<I", 0x12FD0107) + b"\0" * 200)
    r = read_resin_file(str(p))
    assert "зашифрованный" in r["format"] and "note" in r


def test_rgb565():
    png = base64.b64decode(rgb565_to_png_b64(struct.pack("<4H", 0xFFFF, 0, 0xF800, 0x001F), 2, 2, False))
    assert png[:4] == b"\x89PNG"
