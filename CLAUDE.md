# 刘全 v2（liuquan）— Etsy 综合智能运营系统

AI 大管家系统：盯着店铺的生意事实（订单/库存/客户/流量），主动判断该做什么，变成任务派给团队，人做完结果回流，AI 判断下一步，循环往复。

**当前阶段：设计期，尚无代码。** 路线：需求分析 ✅ → 概设 ✅ → 详设（当前）→ 页面原型 → 开发 → 测试。

## 项目文档

- `docs/superpowers/specs/2026-08-26-刘全v2综合智能运营系统-概要设计.md` — 概设 v0.3（当前基线，已业务拍板）
- `综合智能运营系统_项目概设_v0.2.md` — 早期与 GPT 讨论的草稿，仅存档，以 v0.3 为准

## 一句话架构

```text
L1 业务应用层（TM 任务中心 / CRM / ERP 只读增强 / SEO / 扒图）
    ↓  统一契约：Event / Context / Action / Task（Business AI Contract）
L2 AI 引擎层（liuquan-engine，新建）
    ↓  PydanticAI
L3 模型层（DeepSeek，字符串可切）
    ↑
L0 数据层（PG16 单实例双 database：业务库 + 引擎库隔离；外部只读 NocoBase v_* 视图）
```

## 最高工程原则：AI 100% 可控

- AI 只在 REASON 相位（及 ACT 的内容生成环节）出现
- AI 输出必过三层校验：PydanticAI 类型校验 → 业务规则代码校验 → 审计抽查
- 状态转换全部代码写死；AI 不能决定下一步、不能直接写业务库、不能越域访问
- 口诀：**AI 可以提出，代码负责验证；AI 可以判断，业务系统负责最终执行**
- 不做自主 Agent，不做自由工作流编排（工序链预定义，组合权收归系统设计者）

## 技术栈（全免费开源，唯一成本是 DeepSeek API）

FastAPI + SQLAlchemy 2.0 · Jinja2 + HTMX（原型阶段可再评估）· PostgreSQL 16 · PydanticAI + DeepSeek · PG 内置队列（SKIP LOCKED）· systemd 常驻。

引擎继承广成（studio）之骨（5 相位状态机 / Policy 门禁 / 检查点 / 暂停恢复 / 审计 / YAML 声明式注册 / 测试四绿守门）+ PydanticAI 之心（output_type 契约 / 校验失败 re-ask / 模型字符串切换）。全新代码库，不 import 广成。

## 生产环境红线（不可违反）

- **三机架构**：AWS（34.228.44.95）是唯一 Etsy 平台出口（防封店）；本机/办公机永不调 Etsy 卖家 API
- **Etsy 数据链路（AWS → 阿里云）一根线不动**
- **刘全一期不做任何对 Etsy 的写操作**（上架/改价走人工 + 任务单）
- **NocoBase 生产 ERP 不动**：只读其 PG 的 25 个 v_* 契约视图；永不删除/移动其数据目录
- 部署在阿里云（112.124.33.142），与 NocoBase 同机，新 PG16 双 database

## 关联资产（外部目录，只读参考）

| 目录 | 角色 |
|---|---|
| `/home/winhous/project/studio` | 广成引擎（机制参考，退出刘全生产链路） |
| `/home/winhous/project/EOMS` | 刘全初版（待退役，CRM 链路已验证 5.8s） |
| `/home/winhous/project/erp` | NocoBase 生产 ERP + Etsy 拉取链路文档（三工作台方案 v3） |

## 开发纪律

- 每个工序：声明 YAML + schema 校验不合规拒载；输出必须是 Pydantic Model
- 发版门槛：单测 + registry 一致性 + lint 全绿（四绿纪律，承自广成）
- 每次 LLM 调用记输入/输出/token/耗时/成本（审计全量落盘）
- 业务决策由用户确认，引擎内部技术细节由技术方确认（用户已授权）

## 详设待办

概设第 8 章：引擎表结构、工序 YAML schema、Business AI Contract 正式规范、TM 数据模型、域权限矩阵、PydanticAI 重试/降级策略、定时链清单（需业务输入）、配额记账、EOMS 数据迁移脚本。
