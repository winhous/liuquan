# 刘全 v2（liuquan）— Etsy 综合智能运营系统

AI 大管家系统：盯着店铺的生意事实（订单/库存/客户/流量），主动判断该做什么，变成任务派给团队，人做完结果回流，AI 判断下一步，循环往复。

**当前阶段：设计期，尚无代码。** 路线：需求分析 ✅ → 概设 ✅ → 详设（当前）→ 页面原型 → 开发 → 测试。

## 项目文档（docs/，会话启动先读）

- `docs/项目状态.md` - 当前最新状态（与项目实际完全一致，先读这个）
- `docs/概要设计-v0.3.md` - 概设 v0.3（业务拍板基线）
- `docs/版本规划.md` - 版本里程碑 v0.1~v1.0（版本 = 能力里程碑）
- `docs/建设方案.md` - 各版本详细实施（做什么/怎么做/完成形态/验收）
- `docs/开发规范.md` - 20 条可判定规矩（R1-R20）+ 验收检查表
- `docs/变更日志.md` - 改动流水账（只增不减）
- `docs/概设-v0.2-存档.md` - 早期草稿，仅存档

规矩：改架构先改文档再动代码；凡改动工序/规范/架构/依赖，必在变更日志追加一条「因为…所以…」。

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

详见 `docs/开发规范.md`（R1-R20）与 `docs/版本规划.md`。要点：四绿守门（lint/一致性/单测/verify）；改动必记变更日志；业务决策用户确认、引擎技术细节技术方确认（已授权）。
