"""CAN 通信诊断引擎（Tier 1，确定性结论）。

消费 SeriesStore 的紧凑帧数组，产出结构化 Finding 列表与能力矩阵。
本期（M1）覆盖：总线负载、周期报文缺失、节点离线、总线静默 —— 全部可由
纯数据帧时间流判定。错误帧 / 状态机 / 物理层推断在后续里程碑接入，
此处通过 Capabilities 显式声明「不可用」。
"""
from __future__ import annotations

from typing import Optional

import numpy as np

from .model import Capabilities, DiagConfig, Finding

# 帧位宽近似：标准帧 47 + 8*DLC，扩展帧 65 + 8*DLC，位填充 ×1.2。
# 负载阈值判断对 ±10% 不敏感，故用近似而非逐帧精确填充计算（见最终方案取舍）。
_STD_BITS = 47.0
_EXT_BITS = 65.0
_STUFFING = 1.2


def _frame_bits(dlc: np.ndarray, is_extended: np.ndarray) -> np.ndarray:
    base = np.where(is_extended, _EXT_BITS, _STD_BITS)
    return (base + 8.0 * dlc) * _STUFFING


# --------------------------------------------------------------------------- #
# 负载
# --------------------------------------------------------------------------- #
def _bus_load(ts, dlc, is_extended, channel_mask, baud, window_s):
    """返回 (mean_pct, peak_pct)；空集返回 (0.0, 0.0)。"""
    idx = np.where(channel_mask)[0]
    if len(idx) == 0:
        return 0.0, 0.0
    bits = _frame_bits(dlc[idx], is_extended[idx])
    t = ts[idx]
    span = float(t[-1] - t[0])
    if span <= 0:
        return 0.0, 0.0
    total_bits = float(bits.sum())
    mean = total_bits / (baud * span) * 100.0

    # 峰值：按 load_window 分桶
    n_bins = max(1, int(np.ceil(span / window_s)))
    edges = np.linspace(t[0], t[-1], n_bins + 1)
    hist = np.histogram(t, bins=edges, weights=bits)[0]
    peak = float(hist.max()) / (baud * window_s) * 100.0
    return mean, peak


def _load_findings(ts, dlc, is_extended, channels, cfg, cap: Capabilities) -> list[Finding]:
    findings = []
    means, peaks = [], []
    for ch in sorted(set(int(c) for c in channels)):
        mask = channels == ch
        mean, peak = _bus_load(ts, dlc, is_extended, mask, cfg.baud, cfg.load_window_ms / 1000.0)
        means.append(mean)
        peaks.append(peak)
        ent = [f"ch{ch}"]
        if peak >= cfg.load_crit_pct or mean >= cfg.load_crit_pct:
            findings.append(Finding(
                check="bus_load", severity="critical",
                title=f"总线 {ch} 负载过高",
                explanation=f"平均负载 {mean:.1f}%、峰值 {peak:.1f}%（波特率 {cfg.baud/1000:.0f}k，"
                            f"窗口 {cfg.load_window_ms:.0f}ms，位填充按 1.2× 估算）",
                suggestion="检查报文发送频率与总线拓扑，考虑降低周期报文速率或改用 CAN FD / 多总线分流",
                entities=ent, evidence=[]))
        elif peak >= cfg.load_warn_pct or mean >= cfg.load_warn_pct:
            findings.append(Finding(
                check="bus_load", severity="warn",
                title=f"总线 {ch} 负载偏高",
                explanation=f"平均负载 {mean:.1f}%、峰值 {peak:.1f}%（波特率 {cfg.baud/1000:.0f}k）",
                suggestion="持续观察；若接近周期报文预算上限，考虑优化调度",
                entities=ent, evidence=[]))
    return findings, means, peaks


# --------------------------------------------------------------------------- #
# 周期缺失
# --------------------------------------------------------------------------- #
def _periodicity_findings(ts, ids, cfg: DiagConfig) -> list[Finding]:
    findings = []
    for mid, period_ms in cfg.periodic_msgs.items():
        idx = np.where(ids == mid)[0]
        if len(idx) < 2:
            continue
        period = period_ms / 1000.0
        t = ts[idx]
        gaps = np.diff(t)
        bad = gaps > period * (1.0 + cfg.period_tolerance)
        for j in np.where(bad)[0]:
            gap = float(gaps[j])
            missing = max(1, int(round(gap / period)) - 1)
            findings.append(Finding(
                check="periodicity", severity="warn",
                title=f"报文 0x{mid:X} 周期缺失",
                explanation=f"期望周期 {period_ms:.0f}ms，实际间隔 {gap*1000:.1f}ms，"
                            f"约缺 {missing} 帧",
                suggestion="检查发送节点是否异常、是否被更高优先级报文抢占、或节点已离线",
                time_start=float(t[j]), time_end=float(t[j + 1]),
                entities=[f"0x{mid:X}"],
                evidence=[{"t": float(t[j]), "id": mid}, {"t": float(t[j + 1]), "id": mid}]))
    return findings


# --------------------------------------------------------------------------- #
# 节点离线 / 总线静默
# --------------------------------------------------------------------------- #
def _silence_findings(ts, cfg: DiagConfig) -> list[Finding]:
    """整条总线（所有通道合并）出现长无流量间隔 -> 总线静默。"""
    findings = []
    if len(ts) < 2:
        return findings
    gaps = np.diff(ts)
    bad = gaps > cfg.silence_ms / 1000.0
    for j in np.where(bad)[0]:
        findings.append(Finding(
            check="bus_silence", severity="error",
            title="总线静默",
            explanation=f"整条总线在 t={ts[j]:.3f}~{ts[j+1]:.3f}s 无任何报文，"
                        f"持续 {gaps[j]*1000:.0f}ms",
            suggestion="区分整机休眠/断电（物理层）与接口脱落；若仅个别节点消失则非总线静默",
            time_start=float(ts[j]), time_end=float(ts[j + 1]),
            entities=["bus"], evidence=[{"t": float(ts[j])}, {"t": float(ts[j + 1])}]))
    return findings


def _offline_findings(ts, ids, cfg: DiagConfig) -> list[Finding]:
    """某节点的报文长时间缺席，而总线其余流量仍在 -> 单节点离线。"""
    findings = []
    for node in cfg.nodes:
        if not node.msg_ids:
            continue
        mask = np.isin(ids, node.msg_ids)
        idx = np.where(mask)[0]
        if len(idx) == 0:
            # 整个日志里该节点从未出现
            findings.append(Finding(
                check="node_offline", severity="error",
                title=f"节点 {node.name} 全程无报文",
                explanation=f"日志全程未见节点 {node.name} 的任何报文（期望报文 "
                            f"{', '.join(f'0x{m:X}' for m in node.msg_ids)}）",
                suggestion="检查该节点供电/接线/CAN 收发器，或确认日志是否未接入该通道",
                entities=[node.name]))
            continue
        node_t = ts[idx]
        timeout = node.period_ms / 1000.0 * node.timeout_factor
        # 节点帧之间的缺口，排除首尾之外
        gaps = np.diff(node_t)
        bad = gaps > timeout
        for j in np.where(bad)[0]:
            start, end = float(node_t[j]), float(node_t[j + 1])
            # 判断期间总线是否仍有其它流量（区分总线静默 vs 单节点离线）
            other_traffic = np.any((ts >= start) & (ts <= end) & ~mask)
            if not other_traffic:
                continue  # 整个总线都静默，交给 bus_silence，不重复报节点离线
            findings.append(Finding(
                check="node_offline", severity="error",
                title=f"节点 {node.name} 离线",
                explanation=f"节点 {node.name} 在 t={start:.3f}~{end:.3f}s 无报文 "
                            f"(超时阈值 {node.period_ms*node.timeout_factor:.0f}ms)，"
                            f"但总线其余节点仍在通信",
                suggestion="检查该节点供电/连接/收发器/应用层调度；若为休眠唤醒需在配置中排除",
                time_start=start, time_end=end,
                entities=[node.name],
                evidence=[{"t": start, "id": node.msg_ids[0]}]))
    return findings


# --------------------------------------------------------------------------- #
# Tier 2 / Tier 3：错误帧 / 状态机 / 物理层推断
# --------------------------------------------------------------------------- #
def _error_storms(ts: np.ndarray, window_s: float, threshold: int) -> list[tuple]:
    if len(ts) < threshold:
        return []
    span = float(ts[-1] - ts[0])
    if span <= 0:
        return []
    n = max(1, int(np.ceil(span / window_s)))
    edges = np.linspace(ts[0], ts[-1], n + 1)
    counts, _ = np.histogram(ts, bins=edges)
    out = []
    for i in np.where(counts >= threshold)[0]:
        out.append((float(edges[i]), float(edges[i + 1]), int(counts[i])))
    return out


def _error_frame_findings(events, cfg: DiagConfig) -> list[Finding]:
    errs = [e for e in events if e.kind == "error"]
    if not errs:
        return []
    types: dict[str, int] = {}
    for e in errs:
        types[e.error_type or "other"] = types.get(e.error_type or "other", 0) + 1
    total = len(errs)
    findings = []
    ts = np.array([e.t for e in errs], dtype=np.float64)
    storms = _error_storms(ts, cfg.error_burst_window_ms / 1000.0, cfg.error_burst_threshold)
    for s, e, cnt in storms:
        findings.append(Finding(
            check="error_frame", severity="critical",
            title="错误帧风暴",
            explanation=f"在 {s:.3f}~{e:.3f}s 的 {cfg.error_burst_window_ms:.0f}ms 窗口内"
                        f"出现 {cnt} 个错误帧",
            suggestion="检查是否有节点故障、接地/屏蔽问题或外部干扰源",
            time_start=s, time_end=e, entities=["bus"], evidence=[{"t": s}]))
    if not storms:
        findings.append(Finding(
            check="error_frame", severity="warn", title=f"检测到 {total} 个错误帧",
            explanation="错误类型分布 " + ", ".join(f"{k}:{v}" for k, v in
                                                     sorted(types.items(), key=lambda x: -x[1])),
            suggestion="ACK 错误多指向节点缺席；位级错误多指向波特率/布线",
            entities=["bus"]))
    return findings


def _state_machine_findings(events, cfg: DiagConfig) -> list[Finding]:
    findings = []
    status = sorted((e for e in events if e.kind == "status" and e.state), key=lambda e: e.t)
    seen = set()
    for e in status:
        if e.state in seen:
            continue
        seen.add(e.state)
        if e.state == "bus_off":
            findings.append(Finding(
                check="error_state_machine", severity="critical", title="总线进入 Bus-Off",
                explanation=f"t={e.t:.3f}s 检测到 Bus-Off（错误累计到极限，节点脱离总线）",
                suggestion="检查故障节点收发器/接线；Bus-Off 恢复需应用层主动复位",
                time_start=e.t, entities=["bus"]))
        elif e.state == "passive":
            findings.append(Finding(
                check="error_state_machine", severity="warn", title="进入 Error-Passive",
                explanation=f"t={e.t:.3f}s 检测到 Error-Passive（错误计数 TEC/REC > 127）",
                suggestion="观察错误计数是否继续攀升；排查间歇性错误源",
                time_start=e.t, entities=["bus"]))
    return findings


def _physical_findings(events, cfg: DiagConfig) -> list[Finding]:
    """Tier 3 物理层推断：只给置信度排序的假设 + 测量建议，绝不武断下结论。"""
    errs = [e for e in events if e.kind == "error"]
    if not errs:
        return []
    types: dict[str, int] = {}
    for e in errs:
        types[e.error_type or "other"] = types.get(e.error_type or "other", 0) + 1
    total = len(errs)
    bit_level = sum(types.get(k, 0) for k in ("form", "stuff", "crc", "bit"))
    ack = types.get("ack", 0)
    findings = []
    if total and bit_level / total > 0.5 and bit_level > ack:
        findings.append(Finding(
            check="baud_mismatch", severity="warn", title="疑似波特率/采样点不匹配",
            confidence=0.5,
            explanation=f"位级错误（form/stuff/crc/bit）占 {bit_level}/{total}，"
                        f"明显高于 ACK 错误（{ack}），典型于波特率或采样点偏差",
            suggestion="用示波器测量该节点位宽与采样点，核对波特率与位时序配置",
            entities=["bus"]))
    if total and ack > 0 and bit_level > 0 and 0.2 < ack / total < 0.8:
        findings.append(Finding(
            check="termination_wiring", severity="warn", title="疑似终端/布线问题",
            confidence=0.4,
            explanation=f"错误类型混合（ACK {ack}、位级 {bit_level}），无单一主导类型，"
                        f"符合终端电阻缺失/差分线受损的间歇性错误特征",
            suggestion="测量总线两端终端电阻（约 60Ω 并联），示波器观察差分电平与反射",
            entities=["bus"]))
    return findings


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def _detect_capabilities(channels, events) -> Capabilities:
    """从解析出的事件实测探测能力（非文档假定）。"""
    n_ch = int(max(channels)) + 1 if len(channels) else 1
    return Capabilities.detect(events, n_ch)


def run_diagnostics(store, dbc=None, config: Optional[DiagConfig] = None) -> dict:
    """对已加载日志运行 Tier 1 诊断，返回完整报告 dict。

    报告结构：{capabilities, summary, findings, metrics}。
    findings 按 severity 降序；metrics 供前端绘制负载曲线等。
    """
    if store is None or store.frame_count == 0:
        return {"capabilities": Capabilities().to_dict(), "summary": None,
                "findings": [], "metrics": {}}

    ts, ids, dlc, channels = store.frame_arrays()
    ts = np.asarray(ts, dtype=np.float64)
    ids = np.asarray(ids, dtype=np.uint32)
    dlc = np.asarray(dlc, dtype=np.uint8)
    channels = np.asarray(channels, dtype=np.uint8)
    is_extended = ids > 0x7FF
    events = list(store.events()) if hasattr(store, "events") else []

    cfg = config or DiagConfig.from_dbc(dbc)
    cap = _detect_capabilities(channels, events)

    load_findings, means, peaks = _load_findings(ts, dlc, is_extended, channels, cfg, cap)
    findings = load_findings
    findings += _periodicity_findings(ts, ids, cfg)
    findings += _silence_findings(ts, cfg)
    findings += _offline_findings(ts, ids, cfg)
    findings += _error_frame_findings(events, cfg)
    findings += _state_machine_findings(events, cfg)
    findings += _physical_findings(events, cfg)

    _sev_order = {"critical": 0, "error": 1, "warn": 2, "info": 3}
    findings.sort(key=lambda f: (_sev_order.get(f.severity, 9), -(f.time_start or 0)))

    summary = {
        "frame_count": int(len(ts)),
        "start": float(ts[0]),
        "end": float(ts[-1]),
        "span_s": float(ts[-1] - ts[0]),
        "channels": sorted(set(int(c) for c in channels)),
        "message_ids": int(len(set(ids.tolist()))),
        "nodes": [n.name for n in cfg.nodes],
        "baud": cfg.baud,
        "error_events": sum(1 for e in events if e.kind == "error"),
        "status_events": sum(1 for e in events if e.kind == "status"),
        "finding_count": len(findings),
        "severity_counts": {s: sum(1 for f in findings if f.severity == s)
                            for s in ("critical", "error", "warn", "info")},
    }

    return {
        "capabilities": cap.to_dict(),
        "summary": summary,
        "findings": [f.to_dict() for f in findings],
        "metrics": {
            "load_mean_pct": [round(m, 2) for m in means],
            "load_peak_pct": [round(p, 2) for p in peaks],
        },
    }
