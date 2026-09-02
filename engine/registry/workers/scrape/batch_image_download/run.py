"""batch_image_download 工序 ACT（详设-v0.6 §5.2，新工序；2026-09-03 批 4 前修正：
体检内联本工序——下载时本地路径在手，一次写接口带回 width/height/watermark/source_mark，
避免中间产物回读；image_inspect 保留为独立工序供「重新体检」）。

纯代码工序（reason: none，无 LLM 调用，零 token 成本）：
逐链接：经 ctx.biz_client GET /api/biz/scrape/links/{id} 读 url/source/status →
按来源/域名路由 connector（xhs / xianyu / http_image，ctx.connectors 取；
缺失/不可用 → 链接 failed + error_note）→ 调用 connector（url, batch_id）取图 →
成功逐图 PIL 内联体检（宽高 + 水印启发式，照 image_inspect）→
POST /api/biz/scrape/images（link_record_id + url 幂等 409 忽略，
带 width/height/watermark/source_mark='scraped'/batch_id/source/desc/tags/
author_id/local_path/day_dir，status='downloaded'）→
PATCH /api/biz/scrape/links/{id}（status done/failed + image_count + desc/tags/
author_id + storage_dir + error_note + degraded_note）。

元数据降级标注（详设 §6.3）：connector ok=True 但 note 非空
（db-missing/no-title/no-seller-id/…）→ link_record.degraded_note 落库；
connector ok=False → link_record.status=failed + error_note=note。

输入：BatchDownloadInput{link_ids[], batch_id, from_queue}
输出：ScrapeBatchResult{links[], image_ids[], from_queue, batch_id, note}
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from engine.core.context import EngineContext
from models.workers import BatchDownloadInput, ScrapeBatchResult

logger = logging.getLogger(__name__)

# 水印启发式阈值（照 image_inspect 启发式；config/ 占位保留 R10 config_dir 契约）
_MIN_WIDTH = 800
_MIN_HEIGHT = 600
_WATERMARK_CORNER_RATIO = 0.15
_WATERMARK_STD_THRESHOLD = 40

# 来源 → connector id 路由（与 image_download 域名路由同源；其余 → http_image）
_SOURCE_CONNECTOR = {"xhs": "xhs", "xianyu": "xianyu", "http": "http_image"}


def _detect_source(url: str) -> str:
    """按域名自动识别来源（链接记录 source 缺失时兜底）。"""
    host = urlparse(url).hostname or ""
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "xhs"
    if "goofish.com" in host or "2.taobao.com" in host:
        return "xianyu"
    return "http"


def _connector_id_for(source: str, url: str) -> str:
    return _SOURCE_CONNECTOR.get(source) or _SOURCE_CONNECTOR.get(_detect_source(url), "http_image")


# ---- PIL 内联体检（照 image_inspect 启发式；宽高 + 水印右下角亮度方差）----


def _get_dimensions(img_path: Path) -> tuple[int | None, int | None]:
    try:
        from PIL import Image

        img = Image.open(str(img_path))
        return img.size  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return None, None


def _check_watermark(img_path: Path) -> bool:
    try:
        from PIL import Image

        img = Image.open(str(img_path))
        w, h = img.size
        right_margin = int(w * (1 - _WATERMARK_CORNER_RATIO))
        bottom_margin = int(h * (1 - _WATERMARK_CORNER_RATIO))
        crop = img.crop((right_margin, bottom_margin, w, h))
        gray = crop.convert("L")
        pixels = list(gray.getdata())
        if not pixels:
            return False
        mean = sum(pixels) / len(pixels)
        variance = sum((p - mean) ** 2 for p in pixels) / len(pixels)
        return variance**0.5 > _WATERMARK_STD_THRESHOLD
    except Exception:  # noqa: BLE001
        return False


def _inspect_path(path: Path) -> dict[str, Any]:
    """单图内联体检：宽高 + 水印（文件缺失不阻断，note 记）。"""
    if not path.exists():
        return {"width": None, "height": None, "watermark": False, "note": f"文件不存在: {path}"}
    width, height = _get_dimensions(path)
    watermark = _check_watermark(path) if width and height else False
    return {"width": width, "height": height, "watermark": watermark, "note": ""}


async def run(inputs: BatchDownloadInput, ctx: EngineContext) -> ScrapeBatchResult:
    """逐链接下载 → 内联体检 → 图片挂链接落库 → 更新链接记录（决策 26 全走接口）。"""
    biz_client = ctx.biz_client
    connectors = ctx.connectors if ctx.connectors else {}
    link_summaries: list[dict[str, Any]] = []
    image_ids: list[int] = []
    note_parts: list[str] = []

    if biz_client is None:
        logger.warning("batch_image_download: biz_client 未注入（CLI/测试），跳过下载链")
        return ScrapeBatchResult(
            links=[],
            image_ids=[],
            from_queue=bool(inputs.from_queue),
            batch_id=inputs.batch_id,
            note="biz_client 未注入（无法读写链接/图片接口）",
        )

    for link_id in inputs.link_ids:
        summary = await _process_link(
            link_id, inputs.batch_id, connectors, biz_client
        )
        link_summaries.append(summary)
        image_ids.extend(summary.get("image_ids") or [])
        if summary.get("note"):
            note_parts.append(summary["note"])

    return ScrapeBatchResult(
        links=link_summaries,
        image_ids=image_ids,
        from_queue=bool(inputs.from_queue),
        batch_id=inputs.batch_id,
        note="；".join(note_parts) if note_parts else "",
    )


async def _process_link(
    link_id: int, batch_id: str, connectors: dict[str, Any], biz_client: Any
) -> dict[str, Any]:
    """单链接：读信息 → 路由 connector → 下载 → 内联体检 → 图片落库 → 链接更新。"""
    base = {"link_id": link_id, "status": "failed", "image_ids": []}
    try:
        resp = await biz_client.get(f"/scrape/links/{link_id}")
    except Exception as exc:  # noqa: BLE001
        base["error_note"] = f"读链接记录失败：{exc}"
        return base
    if resp.status_code != 200:
        base["error_note"] = f"读链接记录 HTTP {resp.status_code}"
        return base
    detail = resp.json()
    link = detail.get("link") or {}
    url = str(link.get("url") or "")
    source = str(link.get("source") or "") or _detect_source(url)
    if not url:
        base["error_note"] = "链接记录缺 url"
        await _fail_link(biz_client, link_id, base["error_note"])
        return base

    # done 链接幂等跳过（定时重跑不重下，详设 §5.4）
    if link.get("status") == "done":
        base["status"] = "skipped"
        base["note"] = "already-done（幂等跳过不重下）"
        return base

    connector = connectors.get(_connector_id_for(source, url))
    if connector is None:
        base["error_note"] = f"connector '{source}' 未注入（ctx.connectors 无此 key）"
        await _fail_link(biz_client, link_id, base["error_note"])
        return base
    if not getattr(connector, "available", True):
        base["error_note"] = f"connector '{source}' 不可用（vendor 未配置或依赖未安装）"
        await _fail_link(biz_client, link_id, base["error_note"])
        return base

    try:
        result = await connector.download(url, batch_id=batch_id)
    except Exception as exc:  # noqa: BLE001
        base["error_note"] = f"connector '{source}' 调用异常: {exc}"
        await _fail_link(biz_client, link_id, base["error_note"])
        return base

    if not result.ok:
        # 失败透传（未落盘 / xsec_token 过期 / 失效页…）→ 链接 failed + error_note
        base["error_note"] = result.note or f"connector '{source}' 下载失败"
        await _fail_link(biz_client, link_id, base["error_note"])
        return base

    data = result.data or {}
    paths = [str(p) for p in (data.get("paths") or []) if str(p)]
    if not paths:
        base["error_note"] = f"connector '{source}' 未返回任何落盘路径"
        await _fail_link(biz_client, link_id, base["error_note"])
        return base

    # 成功：逐图内联体检 + 图片落库（挂链接，幂等 409 忽略）
    desc = str(data.get("desc") or "")
    tags = list(data.get("tags") or [])
    author_id = str(data.get("author_id") or "")
    day_dir = str(data.get("day_dir") or "")
    base["desc"] = desc
    base["tags"] = tags
    base["author_id"] = author_id

    created_ids: list[int] = []
    storage_dir = ""
    for idx, path_str in enumerate(paths):
        inspect = _inspect_path(Path(path_str))
        # 图片 url = 原始分享链接 + #img-<序号>（连接器只回本地路径无逐图源 URL；
        # 稳定可复现 → (link_record_id, url) 幂等键成立）
        img_url = f"{url}#img-{idx + 1}"
        row = await _save_image(
            biz_client,
            link_id=link_id,
            batch_id=batch_id,
            source=source,
            url=img_url,
            local_path=path_str,
            day_dir=day_dir,
            desc=desc,
            tags=tags,
            author_id=author_id,
            width=inspect.get("width"),
            height=inspect.get("height"),
            watermark=bool(inspect.get("watermark")),
            status="downloaded",
        )
        if row is not None and row.get("id") is not None:
            created_ids.append(int(row["id"]))
        if not storage_dir:
            storage_dir = str(Path(path_str).parent)

    # 元数据降级标注：connector ok=True 但 note 非空（db-missing/no-title/…）
    degraded_note = ""
    if result.note:
        degraded_note = str(result.note)

    # 更新链接记录（done + 图数 + 元数据 + storage_dir + degraded_note）
    patch: dict[str, Any] = {
        "status": "done",
        "image_count": len(created_ids),
        "desc": desc,
        "tags": tags,
        "author_id": author_id,
        "storage_dir": storage_dir,
        "degraded_note": degraded_note,
    }
    await _update_link(biz_client, link_id, patch)

    base["status"] = "done"
    base["image_ids"] = created_ids
    base["image_count"] = len(created_ids)
    if degraded_note:
        base["degraded_note"] = degraded_note
    if result.note:
        base["note"] = str(result.note)
    return base


async def _save_image(
    biz_client: Any,
    *,
    link_id: int,
    batch_id: str,
    source: str,
    url: str,
    local_path: str,
    day_dir: str,
    desc: str,
    tags: list[str],
    author_id: str,
    width: int | None,
    height: int | None,
    watermark: bool,
    status: str,
) -> dict[str, Any] | None:
    """POST /api/biz/scrape/images；link_record_id + url 幂等 409 忽略（返回 None）。"""
    try:
        resp = await biz_client.post(
            "/scrape/images",
            {
                "link_record_id": link_id,
                "batch_id": batch_id,
                "source": source,
                "url": url,
                "local_path": local_path,
                "day_dir": day_dir,
                "desc": desc,
                "tags": tags,
                "author_id": author_id,
                "width": width,
                "height": height,
                "watermark": watermark,
                "source_mark": "scraped",
                "status": status,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("batch_image_download: POST /scrape/images 失败：%s", exc)
        return None
    if resp.status_code == 409:
        return None  # 幂等防重：同 link_record_id + url 已存在，忽略
    if resp.status_code != 200:
        logger.warning(
            "batch_image_download: POST /scrape/images HTTP %s", resp.status_code
        )
        return None
    return resp.json()


async def _update_link(biz_client: Any, link_id: int, patch: dict[str, Any]) -> None:
    """PATCH /api/biz/scrape/links/{id}（终态写回）。"""
    try:
        resp = await biz_client.patch(f"/scrape/links/{link_id}", patch)
        if resp.status_code != 200:
            logger.warning(
                "batch_image_download: PATCH /scrape/links/%s HTTP %s",
                link_id, resp.status_code,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("batch_image_download: PATCH /scrape/links 失败：%s", exc)


async def _fail_link(biz_client: Any, link_id: int, error_note: str) -> None:
    """链接标 failed + error_note（connector 失败/不可用/读不到）。"""
    await _update_link(biz_client, link_id, {"status": "failed", "error_note": error_note})
