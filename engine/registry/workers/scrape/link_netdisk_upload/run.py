"""link_netdisk_upload 工序 ACT（详设-v0.6 §15.2/§15.6 批 7）。

纯代码工序（reason: none，无 LLM 调用，零 token 成本）：
手动补传（历史数据）——素材库链接行/详情页「上传网盘」按钮触发
scrape_upload_chain → 本工序逐条调用 engine.netdisk.upload_link_folder
（同一执行点与自动上传共用；来源→夸克网盘子目录映射住 engine.netdisk，
本文件不写字面量——P1）：
逐 link_id：GET 链接（ctx.biz_client）→ 守卫（status=done / netdisk_status≠
uploaded / 文件夹有图）→ ctx.connectors["quark"].upload_folder → 成功 PATCH
link_record netdisk 三列（web PATCH handler 同步文件夹 meta.txt 网盘行）；
失败 PATCH netdisk_status=failed + error_note（页面可见，不阻断链）。

输入：ScrapeUploadInput{link_ids[]}
输出：UploadResult{uploaded[], failed[], skipped[], note}

降级：biz_client / quark connector / storage_dir 缺失 → 逐条 failed note
（upload_link_folder 内部守卫处理），链任务仍 DONE（§15.2：上传失败不阻断）。
"""

from __future__ import annotations

import logging

from engine.core.context import EngineContext
from models.workers import ScrapeUploadInput, UploadResult

logger = logging.getLogger(__name__)


async def run(inputs: ScrapeUploadInput, ctx: EngineContext) -> UploadResult:
    """逐条上传链接文件夹到夸克网盘并回填（同一执行点 = engine/netdisk.py）。"""
    from engine.netdisk import upload_link_folder

    biz_client = ctx.biz_client
    connectors = ctx.connectors if ctx.connectors else {}
    quark_connector = connectors.get("quark")
    storage_dir = ctx.storage_dir

    uploaded: list[dict] = []
    failed: list[dict] = []
    skipped: list[dict] = []

    for link_id in inputs.link_ids:
        try:
            summary = await upload_link_folder(
                biz_client, link_id, storage_dir, quark_connector
            )
        except Exception as exc:  # noqa: BLE001 - 单条异常不阻断整链
            logger.warning("link_netdisk_upload: link %s 上传异常：%s", link_id, exc)
            failed.append({"link_id": link_id, "note": f"夸克上传失败：{exc}"})
            continue
        action = summary.get("action")
        if action == "uploaded":
            uploaded.append(
                {
                    "link_id": link_id,
                    "netdisk_url": summary.get("netdisk_url", ""),
                    "note": summary.get("note", ""),
                }
            )
        elif action == "skipped":
            skipped.append(
                {"link_id": link_id, "note": summary.get("note", "")}
            )
        else:
            failed.append(
                {"link_id": link_id, "note": summary.get("note", "夸克上传失败")}
            )

    parts = [
        f"已上传 {len(uploaded)} 条" if uploaded else "",
        f"失败 {len(failed)} 条" if failed else "",
        f"跳过 {len(skipped)} 条" if skipped else "",
    ]
    return UploadResult(
        uploaded=uploaded,
        failed=failed,
        skipped=skipped,
        note="；".join(p for p in parts if p),
    )
