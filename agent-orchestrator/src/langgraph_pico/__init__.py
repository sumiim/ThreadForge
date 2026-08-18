"""ThreadForge 原生编排（§7.7.1 阶段 2 + §7.8.9）。

§7.8.9 阶段 4 收尾：LangGraph 兼容层（run_agent / graph.py）已彻底删除，
生产只走 run_native（AgentLoop 单循环 + 证据收敛 + review subagent）。
"""

from .native_runner import run_native
from .review_gate import run_review_gate

__all__ = [
    "run_native",
    "run_review_gate",
]
