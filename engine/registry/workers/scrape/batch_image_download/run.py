"""batch_image_download 工序 ACT（详设-v0.6 §5.2 + §15.1 复核反馈修订，批 6 归集改造）。

纯代码工序（reason: none，无 LLM 调用，零 token 成本）：
逐链接：经 ctx.biz_client GET /api/biz/scrape/links/{id} 读 url/source/status →
按来源/域名路由 connector（xhs / xianyu / http_image，ctx.connectors 取；
缺失/不可用 → 链接 failed + error_note）→ 调用 connector（url, batch_id）取图 →
**归集（详设 §15.1，批 6）**：connector 下载产物移动到该链接的专属文件夹
`{scrape.storage_dir}/{source}/{folder_key}/`（folder_key = 来源规范化短键：
xhs=note id（url 路径段）/ xianyu=item id（url query id）/ http=url md5 前 8 位，
取不到回退 `link_{link_record_id}`；文件按原顺序重命名为 01.xxx/02.xxx…
保序，真实扩展名保留）→ connector 中转目录清理（删空中转目录；xhs 的
ExploreData.db 所在 Download 目录含库文件不空 → 自动保留）→ 文件夹内写
`meta.txt`（人可读文本，清单见 §15.1：标题/来源/原链接/作者ID/描述/标签/
图片数/爬取时间/批次/网盘分享链接（本批未上传）/备注）→
逐图 PIL 内联体检（宽高 + 水印启发式）→
POST /api/biz/scrape/images（link_record_id + url 幂等 409 忽略，
带 width/height/watermark/source_mark='scraped'/batch_id/source/desc/tags/
author_id/local_path（**相对 scrape.storage_dir**）/day_dir，status='downloaded'）→
PATCH /api/biz/scrape/links/{id}（status done + image_count + desc/tags/author_id
+ storage_dir（链接文件夹相对路径）+ error_note + degraded_note）。

元数据降级标注（详设 §6.3）：connector ok=True 但 note 非空
（db-missing/no-title/no-seller-id/…）→ link_record.degraded_note 落库 +
meta.txt 备注行；connector ok=False → link_record.status=failed + error_note
（无文件夹产物）。ok=True 但无图 → 同 failed（未落盘）。

storage_dir 来源（详设 §15.1 技术定，批 6）：EngineContext.storage_dir——
server/runner 装配时从 engine-params（scrape.storage_dir）注入（与 connector
落盘根同源）；CLI/测试未注入（None）→ 链接 failed + error_note，不产生半成品。

输入：BatchDownloadInput{link_ids[], batch_id, from_queue}
输出：ScrapeBatchResult{links[], image_ids[], from_queue, batch_id, note}
"""

from __future__ import annotations

import hashlib
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

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

# meta.txt 来源行中文标注（§15.1：xhs（小红书）｜ xianyu（闲鱼）｜ http）
_SOURCE_META_LABEL = {"xhs": "xhs（小红书）", "xianyu": "xianyu（闲鱼）", "http": "http"}

# xhs note id：xiaohongshu.com/(discovery/item|explore|item)/<hex> 路径段
_XHS_NOTE_ID_RE = re.compile(
    r"xiaohongshu\.com/(?:discovery/item|explore|item)/([a-z0-9]+)",
    re.IGNORECASE,
)


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


def _folder_key_for(source: str, url: str, link_id: int) -> str:
    """来源规范化短键（详设 §15.1）：xhs=note id / xianyu=item id / http=8 位短 hash；
    取不到回退 link_{link_record_id}（保证一链接一文件夹不冲突）。"""
    key = ""
    try:
        if source == "xhs":
            m = _XHS_NOTE_ID_RE.search(url or "")
            key = m.group(1) if m else ""
        elif source == "xianyu":
            ids = parse_qs(urlparse(url).query).get("id") or []
            cand = ids[0] if ids else ""
            key = cand if cand and cand.isalnum() else ""
        elif source == "http":
            key = hashlib.md5((url or "").encode("utf-8")).hexdigest()[:8]
    except Exception:  # noqa: BLE001 - 取不到回退，不阻断
        key = ""
    return key or f"link_{link_id}"


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


# ---- 归集（详设 §15.1，批 6）：移动 + 编号 + 中转目录清理 ----


def _dest_suffix(src: Path) -> str:
    """目标文件后缀 = 源文件真实扩展名（连接器只落图片文件；编号命名保留格式，
    保证缩略图/网盘预览按真实格式渲染；无后缀文件保持无后缀）。"""
    return src.suffix.lower()


def _consolidate_images(paths: list[str], link_folder: Path) -> list[Path]:
    """connector 下载产物移动到链接文件夹（顺序编号保序，重名覆盖，去重）。

    返回移动后的文件路径列表（顺序 = 去重后的原顺序）；源文件已在目标文件夹
    （闲鱼连接器直接落盘商品目录 = 链接文件夹）时跳过移动。
    """
    link_folder.mkdir(parents=True, exist_ok=True)

    # 去重保序（同文件可能重复出现在 connector paths）
    unique: list[Path] = []
    seen: set[str] = set()
    for p in paths:
        rp = Path(str(p)).resolve()
        if str(rp) not in seen:
            seen.add(str(rp))
            unique.append(rp)

    dests: list[Path] = []
    names: set[str] = set()
    for idx, src in enumerate(unique, start=1):
        dest = link_folder / f"{idx:02d}{_dest_suffix(src)}"
        if src != dest.resolve():  # 已在目标位（如闲鱼）跳过；否则移动（重名覆盖）
            shutil.move(str(src), str(dest))
        dests.append(dest)
        names.add(dest.name)

    # 清理链接文件夹内不属于本次下载的残留文件（重跑/部分失败遗留；
    # 本文件夹专属于该链接，非本次编号文件 + meta.txt 即为残留，目录保持确定性）
    for f in list(link_folder.iterdir()):
        if f.is_file() and f.name != "meta.txt" and f.name not in names:
            try:
                f.unlink()
            except OSError:  # noqa: BLE001 - 残留清理失败不阻断
                pass
    return dests


def _cleanup_empty_src_dirs(srcs: list[Path], link_folder: Path, storage_root: Path) -> None:
    """删除 connector 中转目录中已变空的目录（逐级向上，直到非空/根/链接文件夹）。

    xhs 的 ExploreData.db 所在 Download 目录含库文件不空 → 自动保留（不动库）。
    """
    lf = link_folder.resolve()
    root = storage_root.resolve()
    for src in srcs:
        d = src.parent
        while d != lf and d != root:
            try:
                if d.exists() and not any(d.iterdir()):
                    d.rmdir()
                    d = d.parent
                else:
                    break
            except OSError:  # noqa: BLE001 - 清理失败不阻断（空目录残留无害）
                break


# ---- meta.txt（详设 §15.1 人可读文本） ----


def _meta_lines(
    *,
    source: str,
    url: str,
    desc: str,
    tags: list[str],
    author_id: str,
    image_count: int,
    batch_id: str,
    error_note: str,
    degraded_note: str,
) -> list[str]:
    """链接文件夹内 meta.txt 行清单（§15.1：人可读文字信息）。"""
    title = ""
    for line in (desc or "").splitlines():
        stripped = line.strip()
        if stripped:
            title = stripped
            break
    if not title:
        title = (url or "").strip()
        if len(title) > 100:
            title = title[:100] + "…"

    lines = [
        f"标题：{title}",
        f"来源：{_SOURCE_META_LABEL.get(source, source)}",
        f"原链接：{url}",
        f"作者ID：{author_id if author_id else '无'}",
        f"描述：{desc or ''}",
        f"标签：{', '.join(str(t) for t in tags)}",
        f"图片数：{image_count}",
        f"爬取时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"批次：{batch_id}",
        "网盘分享链接：未上传",  # 批 7 上传夸克后回填链接
    ]
    notes = [n for n in (error_note, degraded_note) if n]
    for note in notes:
        lines.append(f"备注：{note}")
    return lines


def _write_meta(link_folder: Path, **kwargs: Any) -> None:
    """写 meta.txt（UTF-8，人可读）。"""
    content = "\n".join(_meta_lines(**kwargs)) + "\n"
    (link_folder / "meta.txt").write_text(content, encoding="utf-8")


async def run(inputs: BatchDownloadInput, ctx: EngineContext) -> ScrapeBatchResult:
    """逐链接下载 → 归集（一链接一文件夹 + meta.txt）→ 内联体检 → 图片落库 → 链接更新。"""
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
            link_id, inputs.batch_id, connectors, biz_client, ctx.storage_dir
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
    link_id: int,
    batch_id: str,
    connectors: dict[str, Any],
    biz_client: Any,
    storage_dir: str | None,
) -> dict[str, Any]:
    """单链接：读信息 → 建链接文件夹（storage_dir 注入）→ 路由 connector →
    下载 → 归集 + meta.txt → 内联体检 → 图片落库（相对路径）→ 链接更新。"""
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

    # storage_dir 来源 = EngineContext.storage_dir（§15.1 技术定：装配注入）。
    # 未注入 → 无法建链接文件夹，fail（不产生半成品；生产路径启动即注入）
    if not storage_dir:
        base["error_note"] = "storage_dir 未注入（无法建链接文件夹，无法归集）"
        await _fail_link(biz_client, link_id, base["error_note"])
        return base
    storage_root = Path(str(storage_dir)).resolve()
    folder_key = _folder_key_for(source, url, link_id)
    link_folder = storage_root / source / folder_key

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

    desc = str(data.get("desc") or "")
    tags = list(data.get("tags") or [])
    author_id = str(data.get("author_id") or "")
    day_dir = str(data.get("day_dir") or "")

    # ---- 归集：移动到链接文件夹 + 清理中转目录 + 写 meta.txt ----
    try:
        link_folder.mkdir(parents=True, exist_ok=True)
        dests = _consolidate_images(paths, link_folder)
        _cleanup_empty_src_dirs([Path(str(p)).resolve() for p in paths], link_folder, storage_root)
        _write_meta(
            link_folder,
            source=source,
            url=url,
            desc=desc,
            tags=tags,
            author_id=author_id,
            image_count=len(dests),
            batch_id=batch_id,
            error_note="",
            degraded_note=str(result.note or ""),
        )
    except Exception as exc:  # noqa: BLE001
        base["error_note"] = f"归集失败（移动/写 meta.txt）：{exc}"
        await _fail_link(biz_client, link_id, base["error_note"])
        return base

    # ---- 成功：逐图内联体检 + 图片落库（挂链接，local_path 相对 storage_dir）----
    base["desc"] = desc
    base["tags"] = tags
    base["author_id"] = author_id

    created_ids: list[int] = []
    rel_folder = str(link_folder.relative_to(storage_root)).replace("\\", "/")
    for idx, dest in enumerate(dests, start=1):
        inspect = _inspect_path(dest)
        # 图片 url = 原始分享链接 + #img-<序号>（连接器只回本地路径无逐图源 URL；
        # 稳定可复现 → (link_record_id, url) 幂等键成立）
        img_url = f"{url}#img-{idx}"
        row = await _save_image(
            biz_client,
            link_id=link_id,
            batch_id=batch_id,
            source=source,
            url=img_url,
            local_path=str(dest.relative_to(storage_root)).replace("\\", "/"),
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

    # 元数据降级标注：connector ok=True 但 note 非空（db-missing/no-title/…）
    degraded_note = ""
    if result.note:
        degraded_note = str(result.note)

    # 更新链接记录（done + 图数 + 元数据 + storage_dir（相对路径）+ degraded_note）
    patch: dict[str, Any] = {
        "status": "done",
        "image_count": len(created_ids),
        "desc": desc,
        "tags": tags,
        "author_id": author_id,
        "storage_dir": rel_folder,
        "degraded_note": degraded_note,
    }
    await _update_link(biz_client, link_id, patch)

    base["status"] = "done"
    base["image_ids"] = created_ids
    base["image_count"] = len(created_ids)
    base["storage_dir"] = rel_folder
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
