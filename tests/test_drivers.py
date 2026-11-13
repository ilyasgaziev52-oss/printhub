import asyncio
import os
import subprocess
import tempfile

import pytest

from printhub.drivers.bambu import (BambuDriver, _cn_from_der, build_ams_mapping, merge_report,
                                    parse_list_line, parse_ssdp)
from printhub.drivers.elegoo import ElegooSdcpDriver
from printhub.drivers.moonraker import MoonrakerDriver
from printhub.models import PrinterConfig, PrinterState

from printhub.emulators import MoonrakerPrinter, SdcpPrinter


async def wait_for(cond, timeout=5.0):
    t = asyncio.get_running_loop().time()
    while not cond():
        if asyncio.get_running_loop().time() - t > timeout:
            raise AssertionError("условие не выполнилось")
        await asyncio.sleep(0.05)


# ------------------------------------------------------------------ Bambu (без сети)
def test_bambu_merge_and_status():
    drv = BambuDriver(PrinterConfig(id="b", name="P1S", kind="bambu", host="1.2.3.4", serial="01P00A000000000",
                                    access_code="12345678"))
    drv.model = "P1S"
    full = {"gcode_state": "RUNNING", "mc_percent": 42, "mc_remaining_time": 75, "layer_num": 10, "total_layer_num": 100,
            "nozzle_temper": 219.5, "nozzle_target_temper": 220, "bed_temper": 60, "bed_target_temper": 60,
            "subtask_name": "Cube", "spd_lvl": 2, "cooling_fan_speed": "15", "fun": "3EC18FFF9CFF",
            "lights_report": [{"node": "chamber_light", "mode": "on"}],
            "ams": {"tray_now": "1", "ams": [{"id": "0", "tray": [{"id": "0"}, {"id": "1", "tray_type": "PLA", "tray_color": "FF0000FF", "remain": 80}]}]}}
    merge_report(drv.report, full)
    # дельта: изменился только один лоток и процент
    merge_report(drv.report, {"mc_percent": 43, "ams": {"ams": [{"id": "0", "tray": [{"id": "1", "remain": 79}]}]}})
    asyncio.run(drv._apply())
    st = drv.status
    assert st.state == PrinterState.PRINTING
    assert st.progress == 43 and st.remaining_s == 75 * 60
    assert st.temps["nozzle"].current == 219.5
    assert "chamber" not in st.temps  # у P1S нет датчика камеры
    assert st.light is True and st.speed_level == "стандарт" and st.fans["part"] == 100
    slots = {m.id: m for m in st.materials}
    assert slots["1"].remain_pct == 79 and slots["1"].color == "#FF0000" and slots["1"].active
    assert drv.dev_mode is True and drv.capabilities().pause


def test_bambu_dev_mode_off_and_cancel():
    drv = BambuDriver(PrinterConfig(id="b", name="X1C", kind="bambu", host="h", serial="00M1", access_code="1"))
    drv.model = "X1 Carbon"
    merge_report(drv.report, {"gcode_state": "FAILED", "print_error": 0x0300400C, "fun": "3EC1AFFF9CFF"})
    asyncio.run(drv._apply())
    assert drv.status.state == PrinterState.CANCELLED
    assert drv.dev_mode is False
    caps = drv.capabilities()
    assert not caps.pause and not caps.start and caps.light and caps.camera
    assert "Режим" in drv.status.message


def test_bambu_h2d_packed_temps():
    drv = BambuDriver(PrinterConfig(id="b", name="H2D", kind="bambu", host="h", serial="0941", access_code="1"))
    drv.model = "H2D"
    packed = lambda cur, tgt: (tgt << 16) | cur  # noqa: E731
    merge_report(drv.report, {"gcode_state": "IDLE", "device": {
        "extruder": {"info": [{"id": 0, "temp": packed(220, 230)}, {"id": 1, "temp": packed(30, 0)}]},
        "bed": {"info": {"temp": packed(55, 60)}}, "ctc": {"info": {"temp": packed(35, 0)}}}})
    asyncio.run(drv._apply())
    t = drv.status.temps
    assert t["nozzle"].current == 220 and t["nozzle"].target == 230
    assert t["nozzle1"].current == 30 and t["bed"].target == 60 and t["chamber"].current == 35


def test_ams_mapping():
    trays = [{"gid": 0, "ams_id": 0, "slot_id": 0, "type": "PLA", "color": "#FFFFFF"},
             {"gid": 1, "ams_id": 0, "slot_id": 1, "type": "PLA", "color": "#FF0000"},
             {"gid": 2, "ams_id": 0, "slot_id": 2, "type": "PETG", "color": "#000000"}]
    fil = [{"id": "1", "type": "PLA", "color": "#F00000", "used_g": 10}, {"id": "3", "type": "PETG", "color": "#111111", "used_g": 2}]
    m, m2 = build_ams_mapping(fil, trays)
    assert m == [1, -1, 2]
    assert m2[0] == {"ams_id": 0, "slot_id": 1} and m2[1] == {"ams_id": 255, "slot_id": 255}


def test_ssdp_parse():
    pkt = ("NOTIFY * HTTP/1.1\r\nHost: 239.255.255.250:1990\r\nLocation: 192.168.1.30\r\n"
           "NT: urn:bambulab-com:device:3dprinter:1\r\nUSN: 22E8BJ610801473\r\nDevModel.bambu.com: N7\r\n"
           "DevName.bambu.com: Мой P2S\r\nDevConnect.bambu.com: cloud\r\nDevVersion.bambu.com: 01.02.00.00\r\n\r\n").encode()
    d = parse_ssdp(pkt, "192.168.1.30")
    assert d and d.host == "192.168.1.30" and d.serial == "22E8BJ610801473"
    assert d.model == "Bambu Lab P2S" and d.name == "Мой P2S" and "LAN Only" in d.notes


def test_ftp_list_parse():
    p = parse_list_line("-rw-r--r-- 1 1000 1000 1632221 Jun 17  2025 Benchy.gcode.3mf")
    assert p and not p[0] and p[1] == 1632221 and p[3] == "Benchy.gcode.3mf"
    assert parse_list_line("drwxr-xr-x 2 1000 1000 4096 Jun 18 00:27 cache")[0] is True


def test_cn_from_cert():
    with tempfile.TemporaryDirectory() as d:
        key, crt, der = (os.path.join(d, n) for n in ("k.pem", "c.pem", "c.der"))
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                            "-days", "1", "-subj", "/O=BBL Technologies/CN=01P00A123456789"], capture_output=True)
        if r.returncode != 0:
            pytest.skip("openssl недоступен")
        subprocess.run(["openssl", "x509", "-in", crt, "-outform", "DER", "-out", der], check=True)
        assert _cn_from_der(open(der, "rb").read()) == "01P00A123456789"


# ------------------------------------------------------------------ Elegoo SDCP (эмулятор)
async def test_elegoo_sdcp_flow():
    pr = SdcpPrinter()
    await pr.start()
    cfg = PrinterConfig(id="e", name="CC", kind="elegoo_sdcp", host="127.0.0.1", port=pr.ws_port,
                        serial=pr.MBID, extra={"upload_port": pr.http_port})
    updates = []
    drv = ElegooSdcpDriver(cfg, on_update=lambda d: updates.append(d.status.state))
    await drv.start()
    try:
        await wait_for(lambda: drv.status.state == PrinterState.IDLE)
        assert drv.status.temps["nozzle"].current == 26.0
        assert drv.status.firmware == "V1.1.29"
        # загрузка в несколько кусков (>1 МиБ)
        with tempfile.NamedTemporaryFile("wb", suffix=".gcode", delete=False) as f:
            f.write(os.urandom(2_500_000))
        prog = []
        name = await drv.upload(f.name, "part.gcode", lambda s, t: prog.append((s, t)))
        assert name == "part.gcode" and len(pr.uploaded["part.gcode"]) == 2_500_000
        assert prog[-1] == (2_500_000, 2_500_000) and len(prog) == 3
        files = await drv.list_files()
        assert files[0].name == "part.gcode"
        await drv.start_print("part.gcode", {"calibration": False})
        start_cmd = [c for c in pr.received if c["Cmd"] == 128][-1]
        assert start_cmd["Data"] == {"Filename": "part.gcode", "StartLayer": 0, "Calibration_switch": 0,
                                     "PrintPlatformType": 0, "Tlp_Switch": 0, "slot_map": []}
        await wait_for(lambda: drv.status.state == PrinterState.PRINTING)
        assert drv.status.progress == 10 and drv.status.remaining_s == 540
        await drv.pause()
        await wait_for(lambda: drv.status.state == PrinterState.PAUSED)
        await drv.resume()
        await wait_for(lambda: drv.status.state == PrinterState.PRINTING)
        await drv.set_light(True)
        await wait_for(lambda: drv.status.light is True)
        await drv.cancel()
        await wait_for(lambda: drv.status.state == PrinterState.CANCELLED)
        # не было отправлено ни одной незадокументированной команды
        assert {c["Cmd"] for c in pr.received} <= {0, 1, 128, 129, 130, 131, 258, 403, 512}
        os.unlink(f.name)
    finally:
        await drv.stop()
        await pr.stop()


def test_elegoo_resin_status_ms():
    cfg = PrinterConfig(id="r", name="S4U", kind="elegoo_sdcp", host="h", technology="resin", model="Elegoo Saturn 4 Ultra")
    drv = ElegooSdcpDriver(cfg)
    drv.raw_status = {"CurrentStatus": [1], "TempOfUVLED": 40, "PrintInfo": {"Status": 3, "CurrentLayer": 250, "TotalLayer": 1000,
                                                                            "CurrentTicks": 1_800_000, "TotalTicks": 7_200_000, "Filename": "a.goo"}}
    asyncio.run(drv._apply())
    st = drv.status
    assert st.state == PrinterState.PRINTING and st.progress == 25.0
    assert st.elapsed_s == 1800 and st.remaining_s == 5400
    assert drv.accepted_extensions() == (".goo", ".ctb") and not drv.capabilities().light


# ------------------------------------------------------------------ Moonraker (эмулятор)
async def test_moonraker_flow():
    pr = MoonrakerPrinter()
    await pr.start()
    cfg = PrinterConfig(id="m", name="N4", kind="moonraker", host="127.0.0.1", port=pr.port)
    drv = MoonrakerDriver(cfg)
    await drv.start()
    try:
        await wait_for(lambda: drv.status.state == PrinterState.IDLE)
        assert drv.status.temps["bed"].current == 24.0
        with tempfile.NamedTemporaryFile("w", suffix=".gcode", delete=False) as f:
            f.write("G28\n")
        await drv.upload(f.name, "a.gcode")
        assert "a.gcode" in pr.files
        await drv.start_print("a.gcode")
        await wait_for(lambda: drv.status.state == PrinterState.PRINTING and drv.status.remaining_s is not None)
        assert drv.status.progress == 5.0 and drv.status.remaining_s > 500
        await drv.pause()
        await wait_for(lambda: drv.status.state == PrinterState.PAUSED)
        await drv.send_gcode("M117 hi")
        assert "gcode:M117 hi" in pr.calls
        await drv.cancel()
        await wait_for(lambda: drv.status.state == PrinterState.CANCELLED)
        os.unlink(f.name)
    finally:
        await drv.stop()
        await pr.stop()
