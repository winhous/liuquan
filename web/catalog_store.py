"""web/catalog_store.py：★档案读 DAO（详设-v0.7 §8.1 读接口 + §4.3 页面）。

页面读快路径 + api_biz 读接口共用。
"""

from __future__ import annotations

from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.catalog import (
    ImageShopUsage,
    Item,
    ItemBom,
    ItemImage,
)
from models.sys import Shop


async def list_items(
    session: AsyncSession,
    *,
    product_name: str | None = None,
    kind: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """档案列表（分页 + 筛选）。返回 {items, total, page, pages}。"""
    q = select(Item)
    count_q = select(func.count()).select_from(Item)

    if product_name:
        q = q.where(Item.product_name == product_name)
        count_q = count_q.where(Item.product_name == product_name)
    if kind:
        q = q.where(Item.kind == kind)
        count_q = count_q.where(Item.kind == kind)
    if keyword:
        pat = f"%{keyword}%"
        cond = or_(Item.code.ilike(pat), Item.name.ilike(pat), Item.product_name.ilike(pat))
        q = q.where(cond)
        count_q = count_q.where(cond)

    total = (await session.execute(count_q)).scalar_one() or 0
    pages = max(1, (total + page_size - 1) // page_size)
    q = q.order_by(Item.product_name.nullsfirst(), Item.name).offset(
        (page - 1) * page_size
    ).limit(page_size)

    rows = (await session.execute(q)).scalars().all()
    return {
        "items": [
            {
                "id": i.id,
                "code": i.code,
                "name": i.name,
                "product_name": i.product_name,
                "product_code": i.product_code,
                "specs": i.specs or {},
                "kind": i.kind,
                "cost": float(i.cost) if i.cost is not None else None,
                "supplier": i.supplier or "",
                "status": i.status,
                "remark": i.remark or "",
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in rows
        ],
        "total": total,
        "page": page,
        "pages": pages,
    }


async def get_item_detail(session: AsyncSession, item_id: int) -> dict | None:
    """档案详情（信息+配方+图库+足迹+库存摘要）。"""
    item = await session.get(Item, item_id)
    if item is None:
        return None

    # 配方 BOM
    bom = []
    if item.kind == "combo":
        bom_rows = (
            await session.execute(
                select(ItemBom, Item.code.label("child_code"), Item.name.label("child_name"))
                .join(Item, ItemBom.child_item_id == Item.id)
                .where(ItemBom.parent_item_id == item_id)
            )
        ).all()
        for b in bom_rows:
            bom.append({
                "id": b.ItemBom.id,
                "child_item_id": b.ItemBom.child_item_id,
                "child_code": b.child_code,
                "child_name": b.child_name,
                "qty": float(b.ItemBom.qty),
            })

    # 图库
    img_rows = (
        await session.execute(
            select(ItemImage).where(ItemImage.item_id == item_id).order_by(ItemImage.sort)
        )
    ).scalars().all()
    images = []
    for ii in img_rows:
        # 查足迹
        usages = (
            await session.execute(
                select(
                    ImageShopUsage.id.label("usage_id"),
                    Shop.name.label("shop_name"),
                    Shop.platform,
                    ImageShopUsage.note,
                )
                .join(Shop, ImageShopUsage.shop_id == Shop.id)
                .where(ImageShopUsage.image_file_id == ii.image_file_id)
            )
        ).all()
        images.append({
            "item_image_id": ii.id,
            "image_file_id": ii.image_file_id,
            "is_main": ii.is_main,
            "sort": ii.sort,
            "usages": [
                {
                    "usage_id": u.usage_id,
                    "shop_name": u.shop_name,
                    "platform": u.platform,
                    "note": u.note or "",
                }
                for u in usages
            ],
        })

    # 库存摘要已移至 /skus/stock 库存管理页（§16.5 M18 去库存）

    return {
        "id": item.id,
        "code": item.code,
        "name": item.name,
        "product_name": item.product_name,
        "product_code": item.product_code,
        "specs": item.specs or {},
        "kind": item.kind,
        "cost": float(item.cost) if item.cost is not None else None,
        "supplier": item.supplier or "",
        "status": item.status,
        "remark": item.remark or "",
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
        "bom": bom,
        "images": images,
    }


__all__ = ["list_items", "get_item_detail"]
