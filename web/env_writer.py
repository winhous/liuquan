"""web/env_writer.py：.env 写入友好壳（详设-v0.4，R20 密钥纪律）。

write_env_var(name, value) -> bool：
- 读写仓库根 .env（Path 定位照 web/feishu.py 的 _REPO_ROOT 模式）
- 键已存在则替换该行（保留注释/空行不变）；不存在则追加
- 不触碰其他键；返回成功与否
- 供 API 密钥/飞书 webhook 设置页用（R20：密钥只进 .env，P2 不因密钥
  字面量违规——值来自调用方参数，不硬编码）
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOTENV_PATH = _REPO_ROOT / ".env"


def write_env_var(name: str, value: str, *, dotenv_path: Path | None = None) -> bool:
    """写入 .env 单个变量（键已存在替换，不存在追加）。

    返回 True 表示写入成功；False 表示写入失败（目录不存在/权限等）。
    只替换 NAME=... 格式的行（不碰注释行/空行/其他键），正则锚定行首。
    """
    path = dotenv_path or _DOTENV_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 读取现有内容
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        else:
            lines = []

        # 替换已存在的键（匹配行首 NAME= 或 NAME ="..." 或 NAME = "..."）
        pattern = re.compile(rf"^{re.escape(name)}\s*=\s*")
        replaced = False
        new_lines: list[str] = []
        for line in lines:
            if pattern.match(line):
                new_lines.append(f"{name}={value}\n")
                replaced = True
            else:
                new_lines.append(line)

        if not replaced:
            # 追加新行（确保前面有换行）
            if new_lines and not new_lines[-1].endswith("\n"):
                new_lines[-1] += "\n"
            new_lines.append(f"{name}={value}\n")

        path.write_text("".join(new_lines), encoding="utf-8")
        logger.info("写入 .env 变量 %s 成功", name)
        return True
    except (OSError, IOError) as exc:
        logger.warning("写入 .env 变量 %s 失败：%s", name, exc)
        return False


__all__ = ["write_env_var"]
