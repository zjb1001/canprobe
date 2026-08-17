"""诊断报告导出（Markdown / JSON）。"""
from __future__ import annotations

_SEV_ORDER = {"critical": 0, "error": 1, "warn": 2, "info": 3}
_SEV_EMOJI = {"critical": "🔴", "error": "🟠", "warn": "🟡", "info": "🔵"}


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
    findings = report.get("findings", [])
    if not findings:
        lines.append("未发现异常（注意：仅在「可用检查」范围内有效）。")
    else:
        for f in sorted(findings, key=lambda x: _SEV_ORDER.get(x["severity"], 9)):
            conf = f"（置信度 {f['confidence']:.0%}，推断）" if f.get("confidence", 1.0) < 1.0 else ""
            lines.append(f"- {_SEV_EMOJI.get(f['severity'], '•')} **[{f['severity']}] {f['title']}**{conf}")
            lines.append(f"  - {f['explanation']}")
            if f.get("suggestion"):
                lines.append(f"  - 建议：{f['suggestion']}")
            if f.get("time_start") is not None:
                end = f.get("time_end") or f["time_start"]
                lines.append(f"  - 时间：t={f['time_start']:.3f}~{end:.3f}s")
    return "\n".join(lines) + "\n"
