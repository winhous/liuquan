"""todo_generate 工序契约位：Model 定义见 models.workers，此处 re-export（loader L3 搜索源）。"""

from models.workers import TodoCandidateResult, TodoGenerateInput

__all__ = ["TodoGenerateInput", "TodoCandidateResult"]
