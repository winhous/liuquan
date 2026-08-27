# AI 可控与反幻觉方法论报告（刘全 × 广成，含实际遵守审计）

> 本报告回答两个问题，并给出可直接执行的清单：
> 1. 刘全（liuquan）与广成（studio）是怎么**强制 AI 遵守规范**、**减少幻觉**的？
> 2. 它们**是否真的遵守了自己的规范**？哪些是机器真强制、哪些只是纸面规矩？
>
> 撰写日期：2026-08-27。全部结论有实测证据（见 §5、§7 证据索引），不是印象流。
> 目标读者：**接手的另一个 AI**——读完 §6「可操作手册」就知道该怎么做。

---

# 1. 两个项目速览

| | 广成（/home/winhous/project/studio） | 刘全（/home/winhous/project/liuquan） |
|---|---|---|
| 定位 | 自研 Agent Runtime OS（单人/小团队），28 个工序 | 广成的继任者：Etsy 综合智能运营系统（AI 大管家） |
| 当前状态 | 生产链路已退出，机制参考（444 单测全绿） | v0.1 引擎地基开发中（T1 骨架+四绿总门已落地） |
| 与对方关系 | 刘全"取广成之骨"的机制来源 | "取骨（机制层）+ PydanticAI 之心（调用层）"，全新代码库，不 import 广成 |
| 强制文档 | `docs/core/广成-开发规范.md`（19 条原则） | `docs/开发规范.md`（R1-R25）+ `docs/开发流程.md`（三层防线六项设置） |

---

# 2. 方法论总框架：三层防线

两个项目共用一个强制哲学（刘全的 `开发流程.md` 把广成经验显式化为三层）：

```text
第 1 层  让 AI 知道规范    （会话入口卡 / 启动读文档）            -- 引导，最弱
第 2 层  让 AI 难以违规    （详设先行 / 逐任务执行 / 变更日志卡点）  -- 流程
第 3 层  让违规过不去      （机器检查 / git hook / 验收断言）       -- 强制，唯一真正的强制
```

> **核心信条：AI 可以提出，代码负责验证；AI 可以判断，系统负责最终执行。**
> 第 3 层是唯一与 AI 自觉无关的强制——脚本不绿，提交都过不去。

---

# 3. 广成方法论（机制层——刘全继承的"骨"）

## 3.1 运行时强制（让引擎里的 AI 违规不了）

| 机制 | 实现 | 代码证据 |
|---|---|---|
| 状态机驱动 | 5 相位 `NEW_TASK→REASON→ACT→OBSERVE→VERIFY→DONE`，转换表写死，AI 无权决定下一步 | `runtime/state_machine.py`（41 行，零 LLM 调用） |
| 相位门禁 Policy | REASON 只读、ACT 门禁、VERIFY 只读；shell/pip 敏感动作拦截；高风险动作 PAUSED_FOR_APPROVAL | `runtime/policy_engine.py`（79 行） |
| 声明式注册表 | `registry/*.yaml` 声明 agents/skills/connectors/models/tools；缺必填字段拒载 | `runtime/skill_contract.py`（REQUIRED_*_FIELDS） |
| 工序契约 | 工序 = `skills/<id>/run.py` 的 `run(inputs, engine) -> dict`；交付物过 `result_contracts` 校验 | `runtime/skill_executor.py` + `runtime/result_contracts.py` |
| 检查点 | 每步 JSON 落盘 `data/checkpoints/`；PAUSED 可恢复、崩溃可续跑 | `runtime/checkpoint_service.py` |
| 事件审计 | 每步 `events.jsonl` 落盘（phase/type/payload） | `runtime/event_log.py` |
| 硬编码治理 | 19 条原则 + lint 机器化（见 3.2） | `runtime/lint_principles.py` |

## 3.2 开发过程强制（让写代码的 AI 违规不了）

- **19 条可判定原则**（命名/业务隔离/桩机制/工序边界/交付物传递/组合优先/唯一跑法/治理审批/文档规范/测试/版本号/硬编码/守门…），每条配**检查项**（grep 能查的判定），不写叙事。
- **四绿守门**：lint 0 违规 + registry consistency 全过 + 全量单测 + `runtime.cli verify`。任一不绿 = 改动未完成。
- **变更日志只增不减**：凡改动工序/工具/规范/架构/依赖，追加一条「因为…所以…」。
- **版本规矩**：版本 = 平台能力里程碑，发版必须 git tag + commit；SKILL 完成 ≠ 版本完成（防止"版本悬空"）。
- **测试 fake/stub**：单测零网络，改完必须跑。

## 3.3 反幻觉手段（广成版——较弱，正是刘全要治的短板）

1. **prompt 内嵌 JSON schema 提示**：工序系统提示词里写死输出结构（如 customer-chat-extract 的 `_JSON_SCHEMA_HINT`："你只输出 JSON"）——**但只是"嘴上说"，无机器强制**。
2. **result_contracts 弱校验**：只查 `skill_id / status / approval_required / payload 存在性`，不查字段级类型。
3. **VERIFY 相位代码断言**：业务断言代码写死（如"译文非空、条数对得上"）。
4. **失败显式化**：工序失败返回 `{status, reason, checks}`，不吞错误。

**广成反幻觉的代码实证弱点**（刘全概设 v0.3 逐条对过代码，属实）：

| 弱点 | 证据 |
|---|---|
| JSON 解析失败直接 return None，工序判 failed，无自动重问 | `runtime/llm_client.py::chat_json` → `json.JSONDecodeError` 时 `return None` |
| 60+ 行手写 `_normalize_*` 容错函数（结构不稳的补偿） | `skills/customer-chat-extract/run.py` 的 `_normalize_translations/_normalize_snapshot/_normalize_follow_ups` 等 |
| 模型切换靠 5 层环境变量 fallback + key 格式嗅探补丁 | `llm_client.py` 构造器（JOJOCODE/GUANGCHENG/MIMO/OPENROUTER/OPENAI 逐层降级 + `_looks_like_openrouter_key`） |
| 用量观测只有 `last_error` 一个字符串，无 token/耗时审计 | `llm_client.py` |
| 无调度、无常驻（拉模式，需本地 CLI 触发） | EOMS 用 subprocess 调 `runtime.cli run`，同步阻塞最长 300s |

---

# 4. 刘全方法论（升级版——"骨 + PydanticAI 之心"）

## 4.1 运行时强制（R1-R25 落地要点）

| 规矩 | 内容 | 强制方式 |
|---|---|---|
| **R1 AI 只在指定相位** | AI 仅限 REASON（及 ACT 内容生成）；状态转换/下一步/写库/越域全代码写死 | 状态机转换表写死（详设 §2.2） |
| **R2 输出即类型** | 工序输出必须 Pydantic Model（PydanticAI `output_type`）；校验失败自动 re-ask（上限 2）；禁手写 `_normalize_*` | PydanticAI 框架级强制 |
| **R3 三层校验缺一不可** | 类型校验 → 业务规则代码校验（条数/引用/数值）→ 审计落盘 | 引擎代码路径写死 |
| **R4 唯一跑法** | 一切 LLM 调用只走 `engine/core/llm/`；web 层禁 import pydantic_ai / api_key / 模型串 | lint P2/P3 联合执法 |
| **R5 AI 不碰账本** | AI 只产建议/分析结果；**全部提案人工审**（2026-08-27 拍板，无代码自动通过） | 提案审核流 + R9 |
| **R6-R11 工序纪律** | run(inputs, engine) 契约 / 声明式注册拒载 / 域隔离 / 风险分级 / 硬编码四层防线 / 模型引用化 | loader L1-L9 + lint P1 |
| **R21-R24 低耦合三通道** | 部件间只允许：契约（Model）/ 声明（YAML）/ 配置。禁 import 实现、禁跨 schema 外键、禁裸 dict | lint P3 规则 2 + Alembic 审查 + registry 一致性 |
| **R12 桩注入式测试** | 五件套桩（标准/捕获/失效/坏输出/空输出），构造注入，零网络；桩只住 tests/ | lint P3 规则 4（桩泄漏拒载） |

**loader 拒载规则（L1-L9，引擎不起）**：id 不唯一、域越界、Model 不存在、模型串内联、链引用未注册工序、引用后序步、`risk: transaction`、声明文件缺失、Model 结构变更未 bump version——任一红，引擎启动失败。**结构漂移活不过启动。**

**lint 三规则（P1/P2/P3）**：
- **P1 业务词黑名单**：run.py 命中业务规格值即违规；**词表从注册表 YAML + config/ 自动生成**（治广成"黑名单手动维护、覆盖面窄"的坑）。
- **P2 凭据端点零容忍**：URL/IP/`sk-`/api_key/绝对路径/具体模型 id——红线级，唯一合法来源是 models.yaml 的 `env:` 前缀。
- **P3 契约纪律**：工序签名不符、裸 dict 返回、跨 worker import、`web/scripts` import `engine.core/workers`、桩泄漏、models.yaml 出现真值密钥。

## 4.2 开发过程强制（`开发流程.md` 三层防线六项设置）

1. **① 入口卡**：CLAUDE.md 铁律 7 条内联（不是指针）——四绿/变更日志/红线/详设先行，每次会话自动进上下文。
2. **② 详设先行**：每个版本开工前详设文档 → 业务/技术双确认 → 才写代码；发现详设有误先改详设再改代码。
3. **③ 逐任务执行卡**：版本拆小任务，每任务 TDD → 四绿 → 变更日志 → 单独 commit；禁批量提交、禁"先写完再补测试"。
4. **④ 四绿 hook**：`scripts/check.sh`（lint + registry + 单测 + verify）挂 **git pre-commit hook**，不绿不让提交；`--no-verify` 豁免必须留痕。
5. **⑤ 验收断言**：版本验收标准写成可执行测试（`@version_acceptance` 标记）；**AI 无法"自称完成"**——跑 `pytest -m version_acceptance` 见真章。
6. **⑥ 发版前审计**：每版 git tag 前跑合规审计（文档↔代码一致性、变更日志完整性、红线抽查），红项先修再发版。

## 4.3 反幻觉手段（刘全版——针对广成短板逐条补强）

| # | 手段 | 机制 | 治什么 |
|---|---|---|---|
| 1 | **输出即类型 + re-ask** | PydanticAI `output_type` 校验不过自动带错误重问（2 次），耗尽 FAILED | 治广成"解析失败 return None 直接 failed" |
| 2 | **Context 事实供给** | Context 契约"我能给你看什么"：AI 判断必须基于声明域的业务数据，不靠记忆 | 治"无事实瞎编" |
| 3 | **无依据不出建议** | TaskProposal 必带 `evidence`（EvidenceRef：消息/指标/订单视图 + quote 原文摘录）；`risk: suggest` 工序 evidence 为空 = 代码拒绝产出提案 | 治"凭空给建议" |
| 4 | **可回放证据链** | 提案带 `SourceTrace`（chain/task/worker + audit_ids），`audit_ids` 必须在审计表查得到 | 治"结论不可追责" |
| 5 | **业务规则代码校验** | 条数对得上原文、引用存在、数值合理 | 治"看起来对其实错" |
| 6 | **VERIFY 断言** | 断言不过 = FAILED；空输出/半成品被拦——**宁失败不假成功** | 治"假成功" |
| 7 | **无静默降级** | DeepSeek 故障 = 显式 FAILED + 原因，不产假结果 | 治"降级成编造" |
| 8 | **全部提案人工审** | 决策 12：无代码自动通过路径 | 治"机器漏网" |
| 9 | **域隔离** | AI 只看声明域数据，不暴露无关信息 | 治"自由发挥" |
| 10 | **审计永久保留 + 先审计后调用** | 每次 LLM 调用记 input/output 全文/token/耗时/reask；审计不可写则调用不允许发生 | 治"不可追溯" |

---

# 5. 实际遵守审计（2026-08-27 实测证据）

> 方法：直接跑两个项目的守门脚本 + 针对性 grep。不是看文档自我描述。

## 5.1 广成实测：机制在位、跑得绿，但覆盖面窄 + 可跳过

| 检查项 | 实测结果 | 证据 |
|---|---|---|
| 全量单测 | ✅ **444 个全绿**（24.5s） | `unittest discover -s tests` → "Ran 444 tests ... OK" |
| registry consistency | ✅ **113 项全过** | `RegistryConsistencyChecker.run()` → status=passed, 113/113 |
| lint（开发规范机器化） | ⚠️ **0 finding，但覆盖面极窄** | 词表仅 5 业务词 + 8 默认值词；`skills/*/run.py` 里 **98 处** etsy/feishu/客户/店铺 等业务词全部放行 |
| verify 单组件 | ✅ 34 项全过（跳过 tests/probe 时） | `cli verify --skip-tests --skip-runtime-probe` → passed 34 |
| **规范 vs 实现矛盾** | ❌ **verify 带 `--skip-*` 旗标可跳过一切** | `runtime/cli.py` 有 `--skip-lint/--skip-tests/--skip-registry-consistency/--skip-result-contracts/--skip-traceability/--skip-runtime-probe`，而广成开发规范原则 18 明文"校验器不得绕过：常规流程不允许 skip 旗标" |
| lint 覆盖面 | ❌ 19 条原则只机器化 **5 条**（1/2/3/5/13）；原则 7 明说"难以硬判，当前跳过" | `lint_principles.py` 文件头注释 |
| 词表维护 | ❌ 手动维护 5 词 | `lint_principles.py` 的 BUSINESS_WORD_BLACKLIST 常量 |
| 反幻觉能力 | ❌ 弱：无 re-ask、手写 normalize、无 token 审计 | §3.3 代码证据 |

**结论：广成是"机制健全但半强制"**——状态机/Policy/注册/检查点/审计/测试守门是真的、绿的；但 lint 词表太小形同虚设（98 处违规放行），verify 可一键跳过，与自己的规范打架。

## 5.2 刘全实测：hook 已生效，但核心强制件还在 T2/T4/T7（诚实 skipped）

| 检查项 | 实测结果 | 证据 |
|---|---|---|
| pre-commit hook | ✅ **已配置生效** | `git config core.hooksPath` = `scripts/hooks`；hook 调 check.sh |
| 四绿总门 | ⚠️ 3/4 skipped（lint→T2 / registry→T4 / verify→T7），**仅单测真跑**（4 个骨架测试绿） | `bash scripts/check.sh` 输出 |
| 设计层强制件 | 📋 **完整但大部分未落地**：loader L1-L9、lint P1/P2/P3、状态机、Policy、审计、检查点 | `docs/详设-v0.1-引擎地基.md` §2-§10 已定稿，按 §11 文件清单逐任务推进 |
| 验收断言 | 📋 待 v0.1 落地（`@version_acceptance` 尚未建） | `项目状态.md`「开发强制机制」 |

**结论：刘全是"设计更严、落地进行中"**——强制架构（R1-R25 + 三层防线 + 四绿 hook）已定稿并开始落地，但目前机器真强制的只有"单测 + hook 机制"；**skipped 是诚实的过渡态（规则未建），不是绕过（与广成的 skip 旗标性质不同）**。

## 5.3 一句话总结

> **广成**：能跑绿，但 lint 形同虚设、校验可跳过——规范与实现有出入；
> **刘全**：规矩更严、每一条都配了机器执法设计，但目前大多还是纸面——落地中。
> 两者共同的优点：**守门脚本真的在跑、skipped 状态诚实标注、测试零网络**。
> 两者共同的教训：**机器强制的强度 = 词表/规则的覆盖面 + 是否可跳过**，这两点决定规范是"真强制"还是"装饰"。

---

# 6. 可操作手册（另一个 AI 接手时按这个做）

## 6.1 强制 AI 守规范的 8 条铁律（做什么 / 怎么做 / 验收）

1. **规矩只写"可判定的"**：能检查过/不过，不写架构叙事。→ 每条规矩配一行 grep/脚本判定。验收：规矩表里每行都有检查项。
2. **关键规矩内联到会话入口**：CLAUDE.md 铁律（四绿/变更日志/红线/详设先行）直接写进入口文件，不是指针。验收：新会话只读入口文件就知道红线。
3. **每个可判定规矩配机器检查**：lint/loader/一致性检查，不靠提示词自律。验收：违规样本能被脚本拦下（反向测试）。
4. **机器检查挂 git hook**：不绿不让提交（pre-commit），`--no-verify` 豁免必须留痕。验收：故意放一个违规 commit 被拦。
5. **每条机器检查配反向测试**：防止校验器本身被改坏（刘全 R10 守门层、A6 验收断言）。验收：反向测试在四绿里。
6. **"关系"进声明不进代码**：工序读什么数据/用什么模型/和谁组链 = YAML 声明，改关系 = 改 YAML（刘全 R23）。验收：`grep "from engine.workers" web/` 无结果。
7. **禁"跳过/豁免"旗标**：校验器可跳过 = 校验器不存在（广成实锤教训）。验收：守门脚本无 `--skip-*` 参数；有 skipped 必须注明落地任务号。
8. **验收断言 + 发版审计**：版本验收写成可执行测试（`@version_acceptance`），AI 无法自称完成；发版前跑合规审计。验收：`pytest -m version_acceptance` 全绿才算完成。

## 6.2 减少幻觉的 6 道闸门（按顺序，缺一不可）

1. **结构闸**：输出即类型（structured output + 校验失败 re-ask），格式错就重问，耗尽 FAILED——不留"格式不对也算成功"的口子。
2. **事实闸**：AI 的输入只来自声明过的 Context 数据供给，判断必须基于给定事实，不靠模型记忆。
3. **依据闸**：建议必须带 evidence（业务对象 id + 原文摘录），无依据不出建议——代码校验，不是 AI 自律。
4. **核对闸**：AI 输出落库前过业务规则代码校验（条数/引用/数值），把"看起来对"变"核对过"。
5. **失败闸**：无静默降级——外部不可用 = 显式 FAILED + 原因，宁可失败不给假结果（宁失败不假成功）。
6. **人审闸**：建议类全部人工审核，执行类人工审批——机器漏网的幻觉也过不了人的审核。

## 6.3 从广成教训提炼的 6 个坑（刘全怎么修的）

| 广成的坑 | 实证 | 刘全的修法 |
|---|---|---|
| 黑名单手动维护、覆盖面窄 | 98 处业务词放行 | P1 词表从注册表/config 自动生成 |
| 校验器可跳过 | `verify --skip-*` 与原则 18 矛盾 | 无 skip 旗标；skipped 必须注明落地任务号 |
| 契约弱校验 | result_contracts 只查存在性 | PydanticAI output_type 强类型 + re-ask |
| JSON 失败即 failed、无自纠 | `chat_json` 解析失败 return None | re-ask 2 次自动修正 |
| 手写 normalize 补偿结构不稳 | `_normalize_*` 60+ 行 | 类型定义即校验器，禁手写 normalize |
| 无 LLM 调用级审计 | 只有 last_error 字符串 | 每次调用全量审计 + 先审计后调用 + 永久保留 |

## 6.4 接手刘全 v0.1 开发的实操清单

1. 读文档顺序：`项目状态.md` → 本次任务相关规范/详设 → `开发流程.md` 执行卡。
2. 开发前跑 `bash scripts/check.sh` 确认基线；每次 commit 前必须四绿（hook 会自动跑）。
3. 按 `详设-v0.1-引擎地基.md` §11 文件清单逐任务开发（当前在 T2：`engine/lint` 三规则，把 check.sh 绿 1 从 skipped 转真）；每个任务 TDD + 四绿 + 变更日志 + 单独 commit。
4. 写 AI 工序时对照 R2/R6/R10/R12：输出 Pydantic Model、run(inputs, ctx) 契约、业务值进 config/、测试用桩注入式（五件套）。
5. 完成标准 = 验收断言全绿 + 交叉 review（R25：文档同步 + 详设↔代码）+ 用户复核；禁止"我觉得写完了"。
6. 红线复习：永不写 Etsy、永不写 NocoBase（只读 v_* 视图）、本机不直连 Etsy、密钥只进 `.env`。

---

# 7. 证据索引（本次审计的原始出处）

| 证据 | 位置 |
|---|---|
| 广成状态机 5 相位 | `studio/runtime/state_machine.py` |
| 广成 Policy 相位门禁 | `studio/runtime/policy_engine.py` |
| 广成检查点 JSON | `studio/runtime/checkpoint_service.py` |
| 广成事件审计 | `studio/runtime/event_log.py` |
| 广成工序契约/弱契约校验 | `studio/runtime/skill_executor.py`、`result_contracts.py` |
| 广成 LLM 5 层 fallback / 解析失败 return None | `studio/runtime/llm_client.py` |
| 广成手写 normalize | `studio/skills/customer-chat-extract/run.py` |
| 广成 lint 只覆盖 5 条 + 5 词黑名单 | `studio/runtime/lint_principles.py`（文件头注释 + 常量） |
| 广成 verify 可跳过一切 | `studio/runtime/cli.py`（verify 子命令 `--skip-*` 参数） |
| 广成 444 单测全绿 / 113 registry 全过 | 本次实测（§5.1） |
| 广成 98 处业务词放行 | 本次实测 grep（§5.1） |
| 刘全 R1-R25 / 低耦合 / 桩规范 | `liuquan/docs/开发规范.md` |
| 刘全三层防线六项设置 | `liuquan/docs/开发流程.md` |
| 刘全状态机转换表 / loader L1-L9 / lint 细则 / 四契约 / 审计 | `liuquan/docs/详设-v0.1-引擎地基.md` |
| 刘全四绿 hook 已生效 / 3 项 skipped | 本次实测 `bash scripts/check.sh` + `git config core.hooksPath`（§5.2） |
| 刘全全部提案人工审（决策 12） | `liuquan/docs/项目状态.md` 补充拍板 |
