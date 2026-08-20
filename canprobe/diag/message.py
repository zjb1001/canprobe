"""报文级 / 信号级体检：给定一条报文（如 0x270），列出它在这份日志里的全部异常。

总线级诊断（:mod:`canprobe.diag.engine`）回答的是「这条总线有没有毛病」，而现场
排查往往反过来——客户直接报「0x270 这条报文有问题」。这里就是那条入口：输入
`0x270` / `624` / 报文名，直接给出「录到没有、周期对不对、什么时候断的、DLC 变没变、
哪个信号卡死 / 越界 / 计数器不跳」，每条结论都带时刻，可点回 Graphics。

两级代价刻意分开：
* :func:`rank_messages` 扫全日志所有报文，**只看帧级事实**（时序 / DLC / 载荷字节），
  不解码任何信号，用于「还不知道是哪条报文出问题」。
* :func:`diagnose_message` 钻取单条报文，才解码它的信号（``SeriesStore`` 本来就是
  按报文惰性解码的，代价只有这一条报文）。
"""
from __future__ import annotations

import re
from typing import Optional

import numpy as np

from .model import DiagConfig, Finding

# 名字命中即认定用途。行为判据（见 _looks_like_counter）作为补充，两者取并集：
# 有的 DBC 把计数器叫 `BCM_MsgCnt`，也有的只叫 `Rolling`。
_COUNTER_RE = re.compile(r"(?i)(rolling|alive|counter|cntr|msgcnt|_cnt|\bcnt\b|_alv|_rc\b)")
_CHECKSUM_RE = re.compile(r"(?i)(checksum|chksum|chks|crc|_cs\b)")

# 停发 / 迟到判定：末帧距日志结束超过这么多个周期，且至少 _STOP_MIN_S 秒。
# 只按周期倍数判会在 2ms 周期的报文上把 10ms 的收尾偏差报成「停发」。
_STOP_FACTOR = 5.0
_STOP_MIN_S = 0.5
# 突发判定：间隔小于期望周期的这个比例
_BURST_RATIO = 0.25
# 中断判定：间隔至少要到周期的 1.5 倍（= 真的漏了一帧），否则只是抖动
_GAP_MIN_RATIO = 0.5
# 一份报告里最多逐条列出多少个中断，其余汇总成一条（绝不静默截断）
_GAP_LIST_MAX = 10
# 错误帧关联窗口
_ERROR_NEAR_S = 0.05
# 信号 / 载荷卡死的判定长度，见 _stuck_threshold
_STUCK_MIN_S = 1.0
_STUCK_MIN_RATIO = 0.25
_STUCK_CAP_S = 10.0
# 恒定段还要达到「该信号自身平均变化间隔」的这么多倍才算卡死
_STUCK_VS_TYPICAL = 10.0


# --------------------------------------------------------------------------- #
# 输入解析：0x270 / 624 / 270h / 报文名
# --------------------------------------------------------------------------- #
def resolve_message(dbc, store, ident) -> dict:
    """把用户输入解析成报文 ID。

    返回 ``{"id":int|None, "name":str|None, "matched_by":str, "error":str|None,
    "candidates":[...]}``。``matched_by ∈ {hex, dec, dec_as_hex, name, name_prefix}``。

    十进制串查无此 ID、而按十六进制解释查得到时，按十六进制解释并把
    ``matched_by`` 标成 ``dec_as_hex`` —— 客户嘴里的「270」十有八九是 0x270，
    但这个改判必须写在报告里，不能悄悄发生。
    """
    raw = str(ident or "").strip()
    if not raw:
        return {"id": None, "name": None, "matched_by": None,
                "error": "请输入报文 ID（如 0x270 / 624）或报文名", "candidates": []}

    known = set(dbc.messages) if dbc is not None else set()
    present = store.present_message_ids() if store is not None else set()

    def _hit(mid: int) -> bool:
        return mid in known or mid in present

    def _name_of(mid: int) -> Optional[str]:
        m = dbc.messages.get(mid) if dbc is not None else None
        return m.name if m else None

    token = raw.replace(" ", "")
    hex_body = None
    if token[:2].lower() == "0x":
        hex_body = token[2:]
    elif token[-1:].lower() == "h" and _is_hex(token[:-1]):
        hex_body = token[:-1]

    if hex_body is not None and _is_hex(hex_body):
        mid = int(hex_body, 16)
        return {"id": mid, "name": _name_of(mid), "matched_by": "hex",
                "error": None, "candidates": []}

    if token.isdigit():
        dec = int(token, 10)
        as_hex = int(token, 16)          # 纯十进制串一定也是合法十六进制串
        if _hit(dec) or dec == as_hex or not _hit(as_hex):
            return {"id": dec, "name": _name_of(dec), "matched_by": "dec",
                    "error": None, "candidates": []}
        return {"id": as_hex, "name": _name_of(as_hex), "matched_by": "dec_as_hex",
                "error": None, "candidates": []}

    if _is_hex(token) and not (dbc and _name_matches(dbc, token)):
        mid = int(token, 16)
        return {"id": mid, "name": _name_of(mid), "matched_by": "hex",
                "error": None, "candidates": []}

    # 按名字找
    if dbc is not None:
        exact = _name_matches(dbc, token)
        if len(exact) == 1:
            mid = exact[0]
            return {"id": mid, "name": _name_of(mid), "matched_by": "name",
                    "error": None, "candidates": []}
        subs = [mid for mid, m in dbc.messages.items() if token.lower() in m.name.lower()]
        if len(subs) == 1:
            return {"id": subs[0], "name": _name_of(subs[0]), "matched_by": "name_prefix",
                    "error": None, "candidates": []}
        if len(subs) > 1:
            cands = [{"id": mid, "name": _name_of(mid)} for mid in sorted(subs)[:20]]
            return {"id": None, "name": None, "matched_by": None,
                    "error": f"「{raw}」匹配到 {len(subs)} 条报文，请写得更具体或直接用 ID",
                    "candidates": cands}

    return {"id": None, "name": None, "matched_by": None,
            "error": f"无法识别「{raw}」：既不是合法 ID（0x270 / 624），"
                     f"当前 DBC 里也没有同名报文", "candidates": []}


def _is_hex(s: str) -> bool:
    return bool(s) and all(c in "0123456789abcdefABCDEF" for c in s)


def _name_matches(dbc, token: str) -> list[int]:
    low = token.lower()
    return [mid for mid, m in dbc.messages.items() if m.name.lower() == low]


# --------------------------------------------------------------------------- #
# 帧级统计
# --------------------------------------------------------------------------- #
def _period_stats(ts: np.ndarray) -> Optional[dict]:
    if len(ts) < 2:
        return None
    d = np.diff(ts) * 1000.0
    return {
        "median": float(np.median(d)),
        "mean": float(d.mean()),
        "min": float(d.min()),
        "max": float(d.max()),
        "p99": float(np.percentile(d, 99)),
        "std": float(d.std()),
    }


def _longest_run(values) -> tuple[int, int]:
    """最长恒定段的 ``[起始下标, 结束下标]``（闭区间）。空序列返回 (0, 0)。"""
    n = len(values)
    if n == 0:
        return 0, 0
    if n == 1:
        return 0, 0
    changed = np.asarray(values[1:] != values[:-1])
    edges = np.flatnonzero(changed) + 1
    starts = np.concatenate(([0], edges))
    ends = np.concatenate((edges, [n])) - 1
    k = int(np.argmax(ends - starts))
    return int(starts[k]), int(ends[k])


def _stuck_threshold(span_s: float) -> float:
    """恒定多久才算「卡死」。

    纯比例（占跨度 25%）在长日志里会失灵——10 分钟的日志里冻结 1 分钟显然是问题，
    却够不上 150s；纯绝对值在短日志里又太钝。取「跨度的 25%，但不低于 1s、
    不高于 10s」：短日志按比例，长日志封顶在 10s。
    """
    return max(_STUCK_MIN_S, min(span_s * _STUCK_MIN_RATIO, _STUCK_CAP_S))


def _bus_active_between(all_ts: np.ndarray, t0: float, t1: float) -> bool:
    """缺口期间总线上是否还有别的流量 —— 区分「这条报文没了」和「总线静默」。"""
    lo = int(np.searchsorted(all_ts, t0, side="right"))
    hi = int(np.searchsorted(all_ts, t1, side="left"))
    return hi > lo


def _base_period(per, expected_ms, tol) -> tuple[Optional[float], str]:
    """缺口判据的基准周期与它的来源。

    DBC 的 GenMsgCycleTime 与实测差太多时（DBC 过期、该报文实际按别的节奏发），
    拿它当基准会把每一帧都判成「迟到」——一条报文能刷出几万条中断。这种情况
    改用实测中位周期，「和 DBC 不符」由 msg_period 单独报一次就够了。
    """
    if per and (not expected_ms or abs(per["median"] - expected_ms) / expected_ms > tol):
        return per["median"], "measured"
    return expected_ms, "dbc"


def _timing_on(ts: np.ndarray, bus_ts: np.ndarray, expected_ms, tol) -> dict:
    """一串同 ID 同通道的时间戳上的周期 / 缺口 / 突发。"""
    per = _period_stats(ts)
    base_ms, base_src = _base_period(per, expected_ms, tol)
    out = {"frames": int(len(ts)),
           "first": float(ts[0]) if len(ts) else None,
           "last": float(ts[-1]) if len(ts) else None,
           "period_ms": per, "base_period_ms": base_ms,
           "base_period_source": base_src, "gaps": [], "bursts": 0}
    if len(ts) > 1 and base_ms:
        base = base_ms / 1000.0
        d = np.diff(ts)
        # 至少真的漏掉一帧才算中断：±tol 内的抖动是正常仲裁排队，不是缺帧
        for j in np.flatnonzero(d > base * (1.0 + max(tol, _GAP_MIN_RATIO))):
            t0, t1 = float(ts[j]), float(ts[j + 1])
            out["gaps"].append({
                "t0": t0, "t1": t1, "gap_ms": float(d[j] * 1000.0),
                "missing": max(1, int(round(d[j] / base)) - 1),
                "bus_active": _bus_active_between(bus_ts, t0, t1),
            })
        out["bursts"] = int(np.count_nonzero(d < base * _BURST_RATIO))
    return out


def _frame_level_stats(ts, dlc, data, channels, all_ts, expected_ms, tol,
                       all_channels=None) -> dict:
    """一条报文的帧级事实。

    **时序一律按通道分开算**：同一个 ID 出现在两条总线上（网关双写很常见）时，
    把两路时间戳混在一起会让实测周期腰斩、并凭空造出上万个「缺口」和「突发」。
    单通道日志下 per-channel 结果与全局完全一致，行为不变。
    """
    chans = sorted({int(c) for c in channels})
    stats = {
        "frames": int(len(ts)),
        "first": float(ts[0]) if len(ts) else None,
        "last": float(ts[-1]) if len(ts) else None,
        "channels": chans,
        "dlc": {str(int(k)): int(v) for k, v in
                zip(*np.unique(dlc, return_counts=True))} if len(dlc) else {},
        "expected_period_ms": expected_ms,
        "payload_constant": False,
        "payload_stuck_run": None,
    }
    span = float(ts[-1] - ts[0]) if len(ts) > 1 else 0.0
    stats["span_s"] = span
    stats["rate_hz"] = (len(ts) - 1) / span if span > 0 else None

    per_channel = {}
    for ch in chans:
        sel = channels == ch
        bus_ts = all_ts
        if all_channels is not None and len(chans) > 1:
            bus_ts = all_ts[all_channels == ch]   # 「总线还活着吗」只看同一条总线
        per_channel[ch] = _timing_on(ts[sel], bus_ts, expected_ms, tol)
    stats["per_channel"] = per_channel

    # 顶层指标取帧数最多的那条通道（单通道时即它自己），缺口/突发则合并全部通道
    primary = max(chans, key=lambda c: per_channel[c]["frames"]) if chans else None
    prim = per_channel.get(primary, {})
    stats["primary_channel"] = primary
    stats["period_ms"] = prim.get("period_ms")
    stats["base_period_ms"] = prim.get("base_period_ms")
    stats["base_period_source"] = prim.get("base_period_source", "dbc")
    stats["gaps"] = [dict(g, ch=ch) for ch in chans for g in per_channel[ch]["gaps"]]
    stats["gaps"].sort(key=lambda g: g["t0"])
    stats["bursts"] = sum(per_channel[ch]["bursts"] for ch in chans)

    if len(data) and data.shape[1]:
        rows = data
        stats["payload_constant"] = bool(np.all(rows == rows[0]))
        if not stats["payload_constant"] and len(rows) > 1:
            # 按行做「与上一帧是否相同」，找最长不变段
            same = np.all(rows[1:] == rows[:-1], axis=1)
            i0, i1 = _longest_run_from_same(same)
            if i1 > i0:
                stats["payload_stuck_run"] = {
                    "t0": float(ts[i0]), "t1": float(ts[i1]),
                    "frames": int(i1 - i0 + 1),
                    "duration_s": float(ts[i1] - ts[i0]),
                }
    return stats


def _longest_run_from_same(same: np.ndarray) -> tuple[int, int]:
    """由「第 i 帧与第 i-1 帧相同」的布尔序列求最长恒定段闭区间下标。"""
    best_len, best_end, cur = 0, 0, 0
    for i, s in enumerate(same.tolist()):
        cur = cur + 1 if s else 0
        if cur > best_len:
            best_len, best_end = cur, i + 1
    return best_end - best_len, best_end


# --------------------------------------------------------------------------- #
# 帧级结论
# --------------------------------------------------------------------------- #
def _nearby_messages(store, dbc, mid: int) -> list[dict]:
    """报了个日志里没有的 ID 时，给出可能是「想查的其实是这个」的候选。"""
    present = sorted(store.present_message_ids()) if store is not None else []
    out = []
    for other in present:
        if other == mid:
            continue
        twin = abs(other - mid) <= 4
        # 十六 / 十进制写混：0x270 与 十进制 270(=0x10E)
        hexdec = False
        try:
            hexdec = other == int(str(mid), 16) or mid == int(str(other), 16)
        except ValueError:
            pass
        if twin or hexdec:
            m = dbc.messages.get(other) if dbc is not None else None
            out.append({"id": other, "name": m.name if m else None,
                        "frames": int(len(store._indices_for_id(other)))})
    return out[:8]


def _absent_findings(store, dbc, mid: int) -> list[Finding]:
    label = f"0x{mid:X}"
    in_dbc = dbc is not None and mid in dbc.messages
    near = _nearby_messages(store, dbc, mid)
    extra = ""
    if near:
        extra = "；日志里有这些相近 ID：" + ", ".join(
            f"0x{n['id']:X}{'(' + n['name'] + ')' if n['name'] else ''}×{n['frames']}"
            for n in near)
    if in_dbc:
        m = dbc.messages[mid]
        why = (f"DBC 里定义了 {m.name}（发送节点 {', '.join(m.senders) or '未标注'}）"
               f"，但这份日志里一帧都没有")
        sug = ("确认该节点是否上电/在网、该报文是否在本次工况下才发送、"
               "以及录制通道是否覆盖这条总线")
    else:
        why = "当前 DBC 里没有这条报文，日志里也一帧都没有"
        sug = "确认 DBC 版本与报文 ID 是否写对（十六进制 / 十进制容易混）"
    return [Finding(
        check="msg_absent", severity="error", title=f"报文 {label} 在日志中不存在",
        explanation=why + extra, suggestion=sug, entities=[label])]


def _timing_findings(mid: int, stats: dict, cfg: DiagConfig,
                     dbc_cycle: Optional[float], log_end: Optional[float]) -> list[Finding]:
    label = f"0x{mid:X}"
    out: list[Finding] = []
    per = stats["period_ms"]

    if stats["frames"] == 1:
        out.append(Finding(
            check="msg_period", severity="warn", title=f"报文 {label} 全程只有 1 帧",
            explanation=f"整份日志里这条报文只出现在 t={stats['first']:.3f}s，无法构成周期",
            suggestion="若它本应是周期报文，检查发送节点是否发一帧就停；若是事件报文属正常",
            time_start=stats["first"], entities=[label]))
        return out

    if per is None:
        return out

    if dbc_cycle:
        dev = (per["median"] - dbc_cycle) / dbc_cycle
        if abs(dev) > cfg.period_tolerance:
            out.append(Finding(
                check="msg_period", severity="warn", title=f"报文 {label} 实测周期与 DBC 不符",
                explanation=f"DBC 期望 {dbc_cycle:.0f}ms，实测中位 {per['median']:.1f}ms"
                            f"（偏差 {dev*100:+.0f}%，min {per['min']:.1f} / "
                            f"max {per['max']:.1f} / p99 {per['p99']:.1f}ms）。"
                            f"下面的中断判定改以实测中位周期为基准，否则每一帧都会被判成迟到",
                suggestion="检查发送节点的调度周期配置，或确认 DBC 的 GenMsgCycleTime 是否过期",
                entities=[label]))
        elif per["p99"] > dbc_cycle * (1.0 + cfg.period_tolerance):
            out.append(Finding(
                check="msg_period", severity="warn", title=f"报文 {label} 周期抖动偏大",
                explanation=f"中位周期 {per['median']:.1f}ms 与 DBC {dbc_cycle:.0f}ms 相符，"
                            f"但 p99 达 {per['p99']:.1f}ms、最大 {per['max']:.1f}ms",
                suggestion="抖动多来自发送节点任务被抢占或总线仲裁排队；结合负载一起看",
                entities=[label]))
    else:
        out.append(Finding(
            check="msg_period", severity="info", title=f"报文 {label} 实测周期 {per['median']:.1f}ms",
            explanation=f"DBC 未标注 GenMsgCycleTime，以实测中位周期为基准判缺口"
                        f"（min {per['min']:.1f} / max {per['max']:.1f} / p99 {per['p99']:.1f}ms）",
            suggestion="若这条报文本是事件触发型，缺口类结论应按事件语义解读",
            entities=[label]))

    base = stats["base_period_ms"]
    gaps = stats["gaps"]
    # 几千个中断逐条列出来只会把报告淹了：按时长取最长的若干条，剩下的汇总成一条。
    # 汇总条必须存在——静默截断会让人以为"就这几处"。
    listed = sorted(gaps, key=lambda g: -g["gap_ms"])[:_GAP_LIST_MAX]
    if len(gaps) > len(listed):
        total_missing = sum(g["missing"] for g in gaps)
        out.append(Finding(
            check="msg_gap", severity="error", title=f"报文 {label} 共 {len(gaps)} 处中断",
            explanation=f"按 {base:.0f}ms 周期（{'DBC 标注' if stats['base_period_source'] == 'dbc' else '实测中位'}）"
                        f"累计约缺 {total_missing} 帧，占应发帧数的 "
                        f"{total_missing / (total_missing + stats['frames']) * 100:.1f}%。"
                        f"下面只逐条列出最长的 {len(listed)} 处",
            suggestion="缺帧密集且分散多指向总线负载/仲裁；集中在某几段则查发送节点当时的状态",
            time_start=min(g["t0"] for g in gaps), time_end=max(g["t1"] for g in gaps),
            entities=[label]))
    multi_ch = len(stats["channels"]) > 1
    for g in sorted(listed, key=lambda g: g["t0"]):
        where = ("此期间总线上其它报文仍在通信 —— 是这条报文单独消失，不是总线静默"
                 if g["bus_active"] else "此期间整条总线都没有报文 —— 属总线级静默，不能只怪这条报文")
        ch_tag = f"（ch{g['ch']}）" if (multi_ch and "ch" in g) else ""
        out.append(Finding(
            check="msg_gap", severity="error" if g["bus_active"] else "warn",
            title=f"报文 {label} 中断 {g['gap_ms']:.0f}ms{ch_tag}",
            explanation=f"t={g['t0']:.3f}~{g['t1']:.3f}s 无此报文，按 {base:.0f}ms 周期"
                        f"约缺 {g['missing']} 帧。{where}",
            suggestion=("检查发送节点在该时刻的状态（复位/休眠/任务超时）与该时段的错误帧"
                        if g["bus_active"] else "检查整机供电或录制中断"),
            time_start=g["t0"], time_end=g["t1"], entities=[label],
            evidence=[{"t": g["t0"], "id": mid}, {"t": g["t1"], "id": mid}]))

    if stats["bursts"]:
        out.append(Finding(
            check="msg_burst", severity="warn", title=f"报文 {label} 存在异常密集发送",
            explanation=f"有 {stats['bursts']} 次相邻间隔小于期望周期的 {_BURST_RATIO:.0%}"
                        f"（期望 {base:.0f}ms，最小实测 {per['min']:.1f}ms）",
            suggestion="常见于多个节点发同一 ID、网关回灌、或事件报文与周期报文叠加",
            entities=[label]))

    if base and log_end is not None and stats["last"] is not None:
        tail = log_end - stats["last"]
        if tail > max(base / 1000.0 * _STOP_FACTOR, _STOP_MIN_S):
            out.append(Finding(
                check="msg_stop", severity="error", title=f"报文 {label} 中途停发",
                explanation=f"末帧在 t={stats['last']:.3f}s，此后到日志结束的 {tail:.3f}s "
                            f"再没出现过（周期 {base:.0f}ms）",
                suggestion="发送节点掉电 / 复位 / Bus-Off 未恢复都会这样；查该时刻前后的错误帧",
                time_start=stats["last"], time_end=log_end, entities=[label],
                evidence=[{"t": stats["last"], "id": mid}]))
    return out


def _late_finding(mid: int, stats: dict, log_start: Optional[float]) -> list[Finding]:
    base = stats.get("base_period_ms")
    if not base or log_start is None or stats["first"] is None:
        return []
    head = stats["first"] - log_start
    if head <= max(base / 1000.0 * _STOP_FACTOR, _STOP_MIN_S):
        return []
    return [Finding(
        check="msg_late", severity="warn", title=f"报文 0x{mid:X} 迟到",
        explanation=f"日志从 t={log_start:.3f}s 开始，这条报文到 t={stats['first']:.3f}s "
                    f"才第一次出现（迟 {head:.3f}s，周期 {base:.0f}ms）",
        suggestion="节点上电较晚 / 网络管理唤醒较晚 / 该报文由某条件触发后才开始周期发送",
        time_start=log_start, time_end=stats["first"], entities=[f"0x{mid:X}"])]


def _dlc_findings(mid: int, stats: dict, dbc_len: Optional[int]) -> list[Finding]:
    label = f"0x{mid:X}"
    out = []
    dist = stats["dlc"]
    if len(dist) > 1:
        detail = ", ".join(f"{k} 字节×{v}" for k, v in sorted(dist.items(), key=lambda x: -x[1]))
        out.append(Finding(
            check="msg_dlc", severity="warn", title=f"报文 {label} 长度不一致",
            explanation=f"同一 ID 出现多种 DLC：{detail}",
            suggestion="多为多个节点复用同一 ID、或发送方版本不一致；按 DBC 定义的长度核对",
            entities=[label]))
    elif dist and dbc_len is not None:
        only = int(next(iter(dist)))
        if only != dbc_len:
            out.append(Finding(
                check="msg_dlc", severity="warn", title=f"报文 {label} 长度与 DBC 不符",
                explanation=f"日志中恒为 {only} 字节，DBC 定义 {dbc_len} 字节",
                suggestion="长度短于 DBC 定义时 cantools 会整条拒绝解码，信号全为空",
                entities=[label]))
    if len(stats["channels"]) > 1:
        out.append(Finding(
            check="msg_multichannel", severity="info", title=f"报文 {label} 出现在多个通道",
            explanation=f"通道 {stats['channels']} 上都有这条报文",
            suggestion="通常是网关双写；分析时注意两路的时序与内容可能不同",
            entities=[label]))
    return out


def _payload_findings(mid: int, stats: dict) -> list[Finding]:
    label = f"0x{mid:X}"
    if stats["frames"] < 3:
        return []
    if stats["payload_constant"]:
        return [Finding(
            check="msg_payload_stuck", severity="warn", title=f"报文 {label} 载荷全程不变",
            explanation=f"{stats['frames']} 帧的数据字节完全相同，报文在发但内容一动不动",
            suggestion="发送节点应用层挂死 / 数据源未更新时就是这个样子；"
                       "若这条报文本就是静态配置类信息则属正常",
            time_start=stats["first"], time_end=stats["last"], entities=[label])]
    run = stats.get("payload_stuck_run")
    span = stats.get("span_s") or 0.0
    if run and run["duration_s"] >= _stuck_threshold(span):
        return [Finding(
            check="msg_payload_stuck", severity="warn", title=f"报文 {label} 载荷长时间冻结",
            explanation=f"t={run['t0']:.3f}~{run['t1']:.3f}s（{run['duration_s']:.3f}s、"
                        f"{run['frames']} 帧）数据字节一字不变，其余时段有变化",
            suggestion="对照该时段的车辆工况：若工况在变而报文不变，指向发送节点数据链路卡死",
            time_start=run["t0"], time_end=run["t1"], entities=[label])]
    return []


def _error_nearby_findings(mid: int, stats: dict, events) -> list[Finding]:
    """把 Tier2 的错误帧 / 状态事件挂到这条报文的缺口上。"""
    if not events or not stats["gaps"]:
        return []
    ev_ts = np.array([e.t for e in events], dtype=np.float64)
    out = []
    for g in stats["gaps"]:
        lo = np.searchsorted(ev_ts, g["t0"] - _ERROR_NEAR_S, side="left")
        hi = np.searchsorted(ev_ts, g["t1"] + _ERROR_NEAR_S, side="right")
        n = int(hi - lo)
        if not n:
            continue
        kinds: dict[str, int] = {}
        for e in events[int(lo):int(hi)]:
            key = e.error_type or e.state or e.kind
            kinds[key] = kinds.get(key, 0) + 1
        out.append(Finding(
            check="msg_error_nearby", severity="error",
            title=f"报文 0x{mid:X} 的中断伴随 {n} 个错误事件",
            explanation=f"t={g['t0']:.3f}~{g['t1']:.3f}s 的中断前后 ±{_ERROR_NEAR_S*1000:.0f}ms "
                        f"内有：" + ", ".join(f"{k}×{v}" for k, v in kinds.items()),
            suggestion="错误帧与报文消失同时出现，优先查该节点的收发器/接线，而非应用层",
            time_start=g["t0"], time_end=g["t1"], entities=[f"0x{mid:X}"]))
    return out[:10]


# --------------------------------------------------------------------------- #
# 信号级
# --------------------------------------------------------------------------- #
def _looks_like_counter(raw: np.ndarray, modulus: int) -> bool:
    if len(raw) < 8 or modulus <= 1:
        return False
    finite = raw[~np.isnan(raw)]
    if len(finite) < 8 or len(np.unique(finite)) < 4:
        return False
    if not np.all(np.equal(np.mod(finite, 1), 0)):
        return False
    d = np.diff(finite)
    step = (d == 1) | (d == -(modulus - 1))
    return float(step.mean()) >= 0.7


def _raw_field_report(ts, dlc, data, channels, stats: dict):
    """无 DBC 时从原始载荷中寻找 rolling counter / checksum 候选。

    这里只报告可由数据证明的行为：按主通道分别观察 byte / nibble 是否呈 +1 环绕，
    并沿用计数器跳变、冻结判据。CRC 算法、覆盖范围和初值无法从一段载荷唯一反推，
    因此只列出高变化字节作为候选，绝不声称校验通过或失败。
    """
    primary = stats.get("primary_channel")
    if primary is None:
        return None, []
    selected = channels == primary
    ch_ts, ch_dlc, ch_data = ts[selected], dlc[selected], data[selected]
    if len(ch_ts) < 8:
        return None, []
    width = int(ch_dlc.min()) if len(ch_dlc) else 0
    if width <= 0:
        return None, []

    fields = []
    for byte_index in range(width):
        byte = ch_data[:, byte_index].astype(np.float64)
        fields.extend([
            (f"Byte{byte_index}", byte, 256, byte_index, "byte"),
            (f"Byte{byte_index}.low", np.mod(byte, 16), 16, byte_index, "nibble"),
            (f"Byte{byte_index}.high", np.floor_divide(byte, 16), 16, byte_index, "nibble"),
        ])

    candidates = []
    for name, values, modulus, byte_index, kind in fields:
        unique = int(len(np.unique(values)))
        if unique < 4:
            continue
        diff = np.diff(values)
        normal = (diff == 1) | (diff == -(modulus - 1))
        stable = diff == 0
        progression = float(normal.mean()) if len(normal) else 0.0
        explainable = float((normal | stable).mean()) if len(normal) else 0.0
        if progression >= 0.55 and explainable >= 0.85:
            candidates.append({
                "name": name, "byte": byte_index, "kind": kind,
                "bits": 8 if kind == "byte" else 4, "modulus": modulus,
                "samples": int(len(values)), "unique": unique,
                "min": int(values.min()), "max": int(values.max()),
                "step_ratio": progression, "values": values,
            })

    # 同一个 byte 若 nibble 已能更准确解释 +1 环绕，就不要再把整 byte 重复列为候选。
    candidates.sort(key=lambda item: (-item["step_ratio"], item["bits"]))
    chosen = []
    used_parts = set()
    for candidate in candidates:
        part = (candidate["byte"], candidate["kind"])
        if part in used_parts or any(c["byte"] == candidate["byte"] for c in chosen):
            continue
        chosen.append(candidate)
        used_parts.add(part)

    findings = []
    counter_stats = {
        "gaps": stats.get("per_channel", {}).get(primary, {}).get("gaps", []),
    }
    base_ms = stats.get("per_channel", {}).get(primary, {}).get("base_period_ms") or 0.0
    for candidate in chosen:
        issues = []

        def issue(text, _t=None):
            issues.append(text)

        candidate_findings = _counter_findings(
            candidate["name"], f"ch{primary} raw", ch_ts, candidate.pop("values"),
            candidate["modulus"], counter_stats, base_ms, issue,
            check="msg_raw_counter")
        candidate["issues"] = issues
        findings += candidate_findings

    occupied = {candidate["byte"] for candidate in chosen}
    checksum_candidates = []
    for byte_index in range(width):
        if byte_index in occupied:
            continue
        values = ch_data[:, byte_index]
        changes = int(np.count_nonzero(values[1:] != values[:-1]))
        unique = int(len(np.unique(values)))
        change_ratio = changes / max(1, len(values) - 1)
        if unique >= 8 and change_ratio >= 0.5:
            checksum_candidates.append({
                "name": f"Byte{byte_index}", "unique": unique,
                "change_ratio": change_ratio,
            })
    checksum_candidates.sort(key=lambda item: (-item["change_ratio"], -item["unique"]))

    report = {
        "channel": int(primary), "frames": int(len(ch_ts)), "common_dlc": width,
        "counter_candidates": chosen,
        "checksum_candidates": checksum_candidates[:4],
        "limitation": "未加载包含该报文的 DBC：可从原始位模式识别 rolling counter 候选，"
                      "但 CRC/checksum 的算法、覆盖字节、初值与异或值无法仅凭日志可靠确定。",
    }
    return report, findings


def _signal_report(store, dbc, mid: int, stats: dict, cfg: DiagConfig):
    """逐信号统计 + 结论。返回 ``(rows, findings)``。"""
    rows, findings = [], []
    if dbc is None or mid not in dbc.messages:
        return rows, findings
    msg = dbc.messages[mid]
    base_ms = stats.get("base_period_ms") or 0.0
    span = stats.get("span_s") or 0.0
    label = f"0x{mid:X}"

    for meta in msg.signals:
        name = meta.name
        t, values = store.series(name)
        num_t, num = store.numeric_series(name)
        is_enum = store.is_enum(name)
        row = {
            "name": name, "unit": meta.unit, "samples": int(len(t)),
            "enum": is_enum, "constant": None, "changes": 0, "unique": 0,
            "min": None, "max": None, "value": None, "issues": [],
            "first_issue_t": None, "role": None,
        }
        if not len(t):
            row["issues"].append("无数据")
            rows.append(row)
            continue

        changed = np.asarray(values[1:] != values[:-1]) if len(values) > 1 else np.zeros(0, bool)
        row["changes"] = int(np.count_nonzero(changed))
        try:
            row["unique"] = int(len({v for v in values.tolist()}))
        except TypeError:
            row["unique"] = int(len(np.unique(num[~np.isnan(num)])))
        finite = num[~np.isnan(num)]
        if len(finite):
            row["min"] = float(finite.min())
            row["max"] = float(finite.max())
        row["value"] = _plain_value(values[-1])
        row["constant"] = row["changes"] == 0

        def _issue(text, t0=None):
            row["issues"].append(text)
            if t0 is not None and row["first_issue_t"] is None:
                row["first_issue_t"] = float(t0)

        # -- 恒定 / 卡死 ---------------------------------------------------- #
        if row["constant"] and len(t) >= 3:
            _issue("全程恒定")
            findings.append(Finding(
                check="sig_constant", severity="info",
                title=f"信号 {name} 全程恒定为 {row['value']}",
                explanation=f"报文 {label} 共 {len(t)} 帧，{name} 一次都没有变过",
                suggestion="若这条信号本应随工况变化，指向发送节点未更新该字段",
                time_start=float(t[0]), time_end=float(t[-1]), entities=[label, name]))
        elif len(t) > 2:
            i0, i1 = _longest_run(values)
            dur = float(t[i1] - t[i0])
            # 「久」要相对这条信号自己的更新节奏来说。故障灯这类信号本就几百秒才动
            # 一次，用绝对时长判会把它们全报成卡死；而一条每 0.4s 就变的踏板信号
            # 冻结 55s 才是真的可疑。判据：恒定段 ≥ 该信号平均变化间隔的 _STUCK_VS_TYPICAL 倍。
            typical = (float(t[-1] - t[0]) / row["changes"]) if row["changes"] else float("inf")
            if dur >= _stuck_threshold(span) and dur >= typical * _STUCK_VS_TYPICAL:
                _issue(f"卡死 {dur:.1f}s", t[i0])
                findings.append(Finding(
                    check="sig_stuck", severity="warn",
                    title=f"信号 {name} 长时间不变",
                    explanation=f"t={t[i0]:.3f}~{t[i1]:.3f}s（{dur:.3f}s、{i1-i0+1} 帧）"
                                f"恒为 {_plain_value(values[i0])}，其余时段有变化",
                    suggestion="对照该时段工况判断是「本该不变」还是「该动没动」",
                    time_start=float(t[i0]), time_end=float(t[i1]), entities=[label, name]))

        # -- 越界 ------------------------------------------------------------ #
        if (not is_enum and meta.minimum is not None and meta.maximum is not None
                and meta.maximum > meta.minimum and len(finite)):
            bad = (num < meta.minimum - 1e-9) | (num > meta.maximum + 1e-9)
            n_bad = int(np.count_nonzero(bad))
            if n_bad:
                j = int(np.flatnonzero(bad)[0])
                _issue(f"越界 {n_bad} 次", num_t[j])
                findings.append(Finding(
                    check="sig_range", severity="warn",
                    title=f"信号 {name} 超出 DBC 量程",
                    explanation=f"DBC 量程 [{meta.minimum:g}, {meta.maximum:g}]{meta.unit}，"
                                f"实测 [{row['min']:g}, {row['max']:g}]，越界 {n_bad}/{len(finite)} 个采样，"
                                f"首次 t={num_t[j]:.3f}s",
                    suggestion="多为发送方填了无效值/错误码，或 DBC 的 scale/offset 与实际不符",
                    time_start=float(num_t[j]), entities=[label, name]))

        # -- 枚举非法值 -------------------------------------------------------- #
        if is_enum:
            bad_idx = [i for i, v in enumerate(values.tolist()) if not isinstance(v, str)]
            if bad_idx:
                vals = sorted({int(values[i]) for i in bad_idx
                               if isinstance(values[i], (int, float, np.integer, np.floating))})
                _issue(f"非法枚举值 {vals[:5]}", t[bad_idx[0]])
                findings.append(Finding(
                    check="sig_enum_invalid", severity="warn",
                    title=f"信号 {name} 出现 DBC 未定义的枚举值",
                    explanation=f"{len(bad_idx)}/{len(values)} 个采样解出的原始值 "
                                f"{vals[:8]} 不在 DBC 的取值表 "
                                f"{sorted(meta.choices)} 中，首次 t={t[bad_idx[0]]:.3f}s",
                    suggestion="发送方用了保留值/错误码，或 DBC 的 VAL_ 表落后于软件版本",
                    time_start=float(t[bad_idx[0]]), entities=[label, name]))

        # -- 计数器 ------------------------------------------------------------ #
        modulus = 2 ** meta.length
        raw = _to_raw(num, meta)
        is_counter = bool(_COUNTER_RE.search(name)) or _looks_like_counter(raw, modulus)
        row["role"] = ("counter" if is_counter else
                       "checksum" if _CHECKSUM_RE.search(name) else None)
        if is_counter and len(raw) > 3:
            findings += _counter_findings(name, label, num_t, raw, modulus, stats, base_ms, _issue)

        # -- 校验和 ------------------------------------------------------------ #
        if _CHECKSUM_RE.search(name) and row["constant"] and len(t) >= 3:
            _issue("校验和恒定")
            findings.append(Finding(
                check="sig_checksum", severity="warn",
                title=f"校验和信号 {name} 恒定不变",
                explanation=f"{name} 在 {len(t)} 帧里始终为 {row['value']}，"
                            f"而载荷{'也未变化' if stats['payload_constant'] else '在变化'}",
                suggestion=("载荷变了校验和却不变，说明发送方没有真正计算校验和（接收方会判无效）"
                            if not stats["payload_constant"] else
                            "载荷本身也没变，校验和恒定属正常，重点看载荷为何冻结"),
                time_start=float(t[0]), time_end=float(t[-1]), entities=[label, name]))

        rows.append(row)
    return rows, findings


def _counter_findings(name, label, t, raw, modulus, stats, base_ms, _issue,
                      check: str = "sig_counter") -> list[Finding]:
    """滚动计数器：跳变（丢帧/乱序）与冻结（发送方挂死）。"""
    out = []
    ok = ~np.isnan(raw)
    if int(np.count_nonzero(ok)) < 4:
        return out
    t = t[ok]
    raw = raw[ok]
    d = np.diff(raw)
    normal = (d == 1) | (d == -(modulus - 1))

    # 报文本身缺帧造成的跳变不算计数器的错，单独扣掉
    gap_edges = set()
    for g in stats["gaps"]:
        j = int(np.searchsorted(t, g["t0"], side="right")) - 1
        if 0 <= j < len(d):
            gap_edges.add(j)
    jump_idx = [int(j) for j in np.flatnonzero(~normal & (d != 0)) if j not in gap_edges]
    if jump_idx:
        first = jump_idx[0]
        # 光标落在「出现异常值的那一帧」而不是它前一帧：用户点过去要看的是坏值本身
        bad_t = float(t[first + 1])
        _issue(f"计数跳变 {len(jump_idx)} 次", bad_t)
        out.append(Finding(
            check=check, severity="error",
            title=f"滚动计数器 {name} 跳变",
            explanation=f"共 {len(jump_idx)} 处不连续（已排除报文缺帧造成的跳变），"
                        f"首次在 t={bad_t:.3f}s：{raw[first]:.0f} → {raw[first+1]:.0f}"
                        f"（模 {modulus}）",
            suggestion="接收方通常据此判定报文无效：查总线丢帧、网关转发乱序、发送方计数逻辑",
            time_start=bad_t, entities=[label, name]))

    # 冻结：连续 diff==0，且报文仍在按周期发
    frozen = np.flatnonzero(d == 0)
    if len(frozen):
        i0, i1 = _longest_run_from_same(d == 0)
        dur = float(t[i1] - t[i0])
        min_dur = max(0.1, base_ms / 1000.0 * 5)
        if dur >= min_dur:
            _issue(f"计数冻结 {dur:.2f}s", t[i0])
            out.append(Finding(
                check=check, severity="error",
                title=f"滚动计数器 {name} 冻结",
                explanation=f"t={t[i0]:.3f}~{t[i1]:.3f}s（{dur:.3f}s、{i1-i0+1} 帧）计数器停在 "
                            f"{raw[i0]:.0f} 不动，而报文仍在照常发送",
                suggestion="报文照发但计数不动 = 发送方应用层任务挂死 / 数据被缓存重发，"
                           "接收方会在超时后判失效；这是「报文有问题」最典型的落点",
                time_start=float(t[i0]), time_end=float(t[i1]), entities=[label, name]))
    return out


def _to_raw(num: np.ndarray, meta) -> np.ndarray:
    scale = meta.scale or 1.0
    return (num - (meta.offset or 0.0)) / scale


def _plain_value(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    return v


# --------------------------------------------------------------------------- #
# 入口：单条报文体检
# --------------------------------------------------------------------------- #
def diagnose_message(store, dbc, mid: int, cfg: Optional[DiagConfig] = None,
                     query: Optional[str] = None, matched_by: Optional[str] = None) -> dict:
    """对一条报文做完整体检，返回 ``{resolved, identity, stats, signals, findings}``。"""
    cfg = cfg or DiagConfig.from_dbc(dbc)
    mid = int(mid)
    label = f"0x{mid:X}"
    meta = dbc.messages.get(mid) if dbc is not None else None
    in_dbc = meta is not None
    in_log = store is not None and mid in store.present_message_ids()

    identity = {
        "id": mid, "hex": label, "name": meta.name if meta else None,
        "senders": list(meta.senders) if meta else [],
        "dbc_length": meta.length if meta else None,
        "dbc_cycle_ms": float(meta.cycle_time) if (meta and meta.cycle_time) else None,
        "is_extended": bool(meta.is_extended) if meta else mid > 0x7FF,
        "signal_count": len(meta.signals) if meta else 0,
        "comment": meta.comment if meta else "",
    }
    report = {
        "query": query, "resolved": {"id": mid, "hex": label,
                                     "name": identity["name"], "matched_by": matched_by},
        "in_dbc": in_dbc, "in_log": in_log, "identity": identity,
        "stats": None, "signals": [], "raw_analysis": None,
        "findings": [], "notes": [],
    }

    if not in_log:
        report["findings"] = [f.to_dict() for f in _absent_findings(store, dbc, mid)]
        report["nearby"] = _nearby_messages(store, dbc, mid)
        return report

    ts, dlc, data, channels = store.message_frames(mid)
    all_ts, _, _, all_ch = store.frame_arrays()
    stats = _frame_level_stats(ts, dlc, data, channels, all_ts,
                               identity["dbc_cycle_ms"], cfg.period_tolerance,
                               all_channels=all_ch)
    report["stats"] = stats

    findings = _timing_findings(mid, stats, cfg, identity["dbc_cycle_ms"], store.end_time)
    findings += _late_finding(mid, stats, store.start_time)
    findings += _dlc_findings(mid, stats, identity["dbc_length"])
    findings += _payload_findings(mid, stats)
    findings += _error_nearby_findings(mid, stats, store.events())

    # 解码状态：录到了但解不开时，信号级检查一条都做不了，必须说清楚
    probe = store.message_decode_status().get(mid) if dbc is not None else None
    stats["decode"] = probe
    if not in_dbc:
        raw_analysis, raw_findings = _raw_field_report(ts, dlc, data, channels, stats)
        report["raw_analysis"] = raw_analysis
        findings += raw_findings
        report["notes"].append(
            f"当前 DBC 里没有 {label}：信号名、位定义、量程和枚举检查未执行；"
            "已改用原始载荷候选分析，CRC/checksum 只能列候选字节，不能验证算法。")
    elif probe and not probe.get("ok"):
        findings.append(Finding(
            check="msg_decode", severity="error", title=f"报文 {label} 无法按 DBC 解码",
            explanation=f"抽验 {probe['probed']} 帧全部失败：{probe.get('error')}"
                        f"（日志 {int(dlc[0])} 字节 / DBC {identity['dbc_length']} 字节）",
            suggestion="长度不符最常见（CAN-FD 报文按经典帧录制）；换配套 DBC 或确认录制配置",
            entities=[label]))
        report["notes"].append("信号级检查已跳过：这条报文一帧都解不开。")
    else:
        rows, sig_findings = _signal_report(store, dbc, mid, stats, cfg)
        report["signals"] = rows
        findings += sig_findings

    _sev = {"critical": 0, "error": 1, "warn": 2, "info": 3}
    findings.sort(key=lambda f: (_sev.get(f.severity, 9), f.time_start if f.time_start else 0))
    report["findings"] = [f.to_dict() for f in findings]
    report["severity_counts"] = {s: sum(1 for f in findings if f.severity == s)
                                 for s in ("critical", "error", "warn", "info")}
    return report


# --------------------------------------------------------------------------- #
# 入口：全日志报文排行（帧级，不解码）
# --------------------------------------------------------------------------- #
def rank_messages(store, dbc, cfg: Optional[DiagConfig] = None, limit: int = 30) -> list[dict]:
    """按「帧级异常严重程度」给全日志的报文排序，用于还不知道该查哪条时。

    只用时序 / DLC / 载荷字节，不解码任何信号 —— 500 万帧的日志也是一次
    argsort + 逐 ID 向量化统计。
    """
    if store is None or store.frame_count == 0:
        return []
    cfg = cfg or DiagConfig.from_dbc(dbc)
    all_ts, _, _, all_ch = store.frame_arrays()
    log_end = store.end_time
    groups = store.group_indices_by_id()
    out = []
    for mid, idx in groups.items():
        meta = dbc.messages.get(mid) if dbc is not None else None
        cycle = float(meta.cycle_time) if (meta and meta.cycle_time) else None
        ts, dlc, data, channels = store.message_frames(mid)
        stats = _frame_level_stats(ts, dlc, data, channels, all_ts, cycle,
                                   cfg.period_tolerance, all_channels=all_ch)

        gaps = stats["gaps"]
        base = stats["base_period_ms"]
        stopped = bool(base and log_end is not None and stats["last"] is not None
                       and (log_end - stats["last"]) > max(base / 1000.0 * _STOP_FACTOR,
                                                            _STOP_MIN_S))
        dlc_mixed = len(stats["dlc"]) > 1
        per = stats["period_ms"]
        dev = (abs(per["median"] - cycle) / cycle) if (per and cycle) else 0.0
        unknown = dbc is not None and meta is None

        score = (len(gaps) * 3 + (20 if stopped else 0) + (8 if dlc_mixed else 0)
                 + (6 if stats["payload_constant"] and stats["frames"] > 10 else 0)
                 + (6 if dev > cfg.period_tolerance else 0)
                 + min(len(gaps), 10) * (2 if any(g["bus_active"] for g in gaps) else 0))
        if score <= 0:
            continue
        head = []
        if stopped:
            head.append(f"t={stats['last']:.3f}s 后停发")
        if gaps:
            worst = max(g["gap_ms"] for g in gaps)
            head.append(f"{len(gaps)} 次中断（最长 {worst:.0f}ms）")
        if dlc_mixed:
            head.append("DLC 不一致")
        if stats["payload_constant"] and stats["frames"] > 10:
            head.append("载荷全程不变")
        if dev > cfg.period_tolerance:
            head.append(f"周期偏差 {dev*100:+.0f}%")
        out.append({
            "id": mid, "hex": f"0x{mid:X}", "name": meta.name if meta else None,
            "unknown_in_dbc": unknown,
            "frames": stats["frames"],
            "period_ms": round(per["median"], 2) if per else None,
            "expected_ms": cycle,
            "gaps": len(gaps),
            "max_gap_ms": round(max((g["gap_ms"] for g in gaps), default=0.0), 1),
            "stopped": stopped,
            "score": int(score),
            "headline": "；".join(head),
        })
    out.sort(key=lambda r: (-r["score"], -r["max_gap_ms"]))
    return out[:limit]
