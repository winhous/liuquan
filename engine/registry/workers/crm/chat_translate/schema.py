"""chat_translate 工序契约位（详设 §11 惯例）：Model 定义见 models.workers，此处 re-export。

loader L3 按 worker.yaml 的 input.model/output.model 名搜索 import；本文件是
工序的契约落点（P3-1 签名注解由 run.py 直接 import models.workers，两侧同名同源）。
"""

from models.workers import ChatTranscriptInput, ChatTranscriptResult

__all__ = ["ChatTranscriptInput", "ChatTranscriptResult"]
