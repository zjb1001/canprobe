"""通信诊断 Tier 2 / Tier 3 + 能力检测 + 导出测试（合成错误帧场景）。"""
from canprobe.decoder import SeriesStore
from canprobe.diag import run_diagnostics, to_markdown
from canprobe.diag.model import DiagConfig
from canprobe.log_parser import Frame, parse_asc


def data(t, fid=0x100):
    return Frame(t=t, frame_id=fid, data=b"\x00" * 8)


def error(t, et="form"):
    return Frame(t=t, frame_id=0, data=b"", kind="error", error_type=et)


def status(t, state):
    return Frame(t=t, frame_id=0, data=b"", kind="status", state=state)


def build(frames):
    return SeriesStore(frames, None)


def fof(report, check):
    return [f for f in report["findings"] if f["check"] == check]


def test_error_storm():
    frames = [data(i * 0.05) for i in range(40)]
    frames += [error(1.0 + i * 0.005) for i in range(20)]  # 20 错误/95ms → 风暴
    report = run_diagnostics(build(frames), None, DiagConfig(periodic_msgs={}, nodes=[]))
    err = fof(report, "error_frame")
    assert err and err[0]["severity"] == "critical"
    assert "风暴" in err[0]["title"]


def test_scattered_errors_warn():
    frames = [data(i * 0.05) for i in range(40)]
    frames += [error(0.5, "crc"), error(1.0, "ack"), error(1.5, "form")]  # 低于风暴阈值
    report = run_diagnostics(build(frames), None, DiagConfig(periodic_msgs={}, nodes=[]))
    err = fof(report, "error_frame")
    assert err and err[0]["severity"] == "warn"
    assert "3 个错误帧" in err[0]["title"]


def test_state_machine_bus_off():
    frames = [data(i * 0.05) for i in range(40)]
    frames += [status(1.0, "passive"), status(1.2, "bus_off")]
    report = run_diagnostics(build(frames), None, DiagConfig(periodic_msgs={}, nodes=[]))
    sm = fof(report, "error_state_machine")
    assert any("Bus-Off" in f["title"] for f in sm)


def test_baud_mismatch_inference():
    # 全是位级错误、无 ACK → 波特率/采样点假设
    frames = [data(i * 0.05) for i in range(40)]
    frames += [error(1.0 + i * 0.02, "form") for i in range(8)]
    frames += [error(1.5 + i * 0.02, "stuff") for i in range(4)]
    report = run_diagnostics(build(frames), None, DiagConfig(periodic_msgs={}, nodes=[]))
    bm = fof(report, "baud_mismatch")
    assert bm and 0 < bm[0]["confidence"] < 1.0


def test_termination_inference():
    # ACK 与位级错误混合、无单一主导 → 终端/布线假设
    frames = [data(i * 0.05) for i in range(40)]
    frames += [error(1.0 + i * 0.02, "ack") for i in range(4)]
    frames += [error(1.5 + i * 0.02, "bit") for i in range(4)]
    report = run_diagnostics(build(frames), None, DiagConfig(periodic_msgs={}, nodes=[]))
    tw = fof(report, "termination_wiring")
    assert tw and 0 < tw[0]["confidence"] < 1.0


def test_capabilities_detect_errors():
    frames = [data(i * 0.05) for i in range(10)] + [error(1.0, "crc")]
    report = run_diagnostics(build(frames), None, DiagConfig(periodic_msgs={}, nodes=[]))
    cap = report["capabilities"]
    assert cap["error_frames"] is True
    assert "error_frame" in cap["available_checks"]
    assert "baud_mismatch" in cap["available_checks"]


def test_asc_error_frame_parsing():
    text = (
        "base hex timestamps absolute\n"
        "Begin Triggerblock\n"
        "0.100000 1 123 Rx d 8 00 11 22 33 44 55 66 77\n"
        "0.200000 1 ErrorFrame Flags = 0x0\n"
        "0.300000 1 BusOff\n"
    )
    frames = parse_asc(text)
    kinds = {f.kind for f in frames}
    assert "error" in kinds and "status" in kinds


def test_markdown_export():
    frames = [data(i * 0.05) for i in range(10)] + [error(1.0, "ack")]
    report = run_diagnostics(build(frames), None, DiagConfig(periodic_msgs={}, nodes=[]))
    md = to_markdown(report)
    assert "CAN 通信诊断报告" in md
    assert "能力清单" in md
    assert "诊断结论" in md
