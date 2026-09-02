"""engine/connectors/xhs.py：小红书扒图（详设-v0.6 §6.1，照广成 runtime/xhs_connector.py 原样搬回）。

- H1 调用方式：子进程 `uv run python -c` 脚本 `from source import XHS` +
  `async with XHS(work_path=..., folder_name='', folder_mode=True, image_download=True,
  video_download=False, live_download=False, record_data=True, download_record=False,
  image_format='JPEG') as xhs: await xhs.extract(url, download=True)`——不再走
  vendor main.py 的 download 入口（vendor main.py 无 download 函数，必 ImportError）；
  cwd=vendor；unset 代理 6 key（http_proxy/https_proxy/HTTP_PROXY/HTTPS_PROXY/
  ALL_PROXY/all_proxy）；PATH 补 ~/.local/bin；180s 超时
- H2 元数据 SQL：`SELECT "作品描述","作品标签","作者ID" FROM explore_data WHERE
  "作品ID" = ? LIMIT 1`（recorder.py DATA_TABLE 实锤：表 explore_data、中文列）；
  库路径 work_path/Download/ExploreData.db；标签 `tags_str.split()` 空格拆分——
  不再查旧错误表/英文列（表/列名全错，元数据必空）
- 差集计数：扒前 rglob 全量（jpeg/jpg/png/webp）→ 扒后 rglob → after - before
  只数本次新增（避免历史图算进 paths/count）
- 未落盘检查：paths 空 → ok=False + 「XHS 下载未落盘任何图片（链接可能失效或
  xsec_token 过期，请重新取最新分享链接）」
- vendor 完整度：available 检查 vendor/source/__init__.py 存在（非仅目录存在）
- 降级标注：ok=True 时 note 记降级原因（no-note-id-in-url / db-missing /
  db-read-error:<类型> / no-matching-row），图已落盘不整体失败

落盘：{storage_dir}/xhs/Download/<作品名>/；ExploreData.db 在 {storage_dir}/xhs/Download/。

测试：fake connector 注入（零网络），vendor 不存在时降级 ok=False。
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

from engine.connectors import ConnectorResult, register_connector

__all__ = ["XHSConnector"]

_CONNECTOR_ID = "xhs"

# 小红书图文链接（xiaohongshu.com，保留 query 含 xsec_token）
_XHS_URL_RE = re.compile(
    r"https?://(?:www\.)?xiaohongshu\.com/(?:discovery/item|explore|item)/[A-Za-z0-9]+(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
# 作品 ID（discovery/item|explore|item 之后的 hex 路径段）
_XHS_NOTE_ID_RE = re.compile(
    r"xiaohongshu\.com/(?:discovery/item|explore|item)/([a-f0-9]+)",
    re.IGNORECASE,
)

# XHS 库下载脚本（照广成，实跑验证过的库 import 方式；图文帖无需登录，xsec_token 鉴权）
# folder_mode=True：每帖独立子文件夹；folder_name=''：库重置为默认 Download 子目录
# download_record=False：禁用全局 ExploreID.db 去重（库默认查全局 ROOT/ExploreID.db，
#   已下载过的作品会跳过且不落盘当前 work_path，导致误判失败）；业务去重改由上层做
_XHS_SCRIPT_TMPL = (
    "import asyncio, sys, traceback\n"
    "from source import XHS\n"
    "async def _m():\n"
    "    async with XHS(work_path={out_dir!r}, folder_name='', folder_mode=True,\n"
    "                   image_download=True, video_download=False, live_download=False,\n"
    "                   record_data=True, download_record=False, image_format='JPEG') as xhs:\n"
    "        await xhs.extract({url!r}, download=True)\n"
    "try:\n"
    "    asyncio.run(_m())\n"
    "except Exception:\n"
    "    traceback.print_exc(); sys.exit(1)\n"
)


class XHSConnector:
    """小红书扒图连接器（照广成 xhs_connector.py 原样搬回，H1/H2）。

    real 调用：XHS-Downloader vendor 子进程（uv run python -c）+ sqlite3 读
    ExploreData.db（explore_data 表中文列）；差集计数 + 未落盘检查 + 降级标注。
    测试：fake connector 注入（零网络），vendor 不完整时降级 ok=False。
    """

    def __init__(self, storage_dir: str | None = None) -> None:
        self._storage_dir = storage_dir or os.environ.get(
            "SCRAPE_STORAGE_DIR", "/opt/liuquan/scrape/"
        ).strip()

    @property
    def available(self) -> bool:
        """vendor 完整度：source/__init__.py 存在（非仅目录存在）。"""
        return (self._vendor() / "source" / "__init__.py").exists()

    def _vendor(self) -> Path:
        """vendor 目录（XHS-Downloader 已复制到仓库 vendor/）。"""
        return Path(__file__).resolve().parent.parent.parent / "vendor" / "XHS-Downloader"

    async def download(
        self,
        url: str,
        *,
        batch_id: str,
        storage_dir: str | None = None,
    ) -> ConnectorResult:
        """下载小红书作品图片（照广成：uv run python -c 子进程 + 差集计数 + 降级标注）。"""
        url = (url or "").strip()
        if not _XHS_URL_RE.search(url):
            return ConnectorResult(
                ok=False,
                note=f"xhs-download 未解析到小红书链接（需 xiaohongshu.com 图文链接，含 xsec_token）: {url}",
                data={"url": url, "batch_id": batch_id},
            )

        vendor = self._vendor()
        if not (vendor / "source" / "__init__.py").exists():
            return ConnectorResult(
                ok=False,
                note=f"XHS-Downloader vendor 不存在或不完整: {vendor}",
                data={"url": url, "batch_id": batch_id},
            )

        # 落盘根目录：storage_dir/xhs；库 folder_name='' 时在 work_path/Download/<作品名>/
        # 落图，ExploreData.db 在 work_path/Download/
        out_dir = Path(storage_dir or self._storage_dir) / "xhs"
        out_dir.mkdir(parents=True, exist_ok=True)

        # 扒之前记录已有图（差集法：只数本次新增，避免把历史扒的图算进去）
        download_dir = out_dir / "Download"
        before = _snapshot_images(download_dir)

        rc, combined = self._run_xhs_download(vendor, url, out_dir)
        if rc != 0:
            return ConnectorResult(
                ok=False,
                note=f"XHS 下载失败(exit={rc}): {combined[:500]}",
                data={"url": url, "batch_id": batch_id},
            )

        # 本次新增 = 扒之后全部 - 扒之前已有
        after = _snapshot_images(download_dir)
        paths = sorted(after - before)
        if not paths:
            return ConnectorResult(
                ok=False,
                note="XHS 下载未落盘任何图片（链接可能失效或 xsec_token 过期，请重新取最新分享链接）。",
                data={"url": url, "batch_id": batch_id},
            )

        meta, meta_note = self._read_xhs_meta(out_dir, url)
        return ConnectorResult(
            ok=True,
            note=meta_note,
            data={
                "paths": [str(p) for p in paths],
                "count": len(paths),
                "desc": meta.get("desc", ""),
                "tags": meta.get("tags", []),
                "author_id": meta.get("author_id", ""),
                "day_dir": str(out_dir),
                "source": "xhs",
                "url": url,
                "batch_id": batch_id,
            },
        )

    # ── 内部：下载 + 元数据 ──────────────────────────────

    def _run_xhs_download(self, vendor: Path, url: str, out_dir: Path) -> tuple[int, str]:
        """cd vendor && uv run python -c '...' 调 XHS 库下载。

        unset 代理（实跑经验：小红书直连可达，代理反而干扰）；PATH 加 ~/.local/bin
        （uv 所在）。返回 (returncode, stdout/stderr 合并)。
        """
        script = _XHS_SCRIPT_TMPL.format(out_dir=str(out_dir), url=url)

        env = os.environ.copy()
        for k in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        env["PATH"] = str(Path.home() / ".local" / "bin") + os.pathsep + env.get("PATH", "")

        try:
            proc = subprocess.run(
                ["uv", "run", "python", "-c", script],
                cwd=str(vendor),
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return 124, "XHS 下载超时(180s)"
        except FileNotFoundError:
            return 127, "uv 命令未找到（PATH 未含 ~/.local/bin）"

        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        return proc.returncode, combined

    def _read_xhs_meta(self, out_dir: Path, url: str) -> tuple[dict[str, Any], str]:
        """从 XHS 库落盘的 ExploreData.db 读该帖的描述/标签/作者ID。

        库 record_data=True 时按 work_path/Download/ExploreData.db 落库，每帖一行。
        按 URL 里的作品ID 匹配「作品ID」列；标签列是空格分隔串，拆成列表。
        返回 (meta_dict, note)：note 非空表示降级原因（db 缺失/无匹配行），供上层标注；
        图已落盘，此处任何失败都降级，不让整体失败。
        """
        note_id = _extract_note_id(url)
        if not note_id:
            return {}, "no-note-id-in-url"

        db_path = out_dir / "Download" / "ExploreData.db"
        if not db_path.exists():
            return {}, "db-missing"

        try:
            conn = sqlite3.connect(str(db_path))
            try:
                row = conn.execute(
                    'SELECT "作品描述", "作品标签", "作者ID" FROM explore_data '
                    'WHERE "作品ID" = ? LIMIT 1',
                    (note_id,),
                ).fetchone()
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001 - db 读取降级，不阻断
            return {}, f"db-read-error:{type(exc).__name__}"

        if not row:
            return {}, "no-matching-row"

        desc, tags_str, author_id = row
        tags = [t for t in (tags_str or "").split() if t]
        return {
            "desc": desc or "",
            "tags": tags,
            "author_id": author_id or "",
        }, ""


# ── 模块级辅助（照广成同风格） ────────────────────────


def _extract_xhs_url(task: str) -> str:
    """从任务文本提取小红书图文链接（保留 query 含 xsec_token）。"""
    if not task:
        return ""
    m = _XHS_URL_RE.search(task)
    return m.group(0) if m else ""


def _extract_note_id(url: str) -> str:
    """从 URL 提取小红书作品 ID（item/<id> 路径段）。"""
    m = _XHS_NOTE_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _snapshot_images(download_dir: Path) -> set[str]:
    """rglob 全量图片（jpeg/jpg/png/webp）→ 路径集合（差集计数用）。"""
    if not download_dir.exists():
        return set()
    return {
        str(p) for p in download_dir.rglob("*")
        if p.suffix.lower() in (".jpeg", ".jpg", ".png", ".webp")
    }


def _factory(ctx: Any) -> XHSConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return XHSConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)
