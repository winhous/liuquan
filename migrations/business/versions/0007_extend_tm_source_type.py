"""扩展 tm.task.source_type CHECK 约束（v0.4 详设 §6）。

现有约束是无名表级 CHECK（PG 自动命名 task_check* 系列，不可静态预知），
upgrade 中先动态查出约束名再 DROP，最后 ADD 具名约束 chk_task_source_type。

Revision ID: 0007
Create Date: 2026-09-01
"""

from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 动态查出旧的 source_type CHECK（PG 可能把 IN 重写为 = ANY (ARRAY[...])，
    # 故用 conname 模式 + 定义文本双保险匹配）
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'tm.task'::regclass AND contype = 'c' "
            "AND (pg_get_constraintdef(oid) LIKE '%source_type%' "
            "     OR pg_get_constraintdef(oid) LIKE '%source_type%IN%' "
            "     OR pg_get_constraintdef(oid) LIKE '%source_type%ANY%')"
        )
    ).fetchall()
    for conname, _defn in rows:
        op.drop_constraint(conname, "task", schema="tm", type_="check")

    # 新具名 CHECK：('ai','manual','schedule')
    op.create_check_constraint(
        "chk_task_source_type",
        "task",
        "source_type IN ('ai','manual','schedule')",
        schema="tm",
    )


def downgrade() -> None:
    op.drop_constraint("chk_task_source_type", "task", schema="tm", type_="check")

    # 恢复原三值 CHECK（不含 'schedule'）
    op.create_check_constraint(
        None,  # 无名约束（恢复原状）
        "task",
        "source_type IN ('ai','manual')",
        schema="tm",
    )
