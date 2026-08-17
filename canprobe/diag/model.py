"""诊断数据模型：能力清单、结论 Finding、诊断配置。

「能力清单」(Capabilities) 是诊断可信度的第一道闸：每种日志格式对错误帧 /
错误计数 / 状态的记录能力不同，诊断引擎据此决定哪些检查「可用 / 降级 / 禁用」，
并在结果里显式声明，避免「没查到问题」被误读为「没有问题」。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

TIER1_CHECKS = ("bus_load", "periodicity", "node_offline", "bus_silence")
TIER2_CHECKS = ("error_frame", "error_state_machine")
TIER3_CHECKS = ("baud_mismatch", "termination_wiring")


@dataclass
class Capabilities:
    """当前已加载日志能提供哪些通信层事实。"""

    error_frames: bool = False
    error_counters: bool = False
    bus_status: bool = False
    directions: bool = False
    channels: int = 1

    @classmethod
    def detect(cls, events, channels: int) -> "Capabilities":
        """从解析出的通信层事件探测能力（实测探测，非文档假定）。"""
        cap = cls(channels=channels)
        for e in events:
            if e.kind == "error":
                cap.error_frames = True
            elif e.kind == "status":
                cap.bus_status = True
            if e.tec is not None or e.rec is not None:
                cap.error_counters = True
            if e.direction:
                cap.directions = True
        return cap

    def to_dict(self) -> dict:
        return {
            "error_frames": self.error_frames,
            "error_counters": self.error_counters,
            "bus_status": self.bus_status,
            "directions": self.directions,
            "channels": self.channels,
            "available_checks": self.available_checks(),
            "unavailable_checks": self.unavailable_checks(),
        }

    def available_checks(self) -> list[str]:
        checks = list(TIER1_CHECKS)
        if self.error_frames:
            checks.append("error_frame")
            # Tier 3 推断层需要错误帧签名才能推理，故同 error_frames 一起可用
            checks += ["baud_mismatch", "termination_wiring"]
        if self.error_counters or self.bus_status:
            checks.append("error_state_machine")
        return checks

    def unavailable_checks(self) -> list[dict]:
        out = []
        for c in TIER2_CHECKS + TIER3_CHECKS:
            if c not in self.available_checks():
                out.append({"check": c, "reason": self._reason(c)})
        return out

    @staticmethod
    def _reason(check: str) -> str:
        if check == "error_frame":
            return "日志未记录错误帧（如 .csv/.json 仅含数据帧）"
        if check == "error_state_machine":
            return "日志未记录错误计数 TEC/REC 或总线状态事件"
        return "物理层推断需错误帧签名，且结论为置信度排序的假设，需示波器/波形工具复核"


@dataclass
class Finding:
    """一条诊断结论（统一 Schema）。"""

    check: str
    severity: str  # info | warn | error | critical
    title: str
    explanation: str
    confidence: float = 1.0  # 0~1；Tier 1 确定性结论 = 1.0
    suggestion: str = ""
    time_start: Optional[float] = None
    time_end: Optional[float] = None
    entities: list = field(default_factory=list)  # 节点名 / 报文 id / 总线号
    evidence: list = field(default_factory=list)  # [{"t":..,"id":..}, ...] 可回跳

    def to_dict(self) -> dict:
        return {
            "check": self.check,
            "severity": self.severity,
            "title": self.title,
            "explanation": self.explanation,
            "confidence": self.confidence,
            "suggestion": self.suggestion,
            "time_start": self.time_start,
            "time_end": self.time_end,
            "entities": self.entities,
            "evidence": self.evidence,
        }


@dataclass
class NodeSpec:
    """一个 ECU 节点的期望行为：它发哪些报文、最快周期多少。"""

    name: str
    msg_ids: list[int] = field(default_factory=list)
    period_ms: float = 100.0  # 该节点最快周期（心跳近似），用于离线超时
    timeout_factor: float = 3.0  # 离线判定 = period_ms * timeout_factor 无任何报文

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "msg_ids": self.msg_ids,
            "period_ms": self.period_ms,
            "timeout_factor": self.timeout_factor,
        }


@dataclass
class DiagConfig:
    """诊断配置。默认值可被 DBC（GenMsgCycleTime / BU_ 发送节点）自动播种后覆盖。"""

    baud: int = 500_000
    load_warn_pct: float = 50.0
    load_crit_pct: float = 70.0
    load_window_ms: float = 100.0
    silence_ms: float = 500.0  # 整条总线无任何报文这么久 -> 总线静默
    period_tolerance: float = 0.25  # 周期容差 ±25%
    error_burst_window_ms: float = 100.0  # 错误帧风暴检测窗口
    error_burst_threshold: int = 10  # 窗口内错误帧达到此数 -> 风暴
    periodic_msgs: dict[int, float] = field(default_factory=dict)  # id -> 周期 ms
    nodes: list[NodeSpec] = field(default_factory=list)

    @classmethod
    def from_dbc(cls, dbc) -> "DiagConfig":
        """从 DBC 元数据播种：周期表来自 GenMsgCycleTime，节点来自 BU_ 发送者。"""
        cfg = cls()
        if dbc is None:
            return cfg
        for mid, msg in dbc.messages.items():
            if msg.cycle_time:
                cfg.periodic_msgs[int(mid)] = float(msg.cycle_time)
            for sender in msg.senders:
                node = next((n for n in cfg.nodes if n.name == sender), None)
                if node is None:
                    node = NodeSpec(name=sender)
                    cfg.nodes.append(node)
                node.msg_ids.append(int(mid))
        # 每个节点周期 = 其报文中最小周期（最快心跳）
        for node in cfg.nodes:
            periods = [cfg.periodic_msgs[m] for m in node.msg_ids if m in cfg.periodic_msgs]
            if periods:
                node.period_ms = min(periods)
        return cfg

    def to_dict(self) -> dict:
        return {
            "baud": self.baud,
            "load_warn_pct": self.load_warn_pct,
            "load_crit_pct": self.load_crit_pct,
            "silence_ms": self.silence_ms,
            "period_tolerance": self.period_tolerance,
            "periodic_msgs": self.periodic_msgs,
            "nodes": [n.to_dict() for n in self.nodes],
        }
