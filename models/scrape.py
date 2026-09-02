"""业务库 ORM（database: liuquan, schema: scrape）—— link_record / image_file 两表。

纯数据层（详设-v0.6 §4：素材库图集层雏形——一个链接一条记录 + 图片挂链接）。

复用 models/tm.py 的 TmBase（业务库 declarative base，R22）。
scrape 表注册进 TmBase.metadata 后，migrations/business/env.py 的 target_metadata
自动涵盖各 schema（R22 同源，env.py 已 import 本模块）。

对齐详设-v0.6 §4.1/§4.2（迁移 0011）：
- link_record：链接记录表（一链接一条）
  - url：原始分享链接（含 xsec_token 等易变 query）
  - normalized_url：规范化链接（去易变 query 后的查重键），UNIQUE 幂等
  - source：来源（xhs / xianyu / http；原 'crm' 语义废弃——CRM 对话图片走
    crm.message_image 表，不落 scrape.image_file）
  - status：状态（pending / downloading / done / failed）
  - image_count：成功落库图片数
  - desc：商品描述（元数据）；tags：标签列表（JSONB 数组）
  - author_id：作者/卖家 ID；batch_id：批次（立即扒=web uuid；定时=sched-<ts>）
  - storage_dir：落盘相对路径（相对 scrape.storage_dir）
  - error_note：失败原因；degraded_note：元数据降级原因
  - created_at / updated_at
- image_file（v0.5 表扩展）：
  - link_record_id：FK → link_record.id ON DELETE CASCADE（图片挂链接）
  - source_mark：图片来源标记，默认 'scraped'，CHECK 四值
    （scraped 扒图 / selfshot 自拍 / ai_generated AI 生成 / authorized 授权，
    底层逻辑 §3.3 防侵权可追溯；v0.7 建档时设置，v0.6 只建列 + 默认）
  - sku_id / shop_id：BIGINT 裸列（v0.7 SKU 建档启用「图挂 SKU×店铺」，本版不加 FK）
  - 原 uq_image_file_batch_url 保留不动（兼容存量）；新增 uq_scrape_image_link_url
    (link_record_id, url) 图片幂等键
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from models.tm import TmBase


class LinkRecord(TmBase):
    """scrape.link_record：链接记录表（详设-v0.6 §4.1，一链接一条）。

    normalized_url 唯一（去 xsec_token 等易变 query 后的查重键）：同作品
    不同 token 重复粘贴返回现有记录（幂等），不重复建、不重复下载。
    source ∈ {xhs, xianyu, http}；status ∈ {pending, downloading, done, failed}。
    """

    __tablename__ = "link_record"
    __table_args__ = (
        UniqueConstraint(
            "normalized_url", name="uq_scrape_link_record_normalized_url"
        ),
        CheckConstraint(
            "source IN ('xhs','xianyu','http')", name="chk_scrape_link_source"
        ),
        CheckConstraint(
            "status IN ('pending','downloading','done','failed')",
            name="chk_scrape_link_status",
        ),
        Index("idx_scrape_link_status_created", "status", "created_at"),
        Index("idx_scrape_link_batch", "batch_id"),
        {"schema": "scrape"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)  # 原始分享链接
    normalized_url: Mapped[str] = mapped_column(Text, nullable=False)  # 查重键
    source: Mapped[str] = mapped_column(Text, nullable=False)  # 'xhs','xianyu','http'
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'pending'")
    )  # 'pending','downloading','done','failed'
    image_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    desc: Mapped[str | None] = mapped_column(
        Text, server_default=text("''")
    )
    tags: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'")
    )
    author_id: Mapped[str | None] = mapped_column(Text)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    storage_dir: Mapped[str | None] = mapped_column(Text)
    error_note: Mapped[str | None] = mapped_column(Text)
    degraded_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ImageFile(TmBase):
    """scrape.image_file：扒图产物表（v0.5 §4.1 + v0.6 §4.2 扩展）。

    - 原 uq_image_file_batch_url（batch_id × url）保留兼容存量；
    - 新增 uq_scrape_image_link_url（link_record_id × url）：新写入全部带
      link_record_id，同链接同 URL 只写一次（图片幂等键）；
    - source_mark 默认 'scraped'；sku_id/shop_id 为 v0.7 留位裸列。
    来源：xhs / xianyu / http（子目录按来源分；'crm' 语义废弃）。
    """

    __tablename__ = "image_file"
    __table_args__ = (
        UniqueConstraint("batch_id", "url", name="uq_image_file_batch_url"),
        UniqueConstraint(
            "link_record_id", "url", name="uq_scrape_image_link_url"
        ),
        CheckConstraint(
            "source_mark IN ('scraped','selfshot','ai_generated','authorized')",
            name="chk_scrape_image_source_mark",
        ),
        Index("idx_scrape_image_file_source_created", "source", "created_at"),
        Index("idx_scrape_image_link", "link_record_id"),
        {"schema": "scrape"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    batch_id: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)  # 'xhs','xianyu','http'
    url: Mapped[str] = mapped_column(Text, nullable=False)
    link_record_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("scrape.link_record.id", ondelete="CASCADE"),
    )  # 图片挂链接
    source_mark: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("'scraped'")
    )  # 'scraped','selfshot','ai_generated','authorized'
    sku_id: Mapped[int | None] = mapped_column(BigInteger)  # v0.7 留位（图挂 SKU）
    shop_id: Mapped[int | None] = mapped_column(BigInteger)  # v0.7 留位（图挂店铺）
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
