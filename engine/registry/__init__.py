"""工序/链/事件/Action/Context 注册表（详设 §4、§6.1；T4b）。

schema：五种 YAML 的 Pydantic schema（ActionDeclaration 复用 models/contract/
action.py 的冻结契约类）。
loader：全册加载 + L1-L9 校验 + 拒载；L9 Model 结构 hash 与工序 version 联动。

公共 API：
- ``load_registry(repo_root, *, models_yaml_path=None) -> Registry``：全册加载；
  不合规抛 ``RegistryLoadError``（聚合全部违规，每条一行「[L#] 文件 详情」）
- ``validate(repo_root) -> list[str]``：不抛，返回违规行列表（registry-check 打印用）
- ``write_hashes(repo_root)``：重写 model_hashes.yaml（registry-check --write-hashes）
- ``model_structure_hash(model_cls)``：Model 字段名+类型结构摘要（L9 hash）
"""

from __future__ import annotations

from .loader import (
    Registry,
    RegistryLoadError,
    load_registry,
    model_structure_hash,
    validate,
    write_hashes,
)
from .schema import (
    ChainDeclaration,
    ChainStep,
    ContextProviderDeclaration,
    EventDeclaration,
    ModelRef,
    RetrySpec,
    WorkerContextRef,
    WorkerDeclaration,
)

__all__ = [
    "ChainDeclaration",
    "ChainStep",
    "ContextProviderDeclaration",
    "EventDeclaration",
    "ModelRef",
    "Registry",
    "RegistryLoadError",
    "RetrySpec",
    "WorkerContextRef",
    "WorkerDeclaration",
    "load_registry",
    "model_structure_hash",
    "validate",
    "write_hashes",
]
