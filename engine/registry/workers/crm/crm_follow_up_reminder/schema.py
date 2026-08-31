"""crm_follow_up_reminder 工序契约位（v0.4 详设-定时闭环-§X.Y）：Model 定义见
models.workers，此处 re-export。

loader L3 按 worker.yaml 的 input.model/output.model 名在 models.workers /
models.contract / models 搜索 import；本文件是工序的契约落点。
"""

from models.workers import ReminderChainInput, ReminderResult

__all__ = ["ReminderChainInput", "ReminderResult"]
