import json
from pathlib import Path

import pytest

from canprobe.log_parser import Frame, parse_asc, parse_csv, parse_json, parse_trc


def test_parse_csv():
    text = (
        "timestamp,id,data\n"
        "0.000000,0x100,00 00 1E 00 00 00 00 00\n"
        "0.020000,0x100,01 00 1E 00 00 00 00 00\n"
    )
    frames = parse_csv(text)
    assert len(frames) == 2
    assert frames[0].frame_id == 0x100
    assert frames[0].data == bytes([0, 0, 0x1E, 0, 0, 0, 0, 0])
    assert frames[1].t == 0.02


def test_parse_asc():
    text = (
        "date Wed Aug 15 21:00:00 2026\n"
        "base hex  timestamps absolute\n"
        "no internal events logged\n"
        "Begin Triggerblock Tue Aug 15 21:00:00 2026\n"
        "   0.000000 1  100             Rx   d 8 00 00 1E 00 00 00 00 00\n"
        "   0.010000 1  101             Rx   d 8 01 00 00 00 00 00 00 00\n"
    )
    frames = parse_asc(text)
    assert len(frames) == 2
    assert frames[0].frame_id == 0x100
    assert frames[0].data[2] == 0x1E
    assert frames[1].frame_id == 0x101


def test_parse_json():
    text = json.dumps([
        {"t": 0.0, "id": 291, "data": "00 11 22"},
        {"t": 0.01, "id": 291, "data": [0x00, 0x11, 0x23]},
    ])
    frames = parse_json(text)
    assert len(frames) == 2
    assert frames[0].data == bytes([0x00, 0x11, 0x22])
    assert frames[1].data == bytes([0x00, 0x11, 0x23])


def test_parse_trc():
    text = (
        "0.000000 100 Rx d 8 00 00 1E 00 00 00 00 00\n"
        "0.010000 101 Rx d 8 01 00 00 00 00 00 00 00\n"
    )
    frames = parse_trc(text)
    assert len(frames) == 2
    assert frames[0].frame_id == 0x100


def test_hex_bytes():
    from canprobe.log_parser import _parse_hex_bytes
    assert _parse_hex_bytes("00 11 22") == bytes([0, 0x11, 0x22])
    assert _parse_hex_bytes("001122") == bytes([0, 0x11, 0x22])
    assert _parse_hex_bytes("0x00 0x11") == bytes([0, 0x11])


def test_iter_frames_window_and_cap():
    from canprobe.log_parser import iter_frames
    frames = list(iter_frames("samples/cruise.csv", t0=5.0, t1=5.2))
    assert frames and all(5.0 <= f.t <= 5.2 for f in frames)
    capped = list(iter_frames("samples/cruise.csv", max_frames=100))
    assert len(capped) == 100


def test_parse_blf_roundtrip(tmp_path):
    can = pytest.importorskip("can")
    from can.io import BLFWriter
    from canprobe.log_parser import parse_blf

    path = str(tmp_path / "test.blf")
    with BLFWriter(path) as w:
        w.on_message_received(can.Message(arbitration_id=0x100, data=[0, 1, 2, 3], timestamp=0.0))
        w.on_message_received(can.Message(arbitration_id=0x101, data=[4, 5], timestamp=0.01))

    frames = parse_blf(path)
    assert len(frames) == 2
    assert frames[0].frame_id == 0x100
    assert frames[0].data == bytes([0, 1, 2, 3])
    assert frames[1].frame_id == 0x101
    assert frames[1].data == bytes([4, 5])


def test_parse_mf4_no_can_channels(tmp_path):
    pytest.importorskip("asammdf")
    import numpy as np
    from asammdf import MDF, Signal
    from canprobe.log_parser import ParseError, parse_mf4

    mdf = MDF()
    t = np.array([0.0, 0.01])
    mdf.append(Signal(samples=np.array([1.0, 2.0]), timestamps=t, name="VehSpd"))
    path = str(tmp_path / "plain.mf4")
    mdf.save(path, overwrite=True)

    with pytest.raises(ParseError):
        parse_mf4(path)


def test_parse_mf4_real_fixture():
    # 需要一个真实的 Vector 总线日志 MF4（samples/can_sample.mf4）才能实测
    from canprobe.log_parser import parse_mf4
    fixture = Path("samples/can_sample.mf4")
    if not fixture.exists():
        pytest.skip("缺少真实 MF4 总线日志样例 samples/can_sample.mf4")
    frames = parse_mf4(str(fixture))
    assert frames
    assert all(f.data for f in frames)
