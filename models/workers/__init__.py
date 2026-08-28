"""工序 input/output Registered Model（详设-v0.1 §4.1/§11；任务 T10）。

工序声明（worker.yaml 的 input.model / output.model）引用的 Model 名必须
与本模块导出名严格一致（loader L3 会 import 校验，引用不到 = 拒载）。
全部 pydantic v2；工序输出必过类型校验（R2 输出即类型）。

Model 清单（T10 首批工序）：
- DemoEchoInput / DemoEchoResult：demo_echo 工序（回显桩驱动）
- ChatTranslateInput / ChatTranslateResult：crm_translate 工序（翻译雏形）
- DemoGreetingParams / DemoGreeting：context/demo.yaml 的 provider 参数/返回
- DemoEchoEventPayload：events/demo.yaml 的事件 payload（只登记不消费）
"""

from __future__ import annotations

from pydantic import BaseModel

__all__ = [
    "ChatTranslateInput",
    "ChatTranslateResult",
    "DemoEchoEventPayload",
    "DemoEchoInput",
    "DemoEchoResult",
    "DemoGreeting",
    "DemoGreetingParams",
]


class DemoEchoInput(BaseModel):
    """demo_echo 工序入参：待回显的文本。"""

    text: str


class DemoEchoResult(BaseModel):
    """demo_echo 工序输出：原样回显文本（确定性，ACT = 回显入参）。"""

    text: str


class ChatTranslateInput(BaseModel):
    """crm_translate 工序入参：原文 + 源/目标语言（缺省 en -> zh）。"""

    text: str
    source_lang: str = "en"
    target_lang: str = "zh"


class ChatTranslateResult(BaseModel):
    """crm_translate 工序输出：译文（AI 只在 REASON 相位出现，输出必过类型校验）。"""

    translated: str
    source_lang: str | None = None
    target_lang: str | None = None


class DemoGreetingParams(BaseModel):
    """demo.greeting provider 查询参数（context/demo.yaml 的 params.model）。"""

    name: str


class DemoGreeting(BaseModel):
    """demo.greeting provider 返回数据（context/demo.yaml 的 returns.model）。"""

    message: str


class DemoEchoEventPayload(BaseModel):
    """demo.echo_done 事件 payload（events/demo.yaml 的 payload_model；只登记不消费）。"""

    text: str
    created_at: str | None = None
