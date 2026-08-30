"""工序 input/output Registered Model（详设-v0.1 §4.1/§11；任务 T10 + v0.2 T4；v0.3 T2）。

工序声明（worker.yaml 的 input.model / output.model）与 Context 声明
（context/*.yaml 的 params.model / returns.model）引用的 Model 名必须
与本模块导出名严格一致（loader L3 会 import 校验，引用不到 = 拒载）。
全部 pydantic v2；工序输出必过类型校验（R2 输出即类型）。

Model 清单：
- v0.1 T10：DemoEchoInput / DemoEchoResult（demo_echo 回显桩）、
  ChatTranslateInput / ChatTranslateResult（crm_translate 翻译雏形）、
  DemoGreetingParams / DemoGreeting（context/demo.yaml provider）、
  DemoEchoEventPayload（events/demo.yaml 事件 payload，只登记不消费）
- v0.2 T4：DemoProposeInput（demo_propose 工序入参 / tm_demo_chain 链入参）
- v0.3 T2（详设-v0.3 §5.1/§5.2，本任务）：
  chat_translate / snapshot_update / todo_generate / customer_reply_draft /
  tm_intent 五工序的 input/output Model（含 TranslationItem / SuggestedNext，
  EvidenceRef 复用 models.contract.task）；crm_chat_context / tm_task_context
  两 Context provider 的 params/returns Model（ChatContextParams/Data、
  TaskContextParams/Data + Brief 族）
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from models.contract.task import EvidenceRef

__all__ = [
    "ChatContextData",
    "ChatContextParams",
    "ChatTranscriptInput",
    "ChatTranscriptResult",
    "ChatTranslateInput",
    "ChatTranslateResult",
    "CustomerBrief",
    "CustomerSnapshotResult",
    "DemoEchoEventPayload",
    "DemoEchoInput",
    "DemoEchoResult",
    "DemoGreeting",
    "DemoGreetingParams",
    "DemoProposeInput",
    "EventBrief",
    "IntentInput",
    "IntentResult",
    "MessageBrief",
    "ReplyDraftInput",
    "ReplyDraftResult",
    "SnapshotBrief",
    "SnapshotUpdateInput",
    "SuggestedNext",
    "TaskContextData",
    "TaskContextParams",
    "TodoCandidateItem",
    "TodoCandidateResult",
    "TodoGenerateInput",
    "TranslationItem",
]


class DemoEchoInput(BaseModel):
    """demo_echo 工序入参：待回显的文本。"""

    text: str


class DemoEchoResult(BaseModel):
    """demo_echo 工序输出：原样回显文本（确定性，ACT = 回显入参）。"""

    text: str


class DemoProposeInput(BaseModel):
    """demo_propose 工序入参 / tm_demo_chain 链入参（详设-v0.2 §7）：echo 文本 + 证据引用。

    text 与 demo_echo 输出结构对齐（链 steps[0].output.text 可直填）；
    ref_id/kind 构成 evidence 依据——决策 16 数据引用封闭性：提案证据只能引用
    链输入喂给本工序的业务对象 id；无 ref_id = 无依据，run 拒产提案
    （详设 v0.1 §6.4 无依据不出建议）。
    """

    text: str
    ref_id: str
    kind: Literal["message", "metric", "order_view", "listing", "image"]


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


# ==== v0.3 T2：五工序 input/output Model + Context provider Model（详设-v0.3 §5.1/§5.2）====


class ChatTranscriptInput(BaseModel):
    """chat_translate 工序入参 / crm_chat_chain 链入参：粘贴的整段对话原文。

    上下文（recent_messages）走 crm_chat_context provider，不入参。
    """

    customer_id: int
    conversation_text: str


class TranslationItem(BaseModel):
    """对话逐条翻译结果单元（chat_translate 输出元素，决策 23 语义）。"""

    source_text: str
    translated_text: str = ""
    direction: Literal["buyer", "seller"]
    language: str = ""


class ChatTranscriptResult(BaseModel):
    """chat_translate 工序输出：逐条翻译列表（方向/语种随条标注）。"""

    translations: list[TranslationItem]
    source_lang: str | None = None
    target_lang: str | None = None


class SnapshotUpdateInput(BaseModel):
    """snapshot_update 工序入参：新译文（旧快照 + recent_messages 走 provider）。"""

    translations: list[TranslationItem]


class CustomerSnapshotResult(BaseModel):
    """snapshot_update 工序输出 / 客户滚动快照（need_history 只增不减由代码保证）。"""

    current_need: str = ""
    need_history: list[str] = []
    sentiment: str = ""
    todos: list[str] = []
    summary: str = ""


class TodoGenerateInput(BaseModel):
    """todo_generate 工序入参：当前滚动快照（recent_messages/existing_open_todos 走 provider）。"""

    snapshot: CustomerSnapshotResult


class SuggestedNext(BaseModel):
    """AI 对任务生成后的下一步建议（决策 27 闭环：确认建任务时写入 tm.task.ai_suggestion）。

    动作集合与 IntentResult 对齐（transfer/assign/tag/note）；v0.3 可执行 =
    assign/tag/note（transfer 目标域未接入时提示不可执行并记录意图，决策 28）。
    """

    action: Literal["transfer", "assign", "tag", "note"]
    target_domain: str | None = None
    target_role: str | None = None
    tags: list[str] | None = None
    note: str | None = None


class TodoCandidateItem(BaseModel):
    """待办候选（todo_generate 输出元素；落 crm.todo_candidate，禁幻觉三件套校验）。

    evidence 引用链内业务对象 id（白名单 = crm_chat_context provider 返回 id 集合）；
    suggested_next 为任务生成后的下一步建议（决策 27，可为空）。
    """

    content: str
    reason: str = ""
    suggested_tags: list[str] = []
    evidence: list[EvidenceRef] = []
    suggested_next: SuggestedNext | None = None


class TodoCandidateResult(BaseModel):
    """todo_generate 工序输出：待办候选列表（「宁可少列不编造」，空数组合法）。"""

    todos: list[TodoCandidateItem]


class ReplyDraftInput(BaseModel):
    """customer_reply_draft 工序入参 / crm_reply_chain 链入参：回复台三模式。

    customer_id 供 crm_chat_context provider params 引用（详设 v0.1 §4.1
    机制：params 值来自本工序 input 字段；详设-v0.3 §5.1 技术定修订）；
    literal 模式 reply_zh 原样回传不经模型（代码路径）。
    """

    customer_id: int
    mode: Literal["auto", "points", "literal"] = "auto"
    points: str = ""
    full_text: str = ""


class ReplyDraftResult(BaseModel):
    """customer_reply_draft 工序输出：英文回复 + 中文对照（literal 模式中文回传原文）。"""

    reply_en: str
    reply_zh: str = ""


class IntentInput(BaseModel):
    """tm_intent 工序入参 / tm_intent_chain 链入参：自然语言流转指令。

    task_id 供 tm_task_context provider params 引用（详设 v0.1 §4.1 机制；
    详设-v0.3 §5.1 技术定修订：input 必须带业务对象 id）。
    """

    task_id: int
    instruction: str


class IntentResult(BaseModel):
    """tm_intent 工序输出：结构化流转指令（决策 28，模糊澄清不猜测执行）。

    clarity=unclear 时 action 可为任意占位、candidates 给出澄清选项；
    v0.3 可执行动作 = assign/tag/note/block（transfer 目标域未接入 -> 提示不可执行）。
    """

    action: Literal["transfer", "assign", "tag", "note", "block"]
    target_domain: str | None = None
    target_role: str | None = None
    tags: list[str] | None = None
    note: str | None = None
    clarity: Literal["clear", "unclear"]
    candidates: list[str] | None = None


# ==== Context provider Model（详设-v0.3 §5.2：crm_chat_context / tm_task_context）====


class ChatContextParams(BaseModel):
    """crm_chat_context provider 查询参数（context/crm.yaml 的 params.model）。"""

    customer_id: int


class CustomerBrief(BaseModel):
    """crm_chat_context 返回：客户摘要（白名单来源 = customer.id）。"""

    id: int
    nickname: str
    latest_summary: str | None = None


class MessageBrief(BaseModel):
    """crm_chat_context 返回：对话消息摘要（白名单来源 = messages[].id）。"""

    id: int
    source_text: str
    translated_text: str = ""
    direction: str = "buyer"


class SnapshotBrief(BaseModel):
    """crm_chat_context 返回：最新快照摘要（白名单来源 = snapshot.id）。"""

    id: int
    current_need: str = ""
    need_history: list[str] = []
    sentiment: str = ""
    todos: list[str] = []
    summary: str = ""


class ChatContextData(BaseModel):
    """crm_chat_context provider 返回数据（context/crm.yaml 的 returns.model）。

    白名单正式化（决策 16 ③）：{customer.id, *messages[].id, snapshot.id}。
    """

    customer: CustomerBrief
    messages: list[MessageBrief]
    snapshot: SnapshotBrief | None = None
    existing_open_todos: list[str] = []


class TaskContextParams(BaseModel):
    """tm_task_context provider 查询参数（context/tm.yaml 的 params.model）。"""

    task_id: int


class EventBrief(BaseModel):
    """tm_task_context 返回：任务事件摘要（recent_events 元素）。"""

    event_type: str
    note: str | None = None
    created_at: str | None = None


class TaskContextData(BaseModel):
    """tm_task_context provider 返回数据（context/tm.yaml 的 returns.model；白名单 = {task_id}）。"""

    task_id: int
    title: str
    domain: str
    status: str
    tags: list[str] = []
    recent_events: list[EventBrief] = []
