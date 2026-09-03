"""web/catalog_service.py：★建档服务层——业务校验唯一实现（详设-v0.7 §8.4）。

纯函数/异步函数风格，照 web/crm_store.py / web/settings_store.py 工程风格。
页面路由与 api_biz 共用同一份校验函数（R21/R26：人和 AI 同路）。

职责（详设 §8.4 清单 + §4 建档流程）：
- create_items：建档（多档同提交；每档校验 code/name/kind/combo 配方）→ 全部落库 active
- patch_item：编辑（code/cost/supplier/name/remark；kind 锁定不可改）
- transition_status：状态转换 active↔delisted
- delete_item：删除守卫（无下游引用才可删）
- add_bom_row / delete_bom_row：combo 配方行管理
- 校验失败抛 CatalogServiceError（中文 message）

命名说明：
- 本模块只做校验 + 业务逻辑；不直接创建 engine/session（由调用方注入 session）。
  与 crm_store.py（自管 engine）风格不同——catalog 域由 api_biz handler 传入 session，
  service 函数只负责校验+写入，职责更纯。
"""

from __future__ import annotations

import re
from typing import Sequence

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.catalog import Item, ItemBom, Stock, StockLedger, ItemImage, ImageShopUsage
from models.sys import Shop

# ---- 常量 ----

_CODE_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_VALID_KINDS = frozenset({"physical", "combo", "custom"})
_VALID_STATUSES = frozenset({"active", "delisted"})


class CatalogServiceError(Exception):
    """业务拒绝（路由捕获 -> 页面 err 提示 / API 返回 4xx/409）。"""


# ---- 校验辅助（§8.4 清单） ----


def _validate_code(code: str | None) -> str:
    """§8.4-1：编号必填 + 格式校验。"""
    if not code or not code.strip():
        raise CatalogServiceError("编号（code）必填")
    code = code.strip()
    if not _CODE_RE.match(code):
        raise CatalogServiceError(
            f"编号格式不合法「{code}」：仅允许字母/数字/下划线/连字符，1-40 字符"
        )
    return code


def _validate_name(name: str | None) -> str:
    """§8.4-2：name 非空 ≤120。"""
    if not name or not name.strip():
        raise CatalogServiceError("档名（name）必填")
    name = name.strip()
    if len(name) > 120:
        raise CatalogServiceError(f"档名超长（{len(name)} > 120）")
    return name


def _validate_kind(kind: str | None) -> str:
    """§8.4-2：kind ∈ physical/combo/custom。"""
    if not kind or kind not in _VALID_KINDS:
        raise CatalogServiceError(
            f"档案类型（kind）不合法「{kind}」，仅允许：physical / combo / custom"
        )
    return kind


async def _check_code_unique(session: AsyncSession, code: str, exclude_id: int = 0) -> None:
    """§8.4-1：全库查重。"""
    q = select(Item.id).where(Item.code == code)
    if exclude_id:
        q = q.where(Item.id != exclude_id)
    existing = (await session.execute(q)).scalar_one_or_none()
    if existing is not None:
        raise CatalogServiceError(f"编号「{code}」已被占用（409）")


async def _check_dup_name_in_product(
    session: AsyncSession,
    product_name: str | None,
    name: str,
    exclude_id: int = 0,
) -> None:
    """§8.4-2：同 product_name + 同 name 冲突 409。

    product_name 均为 NULL 时视为同组（同为空 = 同归类）。
    """
    q = select(Item.id).where(Item.name == name)
    if product_name:
        q = q.where(Item.product_name == product_name)
    else:
        q = q.where(Item.product_name.is_(None))
    if exclude_id:
        q = q.where(Item.id != exclude_id)
    existing = (await session.execute(q)).scalar_one_or_none()
    if existing is not None:
        pn_display = product_name or "(无商品名)"
        raise CatalogServiceError(
            f"同商品「{pn_display}」下已存在同名档案「{name}」（409）"
        )


# ---- BOM 校验辅助 ----


async def _validate_bom_rows(
    session: AsyncSession,
    parent_id: int,
    parent_kind: str,
    bom_rows: list[dict],
) -> None:
    """§8.4-3：combo 配方校验（parent.kind=combo, child.kind=physical, qty>0, 不重复, 不自引用）。"""
    if parent_kind != "combo":
        if bom_rows:
            raise CatalogServiceError("只有 combo 档案可以有配方行")
        return

    if not bom_rows:
        raise CatalogServiceError("combo 档案必须有至少一行配方（child 存在 + qty>0）")

    seen_child_ids: set[int] = set()
    for row in bom_rows:
        child_id = row.get("child_item_id")
        qty = row.get("qty", 1)

        if not child_id or child_id <= 0:
            raise CatalogServiceError("配方行子件 ID 无效")
        if child_id == parent_id:
            raise CatalogServiceError("配方行不能自引用")
        if child_id in seen_child_ids:
            raise CatalogServiceError(f"配方行子件重复（child_item_id={child_id}）")
        seen_child_ids.add(child_id)

        if not qty or float(qty) <= 0:
            raise CatalogServiceError(f"配方行数量必须大于 0（child={child_id}, qty={qty}）")

        # 子件必须存在且为 physical
        child = await session.get(Item, child_id)
        if child is None:
            raise CatalogServiceError(f"子件档案不存在（child_item_id={child_id}）")
        if child.kind != "physical":
            raise CatalogServiceError(
                f"配方行子件必须是 physical 实物档案，当前「{child.code}」类型为 {child.kind}"
            )


# ---- 建档（§4.1 多档同提交） ----


async def create_items(
    session: AsyncSession,
    *,
    product_name: str | None,
    rows: list[dict],
) -> list[int]:
    """建档：多档同提交，全部成功落库 active，任一失败整体回滚。

    每行 dict: {name, kind, code, cost?, supplier?, remark?, bom?[{child_item_id, qty}]}。

    §8.4 清单逐条执行：
    1. code 必填 + 格式 + 全库查重
    2. name 非空 ≤120；kind 三值；同 product_name+name 冲突
    3. combo 配方完整校验
    4. custom 无 BOM

    返回新建 item id 列表。
    """
    if not rows:
        raise CatalogServiceError("至少提供一行建档数据")

    # 预处理 product_name
    pn = product_name.strip() if product_name else None
    if pn and len(pn) > 120:
        raise CatalogServiceError(f"商品名超长（{len(pn)} > 120）")

    created_ids: list[int] = []

    # 收集本次提交内新增的 item（用于同批次内查重）
    # 先逐行校验，再逐行落库（事务内任一失败整体回滚）
    items_to_create: list[dict] = []

    for idx, row in enumerate(rows, 1):
        code = _validate_code(row.get("code"))
        name = _validate_name(row.get("name"))
        kind = _validate_kind(row.get("kind"))
        cost = row.get("cost")
        supplier = (row.get("supplier") or "").strip() or None
        remark = (row.get("remark") or "").strip() or ""
        bom_rows = row.get("bom") or []

        # custom 拒绝 BOM（§8.4-4）
        if kind == "custom" and bom_rows:
            raise CatalogServiceError(f"第 {idx} 行：custom 档案不允许有配方行")

        # 校验 BOM（§8.4-3）
        # 临时 item（用于自引用检查），parent_id=0（尚未入库）
        await _validate_bom_rows(session, 0, kind, bom_rows)

        items_to_create.append({
            "code": code,
            "name": name,
            "product_name": pn,
            "kind": kind,
            "cost": cost,
            "supplier": supplier,
            "remark": remark,
            "bom_rows": bom_rows,
        })

    # 第二遍：查重 + 落库（在事务内）
    # 先做全库查重
    for item_data in items_to_create:
        await _check_code_unique(session, item_data["code"])
        await _check_dup_name_in_product(session, item_data["product_name"], item_data["name"])

    # 落库
    for item_data in items_to_create:
        item = Item(
            code=item_data["code"],
            name=item_data["name"],
            product_name=item_data["product_name"],
            kind=item_data["kind"],
            cost=item_data["cost"],
            supplier=item_data["supplier"],
            remark=item_data["remark"],
            status="active",
        )
        session.add(item)
        await session.flush()  # 获取 id

        # combo 写 BOM
        if item_data["kind"] == "combo" and item_data["bom_rows"]:
            for bom_row in item_data["bom_rows"]:
                session.add(
                    ItemBom(
                        parent_item_id=item.id,
                        child_item_id=bom_row["child_item_id"],
                        qty=bom_row.get("qty", 1),
                    )
                )

        created_ids.append(item.id)

    return created_ids


# ---- 编辑（§8.1 PATCH items/{iid}） ----


async def patch_item(
    session: AsyncSession,
    item_id: int,
    *,
    code: str | None = None,
    cost: float | None = None,
    supplier: str | None = None,
    name: str | None = None,
    remark: str | None = None,
) -> None:
    """编辑档案字段（kind 锁定不可改——详设 §4.3）。

    code 变更需重新校验格式 + 查重。
    """
    item = await session.get(Item, item_id)
    if item is None:
        raise CatalogServiceError("档案不存在")

    if code is not None:
        code = _validate_code(code)
        await _check_code_unique(session, code, exclude_id=item_id)
        item.code = code

    if name is not None:
        name = _validate_name(name)
        # 改名需检查同商品名下是否冲突
        await _check_dup_name_in_product(
            session, item.product_name, name, exclude_id=item_id
        )
        item.name = name

    if cost is not None:
        item.cost = cost

    if supplier is not None:
        item.supplier = supplier.strip() or None

    if remark is not None:
        item.remark = remark.strip() or ""


# ---- 状态机（§8.1 POST items/{iid}/status） ----


async def transition_status(
    session: AsyncSession,
    item_id: int,
    *,
    to: str,
) -> None:
    """状态转换 active↔delisted（§4.5）。"""
    if to not in _VALID_STATUSES:
        raise CatalogServiceError(f"目标状态不合法「{to}」，仅允许：active / delisted")

    item = await session.get(Item, item_id)
    if item is None:
        raise CatalogServiceError("档案不存在")

    if item.status == to:
        return  # 幂等

    if item.status not in _VALID_STATUSES:
        raise CatalogServiceError(f"当前状态「{item.status}」不可转换")

    item.status = to


# ---- 删除守卫（§8.1 DELETE items/{iid}） ----


async def delete_item(session: AsyncSession, item_id: int) -> None:
    """删除档案（无下游引用才可删；有数据只能 delisted）。

    下游引用检查（§4.5 + §8.4-8）：
    - stock 行（库存）
    - stock_ledger 行（流水）
    - item_bom 父子引用
    - item_image 挂图
    - image_shop_usage 足迹
    """
    item = await session.get(Item, item_id)
    if item is None:
        raise CatalogServiceError("档案不存在")

    # 检查下游引用
    reasons: list[str] = []

    # stock 行
    stock_count = (
        await session.execute(
            select(func.count()).select_from(Stock).where(Stock.item_id == item_id)
        )
    ).scalar_one() or 0
    if stock_count > 0:
        reasons.append(f"有 {stock_count} 条库存记录")

    # stock_ledger 流水
    ledger_count = (
        await session.execute(
            select(func.count()).select_from(StockLedger).where(StockLedger.item_id == item_id)
        )
    ).scalar_one() or 0
    if ledger_count > 0:
        reasons.append(f"有 {ledger_count} 条库存流水")

    # item_bom 作为 parent
    bom_parent_count = (
        await session.execute(
            select(func.count()).select_from(ItemBom).where(ItemBom.parent_item_id == item_id)
        )
    ).scalar_one() or 0
    if bom_parent_count > 0:
        reasons.append(f"作为 combo 父件有 {bom_parent_count} 条配方行")

    # item_bom 作为 child（被引用）
    bom_child_count = (
        await session.execute(
            select(func.count()).select_from(ItemBom).where(ItemBom.child_item_id == item_id)
        )
    ).scalar_one() or 0
    if bom_child_count > 0:
        reasons.append(f"作为子件被 {bom_child_count} 条配方行引用")

    # item_image 挂图
    img_count = (
        await session.execute(
            select(func.count()).select_from(ItemImage).where(ItemImage.item_id == item_id)
        )
    ).scalar_one() or 0
    if img_count > 0:
        reasons.append(f"有 {img_count} 张档案图")

    # image_shop_usage：需要通过 item_image 关联检查
    # 检查该 item 挂的图是否在 image_shop_usage 中有足迹
    usage_count = (
        await session.execute(
            text(
                "SELECT COUNT(*) FROM catalog.image_shop_usage u "
                "JOIN catalog.item_image ii ON u.image_file_id = ii.image_file_id "
                "WHERE ii.item_id = :item_id"
            ),
            {"item_id": item_id},
        )
    ).scalar_one() or 0
    if usage_count > 0:
        reasons.append(f"关联图有 {usage_count} 条使用足迹")

    if reasons:
        detail = "、".join(reasons)
        raise CatalogServiceError(
            f"该档案有下游数据引用，不可删除，请先下架保留档案（409）：{detail}"
        )

    await session.delete(item)


# ---- BOM 配方行管理（§8.1 POST/DELETE items/{iid}/bom） ----


async def add_bom_row(
    session: AsyncSession,
    item_id: int,
    *,
    child_item_id: int,
    qty: float = 1,
) -> int:
    """为 combo 档案添加配方行。

    校验（§8.4-3）：
    - parent.kind=combo
    - child.kind=physical
    - qty>0
    - 不重复（UNIQUE(parent, child)）
    - 不自引用
    - active 档禁改 BOM（改配方 = 先 delisted）

    返回新建 item_bom.id。
    """
    item = await session.get(Item, item_id)
    if item is None:
        raise CatalogServiceError("档案不存在")

    if item.kind != "combo":
        raise CatalogServiceError("只有 combo 档案可以添加配方行")

    if item.status == "active":
        raise CatalogServiceError("active 状态的档案不可修改配方，请先下架（delisted）")

    # 子件校验
    if child_item_id == item_id:
        raise CatalogServiceError("配方行不能自引用")

    child = await session.get(Item, child_item_id)
    if child is None:
        raise CatalogServiceError(f"子件档案不存在（child_item_id={child_item_id}）")

    if child.kind != "physical":
        raise CatalogServiceError(
            f"配方行子件必须是 physical 实物档案，当前「{child.code}」类型为 {child.kind}"
        )

    if not qty or float(qty) <= 0:
        raise CatalogServiceError(f"配方行数量必须大于 0（qty={qty}）")

    # 检查重复
    existing_bom = (
        await session.execute(
            select(ItemBom.id).where(
                ItemBom.parent_item_id == item_id,
                ItemBom.child_item_id == child_item_id,
            )
        )
    ).scalar_one_or_none()
    if existing_bom is not None:
        raise CatalogServiceError(
            f"配方行重复：子件「{child.code}」已在该档案配方中"
        )

    bom = ItemBom(
        parent_item_id=item_id,
        child_item_id=child_item_id,
        qty=qty,
    )
    session.add(bom)
    await session.flush()
    return bom.id


async def delete_bom_row(
    session: AsyncSession,
    item_id: int,
    row_id: int,
) -> None:
    """删除 combo 配方行。

    active 档禁改 BOM（§8.4-8；改配方 = 先 delisted）。
    """
    item = await session.get(Item, item_id)
    if item is None:
        raise CatalogServiceError("档案不存在")

    if item.status == "active":
        raise CatalogServiceError("active 状态的档案不可修改配方，请先下架（delisted）")

    bom = await session.get(ItemBom, row_id)
    if bom is None or bom.parent_item_id != item_id:
        raise CatalogServiceError("配方行不存在或不属于该档案")

    await session.delete(bom)


__all__ = [
    "CatalogServiceError",
    "create_items",
    "patch_item",
    "transition_status",
    "delete_item",
    "add_bom_row",
    "delete_bom_row",
]
