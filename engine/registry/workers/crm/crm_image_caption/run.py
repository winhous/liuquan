"""crm_image_caption 工序（详设-v0.5 §6.2）。

reason: llm（调 vision 模型识图）
model: vision（vision 未配置降级时 llm_output=None，见 runner _phase_reason）
输入：ImageCaptionInput（local_paths[], message_image_ids[]）
输出：ImageCaptionResult（captions[{message_image_id, text, note}]）

逻辑：
1. 检查 llm_output（REASON 相位产出）是否为 None（vision 未配置降级）
2. 如果 llm_output=None，输出全部 note 识图模型未配置 + text 空
3. 如果 llm_output 可用，解析识图结果，输出 captions
"""

from __future__ import annotations

import logging

from engine.core.context import EngineContext
from models.workers import CaptionItem, ImageCaptionInput, ImageCaptionResult

logger = logging.getLogger(__name__)


async def run(inputs: ImageCaptionInput, ctx: EngineContext) -> ImageCaptionResult:
    """crm_image_caption：识别图片内容。"""
    # 检查 llm_output（REASON 相位产出）
    llm_output = ctx.llm_output

    # vision 未配置降级：llm_output=None
    if llm_output is None:
        captions = [
            CaptionItem(
                message_image_id=mid,
                text="",
                note="识图模型未配置，跳过识图",
            )
            for mid in inputs.message_image_ids
        ]
        return ImageCaptionResult(
            captions=captions,
            note="识图模型未配置，所有图片识图跳过",
        )

    # vision 可用：解析识图结果
    # llm_output 应该是 ImageCaptionResult 或类似结构
    try:
        # 尝试直接使用 llm_output（如果已经是正确格式）
        if hasattr(llm_output, "captions"):
            return llm_output
        # 否则尝试解析
        captions = []
        for i, path in enumerate(inputs.local_paths):
            mid = inputs.message_image_ids[i] if i < len(inputs.message_image_ids) else 0
            # 从 llm_output 提取文本（简化处理）
            text = str(llm_output) if llm_output else ""
            captions.append(CaptionItem(
                message_image_id=mid,
                text=text,
                note="识图完成",
            ))
        return ImageCaptionResult(captions=captions, note="识图完成")
    except Exception as e:
        logger.exception("解析识图结果失败")
        captions = [
            CaptionItem(
                message_image_id=mid,
                text="",
                note=f"识图解析失败: {e}",
            )
            for mid in inputs.message_image_ids
        ]
        return ImageCaptionResult(
            captions=captions,
            note=f"识图解析失败: {e}",
        )