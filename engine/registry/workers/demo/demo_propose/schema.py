"""demo_propose 工序契约位（详设 §11；v0.2 T4）：Model 定义见 models.workers /
models.contract，此处 re-export。

loader L3 按 worker.yaml 的 input.model/output.model 名在 models.workers /
models.contract / models 搜索 import；本文件是工序的契约落点（P3-1 签名注解
由 run.py 直接 import models.workers / models.contract，两侧同名同源）。
"""

from models.contract.task import TaskProposal
from models.workers import DemoProposeInput

__all__ = ["DemoProposeInput", "TaskProposal"]
