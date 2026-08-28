"""Business AI Contract 四契约：Event / Context / Action / Task（详设 §6）。

这是业务模块接引擎的唯一接口（规范 R21：契约是三种允许依赖之一）。
v0.1 定稿即冻结为公共契约：后续字段变化需走变更日志 + 契约 version+1。

模块划分（详设 §11：models/contract/ 四个文件）：
- event.py      §6.1 ContractEvent（业务 -> 引擎：我发生了什么）
- context.py    §6.2 ContextProvider（业务 -> 引擎：我能给你看什么）
- action.py     §6.3 ActionDeclaration / ActionResult（引擎 -> 业务：你可以建议我做什么）
- task.py       §6.4 SourceTrace / EvidenceRef / TaskProposal（引擎 -> TM：帮我派活给人）
- validation.py §6.4 业务校验函数（代码校验，非 AI 自律）
"""

from __future__ import annotations

from .action import ActionDeclaration, ActionResult
from .context import ContextProvider
from .event import ContractEvent
from .task import EvidenceRef, SourceTrace, TaskProposal
from .validation import assert_suggest_has_evidence, validate_audit_ids

__all__ = [
    "ContractEvent",
    "ContextProvider",
    "ActionDeclaration",
    "ActionResult",
    "SourceTrace",
    "EvidenceRef",
    "TaskProposal",
    "assert_suggest_has_evidence",
    "validate_audit_ids",
]
