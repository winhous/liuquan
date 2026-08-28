"""tests/fixtures/tm 样本加载器：加载即类型校验（R12 测试数据规则 1：
fixture 只住 tests/fixtures/，结构对齐注册 Model——结构漂移测试自然红）。

- evidence_sample(name) -> EvidenceRef            （pydantic model_validate）
- task_proposal_sample(name) -> TaskProposal      （pydantic model_validate）
- task_sample(name, **overrides) -> dict          （对齐 tm.task 列集合 +
  必填列检查 + due 归一为 date；overrides 覆盖字段供测试改日期等）
- task_model(name, **overrides) -> Task           （构造 ORM 瞬态实例）

本目录（tests/fixtures/）是 P2 豁免子树（详设 §9 / engine/lint/p2.py
EXEMPT_SUBTREES），样本内容不受 P2 字面量判据约束。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import yaml

from models.contract.task import EvidenceRef, TaskProposal
from models.tm import Task

_FIXTURES_DIR = Path(__file__).resolve().parent


def _samples(filename: str) -> dict:
    data = yaml.safe_load((_FIXTURES_DIR / filename).read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{filename} 应为映射"
    return data


def evidence_sample(name: str) -> EvidenceRef:
    """假 evidence 样本：EvidenceRef 契约校验（kind/ref_id/quote）。"""
    return EvidenceRef.model_validate(_samples("evidence.yaml")[name])


def task_proposal_sample(name: str) -> TaskProposal:
    """假 TaskProposal 样本：TaskProposal 契约校验（结构漂移即 ValidationError）。"""
    return TaskProposal.model_validate(_samples("task_proposals.yaml")[name])


def _validate_task_fields(data: dict) -> None:
    """tm.task 列集合 + 必填列非空检查（结构漂移 = 未知列/缺必填/空值 -> 红）。"""
    columns = set(Task.__table__.columns.keys())
    unknown = set(data) - columns
    if unknown:
        raise ValueError(f"tasks.yaml 样本含未知列：{sorted(unknown)}（结构漂移）")
    required = {"title", "domain", "role", "due", "source_type", "source", "created_by"}
    bad = [f for f in required if data.get(f) in (None, "")]
    if bad:
        raise ValueError(f"tasks.yaml 样本缺/空必填列：{sorted(bad)}")


def task_sample(name: str, **overrides) -> dict:
    """假任务样本 -> dict（对齐 tm.task 列；overrides 覆盖字段；due 归一 date）。"""
    data = dict(_samples("tasks.yaml")[name])
    data.update(overrides)
    _validate_task_fields(data)
    if isinstance(data.get("due"), str):
        data["due"] = date.fromisoformat(data["due"])
    return data


def task_model(name: str, **overrides) -> Task:
    """假任务样本 -> ORM 瞬态 Task 实例（未入会话；落库由调用方管理）。"""
    return Task(**task_sample(name, **overrides))


__all__ = [
    "evidence_sample",
    "task_model",
    "task_proposal_sample",
    "task_sample",
]
