"""scrape_store — 扒图 DAO（P3-2：零 engine import）。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text

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
                    (batch_id, source, url, local_path, day_dir, desc, tags,
                     author_id, width, height, watermark, status)
                VALUES
                    (:batch_id, :source, :url, :local_path, :day_dir, :desc,
                     :tags::jsonb, :author_id, :width, :height, :watermark, :status)
                RETURNING id, batch_id, source, url, local_path, day_dir, desc,
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
                SELECT id, batch_id, source, url, local_path, day_dir, desc,
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
                SELECT id, batch_id, source, url, local_path, day_dir, desc,
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
        sets.append("desc = :desc")
        params["desc"] = desc
    if tags is not None:
        sets.append("tags = :tags::jsonb")
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
                SELECT id, batch_id, source, url, local_path, day_dir, desc,
                       tags, author_id, width, height, watermark, status, created_at
                FROM scrape.image_file
                WHERE id IN ({placeholders})
                ORDER BY created_at DESC
                """
            ),
            params,
        )
        return _rows(result)
