"""通信诊断引擎（Tier 1）测试。

用内存里合成的故障场景验证：负载、周期缺失、节点离线、总线静默，以及
「能力清单」是否正确声明 M1 尚未支持的错误帧/状态机检查。
"""
from canprobe.decoder import SeriesStore
from canprobe.diag import run_diagnostics
from canprobe.diag.model import DiagConfig, NodeSpec
from canprobe.log_parser import Frame


def gen_periodic(ids_periods_ms, start_ms, end_ms, skip_ranges=None):
    """按周期生成 (t, id)；skip_ranges = {id: [(lo_ms, hi_ms), ...]} 注入缺帧。"""
    skip_ranges = skip_ranges or {}
    out = []
    for mid, period_ms in ids_periods_ms:
        skips = skip_ranges.get(mid, [])
        ms = start_ms
        while ms <= end_ms:
            if not any(lo < ms < hi for lo, hi in skips):
                out.append((ms / 1000.0, mid))
            ms += period_ms
    out.sort()
    return out


def store_from(seq):
    frames = [Frame(t=t, frame_id=fid, data=b"\x00" * 8) for t, fid in seq]
    return SeriesStore(frames, None)


def findings_of(report, check):
    return [f for f in report["findings"] if f["check"] == check]


# --------------------------------------------------------------------------- #
# 负载
# --------------------------------------------------------------------------- #
def test_bus_load_over_threshold():
    # 波特率压到 12.5k，8 字节帧 @100Hz 即远超 50% 负载，应报 critical
    seq = gen_periodic([(0x100, 10)], 0, 1000)
    cfg = DiagConfig(baud=12_500, periodic_msgs={}, nodes=[])
    report = run_diagnostics(store_from(seq), None, cfg)
    loads = findings_of(report, "bus_load")
    assert loads, "应报负载 finding"
    assert loads[0]["severity"] == "critical"
    assert report["metrics"]["load_mean_pct"][0] > 50.0


def test_bus_load_below_threshold_no_finding():
    # 高波特率下同样的帧率负载极低，不应报负载
    seq = gen_periodic([(0x100, 10)], 0, 1000)
    cfg = DiagConfig(baud=500_000, periodic_msgs={}, nodes=[])
    report = run_diagnostics(store_from(seq), None, cfg)
    assert findings_of(report, "bus_load") == []


# --------------------------------------------------------------------------- #
# 周期缺失
# --------------------------------------------------------------------------- #
def test_periodicity_missing():
    # 0x123 每 10ms，500~700ms 之间缺席（缺约 20 帧）
    seq = gen_periodic([(0x123, 10)], 0, 1000, {0x123: [(500, 700)]})
    cfg = DiagConfig(periodic_msgs={0x123: 10.0}, nodes=[])
    report = run_diagnostics(store_from(seq), None, cfg)
    per = findings_of(report, "periodicity")
    assert per, "应报周期缺失"
    assert per[0]["entities"] == ["0x123"]
    assert 0.49 <= per[0]["time_start"] <= 0.51


# --------------------------------------------------------------------------- #
# 节点离线
# --------------------------------------------------------------------------- #
def test_node_offline_while_bus_alive():
    # A(0x100) 与 B(0x200) 各 10ms；A 在 1000~1300ms 停发，B 持续
    seq = gen_periodic([(0x100, 10), (0x200, 10)], 0, 1500, {0x100: [(1000, 1300)]})
    cfg = DiagConfig(
        periodic_msgs={},
        nodes=[NodeSpec("A", [0x100], 10.0, 3.0), NodeSpec("B", [0x200], 10.0, 3.0)],
    )
    report = run_diagnostics(store_from(seq), None, cfg)
    offline = findings_of(report, "node_offline")
    assert offline, "应报节点离线"
    assert {f["entities"][0] for f in offline} == {"A"}


def test_node_never_seen():
    seq = gen_periodic([(0x200, 10)], 0, 1000)
    cfg = DiagConfig(nodes=[NodeSpec("GHOST", [0x100], 10.0, 3.0)], periodic_msgs={})
    report = run_diagnostics(store_from(seq), None, cfg)
    offline = findings_of(report, "node_offline")
    assert offline and "全程无报文" in offline[0]["title"]


# --------------------------------------------------------------------------- #
# 总线静默
# --------------------------------------------------------------------------- #
def test_bus_silence():
    # 0~400ms 正常，随后 400~1200ms 完全静默（无任何报文），再恢复
    seq = gen_periodic([(0x100, 10)], 0, 400) + gen_periodic([(0x100, 10)], 1200, 1500)
    cfg = DiagConfig(silence_ms=500.0, periodic_msgs={}, nodes=[])
    report = run_diagnostics(store_from(seq), None, cfg)
    sil = findings_of(report, "bus_silence")
    assert sil, "应报总线静默"
    assert sil[0]["time_end"] - sil[0]["time_start"] >= 0.5


# --------------------------------------------------------------------------- #
# 能力清单
# --------------------------------------------------------------------------- #
def test_capabilities_declare_unavailable():
    seq = gen_periodic([(0x100, 10)], 0, 100)
    report = run_diagnostics(store_from(seq), None, DiagConfig(periodic_msgs={}, nodes=[]))
    cap = report["capabilities"]
    assert cap["error_frames"] is False
    unavail = {u["check"] for u in cap["unavailable_checks"]}
    assert "error_frame" in unavail
    assert "error_state_machine" in unavail
    assert "baud_mismatch" in unavail
    # Tier1 检查始终可用
    assert set(cap["available_checks"]) >= {"bus_load", "periodicity", "node_offline", "bus_silence"}
