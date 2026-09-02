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
- v0.5 批 2（详设-v0.5 §6.1）：
  keyword_research / seo_optimize 两工序的 input/output Model
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

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
    "ReminderChainInput",
    "ReminderContextData",
    "ReminderContextParams",
    "ReminderCustomer",
    "ReminderItem",
    "ReminderResult",
    "ReplyDraftInput",
    "ReplyDraftResult",
    "ScheduleTaskWrite",
    "SnapshotBrief",
    "SnapshotUpdateInput",
    "SuggestedNext",
    "TaskContextData",
    "TaskContextParams",
    "TodoCandidateItem",
    "TodoCandidateResult",
    "TodoGenerateInput",
    "TranslationItem",
    # v0.5 批 2：SEO 工序 Model
    "KeywordResearchInput",
    "KeywordData",
    "SeoOptimizeInput",
    "SeoOptimizationReport",
    "SeoProductText",
    "HealthcheckInput",
    "HealthcheckItem",
    "HealthcheckResult",
    # v0.5 批 4：扒图工序 Model
    "ImageDownloadInput",
    "ImagePack",
    "ImageInspectInput",
    "InspectionItem",
    "InspectionResult",
    "SuggestionInput",
    "SuggestionResult",
    # v0.6 批 4：拆两链 + 链路修通 Model（详设-v0.6 §5）
    "ScrapeChainInput",
    "LinkRecordCreateInput",
    "LinkCreateResult",
    "BatchDownloadInput",
    "ScrapeBatchResult",
    "ScrapeLinkContextParams",
    "ScrapeLinkContextData",
    "LinkQueueParams",
    "LinkQueueData",
    "ScrapeImageContextData",
    # v0.6 批 7：夸克网盘上传 Model（详设-v0.6 §15.2/§15.6）
    "ScrapeUploadInput",
    "UploadResult",
    # v0.5 批 5：CRM 对话图片工序 Model
    "CrmImageChainInput",
    "MessageImageParams",
    "MessageImageData",
    "ImageScanInput",
    "ImageScanResult",
    "ImageDownloadInputCRM",
    "ImageDownloadResult",
    "ImageCaptionInput",
    "ImageCaptionResult",
    "CaptionItem",
    "ImageSaveInput",
    "ImageSaveResult",
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
    """snapshot_update 工序入参：新译文 + customer_id（供 crm_chat_context provider
    params 引用——详设-v0.3 §5.1 技术定：工序 input 必须带业务对象 id）。"""

    translations: list[TranslationItem]
    customer_id: int


class CustomerSnapshotResult(BaseModel):
    """snapshot_update 工序输出 / 客户滚动快照（need_history 只增不减由代码保证）。"""

    current_need: str = ""
    need_history: list[str] = []
    sentiment: str = ""
    todos: list[str] = []
    summary: str = ""


class TodoGenerateInput(BaseModel):
    """todo_generate 工序入参：当前滚动快照 + customer_id（供 crm_chat_context
    provider params 引用——详设-v0.3 §5.1 技术定：工序 input 必须带业务对象 id）。"""

    snapshot: CustomerSnapshotResult
    customer_id: int


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


# ==== v0.4 T4：提醒链 crm_follow_up_reminder 工序 Model（详设-v0.4 §10.1）====


class ReminderChainInput(BaseModel):
    """crm_follow_up_reminder 工序入参 / crm_reminder_chain 链入参（详设 §10.1）。

    trigger_date：调度器注入的 YYYY-MM-DD 格式日期（当天日期）。
    """

    trigger_date: str  # YYYY-MM-DD 格式


class ReminderItem(BaseModel):
    """提醒项（crm_follow_up_reminder 工序输出元素）。

    每超期客户一条：title/detail/days_since/evidence。
    evidence 引用 provider 返回的 customer_id（白名单内，决策 16③）。
    """

    customer_id: int
    title: str
    detail: str
    days_since: int
    evidence: list[EvidenceRef]


class ReminderResult(BaseModel):
    """crm_follow_up_reminder 工序输出（详设 §10.1）。

    reminders 空数组是合法产出（无超期客户）。
    """

    reminders: list[ReminderItem]


# ==== v0.4 T3：提醒链 Context provider Model（详设-v0.4 §10.2）====


class ReminderContextParams(BaseModel):
    """crm_overdue_context provider 查询参数（context/crm.yaml params.model）。

    无业务对象 id（清单类数据，参数为空）。
    """

    pass


class ReminderCustomer(BaseModel):
    """crm_overdue_context 返回：超期客户摘要。"""

    customer_id: int
    nickname: str
    days_since: int
    latest_summary: str


class ReminderContextData(BaseModel):
    """crm_overdue_context provider 返回数据（context/crm.yaml returns.model）。"""

    customers: list[ReminderCustomer]


# ==== v0.4 T2：定时任务写接口请求 Model（详设-v0.4 §8）====


class ScheduleTaskSource(BaseModel):
    """ScheduleTaskWrite 的 source 字段结构（详设 §6/§8）。"""

    chain_id: str = ""
    engine_task_id: str = ""
    worker_id: str = ""
    customer_id: int
    reminder_date: str  # YYYY-MM-DD
    audit_ids: list[str] = []


class ScheduleTaskWrite(BaseModel):
    """POST /api/biz/tm/schedule-tasks 请求体（详设 §8）。

    防重：source.customer_id + source.reminder_date 必填；
    evidence 非空（决策 16①）。
    """

    title: str = Field(min_length=1, max_length=80)
    detail: str = ""
    domain: str = "crm"
    source: ScheduleTaskSource
    role: str = "运营"
    due: str  # YYYY-MM-DD
    evidence: list[EvidenceRef] = Field(min_length=1, max_length=20)


# ==== v0.5 批 2：SEO 工序 Model（详设-v0.5 §6.1）====


class KeywordResearchInput(BaseModel):
    """keyword_research 工序入参 / keyword_research_chain 链入参（详设-v0.5 §6.1）。

    keywords: 关键词列表（最多 8 个，connector 层硬顶）
    page_size: 每页商品数（≤100，默认 10）
    """

    keywords: list[str] = Field(min_length=1, max_length=8)
    page_size: int | None = Field(default=None, ge=1, le=100)


class KeywordData(BaseModel):
    """keyword_research 工序输出（详设-v0.5 §6.1）。

    source: 数据来源（ehunt-api / ehunt-api+ehunt-keyword-cdp）
    keywords: 逐词画像（product_num/avg_price_top/top_competitors/metrics/related_keywords）
    quota: eHunt 配额回显（used_today/remaining_today）
    metrics_note: 降级提示（CDP 不可用时）
    """

    source: str
    keywords: dict[str, Any] = {}
    quota: dict[str, Any] | None = None
    metrics_note: str | None = None


class SeoProductText(BaseModel):
    """SEO 优化输入的商品文本信息（详设-v0.5 §6.1）。"""

    title: str = ""
    tags: list[str] = []
    description: str = ""


class SeoOptimizeInput(BaseModel):
    """seo_optimize 工序入参 / seo_optimize_chain 链入参（详设-v0.5 §6.1；批 3 修订）。

    product_text: 商品当前文本信息（title/tags/description），可空（无商品文案时
    模型只输出关键词策略建议；交互式优化页仍传完整文案）
    image_path: 可选图片路径（vision 补全理解，本批不接 vision）
    target_keywords: 可选目标关键词（来自 keyword_research 输出）
    playbook_key: 可选 playbooks 键（wall_art/digital/jewelry/clothing/home_candle/personalized/general）
    """

    product_text: SeoProductText | None = None
    image_path: str | None = None  # vision 图片理解待批 4/5 接入
    target_keywords: list[str] | None = None
    playbook_key: str | None = None


class SeoOptimizationReport(BaseModel):
    """seo_optimize 工序输出（详设-v0.5 §6.1）。

    三段一上下文 prompt（商品理解→关键词策略→文案生成，照广成 seo-optimize）；
    归一化 = 纯 Python 硬约束（≤20 字符×13 标签、≤140×5 标题、≤13 材质、≤250 alt）。
    note 明示不保证排名、手动粘贴回 ETSY。
    """

    original: SeoProductText  # 原始输入
    titles: list[dict[str, str]] = Field(default_factory=list)  # 3-5 个标题候选（含 angle）
    tags: list[str] = Field(default_factory=list)  # ≤13 标签，每个 ≤20 字符
    listing_description: str = ""  # 描述重写
    materials: list[str] = Field(default_factory=list)  # ≤13 材质
    alt_text: str = ""  # ≤250 字符
    suggested_category: str = ""  # 从 playbook 的 etsy_category_hints 选
    seo_keywords: list[str] = Field(default_factory=list)  # 6-10 个 SEO 关键词
    search_intent: str = ""  # 搜索意图分析
    keyword_data_appendix: dict[str, Any] | None = None  # 上游关键词数据附录
    playbook: str = ""  # 使用的 playbook key
    note: str = "注意：SEO 优化建议不保证排名，需手动粘贴回 ETSY"  # 固定提示


class HealthcheckInput(BaseModel):
    """listing_healthcheck 工序入参 / seo_healthcheck_chain 链入参（详设-v0.5 §6.1）。

    keywords: 体检关键词列表（缺省读设置键 seo.healthcheck_keywords）
    date: 体检日期（可空，默认当天）
    """

    keywords: list[str] = Field(default_factory=list)
    date: str | None = None  # YYYY-MM-DD 格式


class HealthcheckItem(BaseModel):
    """体检单项：单个关键词的变化/稳定状态。"""

    keyword: str
    product_num: int | None = None
    prev_product_num: int | None = None
    delta_pct: float | None = None
    direction: str = ""  # improved / degraded / stable


class HealthcheckResult(BaseModel):
    """listing_healthcheck 工序输出（详设-v0.5 §6.1；批 3 修订）。

    changed: 变化项（product_num 下降超阈值）
    improved: 提升项（product_num 上升超阈值）
    stable: 稳定项
    metrics: 逐词当前指标快照（keyword -> {product_num, avg_price_top, quota}）
    quota: eHunt 配额回显
    note: 降级/说明信息
    """

    changed: list[HealthcheckItem] = Field(default_factory=list)
    improved: list[HealthcheckItem] = Field(default_factory=list)
    stable: list[HealthcheckItem] = Field(default_factory=list)
    metrics: dict[str, Any] = Field(default_factory=dict)
    quota: dict[str, Any] | None = None
    note: str | None = None


# ==== v0.5 批 4：扒图工序 Model（详设-v0.5 §6.1）====


class ImageDownloadInput(BaseModel):
    """image_download 工序入参（详设-v0.5 §6.1）。

    url: 商品/图片链接
    batch_id: 批次 id
    source: 来源（xhs/xianyu/crm，可选，缺省按域名自动识别）
    """

    url: str
    batch_id: str
    source: str | None = None


class ImagePack(BaseModel):
    """image_download 工序输出：图片包（照广成 etsy-image-pack）。

    paths: 本地落盘路径列表
    count: 图片数量
    desc: 商品描述（从元数据提取）
    tags: 标签列表
    author_id: 作者/卖家 ID
    day_dir: 日期目录
    source: 来源（xhs/xianyu/crm）
    url: 原始链接
    note: 降级说明
    """

    paths: list[str] = []
    count: int = 0
    desc: str = ""
    tags: list[str] = []
    author_id: str = ""
    day_dir: str = ""
    source: str = ""
    url: str = ""
    note: str = ""


class ImageInspectInput(BaseModel):
    """image_inspect 工序入参（详设-v0.5 §6.1）。

    image_ids: image_file.id 列表（从写接口落库后取到的 id）
    """

    image_ids: list[int] = Field(min_length=1)


class InspectionItem(BaseModel):
    """图片体检结果单项。"""

    image_id: int
    width: int | None = None
    height: int | None = None
    watermark: bool = False
    note: str = ""


class InspectionResult(BaseModel):
    """image_inspect 工序输出。"""

    images: list[InspectionItem] = []
    note: str = ""


class SuggestionInput(BaseModel):
    """product_suggestion 工序入参（详设-v0.5 §6.1）。

    image_ids: image_file.id 列表
    batch_id: 批次 id（可选）
    target_keywords: 目标关键词（可选）
    """

    image_ids: list[int] = Field(min_length=1)
    batch_id: str | None = None
    target_keywords: str | None = None


class SuggestionResult(BaseModel):
    """product_suggestion 工序输出：选品建议（走 TaskProposal 候选）。"""

    proposals: list[dict] = []
    note: str = ""


# ==== v0.6 批 4：拆两链 + 链路修通 Model（详设-v0.6 §5/§7）====


class ScrapeChainInput(BaseModel):
    """scrape_download_chain 链入参（详设-v0.6 §5.1：贴链接立即扒 / 定时扒共用）。

    - 立即扒：urls 填满，from_queue=false
    - 定时扒：urls 空，from_queue=true（urls 从定时队列读，§5.4 调度器 input 模板）
    - upload_netdisk（批 7，详设 §15.2）：扒完后是否同步上传夸克网盘——web 扒图页
      「同步上传网盘」复选框传参；定时 input 模板 upload_netdisk: True（未来扒的
      都要传，用户拍板）；缺省 False（本批不读设置键，`netdisk.upload_default`
      设置键归批 8 A74）；链完成消费者 scrape.download_done 读 task.input 判断
    """

    urls: list[str] = []
    batch_id: str
    source: str | None = None
    from_queue: bool = False
    upload_netdisk: bool = False


class LinkRecordCreateInput(BaseModel):
    """link_record_create 工序入参（详设-v0.6 §5.2）。

    from_queue=true 且 urls 空：经 provider scrape.link_queue 读定时队列再建记录。
    """

    urls: list[str] = []
    batch_id: str
    from_queue: bool = False


class LinkCreateResult(BaseModel):
    """link_record_create 工序输出（详设-v0.6 §5.2）。

    link_ids 供下一步 batch_image_download；created_count/skipped_count 区分
    新建与幂等命中（normalized_url 幂等：已存在返回现有行不重复建）。
    队列为空 → link_ids 空（链快速完成）。
    """

    link_ids: list[int] = []
    urls: list[str] = []
    from_queue: bool = False
    batch_id: str = ""
    created_count: int = 0
    skipped_count: int = 0


class BatchDownloadInput(BaseModel):
    """batch_image_download 工序入参（详设-v0.6 §5.2）。

    from_queue 为透传字段（task.input.from_queue → ScrapeBatchResult.from_queue，
    消费者 scrape.download_done 判断是否清定时队列）；链 input 表达式注入。
    """

    link_ids: list[int] = []
    batch_id: str = ""
    from_queue: bool = False


class ScrapeBatchResult(BaseModel):
    """batch_image_download 工序输出 / 下载链末产物（详设-v0.6 §5.2/§5.3）。

    消费者 scrape.download_done 接收类型与链末输出一致（from_queue=true → 清队列）。
    links: 逐链接处理摘要（含 status/error_note/degraded_note）；
    image_ids: 本次落库图片 id 列表。
    """

    links: list[dict] = []
    image_ids: list[int] = []
    from_queue: bool = False
    batch_id: str = ""
    note: str = ""


class ScrapeUploadInput(BaseModel):
    """scrape_upload_chain 链入参 / link_netdisk_upload 工序入参（详设-v0.6 §15.2 批 7）。

    手动补传（历史数据）：素材库链接行/详情页「上传网盘」按钮 → link_ids 单条 →
    POST /scrape/links/{id}/upload → create_task(scrape_upload_chain, {link_ids})。
    """

    link_ids: list[int] = Field(min_length=1)


class UploadResult(BaseModel):
    """link_netdisk_upload 工序输出（批 7）。

    逐条上传结果摘要（uploaded 带 netdisk_url；failed 带 note 全文——
    已 PATCH link_record.netdisk_status/error_note；skipped 带原因——非 done/
    已上传幂等）。链任务 DONE 即代表已尽力执行，失败详情在 link_record 可见。
    """

    uploaded: list[dict] = []
    failed: list[dict] = []
    skipped: list[dict] = []
    note: str = ""


# ---- v0.6 批 4：扒图 Context provider Model（详设-v0.6 §7）----


class ScrapeLinkContextParams(BaseModel):
    """scrape.link_context provider 查询参数（按链接记录 id 白名单查询）。"""

    link_ids: list[int] = []


class ScrapeLinkContextData(BaseModel):
    """scrape.link_context provider 返回（链接记录白名单；白名单 = links[].id）。"""

    links: list[dict] = []


class LinkQueueParams(BaseModel):
    """scrape.link_queue provider 查询参数（定时队列为清单类数据，参数为空）。"""

    pass


class LinkQueueData(BaseModel):
    """scrape.link_queue provider 返回：定时队列链接列表。"""

    urls: list[str] = []


class ScrapeImageContextData(BaseModel):
    """scrape.image_context provider 返回（图片元数据白名单；白名单 = images[].id）。

    images 元素字段：id/batch_id/source/url/link_record_id/source_mark/local_path/
    day_dir/desc/tags/author_id/width/height/watermark/status/created_at。
    """

    images: list[dict] = []


# ==== v0.5 批 5：CRM 对话图片工序 Model（详设-v0.5 §6.1）====


class CrmImageChainInput(BaseModel):
    """crm_image_chain 链入参（详设-v0.5 §6.2）。

    message_image_ids: crm.message_image.id 列表
    """

    message_image_ids: list[int] = Field(min_length=1)


class MessageImageParams(BaseModel):
    """crm.message_images provider 入参。"""

    message_id: int | None = None


class MessageImageData(BaseModel):
    """crm.message_images provider 返回。"""

    images: list[dict] = []


class ImageScanInput(BaseModel):
    """crm_image_scan 工序入参（详设-v0.5 §6.1）。

    message_id: 对话消息 id
    """

    message_id: int


class ImageScanResult(BaseModel):
    """crm_image_scan 工序输出：提取的图片链接列表。"""

    urls: list[str] = []
    note: str = ""


class ImageDownloadInputCRM(BaseModel):
    """crm_image_download 工序入参（详设-v0.5 §6.2）。

    message_image_ids: crm.message_image.id 列表（pending 状态）
    """

    message_image_ids: list[int] = Field(min_length=1)


class ImageDownloadResult(BaseModel):
    """crm_image_download 工序输出：下载结果。"""

    downloaded: list[dict] = []  # [{message_image_id, local_path, ok, note}]
    note: str = ""


class ImageCaptionInput(BaseModel):
    """crm_image_caption 工序入参（详设-v0.5 §6.2）。

    local_paths: 本地图片路径列表
    message_image_ids: 对应的 message_image.id 列表
    """

    local_paths: list[str] = Field(min_length=1)
    message_image_ids: list[int] = Field(min_length=1)


class CaptionItem(BaseModel):
    """识图结果单项。"""

    message_image_id: int
    text: str = ""
    note: str = ""


class ImageCaptionResult(BaseModel):
    """crm_image_caption 工序输出：识图结果列表。"""

    captions: list[CaptionItem] = []
    note: str = ""


class ImageSaveInput(BaseModel):
    """crm_image_save 工序入参（详设-v0.5 §6.2）。

    captions: 识图结果列表
    """

    captions: list[CaptionItem] = Field(min_length=1)


class ImageSaveResult(BaseModel):
    """crm_image_save 工序输出：更新结果。"""

    updated: list[dict] = []  # [{message_image_id, ok, note}]
    note: str = ""


# 重建所有使用 Any 类型的 Model（from __future__ import annotations 导致延迟求值）
KeywordData.model_rebuild()
SeoOptimizationReport.model_rebuild()
HealthcheckResult.model_rebuild()
