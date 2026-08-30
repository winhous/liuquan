"""业务库迁移 0003：tm 侧扩展（详设-v0.3 §4 字段级，决策 25/27/28）。

tm.task 扩展（§4.1）：
- +tags JSONB NOT NULL DEFAULT '[]'（开放标签 list[str]，决策 25）
- +ai_suggestion JSONB 可空（AI 下一步建议，决策 27，结构见 §4.1 表）

tm.task_event 扩展（§4.2）：
- +detail JSONB 可空（结构化详情：from/to/note/指令解析，决策 27/28）
- event_type CHECK 由 9 值扩 12 值：+suggested（AI 建议写入 ai_suggestion）/
  +transferred（流转执行）/ +disagreed（分歧留痕）
  ——PG 改 CHECK 用 op.drop_constraint + op.create_check_constraint：
  0001 内联未命名 CHECK 的 PG 自动名 = task_event_event_type_check（单列规则
  {table}_{column}_check，实测确认）；重建为具名 chk_event_type（models/tm.py
  ORM 同名，R22 同源；downgrade 反向恢复 9 值 + 原名）。

修订号 0003（v0.3）。downgrade 反向：恢复 9 值 CHECK + 删三列。
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

_EVENT_TYPES_9 = (
    "('created','approved','started','completed','voided',"
    "'blocked','unblocked','derived','updated')"
)
_EVENT_TYPES_12 = (
    "('created','approved','started','completed','voided',"
    "'blocked','unblocked','derived','updated',"
    "'suggested','transferred','disagreed')"
)


def upgrade() -> None:
    # ---- tm.task：+tags / +ai_suggestion（§4.1，决策 25/27）----
    op.add_column(
        "task",
        sa.Column(
            "tags",
            sa.dialects.postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        schema="tm",
    )
    op.add_column(
        "task",
        sa.Column("ai_suggestion", sa.dialects.postgresql.JSONB(), nullable=True),
        schema="tm",
    )

    # ---- tm.task_event：+detail + event_type CHECK 9 -> 12（§4.2，决策 27/28）----
    op.add_column(
        "task_event",
        sa.Column("detail", sa.dialects.postgresql.JSONB(), nullable=True),
        schema="tm",
    )
    op.drop_constraint(
        "task_event_event_type_check", "task_event", type_="check", schema="tm"
    )
    op.create_check_constraint(
        "chk_event_type",
        "task_event",
        f"event_type IN {_EVENT_TYPES_12}",
        schema="tm",
    )


def downgrade() -> None:
    # 反向：恢复 9 值 CHECK（原名 task_event_event_type_check，与 0001 内联同名）
    op.drop_constraint("chk_event_type", "task_event", type_="check", schema="tm")
    op.create_check_constraint(
        "task_event_event_type_check",
        "task_event",
        f"event_type IN {_EVENT_TYPES_9}",
        schema="tm",
    )
    op.drop_column("task_event", "detail", schema="tm")
    op.drop_column("task", "ai_suggestion", schema="tm")
    op.drop_column("task", "tags", schema="tm")
