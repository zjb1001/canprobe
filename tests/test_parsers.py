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


def test_fd_dlc_code_expands_to_payload_length():
    """DLC 是 4 bit 编码不是字节数：9..15 对应 12/16/20/24/32/48/64。

    MF4 的 CAN_DataFrame.DLC 存的是原始编码，早先直接拿它当长度切片，
    24 字节的 FD 报文被截成 12 字节，cantools 整条拒绝，表现为
    "这个信号这份日志没录到"。
    """
    from canprobe.log_parser import _FD_DLC_TO_LEN

    assert _FD_DLC_TO_LEN[:9] == (0, 1, 2, 3, 4, 5, 6, 7, 8)
    assert (_FD_DLC_TO_LEN[9], _FD_DLC_TO_LEN[10], _FD_DLC_TO_LEN[12]) == (12, 16, 24)
    assert (_FD_DLC_TO_LEN[13], _FD_DLC_TO_LEN[15]) == (32, 64)


def test_parse_mf4_keeps_full_fd_payload():
    """真实 FD 日志：>8 字节的报文必须整条取出，不能按 DLC 编码截断。"""
    fixture = Path("samples/20260731_EP35_VN1CG000196_VN2CG000207_AVH_HDC.mf4")
    if not fixture.exists():
        pytest.skip("缺少 EP35 MF4 样例")
    pytest.importorskip("asammdf")
    from canprobe.log_parser import parse_mf4

    frames = parse_mf4(str(fixture))
    by_id: dict[int, int] = {}
    for f in frames:
        by_id.setdefault(f.frame_id, len(f.data))
    # 0x270 WCBS_Info 在 DBC 里是 24 字节（DLC 编码 12），0x117 是 16（编码 10）
    assert by_id[0x270] == 24, "FD 负载被按 DLC 编码截断了"
    assert by_id[0x117] == 16
    assert by_id[0x132] == 8


def test_signals_with_data_requires_decodability():
    """报文出现过但解不开时，它的信号不能算"有数据"。

    否则界面显示 15/15 有数据、曲线全空、事件 0 条，"没查到"会被读成"没问题"。
    """
    fixture = Path("samples/20260731_EP35_VN1CG000196_VN2CG000207_AVH_HDC.mf4")
    dbc = Path("samples/03_CCAN_EP_v2.1.0_20260417-MOD.dbc")
    if not (fixture.exists() and dbc.exists()):
        pytest.skip("缺少 EP35 样例")
    pytest.importorskip("asammdf")
    from canprobe.store import Project

    p = Project()
    p.load_dbc(str(dbc))
    p.load_log(str(fixture))
    # 修好截断后这些 FD 报文的信号都解得出来了
    for sig in ("WCBS_BrkPedalTravel", "WCBS_MainCylinderPress", "WCBS_LongitudeACC"):
        t, _ = p.store.series(sig)
        assert len(t) > 0, sig
        assert sig in p.store.signals_with_data()
    # DBC 不认识的 ID 归 unknown，不混进 decode_failures
    assert all(f["name"] is not None for f in p.store.decode_failures())


def test_parse_mf4_real_fixture():
    # 需要一个真实的 Vector 总线日志 MF4（samples/can_sample.mf4）才能实测
    from canprobe.log_parser import parse_mf4
    fixture = Path("samples/can_sample.mf4")
    if not fixture.exists():
        pytest.skip("缺少真实 MF4 总线日志样例 samples/can_sample.mf4")
    frames = parse_mf4(str(fixture))
    assert frames
    assert all(f.data for f in frames)
