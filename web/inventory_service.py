"""web/inventory_service.py：★库存业务校验唯一实现（详设-v0.7 §5 + §8.2/§8.4）。

风格照 web/catalog_service.py：
- 纯函数/异步函数，session 由调用方注入
- 校验失败抛 InventoryServiceError（中文 message）
- write_ledger 是唯一库存变动入口（§5.1/§5.2 事务记账）

职责（详设 §8.4 清单第 5 条 + §8.2 inventory 组）：
- warehouse：list/create（name 唯一 1..60）
- stock：list（按 item/warehouse 过滤）
- ledger.write_ledger：事务记账（读→校验→快照→写 stock→写 ledger）
"""

from __future__ import annotations

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.catalog import (
    Item,
    Stock,
    StockLedger,
    Warehouse,
)

# ---- 常量 ----

_VALID_CHANGE_TYPES = frozenset({"sale", "purchase", "loss", "adjust", "return"})

# ---- 错误 ----


class InventoryServiceError(Exception):
    """业务拒绝（路由捕获 -> 页面 err 提示 / API 返回 4xx/409）。"""


# ---- 仓库（§8.2 warehouses）----


async def list_warehouses(session: AsyncSession) -> list[Warehouse]:
    """返回全部仓库（含 enabled 过滤）。"""
    rows = (
        await session.execute(
            select(Warehouse).where(Warehouse.enabled == True).order_by(Warehouse.id)  # noqa: E712
        )
    ).scalars().all()
    return list(rows)


async def create_warehouse(
    session: AsyncSession, *, name: str, remark: str = ""
) -> int:
    """建仓（name 唯一 1..60）。返回新建 warehouse.id。"""
    if not name or not name.strip():
        raise InventoryServiceError("仓库名（name）必填")
    name = name.strip()
    if len(name) > 60:
        raise InventoryServiceError(f"仓库名超长（{len(name)} > 60）")

    # 查重
    existing = (
        await session.execute(
            select(Warehouse.id).where(Warehouse.name == name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise InventoryServiceError(f"仓库名「{name}」已被占用（409）")

    wh = Warehouse(name=name, remark=remark.strip() or "")
    session.add(wh)
    await session.flush()
    return wh.id


# ---- 库存查询（§8.2 stocks）----


async def list_stocks(
    session: AsyncSession,
    *,
    item_id: int | None = None,
    warehouse_id: int | None = None,
) -> list[dict]:
    """库存查询（按 item/warehouse 过滤）。"""
    q = select(Stock)
    if item_id is not None:
        q = q.where(Stock.item_id == item_id)
    if warehouse_id is not None:
        q = q.where(Stock.warehouse_id == warehouse_id)
    q = q.order_by(Stock.item_id, Stock.warehouse_id)
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": s.id,
            "item_id": s.item_id,
            "warehouse_id": s.warehouse_id,
            "qty": float(s.qty),
            "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        }
        for s in rows
    ]


# ---- 流水查询（§8.2 ledgers）----


async def list_ledgers(
    session: AsyncSession,
    *,
    item_id: int | None = None,
    warehouse_id: int | None = None,
    change_type: str | None = None,
) -> list[dict]:
    """流水查询（按 item/warehouse/type 过滤）。"""
    q = select(StockLedger)
    if item_id is not None:
        q = q.where(StockLedger.item_id == item_id)
    if warehouse_id is not None:
        q = q.where(StockLedger.warehouse_id == warehouse_id)
    if change_type is not None:
        q = q.where(StockLedger.change_type == change_type)
    q = q.order_by(StockLedger.created_at.desc(), StockLedger.id.desc())
    rows = (await session.execute(q)).scalars().all()
    return [
        {
            "id": l.id,
            "item_id": l.item_id,
            "warehouse_id": l.warehouse_id,
            "change_type": l.change_type,
            "qty_delta": float(l.qty_delta),
            "before_qty": float(l.before_qty) if l.before_qty is not None else None,
            "after_qty": float(l.after_qty) if l.after_qty is not None else None,
            "ref_type": l.ref_type,
            "ref_id": l.ref_id,
            "operator": l.operator,
            "note": l.note,
            "created_at": l.created_at.isoformat() if l.created_at else None,
        }
        for l in rows
    ]


# ---- 唯一库存变动入口 write_ledger（§5.1/§5.2/§8.2）----


async def write_ledger(
    session: AsyncSession,
    *,
    item_id: int,
    warehouse_id: int,
    change_type: str,
    qty: float,
    ref_type: str | None = None,
    ref_id: int | None = None,
    note: str = "",
    operator: str = "web",
) -> dict:
    """唯一库存变动入口（§5.1/§5.2）：同事务 读当前 qty → 校验 → 快照 before/after → 写 stock → 写 ledger 行。

    校验（§8.4 第 5 条）：
    1. item 必须存在
    2. item.kind 必须为 physical（combo/custom → 409）
    3. warehouse 必须存在
    4. qty > 0
    5. change_type ∈ 五类（sale/purchase/loss/adjust/return）
    6. sale → 501 NotImplemented（v0.7 无订单回流）
    7. 变动后 stock qty ≥ 0（超扣 → 4xx 且不写任何流水——事务回滚）
    8. stock 行不存在时首次变动自动建行（qty 从 0 起，§5.1）

    返回 {stock_id, ledger_id, before_qty, after_qty}。
    """
    # 1. item 存在性
    item = await session.get(Item, item_id)
    if item is None:
        raise InventoryServiceError("档案不存在")

    # 2. kind = physical（combo/custom → 409）
    if item.kind != "physical":
        raise InventoryServiceError(
            f"只有 physical 实物档案可以记库存，当前类型为 {item.kind}（409）"
        )

    # 3. warehouse 存在性
    wh = await session.get(Warehouse, warehouse_id)
    if wh is None:
        raise InventoryServiceError("仓库不存在")

    # 4. qty > 0
    if not qty or float(qty) <= 0:
        raise InventoryServiceError(f"变动数量必须大于 0（qty={qty}）")

    # 5. change_type ∈ 五类
    if change_type not in _VALID_CHANGE_TYPES:
        raise InventoryServiceError(
            f"变动类型不合法「{change_type}」，仅允许：sale/purchase/loss/adjust/return"
        )

    # 6. sale → 501
    if change_type == "sale":
        raise InventoryServiceError("销售扣减（sale）v0.7 暂不支持，需 v0.9 订单回流（501）")

    # 计算 delta（正入负出）
    if change_type in ("purchase", "return"):
        delta = float(qty)  # 入
    else:
        delta = -float(qty)  # loss/adjust 出

    # 7. 读当前 stock 行
    stock_row = (
        await session.execute(
            select(Stock).where(
                Stock.item_id == item_id,
                Stock.warehouse_id == warehouse_id,
            )
        )
    ).scalar_one_or_none()

    if stock_row is not None:
        before_qty = float(stock_row.qty)
    else:
        before_qty = 0.0

    after_qty = before_qty + delta

    # 7. 超扣检查（变动后 qty ≥ 0）
    if after_qty < 0:
        raise InventoryServiceError(
            f"库存不足：当前 {before_qty}，变动 {delta}，结果 {after_qty}（超扣拦截）"
        )

    # 8. stock 行不存在时自动建行
    if stock_row is None:
        stock_row = Stock(
            item_id=item_id,
            warehouse_id=warehouse_id,
            qty=0,
        )
        session.add(stock_row)
        await session.flush()  # 获取 id

    # 更新 stock
    stock_row.qty = after_qty

    # 写 ledger 行
    ledger = StockLedger(
        item_id=item_id,
        warehouse_id=warehouse_id,
        change_type=change_type,
        qty_delta=delta,
        before_qty=before_qty,
        after_qty=after_qty,
        ref_type=ref_type,
        ref_id=ref_id,
        operator=operator,
        note=note,
    )
    session.add(ledger)
    await session.flush()

    return {
        "stock_id": stock_row.id,
        "ledger_id": ledger.id,
        "before_qty": before_qty,
        "after_qty": after_qty,
    }


__all__ = [
    "InventoryServiceError",
    "list_warehouses",
    "create_warehouse",
    "list_stocks",
    "list_ledgers",
    "write_ledger",
]
