"""诊断报告导出（Markdown / JSON）。"""
from __future__ import annotations

_SEV_ORDER = {"critical": 0, "error": 1, "warn": 2, "info": 3}
_SEV_EMOJI = {"critical": "🔴", "error": "🟠", "warn": "🟡", "info": "🔵"}


def _findings_md(findings: list, lines: list) -> None:
    """结论清单的统一排版（总线级 / 报文级共用）。"""
    if not findings:
        lines.append("未发现异常（注意：仅在「可用检查」范围内有效）。")
        return
    for f in sorted(findings, key=lambda x: _SEV_ORDER.get(x["severity"], 9)):
        conf = f"（置信度 {f['confidence']:.0%}，推断）" if f.get("confidence", 1.0) < 1.0 else ""
        lines.append(f"- {_SEV_EMOJI.get(f['severity'], '•')} **[{f['severity']}] {f['title']}**{conf}")
        lines.append(f"  - {f['explanation']}")
        if f.get("suggestion"):
            lines.append(f"  - 建议：{f['suggestion']}")
        if f.get("time_start") is not None:
            end = f.get("time_end") or f["time_start"]
            lines.append(f"  - 时间：t={f['time_start']:.3f}~{end:.3f}s")


def message_to_markdown(report: dict) -> str:
    """单条报文体检报告 → Markdown。"""
    ident = report.get("identity", {})
    res = report.get("resolved", {})
    title = ident.get("hex") or "?"
    if ident.get("name"):
        title += f" {ident['name']}"
    lines = [f"# 报文体检报告：{title}", ""]

    if res.get("matched_by") == "dec_as_hex":
        lines += [f"> 输入「{report.get('query')}」按十进制在日志/DBC 里查无此 ID，"
                  f"已按十六进制解释为 {ident.get('hex')}。", ""]

    lines += [
        "## 身份",
        "",
        f"- DBC：{'有定义' if report.get('in_dbc') else '**无定义**'}"
        f"{'，发送节点 ' + (', '.join(ident.get('senders') or []) or '未标注') if report.get('in_dbc') else ''}",
        f"- 日志：{'有数据' if report.get('in_log') else '**一帧都没有**'}",
        f"- 长度：DBC {ident.get('dbc_length')} 字节；周期：DBC "
        f"{ident.get('dbc_cycle_ms') or '未标注'} ms；信号数：{ident.get('signal_count')}",
        "",
    ]

    stats = report.get("stats")
    if stats:
        per = stats.get("period_ms") or {}
        lines += [
            "## 时序",
            "",
            f"- 帧数 {stats['frames']}，t={stats.get('first'):.3f}~{stats.get('last'):.3f}s",
            f"- 实测周期：中位 {per.get('median', float('nan')):.1f}ms / min {per.get('min', float('nan')):.1f} / "
            f"max {per.get('max', float('nan')):.1f} / p99 {per.get('p99', float('nan')):.1f}",
            f"- 中断次数：{len(stats.get('gaps', []))}；DLC 分布：{stats.get('dlc')}；"
            f"通道：{stats.get('channels')}",
            "",
        ]

    signals = report.get("signals") or []
    if signals:
        lines += ["## 信号", "", "| 信号 | 采样 | 变化 | 取值范围 | 结论 |", "|---|---|---|---|---|"]
        for s in signals:
            rng = ("—" if s.get("min") is None
                   else f"{s['min']:g} ~ {s['max']:g} {s.get('unit') or ''}".strip())
            lines.append(f"| {s['name']} | {s['samples']} | {s['changes']} | {rng} | "
                         f"{'、'.join(s.get('issues') or []) or '正常'} |")
        lines.append("")

    lines += ["## 诊断结论", ""]
    _findings_md(report.get("findings", []), lines)
    if report.get("notes"):
        lines += ["", "## 说明", ""] + [f"- {n}" for n in report["notes"]]
    return "\n".join(lines) + "\n"


def to_markdown(report: dict) -> str:
    cap = report.get("capabilities", {})
    summary = report.get("summary") or {}
    lines = ["# CAN 通信诊断报告", ""]

    if summary:
        lines += [
            "## 概览",
            "",
            f"- 数据帧：{summary.get('frame_count', 0)}，时间跨度 {summary.get('span_s', 0):.3f}s",
            f"- 通道：{summary.get('channels', [])}，报文 ID 数：{summary.get('message_ids', 0)}",
            f"- 节点：{', '.join(summary.get('nodes', [])) or '（未配置）'}",
            f"- 错误事件：{summary.get('error_events', 0)}，状态事件：{summary.get('status_events', 0)}",
            f"- 波特率假设：{summary.get('baud', 0) / 1000:.0f} kbit/s",
            "",
        ]

    lines += [
        "## 能力清单",
        "",
        "可用检查：`" + ", ".join(cap.get("available_checks", [])) + "`",
        "",
    ]
    if cap.get("unavailable_checks"):
        lines += ["不可用检查：", ""]
        for u in cap["unavailable_checks"]:
            lines.append(f"- `{u['check']}` — {u['reason']}")
        lines.append("")

    lines += ["## 诊断结论", ""]
    _findings_md(report.get("findings", []), lines)
    return "\n".join(lines) + "\n"
