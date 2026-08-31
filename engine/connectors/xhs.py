"""engine/connectors/xhs.py：小红书扒图（详设-v0.5 §5.3）。

照广成 image-download：
- vendor/XHS-Downloader（uv run python -c 子进程调用，库依赖与刘全 venv 隔离）
- folder_name='' + folder_mode=True + download_record=False，unset 代理
- 落盘 {storage_dir}/xhs/Download/<作品名>/
- sqlite3 读 ExploreData.db 取描述/标签/作者 ID
- 元数据失败降级不阻塞

测试：fake connector 注入（零网络），本批不要求真实 XHS-Downloader 可用。
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

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


class XHSConnector:
    """小红书扒图连接器（照广成 image-download）。

    real 调用：XHS-Downloader vendor 子进程 + sqlite3 读 ExploreData.db
    测试：fake connector 注入（零网络），vendor 不存在时降级 ok=False
    """

    def __init__(self, storage_dir: str | None = None) -> None:
        self._storage_dir = storage_dir or os.environ.get(
            "SCRAPE_STORAGE_DIR", "/opt/liuquan/scrape/"
        ).strip()

    @property
    def available(self) -> bool:
        """检查连接器是否可用（vendor 目录存在）。"""
        vendor_dir = Path(__file__).resolve().parent.parent.parent / "vendor" / "XHS-Downloader"
        return vendor_dir.is_dir()

    async def download(
        self,
        url: str,
        *,
        batch_id: str,
        storage_dir: str | None = None,
    ) -> ConnectorResult:
        """下载小红书作品图片（照广成 XHS-Downloader 子进程调用）。"""
        if not self.available:
            return ConnectorResult(
                ok=False,
                note="小红书扒图 vendor 未配置（XHS-Downloader 目录不存在）",
            )

        target_dir = storage_dir or self._storage_dir
        xhs_dir = Path(target_dir) / "xhs"
        xhs_dir.mkdir(parents=True, exist_ok=True)

        vendor_dir = Path(__file__).resolve().parent.parent.parent / "vendor" / "XHS-Downloader"

        try:
            # 照广成：uv run python -c + folder_name='' + folder_mode=True + download_record=False
            # unset 代理
            env = os.environ.copy()
            for k in list(env.keys()):
                if "proxy" in k.lower():
                    del env[k]

            cmd = [
                "uv", "run", "python", "-c",
                (
                    f"import sys; sys.path.insert(0, '{vendor_dir}'); "
                    f"from main import download; "
                    f"download(urls=['{url}'], "
                    f"folder_name='', "
                    f"folder_mode=True, "
                    f"download_record=False, "
                    f"save_path='{xhs_dir}')"
                ),
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(vendor_dir),
                env=env,
            )

            if result.returncode != 0:
                return ConnectorResult(
                    ok=False,
                    note=f"XHS-Downloader 执行失败（rc={result.returncode}）: {result.stderr[:200]}",
                    data={"url": url, "batch_id": batch_id},
                )

            # 尝试从 ExploreData.db 读取元数据
            desc, tags, author_id, day_dir = self._read_metadata(xhs_dir, url)

            # 查找下载的文件
            paths = self._find_downloaded_files(xhs_dir)

            return ConnectorResult(
                ok=True,
                note="小红书下载完成",
                data={
                    "paths": [str(p) for p in paths],
                    "count": len(paths),
                    "desc": desc,
                    "tags": tags,
                    "author_id": author_id,
                    "day_dir": day_dir,
                    "source": "xhs",
                    "url": url,
                },
            )

        except subprocess.TimeoutExpired:
            return ConnectorResult(
                ok=False,
                note="XHS-Downloader 执行超时（120s）",
                data={"url": url, "batch_id": batch_id},
            )
        except Exception as e:
            return ConnectorResult(
                ok=False,
                note=f"XHS 下载异常: {e}",
                data={"url": url, "batch_id": batch_id},
            )

    def _read_metadata(self, xhs_dir: Path, url: str) -> tuple[str, list[str], str, str]:
        """从 ExploreData.db 读取元数据（描述/标签/作者 ID）。"""
        try:
            db_path = xhs_dir / "ExploreData.db"
            if not db_path.exists():
                return "", [], "", ""

            conn = sqlite3.connect(str(db_path))
            cursor = conn.cursor()

            # 提取作品 ID（从 URL）
            note_id = self._extract_note_id(url)
            if not note_id:
                conn.close()
                return "", [], "", ""

            # 查元数据（照广成表结构）
            cursor.execute(
                "SELECT note_id, desc, tags, author_id FROM note_data WHERE note_id = ?",
                (note_id,),
            )
            row = cursor.fetchone()
            conn.close()

            if row:
                _, desc, tags_str, author_id = row
                tags = [t.strip() for t in (tags_str or "").split(",") if t.strip()]
                return desc or "", tags, author_id or "", ""

        except Exception:
            pass  # 元数据失败降级不阻塞

        return "", [], "", ""

    def _extract_note_id(self, url: str) -> str | None:
        """从 URL 提取小红书作品 ID。"""
        match = re.search(r"explore/([a-f0-9]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"discovery/item/([a-f0-9]+)", url)
        if match:
            return match.group(1)
        return None

    def _find_downloaded_files(self, xhs_dir: Path) -> list[Path]:
        """查找下载的图片文件。"""
        paths = []
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.webp"):
            paths.extend(xhs_dir.rglob(ext))
        # 按修改时间排序
        paths.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return paths


def _factory(ctx: Any) -> XHSConnector:
    """工厂函数：注册到 CONNECTORS 注册表。"""
    return XHSConnector()


# 模块加载时注册
register_connector(_CONNECTOR_ID, _factory)
