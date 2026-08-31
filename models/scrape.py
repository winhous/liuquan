"""业务库 ORM（database: liuquan, schema: scrape）—— image_file 表。

纯数据层（详设-v0.5 §4.1：scrape.image_file 表，扒图产物落库）。

复用 models/tm.py 的 TmBase（业务库 declarative base，R22）。
scrape 表注册进 TmBase.metadata 后，migrations/business/env.py 的 target_metadata
自动涵盖三 schema（R22 同源，env.py 已 import 本模块）。

对齐详设 §4.1（修订后）：
- image_file：扒图产物（batch_id × url 唯一，幂等防重）
  - batch_id：批次 id（同一次扒图共享）
  - source：来源（xhs/xianyu/crm）
  - url：原始图片 URL
  - local_path：本地落盘路径
  - day_dir：日期目录（如 20260101）
  - desc：商品描述（从元数据提取）
  - tags：标签列表（JSONB 数组）
  - author_id：作者/卖家 ID
  - width/height：图片宽高（image_inspect 写入）
  - watermark：是否有水印（image_inspect 写入）
  - status：状态（pending/downloaded/failed）
  - created_at：创建时间
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.tm import TmBase


class ImageFile(TmBase):
    """scrape.image_file：扒图产物表（详设-v0.5 §4.1）。

    batch_id × url 唯一（同批同 URL 只写一次，幂等防重）。
    来源：xhs / xianyu / crm（子目录按来源分）。
    """

    __tablename__ = "image_file"
    __table_args__ = (
        UniqueConstraint("batch_id", "url", name="uq_image_file_batch_url"),
        Index("idx_scrape_image_file_source_created", "source", "created_at"),
        {"schema": "scrape"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # 'xhs','xianyu','crm'
    url: Mapped[str] = mapped_column(Text, nullable=False)
    local_path: Mapped[str | None] = mapped_column(Text)
    day_dir: Mapped[str | None] = mapped_column(Text)
    desc: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    author_id: Mapped[str | None] = mapped_column(Text)
    width: Mapped[int | None] = mapped_column(BigInteger)
    height: Mapped[int | None] = mapped_column(BigInteger)
    watermark: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )  # 'pending','downloaded','failed'
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
