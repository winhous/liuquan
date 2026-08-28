"""T9 审计测试（详设-v0.1 §3 engine_audit 表 / §7.3 先审计后调用 / §8 审计与
脱敏；任务 T9 audit.py）。

覆盖（全部以详设 §3/§7.3/§8 为唯一依据）：
- record 字段全对（对齐 engine_audit 表 11 列）；result 三态 ok/reask/failed，
  成功/重问/失败都记（§3 表注释「成功/重问/失败都记」）
- 反向：非法 result 拒绝记录
- get_audit 返回该任务的审计行
- audit_summary 只含安全字段（task/worker/phase/result/model/token/耗时，
  §8 stdout 通道），绝不包含 prompt/输出正文/密钥
- DbAuditGate 可写/不可写正反（实现 AuditGate Protocol，§7.3）；
  未注入探活函数 fail-closed 拒绝
- 先审计后调用顺序（§7.3 原文「审计不可写 = 调用不允许发生」）：
  gate.ensure_writable() 先于 record()；gate 拒绝时记录与调用都不发生

DAO 用桩（FakeAuditDAO 住 tests/，R12 构造注入），零网络零 DB。
"""

from __future__ import annotations

import pytest

from engine.core.audit import (
    AuditRecord,
    AuditRecorder,
    DbAuditGate,
    audit_summary,
)
from engine.core.llm.audit_gate import AuditGateError


class FakeAuditDAO:
    """AuditDAO 桩（住 tests/）。rows 存 append_audit 收到的表列（dict）；
    events 可选：追加 "record" 标记供「先审计后调用」顺序断言。"""

    def __init__(self, events: list[str] | None = None) -> None:
        self.rows: list[dict[str, object]] = []
        self.events = events

    async def append_audit(
        self,
        *,
        task_id: int,
        step_id: int,
        worker_id: str,
        model: str,
        attempt: int,
        result: str,
        input_full: object,
        output_full: object,
        input_tokens: int | None,
        output_tokens: int | None,
        duration_ms: int | None,
    ) -> None:
        self.rows.append(
            {
                "task_id": task_id,
                "step_id": step_id,
                "worker_id": worker_id,
                "model": model,
                "attempt": attempt,
                "result": result,
                "input_full": input_full,
                "output_full": output_full,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "duration_ms": duration_ms,
            }
        )
        if self.events is not None:
            self.events.append("record")

    async def get_audit(self, task_id: int) -> list[dict[str, object]]:
        return [row for row in self.rows if row["task_id"] == task_id]


def _sample_record_kwargs() -> dict[str, object]:
    """record 的 11 个表列样本（对齐 engine_audit 表，详设 §3）。"""
    return {
        "task_id": 7,
        "step_id": 3,
        "worker_id": "echo",
        "model": "deepseek-chat",
        "attempt": 0,
        "result": "ok",
        "input_full": "prompt 正文 sample",
        "output_full": "输出正文 sample",
        "input_tokens": 12,
        "output_tokens": 34,
        "duration_ms": 150,
    }


# ---- record：字段全对 + 三态都记 ----

@pytest.mark.asyncio
async def test_record_writes_all_table_fields() -> None:
    """record 落一条审计，字段对齐 engine_audit 表 11 列（详设 §3）。"""
    dao = FakeAuditDAO()
    recorder = AuditRecorder(dao)

    rec = await recorder.record(**_sample_record_kwargs())

    assert isinstance(rec, AuditRecord)
    assert rec.task_id == 7
    assert rec.step_id == 3
    assert rec.worker_id == "echo"
    assert rec.model == "deepseek-chat"
    assert rec.attempt == 0
    assert rec.result == "ok"
    assert rec.input_full == "prompt 正文 sample"
    assert rec.output_full == "输出正文 sample"
    assert rec.input_tokens == 12
    assert rec.output_tokens == 34
    assert rec.duration_ms == 150
    assert len(dao.rows) == 1
    assert dao.rows[0] == _sample_record_kwargs()


@pytest.mark.asyncio
async def test_record_all_three_results_ok_reask_failed() -> None:
    """成功/重问/失败都记（详设 §3 表注释「成功/重问/失败都记」）。"""
    dao = FakeAuditDAO()
    recorder = AuditRecorder(dao)

    await recorder.record(**{**_sample_record_kwargs(), "result": "ok"})
    await recorder.record(**{**_sample_record_kwargs(), "result": "reask"})
    await recorder.record(**{**_sample_record_kwargs(), "result": "failed"})

    assert [row["result"] for row in dao.rows] == ["ok", "reask", "failed"]


@pytest.mark.asyncio
async def test_record_rejects_invalid_result() -> None:
    """反向：非法 result（非 ok/reask/failed）-> ValueError，不落库。"""
    dao = FakeAuditDAO()
    recorder = AuditRecorder(dao)

    with pytest.raises(ValueError):
        await recorder.record(**{**_sample_record_kwargs(), "result": "bogus"})

    assert dao.rows == []


# ---- get_audit ----

@pytest.mark.asyncio
async def test_get_audit_returns_rows_for_task() -> None:
    """get_audit 按任务返回审计行（A3「audit <task> 可查」的前奏）。"""
    dao = FakeAuditDAO()
    recorder = AuditRecorder(dao)
    await recorder.record(**{**_sample_record_kwargs(), "task_id": 7})
    await recorder.record(**{**_sample_record_kwargs(), "task_id": 8})

    rows7 = await dao.get_audit(7)
    rows8 = await dao.get_audit(8)

    assert len(rows7) == 1 and rows7[0]["task_id"] == 7
    assert len(rows8) == 1 and rows8[0]["task_id"] == 8
    assert rows7[0]["result"] == "ok"
    assert rows7[0]["model"] == "deepseek-chat"


# ---- audit_summary：安全摘要（§8 stdout 通道） ----

def test_audit_summary_contains_safe_fields_only() -> None:
    """摘要含 task/worker/phase/状态/模型/token 数/耗时（§8 stdout 允许内容）。"""
    row = AuditRecord(
        task_id=7,
        step_id=3,
        worker_id="echo",
        model="deepseek-chat",
        attempt=0,
        result="ok",
        input_full="prompt 正文 sample",
        output_full="输出正文 sample",
        input_tokens=12,
        output_tokens=34,
        duration_ms=150,
        phase="REASON",
    )

    summary = audit_summary(row)

    assert "task=7" in summary
    assert "worker=echo" in summary
    assert "phase=REASON" in summary
    assert "result=ok" in summary
    assert "model=deepseek-chat" in summary
    assert "tokens_in=12" in summary
    assert "tokens_out=34" in summary
    assert "duration_ms=150" in summary


def test_audit_summary_omits_prompt_output_and_secrets() -> None:
    """脱敏：摘要绝不包含 prompt/输出正文/密钥内容（§8 stdout 禁止内容）。"""
    secret_input = {"role": "user", "content": "confidential-request-abc"}
    secret_output = "confidential-reply-xyz s3cr3t-cred-9f8e7d"
    row = AuditRecord(
        task_id=7,
        step_id=3,
        worker_id="echo",
        model="deepseek-chat",
        attempt=0,
        result="ok",
        input_full=secret_input,
        output_full=secret_output,
        input_tokens=12,
        output_tokens=34,
        duration_ms=150,
        phase="REASON",
    )

    summary = audit_summary(row)

    assert "confidential-request-abc" not in summary
    assert "confidential-reply-xyz" not in summary
    assert "s3cr3t-cred-9f8e7d" not in summary


def test_audit_summary_accepts_mapping_row() -> None:
    """get_audit 返回的 dict 行同样可摘要（无 phase 列 -> 显示 "-"）。"""
    row = {
        "task_id": 7,
        "worker_id": "echo",
        "phase": None,
        "result": "reask",
        "model": "deepseek-chat",
        "input_tokens": 5,
        "output_tokens": 6,
        "duration_ms": 40,
    }

    summary = audit_summary(row)

    assert "task=7" in summary
    assert "worker=echo" in summary
    assert "phase=-" in summary
    assert "result=reask" in summary
    assert "tokens_in=5" in summary
    assert "tokens_out=6" in summary
    assert "duration_ms=40" in summary


# ---- DbAuditGate：可写/不可写正反（§7.3 先审计后调用） ----

def test_db_audit_gate_writable_allows() -> None:
    """正向：审计可写 -> ensure_writable() 正常返回。"""
    gate = DbAuditGate(writable_check=lambda: True)
    gate.ensure_writable()  # 不抛


def test_db_audit_gate_unwritable_raises() -> None:
    """反向：审计不可写 -> ensure_writable() 抛 AuditGateError。"""
    gate = DbAuditGate(writable_check=lambda: False)
    with pytest.raises(AuditGateError):
        gate.ensure_writable()


def test_db_audit_gate_unconfigured_fails_closed() -> None:
    """反向：未注入探活函数 = 无法证明可写 -> fail-closed 抛 AuditGateError。"""
    gate = DbAuditGate()
    with pytest.raises(AuditGateError):
        gate.ensure_writable()


# ---- 先审计后调用顺序（§7.3：审计不可写 = 调用不允许发生） ----

@pytest.mark.asyncio
async def test_gate_checked_before_record() -> None:
    """顺序约束：gate.ensure_writable() 先于 record()（§7.3 先审计后调用）。"""
    events: list[str] = []
    dao = FakeAuditDAO(events=events)
    recorder = AuditRecorder(dao)

    def writable_check() -> bool:
        events.append("gate")
        return True

    gate = DbAuditGate(writable_check=writable_check)

    # 模拟 runner 的 §7.3 流程：先审计（可写性检查）-> LLM 调用 -> 记录
    gate.ensure_writable()
    events.append("llm")
    await recorder.record(**_sample_record_kwargs())

    assert events == ["gate", "llm", "record"]
    assert len(dao.rows) == 1


@pytest.mark.asyncio
async def test_gate_unwritable_blocks_before_any_record() -> None:
    """反向（§7.3 原文「审计不可写 = 调用不允许发生」）：
    gate 拒绝时，后续记录与调用都不发生。"""
    events: list[str] = []
    dao = FakeAuditDAO(events=events)
    recorder = AuditRecorder(dao)

    def writable_check() -> bool:
        events.append("gate")
        return False

    gate = DbAuditGate(writable_check=writable_check)

    with pytest.raises(AuditGateError):
        gate.ensure_writable()

    assert events == ["gate"]  # 只有 gate 检查发生
    assert dao.rows == []  # record 从未被调用
