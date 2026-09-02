"""web/quark_tool.py：设置页夸克登录块用的夸克 CLI 工具壳（详设-v0.6 §15.2 批 7）。

web 侧（设置 → 扒图设置）独立执行 quark.sh（login --token 授权码 / logout /
get-user-info 探测）——与 engine/connectors/quark.py（上传执行）职责分开：
- web 零 engine import（P3-2/R21），引擎上传走 engine quark connector；
- 本模块只管登录/退出/状态探测三个设置页动作，quark.sh 路径与引擎侧同默认
  （~/.claude/skills/quarkclouddrive/scripts/quark.sh，本机已装；路径不存在 →
  「工具路径缺失」纯文件系统判断，不触子进程）；
- NDJSON 解析 / -1408 未授权判定照广成 quark_upload.py 同口径（代码约 30 行
  重复，换取低耦合——R21 允许的取舍，注释对齐防漂移）；
- 授权码一次性输入（R20 精神）：执行完不落库不落盘、页面不回显；
- 测试 mock subprocess（零真实 quark 调用，详设 §15.6 真实授权延后 manual）。
"""

from __future__ import annotations

import json
import logging
import random
import string
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 默认工具路径（与 engine/connectors/quark.py 同源；可被测试 monkeypatch）
_DEFAULT_QUARK_SH = (
    Path.home() / ".claude" / "skills" / "quarkclouddrive" / "scripts" / "quark.sh"
)

# 子进程公共参数（照广成 quark_upload.py run_quark：--session-input/--session-id）
_SESSION_INPUT = "刘全扒图设置夸克登录"
# 命令超时（登录/状态探测/退出都是轻命令）
_CMD_TIMEOUT = 120


def tool_script_path() -> Path:
    """quark.sh 路径（可配置位：环境变量 QUARK_SH_SCRIPT 覆盖默认，测试注入）。"""
    return Path(_DEFAULT_QUARK_SH)


def tool_available() -> bool:
    """quark.sh 工具是否安装（路径存在——纯文件系统判断，不触子进程）。"""
    return tool_script_path().is_file()


def _gen_session_id() -> str:
    """生成 {unix时间戳}-{6位随机} session-id（照广成）。"""
    ts = int(time.time())
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}-{rand}"


def _parse_ndjson(stdout: str) -> list[dict[str, Any]]:
    """逐行解析 NDJSON（照广成 parse_ndjson，跳过空行/解析失败行）。"""
    objs: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            objs.append(parsed)
    return objs


def _find_result(objs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """取 type=="result" 行（照广成 find_result：首个 result 行）。"""
    for obj in objs:
        if obj.get("type") == "result":
            return obj
    return None


def _is_unauthorized(obj: dict[str, Any]) -> bool:
    """未授权判定（照广成 is_unauthorized：code=-1408 或负 code + 未授权语义 msg）。"""
    code = obj.get("code")
    msg = str(obj.get("msg") or "")
    if code == -1408:
        return True
    if isinstance(code, int) and code < 0:
        return any(k in msg for k in ("未授权", "认证", "token"))
    return False


def _run_tool(args: list[str]) -> list[dict[str, Any]]:
    """执行 quark.sh 命令（附加 session 公共参数 + agent 环境标识）。

    返回 NDJSON 解析列表；工具缺失/超时抛 RuntimeError（调用方转友好提示）。
    未授权不在此抛——结果行含 code=-1408，由调用方按语义处理。
    """
    script = tool_script_path()
    if not script.is_file():
        raise RuntimeError("夸克工具（quark.sh）不存在")
    full = [
        str(script),
        *args,
        "--session-input",
        _SESSION_INPUT,
        "--session-id",
        _gen_session_id(),
    ]
    # 不显式传 env：继承父进程环境即可（quark.sh 内部 export CLAUDECODE/
    # AI_AGENT 等 agent 标识兜底，P2 规则 4：web 不读 os.environ，用 dotenv 读 .env
    # 的既有模式不适用于子进程环境继承——此处是继承而非读取）
    try:
        proc = subprocess.run(
            full, capture_output=True, text=True, timeout=_CMD_TIMEOUT
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("夸克工具执行超时") from exc
    return _parse_ndjson(proc.stdout or "")


def quark_login(code: str) -> tuple[bool, str]:
    """执行授权码登录：quark.sh login --token <code>。

    返回 (ok, 消息)：ok=False 时消息含「夸克未授权，请在 设置 → 扒图设置
    完成登录」等可展示文案（未授权/网络/工具缺失统一不抛）。
    """
    code = (code or "").strip()
    if not code:
        return False, "授权码为空"
    try:
        objs = _run_tool(["login", "--token", code])
    except RuntimeError as exc:
        return False, f"夸克登录失败：{exc}（请在 设置 → 扒图设置 查看夸克登录状态）"
    result = _find_result(objs)
    if result is not None and _is_unauthorized(result):
        return False, "夸克未授权，请在 设置 → 扒图设置 完成登录（授权码无效或已过期）"
    if result is not None and result.get("code") == 0:
        return True, "夸克登录成功"
    msg = str((result or {}).get("msg") or "未知错误")
    return False, f"夸克登录失败：{msg}（请在 设置 → 扒图设置 完成登录）"


def quark_logout() -> tuple[bool, str]:
    """退出夸克登录：quark.sh logout。返回 (ok, 消息)。"""
    try:
        objs = _run_tool(["logout"])
    except RuntimeError as exc:
        return False, f"夸克退出失败：{exc}"
    result = _find_result(objs)
    if result is not None and result.get("code") == 0:
        return True, "已退出夸克登录"
    return False, f"夸克退出失败：{(result or {}).get('msg') or '未知错误'}"


def quark_status() -> dict[str, Any]:
    """夸克登录状态探测：quark.sh get-user-info。

    返回 {state: 'authorized'|'unauthorized'|'error'|'tool-missing', note,
    nickname?}——页面状态行直接可展示（工具路径缺失不触子进程）。
    """
    if not tool_available():
        return {
            "state": "tool-missing",
            "note": "夸克工具路径缺失（quark.sh 未安装），无法登录",
        }
    try:
        objs = _run_tool(["get-user-info"])
    except RuntimeError as exc:
        return {"state": "error", "note": f"夸克状态查询失败：{exc}"}
    result = _find_result(objs)
    if result is None:
        return {"state": "error", "note": "夸克状态查询无结果（工具输出异常）"}
    if _is_unauthorized(result):
        return {
            "state": "unauthorized",
            "note": "夸克未授权，请在 设置 → 扒图设置 完成登录",
        }
    if result.get("code") == 0:
        data = result.get("data") or {}
        return {
            "state": "authorized",
            "note": "已登录夸克网盘",
            "nickname": str(data.get("nickname") or ""),
        }
    return {"state": "error", "note": f"夸克状态查询失败：{(result or {}).get('msg') or '未知错误'}"}
