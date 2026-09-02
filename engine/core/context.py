"""EngineContext：工序 run 函数签名（P3-1）的 ctx 类型（详设-v0.1 §4.1、§11；任务 T12a）。

工序契约（规范 R6 / lint P3-1）：``run(inputs: RegisteredModel, ctx: EngineContext) -> RegisteredModel``。
EngineContext 承载「规格外置」与「低耦合」（R10 架构层防线 / R21 契约依赖）：
工序只碰 inputs / config / context_data / engine 原语，不碰数据库连接、
不 import LLM SDK（R4）、不为缺失上游写死假数据（R6）。

字段语义（任务 T12a 技术定，供变更日志）：
- worker_id：工序声明 id（engine/registry/workers/<域>/<工序>/worker.yaml 的 id）
- domain：工序域（字符串形态，policy.Domain.value）
- inputs：已过本工序 input Model 校验的工序入参（Pydantic Model，R2 输出即类型同源）
- config：工序 config/ 目录全部 yaml 合并内容（规格外置：业务规格值只许住这里，
  不许字面量进 run.py——lint P1 词表自动生成的来源）
- context_data：声明的 Context provider 拉取结果 {provider_id: BaseModel}；
  无 provider / provider 实现未注入时为 {}（v0.1 demo 语义）
- engine：AsyncEngine（ACT 写库通道；v0.1 demo 工序不写 = None，引擎库写入
  由 runner 统一经 DAO 做，工序自身不碰连接）
- llm_output：REASON 相位产出的 LLM 结构化结果（已过本工序 output Model 校验）；
  ACT 执行副作用的依据（§2.1：REASON 产出 -> ACT 执行）。v0.1 集成修复：
  T12a 初版未传（demo-echo 确定性重算可掩盖），翻译雏形必须拿到才能产出——
  经 ctx 传给 run()，避免工序二次调 LLM（R4）。测试固化「llm_output 达 run」。
- task_id / step_id：当前任务与工序实例 id（审计/定位用；None = 未关联）
- chain_id（v0.2 T4 增）：当前链 id（TaskProposal.source.chain_id 追溯用，
  详设-v0.2 §3.4；runner ACT 装配时传入；None = 未关联）
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

__all__ = ["EngineContext"]


@dataclass(frozen=True)
class EngineContext:
    """工序运行上下文（构造注入不可变；runner 在 ACT 相位装配后传给 run）。"""

    worker_id: str
    domain: str
    inputs: BaseModel
    config: dict[str, Any]
    context_data: dict[str, Any]
    engine: Any | None = None
    llm_output: Any = None  # REASON 的 LLM 结构化结果（ACT 副作用依据；None = 未过 REASON）
    task_id: int | None = None
    step_id: int | None = None
    chain_id: str | None = None  # 当前链 id（TaskProposal.source 追溯；None = 未关联）
    connectors: dict[str, Any] | None = None  # v0.5 §5：外部资源连接器（工序按 id 引用）
    # v0.6 批 4（详设-v0.6 §5.3 T4 方案 A）：业务写接口客户端——代码工序的中间产物
    # 读写统一经写接口客户端（决策 26：引擎零业务库连接串，只经接口写；代码工序的
    # 中间产物读非 AI 可见数据，白名单封闭机制（provider）留给 AI 可见数据）。
    # CLI/测试不注入（None，工序内降级 note，照现有 hasattr/缺 key 模式）。
    biz_client: Any | None = None
    # v0.6 批 6（详设-v0.6 §15.1 技术定）：扒图存储根目录（scrape.storage_dir，
    # 引擎启动经 engine-params 读取后装配注入，与 connector 落盘根同源）——
    # batch_image_download 归集（一链接一文件夹 + meta.txt）用它建链接文件夹；
    # image_inspect 用它把相对 local_path 解析为磁盘绝对路径。测试注入 tmp 目录。
    storage_dir: str | None = None
