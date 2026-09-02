"""image_inspect 工序 ACT（详设-v0.6 §5.2，改造修通 T4；独立工序供「重新体检」）。

纯代码工序（reason: none，无 LLM 调用，零 token 成本）：
PIL 读宽高 + 水印启发式判定 → 体检结果 PATCH 写回图片记录。

v0.6 批 4 修通（详设 §5.2）：
- 改经 ctx.biz_client：GET /api/biz/scrape/images?ids=… 读 local_path
  （不再 hasattr(ctx, "biz_client") 恒 None 的旧路径——v0.5 体检工序实际不可用）
- 逐图 PATCH /api/biz/scrape/images/{id}（width/height/watermark 写回落库实锤）
- 文件缺失/路径不可读记 note 不阻断（单图失败不整体失败）
- 下载链不引用（体检已内联 batch_image_download，2026-09-03 详设修正），
  本工序供「重新体检」独立场景 + A67 验收（worker 级测试）

水印启发式（照批 3 详设：右下角 15% 区域亮度方差 > 40 = 可能有水印）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from engine.core.context import EngineContext
from models.workers import ImageInspectInput, InspectionItem, InspectionResult

logger = logging.getLogger(__name__)

# 水印启发式阈值（config/settings.yaml 可配，这里用默认值；config/ 占位）
_WATERMARK_CORNER_RATIO = 0.15
_WATERMARK_STD_THRESHOLD = 40


def _check_watermark(img_path: Path) -> bool:
    """水印启发式判定（右下角 15% 区域亮度方差检测）。"""
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


def _get_dimensions(img_path: Path) -> tuple[int | None, int | None]:
    """读取图片宽高（文件损坏/非图片 → (None, None)）。"""
    try:
        from PIL import Image

        img = Image.open(str(img_path))
        return img.size  # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return None, None


async def run(inputs: ImageInspectInput, ctx: EngineContext) -> InspectionResult:
    """经 biz_client 读 local_path → PIL 体检 → 逐图 PATCH 写回（宽高/水印）。"""
    biz_client = ctx.biz_client
    if biz_client is None:
        return InspectionResult(
            images=[], note="biz_client 未注入（无法读图片路径/写回体检结果）"
        )

    # 批量读图片记录（白名单来源 = 本工序 input image_ids）
    try:
        resp = await biz_client.get(
            "/scrape/images", params={"ids": ",".join(str(i) for i in inputs.image_ids)}
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("image_inspect: GET /scrape/images 失败：%s", exc)
        return InspectionResult(images=[], note=f"读图片记录失败：{exc}")
    if resp.status_code != 200:
        logger.warning("image_inspect: GET /scrape/images HTTP %s", resp.status_code)
        return InspectionResult(images=[], note=f"读图片记录 HTTP {resp.status_code}")

    raw_images = resp.json()
    images = raw_images if isinstance(raw_images, list) else raw_images.get("images", [])

    items: list[InspectionItem] = []
    note_parts: list[str] = []
    for img in images:
        image_id = img.get("id")
        local_path = img.get("local_path")
        if image_id is None:
            continue
        if not local_path:
            items.append(
                InspectionItem(image_id=image_id, note="记录无 local_path（未落盘）")
            )
            note_parts.append("部分图片无 local_path")
            continue
        path = Path(str(local_path))
        if not path.exists():
            items.append(
                InspectionItem(image_id=image_id, note=f"文件不存在: {local_path}")
            )
            note_parts.append("部分图片文件缺失")
            continue

        width, height = _get_dimensions(path)
        watermark = _check_watermark(path) if width and height else False

        # 体检结果写回（决策 26：PATCH 经写接口客户端落库实锤）
        try:
            patch_resp = await biz_client.patch(
                f"/scrape/images/{image_id}",
                {"width": width, "height": height, "watermark": watermark},
            )
            if patch_resp.status_code != 200:
                logger.warning(
                    "image_inspect: PATCH /scrape/images/%s HTTP %s",
                    image_id, patch_resp.status_code,
                )
                note_parts.append("部分体检结果写回失败")
        except Exception as exc:  # noqa: BLE001
            logger.warning("image_inspect: PATCH /scrape/images 失败：%s", exc)
            note_parts.append("部分体检结果写回失败")

        items.append(
            InspectionItem(
                image_id=image_id,
                width=width,
                height=height,
                watermark=watermark,
            )
        )

    return InspectionResult(
        images=items,
        note="；".join(note_parts) if note_parts else "",
    )
