"""crm.todo_candidate 补 suggested_next 列（决策 27：AI 对任务生成后的下一步建议，
确认候选时写入 tm.task.ai_suggestion）。

0002 已应用（本地/生产库）后新增的字段须走新迁移（已应用版本的文件修改不重跑）；
新库 0002（无此列）+ 0004（加列）与老库一致。
"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "todo_candidate",
        sa.Column("suggested_next", sa.dialects.postgresql.JSONB(), nullable=True),
        schema="crm",
    )


def downgrade() -> None:
    op.drop_column("todo_candidate", "suggested_next", schema="crm")
