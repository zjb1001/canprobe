"""CAN 通信层诊断引擎。

在信号/功能分析之上，诊断 CAN 总线本身的通信故障：
负载、周期报文缺失、节点离线、总线静默（Tier 1，日志可证）；
错误帧 / 错误状态机（Tier 2，依赖格式能力，后续）；物理层推断（Tier 3，后续）。
"""
from .engine import run_diagnostics
from .export import to_markdown
from .model import Capabilities, DiagConfig, Finding, NodeSpec

__all__ = ["run_diagnostics", "to_markdown", "Capabilities", "DiagConfig", "Finding", "NodeSpec"]
