"""image_inspect 工序 ACT（详设-v0.5 §6.1）。

纯代码工序（reason: none，无 LLM 调用，零 token 成本）：
PIL 读宽高 + 水印启发式判定。

水印启发式（简化版，config/settings.yaml 规则）：
- 尺寸阈值：min_width / min_height（默认 800×600）
- 水印判定：右下角 15% 区域亮度变化检测（简化：文件名含 watermark/tag）

输入：ImageInspectInput{image_ids[]}
输出：InspectionResult{images[]}
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from engine.core.context import EngineContext
from models.workers import ImageInspectInput, InspectionItem, InspectionResult

logger = logging.getLogger(__name__)

# 水印启发式阈值（config/settings.yaml 可配，这里用默认值）
_MIN_WIDTH = 800
_MIN_HEIGHT = 600


def _check_watermark(img_path: Path) -> bool:
    """水印启发式判定（简化版）。"""
    try:
        from PIL import Image

        img = Image.open(str(img_path))
        w, h = img.size

        # 右下角 15% 区域亮度变化检测
        right_margin = int(w * 0.85)
        bottom_margin = int(h * 0.85)
        crop = img.crop((right_margin, bottom_margin, w, h))

        # 转灰度计算标准差（高变化 = 可能有水印文字）
        gray = crop.convert("L")
        pixels = list(gray.getdata())
        if not pixels:
            return False
        mean = sum(pixels) / len(pixels)
        variance = sum((p - mean) ** 2 for p in pixels) / len(pixels)
        std_dev = variance**0.5

        # 标准差 > 40 = 可能有水印（经验值）
        return std_dev > 40

    except Exception:
        return False


def _get_dimensions(img_path: Path) -> tuple[int | None, int | None]:
    """读取图片宽高。"""
    try:
        from PIL import Image

        img = Image.open(str(img_path))
        return img.size
    except Exception:
        return None, None


def run(inputs: ImageInspectInput, ctx: EngineContext) -> InspectionResult:
    """PIL 读宽高 + 水印启发式。"""
    # 获取 biz_client 读 image_file 元数据
    biz_client = ctx.biz_client if hasattr(ctx, "biz_client") else None

    items = []
    note_parts = []

    for image_id in inputs.image_ids:
        # 通过 biz_client 读 image_file 记录
        local_path = None
        if biz_client:
            try:
                import httpx

                resp = httpx.get(
                    f"{biz_client._base_url}/api/biz/scrape/images/{image_id}",
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    local_path = data.get("local_path")
            except Exception:
                pass

        if not local_path:
            items.append(
                InspectionItem(
                    image_id=image_id,
                    note="无法获取图片路径",
                )
            )
            continue

        path = Path(local_path)
        if not path.exists():
            items.append(
                InspectionItem(
                    image_id=image_id,
                    note=f"文件不存在: {local_path}",
                )
            )
            continue

        width, height = _get_dimensions(path)
        watermark = _check_watermark(path)

        items.append(
            InspectionItem(
                image_id=image_id,
                width=width,
                height=height,
                watermark=watermark,
            )
        )

    if any("无法" in i.note or "不存在" in i.note for i in items):
        note_parts.append("部分图片检查失败")

    return InspectionResult(
        images=items,
        note="；".join(note_parts) if note_parts else "",
    )
