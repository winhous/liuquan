"""engine/netdisk.py：夸克网盘上传执行 helper（详设-v0.6 §15.2/§15.6 批 7）。

自动（链完成消费者 scrape.download_done）与手动（补传链 link_netdisk_upload
工序）共用同一上传执行点：

``upload_link_folder(biz_client, link_id, storage_dir, quark_connector)``
逐链接：
1. GET /api/biz/scrape/links/{id} 读链接（url/source/status/storage_dir/
   netdisk_status——决策 26：读也走写接口客户端）
2. 守卫：status != done → 跳过；netdisk_status == uploaded → 跳过（幂等，
   已上传不重复传）；storage_dir 缺失 → failed；文件夹无图片 → failed
3. quark connector（可用性 available 检查）→ ``upload_folder(folder, sub_folder)``
   ——sub_folder = 夸克网盘子目录名（照广成 PLATFORMS 语义：xhs→小红书 /
   xianyu→闲鱼 / http→其他；映射住本模块，工序 run.py 不写字面量——P1）
4. 成功 → PATCH /api/biz/scrape/links/{id} {netdisk_status:'uploaded',
   netdisk_url: share_url, netdisk_uploaded_at: now}（web PATCH handler 负责
   同步更新本地文件夹 meta.txt 的网盘分享链接行，批 7 技术定）
5. 失败 → PATCH {netdisk_status:'failed', error_note:'夸克上传失败：<原因>'}

上传失败不阻断下载链/补传链本身（逐条独立结果，链任务仍 DONE——§15.2）。
注入式设计（R12）：biz_client / quark_connector 全部可注入，测试零网络
（fake quark connector 住 tests/；真实夸克调用延后 manual 项）。
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 图片后缀（文件夹「有图」判定，照广成 quark_upload.py IMAGE_EXTS）
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

# 来源 → 夸克网盘子目录名（照广成 PLATFORMS：xhs→小红书 / xianyu→闲鱼；
# http 源（HTTP 图片）无广成先例 → 归入「其他」——批 7 技术定）
_SUB_FOLDER_BY_SOURCE = {
    "xhs": "小红书",
    "xianyu": "闲鱼",
    "http": "其他",
}

# 未授权/失败的页面指引文案（§15.2：-1408/上传失败 → error_note 提示登录入口）
_LOGIN_HINT = "请在 设置 → 扒图设置 完成登录"


def _sub_folder_for(source: str) -> str:
    """来源 → 夸克网盘子目录名（默认「其他」，未知来源不报错）。"""
    return _SUB_FOLDER_BY_SOURCE.get(source or "", "其他")


def _folder_has_images(folder: Path) -> bool:
    """文件夹内是否有图片文件（上传前检查，防空目录上传）。"""
    try:
        return any(
            p.is_file() and p.suffix.lower() in _IMAGE_EXTS for p in folder.iterdir()
        )
    except OSError:  # noqa: BLE001 - 目录不可读按无图处理（调用方 failed）
        return False


async def upload_link_folder(
    biz_client: Any,
    link_id: int,
    storage_dir: str | None,
    quark_connector: Any | None,
) -> dict[str, Any]:
    """单链接夸克上传（自动/手动共用执行点）。

    返回摘要 dict：{link_id, action: 'uploaded'|'failed'|'skipped', note,
    netdisk_status}——action=uploaded 带 netdisk_url；skipped 带原因
    （非 done / 已上传）；failed 带 error_note 全文（已 PATCH 落库）。
    """
    summary: dict[str, Any] = {
        "link_id": link_id,
        "action": "failed",
        "note": "",
        "netdisk_status": "failed",
    }

    # ---- 1. 读链接（守卫前置，尽量不产生无效 PATCH）----
    try:
        resp = await biz_client.get(f"/scrape/links/{link_id}")
    except Exception as exc:  # noqa: BLE001 - 网络/配置异常降级
        summary["note"] = f"读链接记录失败：{exc}"
        return summary
    if resp.status_code != 200:
        summary["note"] = f"读链接记录 HTTP {resp.status_code}"
        return summary
    detail = resp.json()
    link = detail.get("link") or {}
    if not link:
        summary["note"] = "链接记录不存在"
        return summary

    if link.get("status") != "done":
        summary["action"] = "skipped"
        summary["note"] = "not-done（仅已完成链接上传）"
        summary["netdisk_status"] = str(link.get("netdisk_status") or "none")
        return summary
    if (link.get("netdisk_status") or "none") == "uploaded":
        summary["action"] = "skipped"
        summary["note"] = "already-uploaded（幂等跳过）"
        summary["netdisk_status"] = "uploaded"
        return summary

    # ---- 2. 本地文件夹守卫（storage_dir 相对路径 + 存储根拼接）----
    rel_dir = str(link.get("storage_dir") or "").strip()
    if not rel_dir:
        return await _fail_patch(
            biz_client, link_id, summary, "链接无本地文件夹（storage_dir 为空）"
        )
    if not storage_dir:
        return await _fail_patch(
            biz_client, link_id, summary, "storage_dir 未注入（无法定位本地文件夹）"
        )
    folder = Path(str(storage_dir)).resolve() / rel_dir
    if not folder.is_dir():
        return await _fail_patch(
            biz_client, link_id, summary, f"本地文件夹不存在：{rel_dir}"
        )
    if not _folder_has_images(folder):
        return await _fail_patch(
            biz_client, link_id, summary, f"本地文件夹无图片：{rel_dir}"
        )

    # ---- 3. quark connector（缺失/不可用 → failed，不产半成品）----
    if quark_connector is None:
        return await _fail_patch(
            biz_client,
            link_id,
            summary,
            f"quark connector 未注入（{_LOGIN_HINT}）",
        )
    if not getattr(quark_connector, "available", True):
        return await _fail_patch(
            biz_client,
            link_id,
            summary,
            f"夸克工具不可用（quark.sh 未安装或路径缺失，{_LOGIN_HINT}）",
        )

    source = str(link.get("source") or "")
    sub_folder = _sub_folder_for(source)
    try:
        result = await quark_connector.upload_folder(str(folder), sub_folder)
    except Exception as exc:  # noqa: BLE001 - connector 异常降级，不阻断链
        return await _fail_patch(
            biz_client, link_id, summary, f"quark 上传调用异常：{exc}"
        )

    if not result.ok:
        note = str(result.note or "夸克上传失败")
        if _LOGIN_HINT not in note:
            note = f"{note}（{_LOGIN_HINT}）"
        return await _fail_patch(biz_client, link_id, summary, note)

    data = result.data or {}
    share_url = str(data.get("share_url") or "").strip()
    if not share_url:
        return await _fail_patch(
            biz_client, link_id, summary, "夸克上传成功但未返回分享链接"
        )

    # ---- 4. 成功：PATCH netdisk 三列回填（web handler 同步 meta.txt 网盘行）----
    patch = {
        "netdisk_status": "uploaded",
        "netdisk_url": share_url,
        "netdisk_uploaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    try:
        resp2 = await biz_client.patch(f"/scrape/links/{link_id}", patch)
        if resp2.status_code != 200:
            summary["note"] = (
                f"上传成功但网盘信息回填 HTTP {resp2.status_code}（页面稍后可重试补传）"
            )
            return summary
    except Exception as exc:  # noqa: BLE001
        summary["note"] = f"上传成功但网盘信息回填失败：{exc}"
        return summary

    summary.update(
        {
            "action": "uploaded",
            "netdisk_status": "uploaded",
            "netdisk_url": share_url,
            "note": f"已上传夸克（{sub_folder}）",
        }
    )
    return summary


async def _fail_patch(
    biz_client: Any,
    link_id: int,
    summary: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """上传失败：PATCH netdisk_status=failed + error_note（页面可见，不阻断链）。"""
    error_note = f"夸克上传失败：{reason}"
    try:
        await biz_client.patch(
            f"/scrape/links/{link_id}",
            {"netdisk_status": "failed", "error_note": error_note},
        )
    except Exception as exc:  # noqa: BLE001 - 回填失败记 note，不抛穿链
        error_note = f"{error_note}（error_note 回填失败：{exc}）"
    summary["note"] = error_note
    summary["netdisk_status"] = "failed"
    return summary
