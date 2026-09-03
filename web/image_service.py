"""web/image_service.py：★图与足迹业务校验唯一实现（详设-v0.7 §6 + §8.1）。

风格照 web/catalog_service.py / web/inventory_service.py：
- 纯函数/异步函数，session 由调用方注入
- 校验失败抛 ImageServiceError（中文 message）

职责（详设 §8.1 catalog 组 图/足迹接口）：
- attach_images(item_id, image_file_ids[])：挂图（校验 item 存在、image_file 存在、
  同档重复挂同图 → 409，详设 §3.4 UNIQUE + 技术定）
- detach_image(item_id, img_id)：撤图
- set_image_meta(item_id, img_id, sort?, is_main?)：排序/主图（is_main 每档案至多一张）
- upload_image(file_bytes, filename, source_mark='selfshot')：建 scrape.image_file 行
  + 落盘 {scrape.storage_dir}/selfshot/<日期>/（读设置键或照 web/scrape_store.py 方式；
  测试用 tmp 目录隔离，不污染真实目录）
- register_usage(image_file_id, shop_id, note?)：登记足迹（图存在、店存在（sys.shop enabled）；
  同图同店重复 → 幂等返回现有 usage_id，详设 §3.5 技术定）
- unregister_usage(image_file_id, usage_id)：撤销足迹
- list_usages(image_file_id?)：join sys.shop 返回 platform + name（软提示：
  "已在 Etsy-店1 用过"）
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from pathlib import Path
from typing import BinaryIO

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.catalog import ImageShopUsage, ItemImage
from models.scrape import ImageFile
from models.sys import Shop


# ---- 错误 ----


class ImageServiceError(Exception):
    """业务拒绝（路由捕获 -> 页面 err 提示 / API 返回 4xx/409）。"""


# ---- 挂图（§8.1 POST items/{iid}/images） ----


async def attach_images(
    session: AsyncSession,
    item_id: int,
    image_file_ids: list[int],
) -> list[int]:
    """挂图到档案图库（详设 §6.1 + §8.4-6）。

    校验：
    1. item 存在
    2. 每个 image_file 存在（scrape.image_file 表）
    3. 同档重复挂同图 → 409（UNIQUE(item_id, image_file_id) + 技术定）

    返回新建 item_image.id 列表。
    """
    if not image_file_ids:
        raise ImageServiceError("至少提供一张图片")

    # 校验 item 存在
    from models.catalog import Item

    item = await session.get(Item, item_id)
    if item is None:
        raise ImageServiceError("档案不存在")

    created_ids: list[int] = []

    for fid in image_file_ids:
        # 校验 image_file 存在
        img_file = await session.get(ImageFile, fid)
        if img_file is None:
            raise ImageServiceError(f"图片不存在（image_file_id={fid}）")

        # 检查是否已挂（同档重复挂同图 → 409）
        existing = (
            await session.execute(
                select(ItemImage.id).where(
                    ItemImage.item_id == item_id,
                    ItemImage.image_file_id == fid,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ImageServiceError(
                f"该图片已挂在此档案（image_file_id={fid}，409）"
            )

        # 挂图
        ii = ItemImage(
            item_id=item_id,
            image_file_id=fid,
            sort=0,
            is_main=False,
        )
        session.add(ii)
        await session.flush()
        created_ids.append(ii.id)

    return created_ids


# ---- 撤图（§8.1 DELETE items/{iid}/images/{img_id}） ----


async def detach_image(
    session: AsyncSession,
    item_id: int,
    img_id: int,
) -> None:
    """从档案图库撤除一张图（详设 §8.1）。

    img_id = catalog.item_image.id（不是 scrape.image_file.id）。
    """
    ii = await session.get(ItemImage, img_id)
    if ii is None or ii.item_id != item_id:
        raise ImageServiceError("图档关联不存在")
    await session.delete(ii)


# ---- 排序/主图（§8.1 PATCH items/{iid}/images/{img_id}） ----


async def set_image_meta(
    session: AsyncSession,
    item_id: int,
    img_id: int,
    *,
    sort: int | None = None,
    is_main: bool | None = None,
) -> None:
    """设置档案图的排序和/或主图标记（详设 §3.4 + §8.1）。

    is_main 每档案至多一张：设为 true 时自动取消该档案其余图的 is_main。
    """
    ii = await session.get(ItemImage, img_id)
    if ii is None or ii.item_id != item_id:
        raise ImageServiceError("图档关联不存在")

    if sort is not None:
        ii.sort = sort

    if is_main is not None and is_main:
        # 先取消该档案所有图的 is_main
        all_imgs = (
            await session.execute(
                select(ItemImage).where(ItemImage.item_id == item_id)
            )
        ).scalars().all()
        for img in all_imgs:
            img.is_main = False
        ii.is_main = True
    elif is_main is not None:
        ii.is_main = is_main


# ---- 上传图（§8.1 POST catalog/images/upload） ----


async def upload_image(
    session: AsyncSession,
    file_bytes: bytes,
    filename: str,
    *,
    source_mark: str = "selfshot",
    storage_root: str | None = None,
) -> int:
    """自己上传图：建 scrape.image_file 行 + 落盘（详设 §6.3 + §2.4）。

    落盘路径：{storage_root}/selfshot/<YYYY-MM-DD>/<hash>_<filename>
    storage_root 由调用方注入（生产读 scrape.storage_dir 设置键；
    测试用 tmp 目录隔离）。

    source_mark ∈ ('scraped','selfshot','ai_generated','authorized')，默认 selfshot。

    返回新建 image_file.id。
    """
    _VALID_MARKS = frozenset({"scraped", "selfshot", "ai_generated", "authorized"})
    if source_mark not in _VALID_MARKS:
        raise ImageServiceError(
            f"来源标记不合法「{source_mark}」，仅允许：scraped/selfshot/ai_generated/authorized"
        )

    if not file_bytes:
        raise ImageServiceError("文件内容为空")

    if not filename or not filename.strip():
        raise ImageServiceError("文件名（filename）必填")

    # 计算文件哈希（用于唯一文件名，避免冲突）
    file_hash = hashlib.sha256(file_bytes).hexdigest()[:16]
    safe_name = Path(filename).name  # 去掉路径前缀
    date_dir = date.today().isoformat()
    stored_name = f"{file_hash}_{safe_name}"

    # 落盘
    if storage_root:
        dest_dir = Path(storage_root) / "selfshot" / date_dir
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest_path = dest_dir / stored_name
        dest_path.write_bytes(file_bytes)

        local_path = str(dest_path)
        day_dir = f"selfshot/{date_dir}"
    else:
        # 无 storage_root 时不落盘（测试场景可选）
        local_path = None
        day_dir = f"selfshot/{date_dir}"

    # 生成 URL（本地存储用 file:// 路径；素材库系统内引用）
    url = f"selfshot://{date_dir}/{stored_name}"

    # 生成 batch_id
    batch_id = f"upload-{file_hash}"

    # 建 scrape.image_file 行
    img_file = ImageFile(
        batch_id=batch_id,
        source="http",  # 上传图来源标记（非扒图链接）
        url=url,
        local_path=local_path,
        day_dir=day_dir,
        source_mark=source_mark,
        status="downloaded",
        width=None,
        height=None,
        watermark=False,
    )
    session.add(img_file)
    await session.flush()

    return img_file.id


# ---- 登记足迹（§8.1 POST catalog/images/{image_id}/usage） ----


async def register_usage(
    session: AsyncSession,
    image_file_id: int,
    shop_id: int,
    *,
    note: str = "",
) -> int:
    """登记图店使用足迹（详设 §3.5 + §6.2）。

    校验：
    1. image_file 存在（scrape.image_file 表）
    2. shop 存在且 enabled
    3. 同图同店重复 → 幂等返回现有 usage_id（技术定，详设 §3.5）

    返回 usage_id（新建或已有）。
    """
    # 校验图存在
    img_file = await session.get(ImageFile, image_file_id)
    if img_file is None:
        raise ImageServiceError(f"图片不存在（image_file_id={image_file_id}）")

    # 校验店存在且 enabled
    shop = await session.get(Shop, shop_id)
    if shop is None:
        raise ImageServiceError(f"店铺不存在（shop_id={shop_id}）")
    if not shop.enabled:
        raise ImageServiceError(f"店铺「{shop.name}」已停用")

    # 同图同店幂等：已存在返回现有
    existing = (
        await session.execute(
            select(ImageShopUsage.id).where(
                ImageShopUsage.image_file_id == image_file_id,
                ImageShopUsage.shop_id == shop_id,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # 新建
    usage = ImageShopUsage(
        image_file_id=image_file_id,
        shop_id=shop_id,
        note=note.strip() or "",
    )
    session.add(usage)
    await session.flush()
    return usage.id


# ---- 撤销足迹（§8.1 DELETE catalog/images/{image_id}/usage/{usage_id}） ----


async def unregister_usage(
    session: AsyncSession,
    image_file_id: int,
    usage_id: int,
) -> None:
    """撤销足迹（详设 §8.1）。"""
    usage = await session.get(ImageShopUsage, usage_id)
    if usage is None or usage.image_file_id != image_file_id:
        raise ImageServiceError("足迹记录不存在")
    await session.delete(usage)


# ---- 足迹查询（§8.1 GET catalog/images/usage?image_file_id=） ----


async def list_usages(
    session: AsyncSession,
    *,
    image_file_id: int | None = None,
) -> list[dict]:
    """足迹查询：join sys.shop 返回 platform + name（详设 §6.2 软提示）。

    返回 [{id, image_file_id, shop_id, shop_name, platform, note, created_at}]。
    """
    q = (
        select(
            ImageShopUsage.id,
            ImageShopUsage.image_file_id,
            ImageShopUsage.shop_id,
            Shop.name.label("shop_name"),
            Shop.platform,
            ImageShopUsage.note,
            ImageShopUsage.created_at,
        )
        .join(Shop, ImageShopUsage.shop_id == Shop.id)
    )
    if image_file_id is not None:
        q = q.where(ImageShopUsage.image_file_id == image_file_id)
    q = q.order_by(ImageShopUsage.created_at.desc())

    rows = (await session.execute(q)).all()
    return [
        {
            "id": r.id,
            "image_file_id": r.image_file_id,
            "shop_id": r.shop_id,
            "shop_name": r.shop_name,
            "platform": r.platform,
            "note": r.note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


__all__ = [
    "ImageServiceError",
    "attach_images",
    "detach_image",
    "set_image_meta",
    "upload_image",
    "register_usage",
    "unregister_usage",
    "list_usages",
]
