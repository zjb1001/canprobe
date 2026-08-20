"""报文级 / 信号级体检测试（合成故障场景 + samples/cruise.dbc）。

场景对应真实排查语境：「客户说 0x270 这条报文有问题」——输入一个 ID，
应当直接给出中断 / 停发 / DLC 不一致 / 载荷冻结 / 枚举非法值 / 计数器冻结等结论。
"""
from pathlib import Path

import pytest

from canprobe.dbc_loader import DbcDatabase
from canprobe.decoder import SeriesStore
from canprobe.diag.message import diagnose_message, rank_messages, resolve_message
from canprobe.diag.model import DiagConfig
from canprobe.log_parser import Frame

DBC_PATH = Path(__file__).resolve().parent.parent / "samples" / "cruise.dbc"

VEH, DRV, CCM, MOT = 0x100, 0x101, 0x102, 0x200


@pytest.fixture(scope="module")
def dbc():
    return DbcDatabase.load(str(DBC_PATH))


def veh(speed_raw=100):
    """VehStatus：VehSpd 占 0|12（小端）。"""
    b = bytearray(8)
    b[0] = speed_raw & 0xFF
    b[1] = (speed_raw >> 8) & 0x0F
    return bytes(b)


def ccm(state=0):
    """CCM_Status：CruiseState 占 20|3（= byte2 的 bit4~6）。"""
    b = bytearray(8)
    b[2] = (state & 0x07) << 4
    return bytes(b)


def mot(counter=0):
    """MotorStatus：MotorSpeed 占 8|16（小端），这里当滚动计数器用。"""
    b = bytearray(8)
    b[1] = counter & 0xFF
    b[2] = (counter >> 8) & 0xFF
    return bytes(b)


def periodic(fid, payload, period_ms=20, start_ms=0, end_ms=2000, skip=()):
    """按周期生成帧；payload 可以是 bytes 或 f(index)->bytes。skip=[(lo_ms,hi_ms)]。"""
    out, ms, i = [], start_ms, 0
    while ms <= end_ms:
        if not any(lo < ms < hi for lo, hi in skip):
            data = payload(i) if callable(payload) else payload
            out.append(Frame(t=ms / 1000.0, frame_id=fid, data=data))
            i += 1
        ms += period_ms
    return out


def build(frames, dbc):
    return SeriesStore(sorted(frames, key=lambda f: f.t), dbc)


def diag(store, dbc, mid):
    return diagnose_message(store, dbc, mid, DiagConfig.from_dbc(dbc))


def fof(report, check):
    return [f for f in report["findings"] if f["check"] == check]


def healthy(dbc):
    """一份没有毛病的日志：三条报文都按 20ms 发，载荷持续变化。"""
    return build(
        periodic(VEH, lambda i: veh(100 + i % 50))
        + periodic(CCM, lambda i: ccm(i % 4))
        + periodic(MOT, lambda i: mot(i % 256)),
        dbc)


# --------------------------------------------------------------------------- #
# 输入解析
# --------------------------------------------------------------------------- #
def test_resolve_hex_dec_and_name(dbc):
    store = healthy(dbc)
    assert resolve_message(dbc, store, "0x100")["id"] == 0x100
    assert resolve_message(dbc, store, "256") == {"id": 256, "name": "VehStatus",
                                                  "matched_by": "dec", "error": None,
                                                  "candidates": []}
    assert resolve_message(dbc, store, "VehStatus")["matched_by"] == "name"
    assert resolve_message(dbc, store, "vehstatus")["id"] == 0x100
    assert resolve_message(dbc, store, "Motor")["id"] == MOT      # 唯一子串


def test_resolve_decimal_falls_back_to_hex(dbc):
    """「270」十进制查无此 ID，但 0x270 在日志里有 —— 按十六进制解释并标明。"""
    store = build(periodic(0x270, veh(), end_ms=200), dbc)
    hit = resolve_message(dbc, store, "270")
    assert hit["id"] == 0x270 and hit["matched_by"] == "dec_as_hex"


def test_resolve_unknown(dbc):
    hit = resolve_message(dbc, healthy(dbc), "没这个报文")
    assert hit["id"] is None and hit["error"]


# --------------------------------------------------------------------------- #
# 帧级：缺口 / 停发 / 迟到 / DLC / 载荷
# --------------------------------------------------------------------------- #
def test_gap_reported_with_missing_count(dbc):
    store = build(periodic(VEH, veh(), skip=[(1000, 1200)]) + periodic(CCM, ccm()), dbc)
    report = diag(store, dbc, VEH)
    gaps = fof(report, "msg_gap")
    assert len(gaps) == 1
    assert gaps[0]["severity"] == "error"          # 总线其余报文仍在 → 单条报文的问题
    assert "缺 9 帧" in gaps[0]["explanation"]
    assert report["stats"]["gaps"][0]["bus_active"] is True
    assert 0.98 < gaps[0]["time_start"] < 1.01


def test_gap_during_bus_silence_downgraded(dbc):
    """整条总线都停了的时段，不该记在这条报文头上。"""
    store = build(periodic(VEH, veh(), skip=[(1000, 1200)])
                  + periodic(CCM, ccm(), skip=[(1000, 1200)]), dbc)
    gaps = fof(diag(store, dbc, VEH), "msg_gap")
    assert len(gaps) == 1 and gaps[0]["severity"] == "warn"
    assert "总线级静默" in gaps[0]["explanation"]


def test_stop_mid_log(dbc):
    store = build(periodic(VEH, veh(), end_ms=1000) + periodic(CCM, ccm(), end_ms=2000), dbc)
    stop = fof(diag(store, dbc, VEH), "msg_stop")
    assert stop and stop[0]["severity"] == "error"
    assert 0.99 < stop[0]["time_start"] < 1.01


def test_late_start(dbc):
    store = build(periodic(VEH, veh(), start_ms=1000) + periodic(CCM, ccm()), dbc)
    assert fof(diag(store, dbc, VEH), "msg_late")


def test_mixed_dlc(dbc):
    frames = periodic(VEH, veh(), end_ms=1000)
    frames += periodic(VEH, b"\x00" * 4, start_ms=1020, end_ms=2000)
    store = build(frames + periodic(CCM, ccm()), dbc)
    dlc = fof(diag(store, dbc, VEH), "msg_dlc")
    assert dlc and "4 字节" in dlc[0]["explanation"] and "8 字节" in dlc[0]["explanation"]


def test_payload_frozen_whole_log(dbc):
    store = build(periodic(VEH, veh(100)) + periodic(CCM, lambda i: ccm(i % 4)), dbc)
    stuck = fof(diag(store, dbc, VEH), "msg_payload_stuck")
    assert stuck and "全程不变" in stuck[0]["title"]


def test_payload_frozen_segment(dbc):
    """前半段在变、后半段冻结 —— 应报冻结区间而不是「全程不变」。"""
    store = build(periodic(VEH, lambda i: veh(100 + (i if i < 25 else 24)))
                  + periodic(CCM, ccm()), dbc)
    stuck = fof(diag(store, dbc, VEH), "msg_payload_stuck")
    assert stuck and "冻结" in stuck[0]["title"]
    assert stuck[0]["time_end"] - stuck[0]["time_start"] > 1.0


def test_absent_message_says_so(dbc):
    store = healthy(dbc)
    report = diag(store, dbc, 0x300)
    assert report["in_log"] is False
    absent = fof(report, "msg_absent")
    assert absent and absent[0]["severity"] == "error"
    assert "一帧都没有" in absent[0]["explanation"]


def test_absent_message_hints_nearby_ids(dbc):
    store = build(periodic(0x270, veh(), end_ms=500), dbc)
    absent = fof(diag(store, dbc, 0x271), "msg_absent")
    assert absent and "0x270" in absent[0]["explanation"]


def test_error_frames_attached_to_gap(dbc):
    frames = periodic(VEH, veh(), skip=[(1000, 1200)]) + periodic(CCM, ccm())
    frames += [Frame(t=1.01 + i * 0.002, frame_id=0, data=b"", kind="error",
                     error_type="ack") for i in range(5)]
    near = fof(diag(build(frames, dbc), dbc, VEH), "msg_error_nearby")
    assert near and "ack×5" in near[0]["explanation"]


def test_multichannel_timing_is_per_channel(dbc):
    """同一 ID 在两条总线上各按 20ms 发：不能把两路时间戳混起来算成 10ms。"""
    frames = periodic(VEH, veh(100))
    for f in periodic(VEH, veh(200), start_ms=10):     # 另一条总线，相位错开 10ms
        frames.append(Frame(t=f.t, frame_id=f.frame_id, data=f.data, channel=1))
    report = diag(build(frames, dbc), dbc, VEH)
    st = report["stats"]
    assert st["channels"] == [0, 1]
    assert 19.0 < st["period_ms"]["median"] < 21.0     # 混算会变成 10ms
    assert st["gaps"] == [] and st["bursts"] == 0
    assert fof(report, "msg_multichannel")


def test_many_gaps_are_summarised_not_truncated(dbc):
    """上百个中断时逐条列会淹掉报告，但必须有一条汇总说清总数。"""
    skips = [(ms, ms + 200) for ms in range(1000, 20000, 500)]
    store = build(periodic(VEH, veh(), end_ms=30000, skip=skips)
                  + periodic(CCM, ccm(), end_ms=30000), dbc)
    gaps = fof(diag(store, dbc, VEH), "msg_gap")
    summary = [f for f in gaps if "共" in f["title"]]
    assert summary and "38 处中断" in summary[0]["title"]
    assert len(gaps) == 1 + 10                         # 汇总 1 条 + 最长的 10 条


def test_stop_needs_absolute_floor(dbc):
    """2ms 周期的报文，末尾差十几毫秒不算「停发」。"""
    store = build(periodic(VEH, veh(), period_ms=2, end_ms=1980)
                  + periodic(CCM, ccm(), period_ms=2, end_ms=2000), dbc)
    assert fof(diag(store, dbc, VEH), "msg_stop") == []


def test_period_mismatch_vs_dbc(dbc):
    """DBC 说 20ms，实际按 50ms 发。"""
    store = build(periodic(VEH, lambda i: veh(100 + i), period_ms=50), dbc)
    per = fof(diag(store, dbc, VEH), "msg_period")
    assert per and "与 DBC 不符" in per[0]["title"]


# --------------------------------------------------------------------------- #
# 信号级
# --------------------------------------------------------------------------- #
def test_signal_constant(dbc):
    report = diag(build(periodic(VEH, veh(100)), dbc), dbc, VEH)
    const = [f for f in fof(report, "sig_constant") if "VehSpd" in f["title"]]
    assert const
    row = next(r for r in report["signals"] if r["name"] == "VehSpd")
    assert row["constant"] is True and row["changes"] == 0


def test_signal_stuck_segment(dbc):
    """VehSpd 前 0.5s 在变，之后 1.5s 卡死。"""
    store = build(periodic(VEH, lambda i: veh(100 + (i if i < 25 else 24))), dbc)
    stuck = [f for f in diag(store, dbc, VEH)["findings"]
             if f["check"] == "sig_stuck" and "VehSpd" in f["title"]]
    assert stuck and stuck[0]["time_end"] - stuck[0]["time_start"] > 1.0


def test_enum_invalid_value(dbc):
    """CruiseState 的 VAL_ 只定义 0~3，日志里出现 5。"""
    store = build(periodic(CCM, lambda i: ccm(5 if 40 <= i < 50 else i % 4)), dbc)
    bad = fof(diag(store, dbc, CCM), "sig_enum_invalid")
    assert bad and "CruiseState" in bad[0]["title"] and "5" in bad[0]["explanation"]


def test_counter_freeze(dbc):
    """MotorSpeed 当计数器用：中段停住 400ms，报文照发。"""
    def payload(i):
        return mot(i if i < 50 else max(50 - (i - 70), 0) + (i - 50 if i >= 70 else 0) + 50 * 0)
    # 更直白地构造：0~49 递增，50~69 冻结在 49，之后继续递增
    def payload(i):  # noqa: F811
        return mot(i if i < 50 else (49 if i < 70 else i - 20))
    store = build(periodic(MOT, payload), dbc)
    frozen = [f for f in fof(diag(store, dbc, MOT), "sig_counter") if "冻结" in f["title"]]
    assert frozen and frozen[0]["severity"] == "error"
    assert frozen[0]["time_end"] - frozen[0]["time_start"] > 0.3


def test_counter_jump(dbc):
    def payload(i):
        return mot(i if i < 50 else i + 37)      # 第 50 帧处 +38 跳变
    store = build(periodic(MOT, payload), dbc)
    jumps = [f for f in fof(diag(store, dbc, MOT), "sig_counter") if "跳变" in f["title"]]
    assert jumps and 0.98 < jumps[0]["time_start"] < 1.01


def test_counter_clean_no_finding(dbc):
    store = build(periodic(MOT, lambda i: mot(i)), dbc)
    assert fof(diag(store, dbc, MOT), "sig_counter") == []


def test_undecodable_message_skips_signal_checks(dbc):
    """录到 4 字节、DBC 定义 8 字节：应明说解不开，而不是静默地没有信号结论。"""
    store = build(periodic(VEH, b"\x01\x02\x03\x04"), dbc)
    report = diag(store, dbc, VEH)
    assert fof(report, "msg_decode")
    assert report["signals"] == []
    assert any("跳过" in n for n in report["notes"])


def test_no_dbc_still_gives_frame_level(dbc):
    store = SeriesStore(sorted(periodic(VEH, veh(), skip=[(1000, 1200)])
                               + periodic(CCM, ccm()), key=lambda f: f.t), None)
    report = diagnose_message(store, None, VEH, DiagConfig())
    assert report["in_dbc"] is False and report["stats"]["frames"] > 0
    assert fof(report, "msg_gap")                     # 无 DBC 时按实测中位周期判缺口
    assert report["signals"] == []


# --------------------------------------------------------------------------- #
# 健康日志 & 排行
# --------------------------------------------------------------------------- #
def test_healthy_message_has_no_findings(dbc):
    report = diag(healthy(dbc), dbc, VEH)
    assert [f for f in report["findings"] if f["severity"] in ("critical", "error", "warn")] == []


def test_rank_puts_broken_message_first(dbc):
    frames = (periodic(VEH, lambda i: veh(100 + i), skip=[(1000, 1400)])
              + periodic(CCM, lambda i: ccm(i % 4))
              + periodic(MOT, lambda i: mot(i % 256)))
    ranking = rank_messages(build(frames, dbc), dbc, DiagConfig.from_dbc(dbc))
    assert ranking and ranking[0]["id"] == VEH
    assert ranking[0]["gaps"] == 1 and "中断" in ranking[0]["headline"]
    assert all(r["id"] != CCM for r in ranking)       # 健康报文不进榜


def test_rank_empty_for_healthy_log(dbc):
    assert rank_messages(healthy(dbc), dbc, DiagConfig.from_dbc(dbc)) == []
