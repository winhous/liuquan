"""工序/链/Context provider/事件四种 YAML 声明的 Pydantic schema（详设-v0.1 §4、§6.1；T4b）。

ActionDeclaration 不复刻：直接复用 ``models/contract/action.py`` 的冻结契约类
（四契约 v0.1 定稿即冻结，T4b 任务规定）。

字段级依据（详设原文）：
- §4.1 工序声明：id（^[a-z][a-z0-9_]*$）/ domain（域枚举，同 policy.Domain）/
  version（正整数，输出 Model 结构变化必须 +1）/ risk（read/suggest/write，
  transaction 一律拒载，loader L7）/ input|output（model 名）/ model（models.yaml
  别名，内联模型串拒载，loader L4）/ retry（max_attempts、timeout_s，缺省取
  §14 技术定值 2 次/30s）/ context（id + params dict）/ prompt（相对路径）/
  config_dir（可为空）
- §4.2 链声明：id / domain（链域 = 工序域，loader L5）/ input（model 名）/
  steps（worker + input：省略 input 或 ``task.input`` = 整链入参直传）
- §4.3 Context provider：id / domain / params.model / returns.model / provider 标识
- §6.1 事件登记位：event_type / domain / payload_model / dedup_window_min /
  trigger_chain（v0.1 只登记不消费）

技术决策（记录，供变更日志）：
- worker/chain/provider 的 id 一律 snake_case（^[a-z][a-z0-9_]*$，L1 规则原文）；
  详设 §4 示例里的连字符/点分 id（chat-translate、crm.customer_history）与规则
  字面冲突，按规则字面执行，示例仅为示意（demo 声明 T10 用 snake_case 命名）。
  event_type 例外：§6.1 示例为点分（crm.message_received），允许点分。
- domain 字段直接用 policy.Domain 枚举（L2 域校验与 policy 同源）；
  risk 用 Literal 三值（transaction 进不了 schema，pydantic 报错由 loader 归 L7）。
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from engine.core.policy import Domain

__all__ = [
    "ChainDeclaration",
    "ChainStep",
    "ContextProviderDeclaration",
    "EventDeclaration",
    "ModelRef",
    "RetrySpec",
    "WorkerContextRef",
    "WorkerDeclaration",
]

# L1 规则原文：id 全局唯一、snake_case（^[a-z][a-z0-9_]*$）
_ID_PATTERN = r"^[a-z][a-z0-9_]*$"
# §6.1 事件 id 示例为点分形态（crm.message_received）
_EVENT_TYPE_PATTERN = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$"

# 工序风险三值（transaction 由 loader L7 归口拒载）
RISK_CHOICES = ("read", "suggest", "write")


class ModelRef(BaseModel):
    """Model 引用（input/output/params/returns 的公共形态，§4.1 原文 model 字段）。"""

    model: str


class RetrySpec(BaseModel):
    """工序级重试（§4.1 retry；缺省取 §14 技术定：重试 2 次 / 单次超时 30s）。"""

    max_attempts: int = Field(default=2, ge=0)
    timeout_s: float = Field(default=30.0, gt=0)


class WorkerContextRef(BaseModel):
    """工序 context 列表项（§4.1）：引用的 provider id + 查询参数（值来自 input 字段）。"""

    id: str
    params: dict[str, str] = Field(default_factory=dict)


class WorkerDeclaration(BaseModel):
    """工序声明（详设 §4.1）。"""

    id: str = Field(pattern=_ID_PATTERN)
    domain: Domain
    version: int = Field(ge=1)
    risk: Literal["read", "suggest", "write"]
    description: str
    input: ModelRef
    output: ModelRef
    model: str
    retry: RetrySpec | None = None
    context: list[WorkerContextRef] = Field(default_factory=list)
    prompt: str
    config_dir: str | None = None


class ChainStep(BaseModel):
    """链步骤（§4.2）：worker + input 引用（省略 input 或 ``task.input`` = 整链入参直传）。"""

    worker: str
    input: str | dict[str, str] | None = None


class ChainDeclaration(BaseModel):
    """链声明（详设 §4.2）。"""

    id: str = Field(pattern=_ID_PATTERN)
    domain: Domain
    description: str
    input: ModelRef
    steps: list[ChainStep]


class ContextProviderDeclaration(BaseModel):
    """Context provider 声明（详设 §4.3 列表项，context/<域>.yaml）。"""

    id: str = Field(pattern=_ID_PATTERN)
    domain: Domain
    description: str
    params: ModelRef
    returns: ModelRef
    provider: str


class EventDeclaration(BaseModel):
    """事件登记位（详设 §6.1；v0.1 只登记不消费）。"""

    event_type: str = Field(pattern=_EVENT_TYPE_PATTERN)
    domain: Domain
    payload_model: str
    dedup_window_min: int = Field(ge=0)
    trigger_chain: str | None = None
