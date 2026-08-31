"""keyword_research 工序 schema：re-export input/output Model。

照广成 keyword-research/run.py 逻辑：纯编排无 LLM，两跳聚合。
"""

from models.workers import KeywordData, KeywordResearchInput

__all__ = ["KeywordResearchInput", "KeywordData"]
