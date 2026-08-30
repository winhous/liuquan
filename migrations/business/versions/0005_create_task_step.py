"""tm.task_step 步骤表（复核反馈 #6：任务分解清单，父任务完成依赖步骤）。

- content 1-200（步骤内容，轻量无负责人/截止）
- status open/done（勾选完成可反勾）
- sort_order 排序；task_id FK ON DELETE CASCADE
- 完成依赖：父任务有未完成步骤时不能 done（应用层 TMWebError 拦截，
  DB 不约束——「步骤完成之后才能结束父任务」是应用规则）
"""

from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_step",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "task_id",
            sa.BigInteger(),
            sa.ForeignKey("tm.task.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'open'"),
        ),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint("length(content) BETWEEN 1 AND 200", name="chk_step_content"),
        sa.CheckConstraint("status IN ('open','done')", name="chk_step_status"),
        sa.PrimaryKeyConstraint("id"),
        schema="tm",
    )
    op.create_index("idx_task_step_task", "task_step", ["task_id", "sort_order"], schema="tm")


def downgrade() -> None:
    op.drop_index("idx_task_step_task", table_name="task_step", schema="tm")
    op.drop_table("task_step", schema="tm")
