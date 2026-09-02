"""scrape_store — 扒图 DAO（P3-2：零 engine import）。

v0.6 批 1 扩展（详设-v0.6 §4.3）：链接记录 CRUD（create_link 幂等 / get_links /
get_link_by_id / update_link）+ 定时队列读写（get_link_queue / set_link_queue）
+ normalize_link_url（去易变 query 查重键）。存量 desc 列在原始 SQL 中补引号
（PG16 实测未加引号的 desc 列名必语法错误，2026-09-03 批 1 修复留痕）。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from web.db import get_db_session


def _rows(result) -> list[dict[str, Any]]:
    """Convert result to list of dicts."""
    return [dict(r._mapping) for r in result] if result else []


async def create_image_file(
    batch_id: str,
    source: str,
    url: str,
    local_path: str | None = None,
    day_dir: str | None = None,
    desc: str | None = None,
    tags: list[str] | None = None,
    author_id: str | None = None,
    width: int | None = None,
    height: int | None = None,
    watermark: bool = False,
    status: str = "pending",
) -> dict[str, Any]:
    """Create image_file record. Returns the created row."""
    import json as _json
    async with get_db_session() as session:
        result = await session.execute(
            text(
                """
                INSERT INTO scrape.image_file
                    (batch_id, source, url, local_path, day_dir, "desc", tags,
                     author_id, width, height, watermark, status)
                VALUES
                    (:batch_id, :source, :url, :local_path, :day_dir, :desc,
                     CAST(:tags AS jsonb), :author_id, :width, :height, :watermark, :status)
                RETURNING id, batch_id, source, url, local_path, day_dir, "desc",
                          tags, author_id, width, height, watermark, status, created_at
                """
            ),
            {
                "batch_id": batch_id,
                "source": source,
                "url": url,
                "local_path": local_path,
                "day_dir": day_dir,
                "desc": desc,
                "tags": _json.dumps(tags or []),
                "author_id": author_id,
                "width": width,
                "height": height,
                "watermark": watermark,
                "status": status,
            },
        )
        await session.commit()
        row = result.mappings().first()
        return dict(row) if row else {}


async def get_images(
    batch_id: str | None = None,
    source: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List images with optional filters."""
    conditions = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if batch_id:
        conditions.append("batch_id = :batch_id")
        params["batch_id"] = batch_id
    if source:
        conditions.append("source = :source")
        params["source"] = source
    if status:
        conditions.append("status = :status")
        params["status"] = status

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    async with get_db_session() as session:
        result = await session.execute(
            text(
                f"""
                SELECT id, batch_id, source, url, local_path, day_dir, "desc",
                       tags, author_id, width, height, watermark, status, created_at
                FROM scrape.image_file
                {where}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        return _rows(result)


async def get_image_by_id(image_id: str) -> dict[str, Any] | None:
    """Get single image by id."""
    async with get_db_session() as session:
        result = await session.execute(
            text(
                """
                SELECT id, batch_id, source, url, local_path, day_dir, "desc",
                       tags, author_id, width, height, watermark, status, created_at
                FROM scrape.image_file
                WHERE id = :id
                """
            ),
            {"id": image_id},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def update_image(
    image_id: str,
    *,
    width: int | None = None,
    height: int | None = None,
    watermark: bool | None = None,
    status: str | None = None,
    local_path: str | None = None,
    desc: str | None = None,
    tags: list[str] | None = None,
    author_id: str | None = None,
) -> dict[str, Any] | None:
    """Update image fields (only non-None values)."""
    import json as _json

    sets = []
    params: dict[str, Any] = {"id": image_id}

    if width is not None:
        sets.append("width = :width")
        params["width"] = width
    if height is not None:
        sets.append("height = :height")
        params["height"] = height
    if watermark is not None:
        sets.append("watermark = :watermark")
        params["watermark"] = watermark
    if status is not None:
        sets.append("status = :status")
        params["status"] = status
    if local_path is not None:
        sets.append("local_path = :local_path")
        params["local_path"] = local_path
    if desc is not None:
        sets.append('"desc" = :desc')
        params["desc"] = desc
    if tags is not None:
        sets.append("tags = CAST(:tags AS jsonb)")
        params["tags"] = _json.dumps(tags)
    if author_id is not None:
        sets.append("author_id = :author_id")
        params["author_id"] = author_id

    if not sets:
        return await get_image_by_id(image_id)

    async with get_db_session() as session:
        await session.execute(
            text(
                f"""
                UPDATE scrape.image_file
                SET {', '.join(sets)}
                WHERE id = :id
                """
            ),
            params,
        )
        await session.commit()
    return await get_image_by_id(image_id)


async def check_batch_idempotent(batch_id: str) -> bool:
    """Check if batch_id already exists (for idempotent POST)."""
    async with get_db_session() as session:
        result = await session.execute(
            text(
                """
                SELECT EXISTS(
                    SELECT 1 FROM scrape.image_file WHERE batch_id = :batch_id LIMIT 1
                ) AS exists_
                """
            ),
            {"batch_id": batch_id},
        )
        row = result.first()
        return row[0] if row else False


_SOURCE_BADGE = {"xhs": "📕", "xianyu": "🐟", "crm": "💬"}


async def get_batches() -> list[dict[str, Any]]:
    """List distinct batches with summary stats."""
    async with get_db_session() as session:
        result = await session.execute(
            text(
                """
                SELECT
                    batch_id,
                    MIN(source) AS source,
                    COUNT(*) AS image_count,
                    MIN(created_at) AS created_at,
                    SUM(CASE WHEN status = 'downloaded' THEN 1 ELSE 0 END) AS downloaded_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                FROM scrape.image_file
                GROUP BY batch_id
                ORDER BY MIN(created_at) DESC
                """
            )
        )
        rows = _rows(result)
        for r in rows:
            src = r.get("source", "")
            r["id"] = r["batch_id"]
            r["name"] = r["batch_id"][:8] + "…"
            r["source_badge"] = _SOURCE_BADGE.get(src, "🔗")
            dl = r.get("downloaded_count") or 0
            fc = r.get("failed_count") or 0
            ic = r.get("image_count") or 0
            if dl == ic and ic > 0:
                r["status_label"] = "已完成"
                r["status_badge"] = "bg-green-100 text-green-700"
            elif fc > 0:
                r["status_label"] = "部分失败"
                r["status_badge"] = "bg-orange-100 text-orange-700"
            else:
                r["status_label"] = "处理中"
                r["status_badge"] = "bg-yellow-100 text-yellow-700"
            r["pending_proposal_count"] = 0
        return rows


async def get_images_by_ids(image_ids: list[str]) -> list[dict[str, Any]]:
    """Get multiple images by IDs."""
    if not image_ids:
        return []
    placeholders = ", ".join([f":id_{i}" for i in range(len(image_ids))])
    params = {f"id_{i}": mid for i, mid in enumerate(image_ids)}

    async with get_db_session() as session:
        result = await session.execute(
            text(
                f"""
                SELECT id, batch_id, source, url, local_path, day_dir, "desc",
                       tags, author_id, width, height, watermark, status, created_at
                FROM scrape.image_file
                WHERE id IN ({placeholders})
                ORDER BY created_at DESC
                """
            ),
            params,
        )
        return _rows(result)


# =====================================================================
# v0.6 批 1：链接记录 + 定时队列（详设-v0.6 §4.1/§4.3/§4.4）
# =====================================================================

# 易变 query 参数（小写比较）：XHS 分享链接的 token/来源参数，规范化时去掉
_VOLATILE_QUERY_PARAMS = ("xsec_token", "xsec_source", "xsec_token_f")

# 链接记录全列（desc 是 PG 保留字，原始 SQL 一律加引号）
_LINK_COLUMNS = (
    'id, url, normalized_url, source, status, image_count, "desc", tags, '
    "author_id, batch_id, storage_dir, error_note, degraded_note, "
    "created_at, updated_at"
)


def normalize_link_url(url: str) -> str:
    """规范化分享链接（查重键）：去掉易变 query（xsec_token 等），保留路径段。

    详设-v0.6 §4.1：同作品不同 xsec_token（token 过期重取）不重复建记录。
    去掉 xsec_token / xsec_source / xsec_token_f；其余 query 保留原值。
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    keep = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _VOLATILE_QUERY_PARAMS
    ]
    query = urlencode(keep) if keep else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


async def _get_link_by_normalized(normalized_url: str) -> dict[str, Any] | None:
    """按规范化链接查现有行（幂等查重）。"""
    async with get_db_session() as session:
        result = await session.execute(
            text(
                f"SELECT {_LINK_COLUMNS} FROM scrape.link_record "
                "WHERE normalized_url = :n"
            ),
            {"n": normalized_url},
        )
        row = result.mappings().first()
        return dict(row) if row else None


async def create_link(
    url: str,
    source: str,
    batch_id: str,
    normalized_url: str | None = None,
    desc: str = "",
    tags: list[str] | None = None,
    author_id: str | None = None,
    storage_dir: str | None = None,
) -> dict[str, Any]:
    """建链接记录（normalized_url 幂等：已存在返回现有行，不重复建）。

    未传 normalized_url 时内部规范化（去 xsec_token 等易变 query，保留路径段）。
    并发竞态由 uq_scrape_link_record_normalized_url 唯一约束兜底：
    IntegrityError → 回查现有行返回（created=false）。
    返回：全部列 + created/existing 标记。
    """
    import json as _json

    norm = normalized_url or normalize_link_url(url)
    existing = await _get_link_by_normalized(norm)
    if existing is not None:
        return {**existing, "created": False, "existing": True}

    try:
        async with get_db_session() as session:
            result = await session.execute(
                text(
                    f"""
                    INSERT INTO scrape.link_record
                        (url, normalized_url, source, status, image_count, "desc",
                         tags, author_id, batch_id, storage_dir)
                    VALUES
                        (:url, :normalized_url, :source, 'pending', 0, :desc,
                         CAST(:tags AS jsonb), :author_id, :batch_id, :storage_dir)
                    RETURNING {_LINK_COLUMNS}
                    """
                ),
                {
                    "url": url,
                    "normalized_url": norm,
                    "source": source,
                    "desc": desc,
                    "tags": _json.dumps(tags or []),
                    "author_id": author_id,
                    "batch_id": batch_id,
                    "storage_dir": storage_dir,
                },
            )
            await session.commit()
            row = result.mappings().first()
            return {**dict(row), "created": True, "existing": False}
    except IntegrityError:
        # 并发竞态：同 normalized_url 已被其他调用建好 → 返回现有行
        existing = await _get_link_by_normalized(norm)
        if existing is None:
            raise
        return {**existing, "created": False, "existing": True}


async def get_links(
    source: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """链接记录列表（来源/状态筛选 + 分页，照 get_images 模式）。"""
    conditions = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if source:
        conditions.append("source = :source")
        params["source"] = source
    if status:
        conditions.append("status = :status")
        params["status"] = status

    where = (" WHERE " + " AND ".join(conditions)) if conditions else ""

    async with get_db_session() as session:
        result = await session.execute(
            text(
                f"""
                SELECT {_LINK_COLUMNS}
                FROM scrape.link_record
                {where}
                ORDER BY created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        )
        return _rows(result)


async def get_link_by_id(link_id: str | int) -> dict[str, Any] | None:
    """单条链接（含该链接图片列表，返回 {"link": ..., "images": [...]}）。"""
    async with get_db_session() as session:
        result = await session.execute(
            text(
                f"SELECT {_LINK_COLUMNS} FROM scrape.link_record WHERE id = :id"
            ),
            {"id": link_id},
        )
        row = result.mappings().first()
        if row is None:
            return None
        link = dict(row)
        imgs = await session.execute(
            text(
                """
                SELECT id, batch_id, source, url, link_record_id, source_mark,
                       sku_id, shop_id, local_path, day_dir, "desc", tags,
                       author_id, width, height, watermark, status, created_at
                FROM scrape.image_file
                WHERE link_record_id = :id
                ORDER BY created_at DESC, id ASC
                """
            ),
            {"id": link_id},
        )
        return {"link": link, "images": _rows(imgs)}


async def update_link(
    link_id: str | int,
    *,
    status: str | None = None,
    image_count: int | None = None,
    desc: str | None = None,
    tags: list[str] | None = None,
    author_id: str | None = None,
    storage_dir: str | None = None,
    error_note: str | None = None,
    degraded_note: str | None = None,
) -> dict[str, Any] | None:
    """更新链接记录（只更新非 None 字段；updated_at 置 now；照 update_image 模式）。

    返回更新后整行（含全部列）；行不存在返回 None。
    """
    import json as _json

    sets = []
    params: dict[str, Any] = {"id": link_id}

    if status is not None:
        sets.append("status = :status")
        params["status"] = status
    if image_count is not None:
        sets.append("image_count = :image_count")
        params["image_count"] = image_count
    if desc is not None:
        sets.append('"desc" = :desc')
        params["desc"] = desc
    if tags is not None:
        sets.append("tags = CAST(:tags AS jsonb)")
        params["tags"] = _json.dumps(tags)
    if author_id is not None:
        sets.append("author_id = :author_id")
        params["author_id"] = author_id
    if storage_dir is not None:
        sets.append("storage_dir = :storage_dir")
        params["storage_dir"] = storage_dir
    if error_note is not None:
        sets.append("error_note = :error_note")
        params["error_note"] = error_note
    if degraded_note is not None:
        sets.append("degraded_note = :degraded_note")
        params["degraded_note"] = degraded_note

    if not sets:
        link = await get_link_by_id(link_id)
        return link["link"] if link else None

    async with get_db_session() as session:
        await session.execute(
            text(
                f"""
                UPDATE scrape.link_record
                SET {', '.join(sets)}, updated_at = now()
                WHERE id = :id
                """
            ),
            params,
        )
        await session.commit()
    link = await get_link_by_id(link_id)
    return link["link"] if link else None


def _settings_store() -> Any:
    """惰性构建 SettingsStore（共享 web.db 惰性引擎，函数式 DAO 风格；R24 零 engine import）。"""
    from web.db import get_engine
    from web.settings_store import SettingsStore

    return SettingsStore(get_engine())


async def get_link_queue(settings: Any = None) -> list[str]:
    """读定时队列（设置键 scrape.link_queue，json 数组，回退 []）。

    settings 可显式注入（测试用嵌入式 PG）；缺省用 web.db 惰性引擎。
    """
    store = settings if settings is not None else _settings_store()
    value = await store.get("scrape.link_queue", [])
    if not isinstance(value, list):
        return []
    return [str(u) for u in value]


async def set_link_queue(urls: list[str], settings: Any = None) -> None:
    """写定时队列（设置键 scrape.link_queue，存 JSON 数组字符串）。"""
    import json as _json

    store = settings if settings is not None else _settings_store()
    await store.set("scrape.link_queue", _json.dumps(list(urls)), "扒图定时队列")
