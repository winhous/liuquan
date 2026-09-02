"""engine/connectors/quark.py：夸克网盘连接器（详设-v0.6 §15.2/§15.6 批 7，照广成 quark_upload.py 平移适配）。

- 工具：本机已装夸克官方 quarkclouddrive CLI——`quark.sh`（quark-drive.cjs 包装，
  补齐 CLAUDECODE/AI_AGENT 等 agent 环境标识后透传参数）；命令 login/logout/
  get-user-info/upload/create-folder/share，NDJSON 输出（type=="result" 行为
  命令结果行；未授权 code=-1408）
- 方法：
  - ``upload_folder(local_path, sub_folder)``：幂等建网盘目录「扒图素材/<sub_folder>」
    （fid 进程内缓存，照广成 _ensure_sub_fid）→ upload <文件夹> --parent-fid →
    share <fids> --url-type 1 --expired-type 1（永久公开分享链接）→ ConnectorResult
    {ok, note, data: {share_url, fids}}；-1408/未授权 → ok=False + note 含
    「夸克未授权，请在 设置 → 扒图设置 完成登录」
  - ``login_status()``：get-user-info 探测（已登录 code=0 / 未授权 -1408）
- 可用性 available = quark.sh 路径存在（路径可配，默认 ~/.claude/skills/
  quarkclouddrive/scripts/quark.sh，本机已装）；不存在 → 降级 ok=False 不抛
- 超时照广成：建目录 120s / 上传 300s / 分享 120s；子进程执行补 agent 环境标识
  （quark.sh 内部 export 兜底，此处再显式给一遍，P2 豁免区——engine/connectors）
- 真实授权 = 延后 manual 项（详设 §15.6：本机已登录过但 token 过期需重新授权码
  登录；刘全不存授权码）；本批测试全 fake/mock（monkeypatch subprocess 或注入
  假 connector），真实 quark 调用零发生

测试：fake connector 注入（A70/A71 验收）+ subprocess monkeypatch（connector 级
单测）；本模块不持 URL/IP 字面量（share_url 来自 CLI 输出）。
"""

from __future__ import annotations

import json
import logging
import os
import random
import string
import subprocess
import time
from pathlib import Path
from typing import Any

from engine.connectors import ConnectorResult, register_connector

logger = logging.getLogger(__name__)

__all__ = ["QuarkConnector"]

_CONNECTOR_ID = "quark"

# 网盘目标顶层目录（照广成 quark_upload.py TOP_FOLDER：扒图素材）
_TOP_FOLDER = "扒图素材"
# 根目录 fid（quark-drive 根目录约定 "0"，照广成）
_ROOT_FID = "0"

# 超时（照广成 quark_upload.py：300s / 120s / 120s）
_UPLOAD_TIMEOUT = 300
_CREATE_FOLDER_TIMEOUT = 120
_SHARE_TIMEOUT = 120
_LOGIN_STATUS_TIMEOUT = 120

# 子进程公共参数（照广成 run_quark：--session-input/--session-id 全量形态）
_SESSION_INPUT = "刘全扒图素材上传"

# agent 环境标识（quark-drive runtime 校验需识别 Agent 环境，照 quark.sh export）
_AGENT_ENV = {
    "CLAUDECODE": "1",
    "AI_AGENT": "claude-code",
    "CLAUDE_CODE_SESSION_ID": "liuquan-upload",
}

# -1408 = 登录态失效/未授权（广成 is_unauthorized 同判据）
_UNAUTHORIZED_CODE = -1408


class QuarkConnector:
    """夸克网盘上传连接器（照广成 quark_upload.py 平移适配）。

    real 调用：quark.sh（quark-drive.cjs 包装）子进程（create-folder/upload/share/
    get-user-info）+ NDJSON 解析 + -1408 未授权检测；fid 进程内缓存（幂等建目录
    不重复调）。测试：fake connector 注入 / subprocess monkeypatch（零真实调用）。
    """

    def __init__(
        self,
        storage_dir: str | None = None,
        script_path: str | None = None,
    ) -> None:
        # storage_dir 保留（与其余 connector 工厂签名一致）；quark 不落盘本地
        self._storage_dir = storage_dir
        self._script_path = script_path or str(
            Path.home() / ".claude" / "skills" / "quarkclouddrive" / "scripts" / "quark.sh"
        )
        # fid 进程内缓存：顶层「扒图素材」fid + 各子目录 fid（fid 稳定可复用）
        self._root_fid: str | None = None
        self._sub_fid_cache: dict[str, str] = {}

    @property
    def available(self) -> bool:
        """quark.sh 工具路径存在（本机已装；不存在 = 未配置工具，降级提示）。"""
        return Path(self._script_path).is_file()

    # ---- 公开方法 ----

    async def upload_folder(self, local_path: str, sub_folder: str) -> ConnectorResult:
        """上传本地文件夹到网盘「扒图素材/<sub_folder>」并建永久公开分享链接。

        幂等建目录（fid 进程内缓存）→ upload <文件夹> --parent-fid → share
        <fids> --url-type 1 --expired-type 1 → ConnectorResult{ok, note,
        data: {share_url, fids}}。未授权/工具缺失/任意步骤失败 → ok=False + note
        （含登录入口提示），不抛穿链。
        """
        if not self.available:
            return ConnectorResult(
                ok=False,
                note=(
                    "夸克工具不可用（quark.sh 不存在），请在 设置 → 扒图设置 "
                    "查看夸克登录状态"
                ),
                data={},
            )

        sub_fid: str | None
        try:
            sub_fid = self._ensure_sub_fid(sub_folder)
        except _UnauthorizedError as exc:
            # 未授权 → ok=False + note 含登录入口提示（照详设 §15.2 文案）
            logger.warning("quark: 未授权（建目录阶段）：%s", exc)
            return ConnectorResult(
                ok=False,
                note="夸克未授权，请在 设置 → 扒图设置 完成登录",
                data={},
            )
        except RuntimeError as exc:
            logger.warning("quark: 建目录失败：%s", exc)
        if sub_fid is None:
            return ConnectorResult(
                ok=False,
                note=(
                    f"夸克网盘目录「{_TOP_FOLDER}/{sub_folder}」创建失败（网络异常或工具输出异常），"
                    "请在 设置 → 扒图设置 查看夸克登录状态"
                ),
                data={},
            )

        # 上传文件夹 → 取文件 fid 列表（照广成 upload_folder：result.data.fids）
        ok, note, fids = self._upload(local_path, sub_fid)
        if not ok or not fids:
            return ConnectorResult(ok=False, note=note, data={"fids": fids or []})

        # 永久公开分享（--url-type 1 = 公开链接无提取码；--expired-type 1 = 永久有效）
        share_url = self._create_share(fids)
        if not share_url:
            return ConnectorResult(
                ok=False,
                note=(
                    "夸克创建分享链接失败（未授权或网络异常），"
                    "请在 设置 → 扒图设置 完成登录"
                ),
                data={"fids": fids},
            )
        return ConnectorResult(
            ok=True,
            note="",
            data={"share_url": share_url, "fids": fids},
        )

    async def login_status(self) -> ConnectorResult:
        """探测登录状态：get-user-info（code=0 → 已登录；-1408 → 未授权）。"""
        if not self.available:
            return ConnectorResult(
                ok=False,
                note="夸克工具不可用（quark.sh 不存在），请在 设置 → 扒图设置 查看",
                data={"authorized": False},
            )
        try:
            objs = self._run_quark(["get-user-info"], _LOGIN_STATUS_TIMEOUT)
        except _UnauthorizedError as exc:
            return ConnectorResult(
                ok=False,
                note=f"夸克未授权，请在 设置 → 扒图设置 完成登录（{exc}）",
                data={"authorized": False},
            )
        except RuntimeError as exc:
            return ConnectorResult(
                ok=False,
                note=f"夸克登录状态查询失败：{exc}",
                data={"authorized": False},
            )
        result = _find_result(objs)
        if result is not None and result.get("code") == 0:
            return ConnectorResult(
                ok=True,
                note="",
                data={"authorized": True, **((result.get("data") or {}))},
            )
        return ConnectorResult(
            ok=False,
            note="夸克未授权，请在 设置 → 扒图设置 完成登录",
            data={"authorized": False},
        )

    # ---- 内部：目录/上传/分享（照广成 quark_upload.py 平移）----

    def _ensure_sub_fid(self, sub_folder: str) -> str | None:
        """幂等确保网盘目录「扒图素材/<sub_folder>」存在，返回其 fid（None=失败）。

        未授权/超时等异常向上抛（_UnauthorizedError / RuntimeError），由
        upload_folder 统一转 ok=False + note；网络/输出异常返回 None（不抛）。
        """
        if sub_folder in self._sub_fid_cache:
            return self._sub_fid_cache[sub_folder]
        if self._root_fid is None:
            self._root_fid = self._create_folder(_TOP_FOLDER, _ROOT_FID)
        if self._root_fid is None:
            return None
        sub_fid = self._create_folder(sub_folder, self._root_fid)
        if sub_fid is None:
            return None
        self._sub_fid_cache[sub_folder] = sub_fid
        return sub_fid

    def _create_folder(self, name: str, parent_fid: str) -> str | None:
        """create-folder 建目录，返回 fid（code!=0/无 fid → None）。"""
        objs = self._run_quark(
            ["create-folder", "--dir-path", name, "--parent-fid", parent_fid],
            _CREATE_FOLDER_TIMEOUT,
        )
        result = _find_result(objs)
        if result is not None and result.get("code") == 0:
            fid = (result.get("data") or {}).get("fid")
            return str(fid) if fid else None
        return None

    def _upload(self, local_path: str, parent_fid: str) -> tuple[bool, str, list[str]]:
        """upload <文件夹> --parent-fid；返回 (是否成功, 摘要或原因, 文件 fid 列表)。"""
        try:
            objs = self._run_quark(
                ["upload", str(local_path), "--parent-fid", parent_fid],
                _UPLOAD_TIMEOUT,
            )
        except _UnauthorizedError as exc:
            return False, f"夸克未授权，请在 设置 → 扒图设置 完成登录（{exc}）", []
        except RuntimeError as exc:
            return False, f"夸克上传超时或工具调用失败（{exc}）", []
        result = _find_result(objs)
        if result is None:
            return False, "夸克上传无结果行（工具输出异常）", []
        if result.get("code") == 0:
            data = result.get("data") or {}
            fids = list(data.get("fids") or [])
            if not fids:
                return False, "夸克上传成功但未返回文件 fid（无法创建分享链接）", []
            count = data.get("successCount")
            summary = f"成功{count}个文件" if count is not None else "成功"
            if data.get("instantUpload"):
                summary += "（含秒传）"
            return True, summary, fids
        return False, f"夸克上传失败 code={result.get('code')} msg={result.get('msg')}", []

    def _create_share(self, fids: list[str]) -> str:
        """share <fids...> --url-type 1 --expired-type 1 → share_url（失败空串）。"""
        try:
            objs = self._run_quark(
                ["share", *fids, "--url-type", "1", "--expired-type", "1"],
                _SHARE_TIMEOUT,
            )
        except (_UnauthorizedError, RuntimeError) as exc:
            logger.warning("quark: 创建分享链接失败：%s", exc)
            return ""
        result = _find_result(objs)
        if result is not None and result.get("code") == 0:
            url = (result.get("data") or {}).get("share_url")
            return str(url) if url else ""
        return ""

    # ---- 子进程执行（照广成 run_quark：公共参数 + NDJSON + 未授权检测）----

    def _run_quark(self, cmd_args: list[str], timeout: int) -> list[dict[str, Any]]:
        """调 quark.sh（自动附加 --session-input/--session-id + agent 环境标识）。

        返回 NDJSON 解析对象列表；未授权（-1408/负 code + 未授权语义 msg）抛
        _UnauthorizedError；超时/工具缺失抛 RuntimeError——调用方降级 ok=False。
        """
        session_id = _gen_session_id()
        full = [
            self._script_path,
            *cmd_args,
            "--session-input",
            _SESSION_INPUT,
            "--session-id",
            session_id,
        ]
        env = os.environ.copy()
        env.update(_AGENT_ENV)
        try:
            proc = subprocess.run(
                full,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"超时({timeout}s)") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"调 quark.sh 失败（路径不存在）") from exc

        objs = _parse_ndjson(proc.stdout or "")
        for obj in objs:
            if obj.get("type") == "result" and _is_unauthorized(obj):
                raise _UnauthorizedError(
                    obj.get("msg") or "授权已过期或未登录"
                )
        return objs


class _UnauthorizedError(Exception):
    """夸克未授权/授权过期（-1408），需用户先在设置页完成授权码登录。"""


# ---- 模块级辅助（照广成 parse_ndjson / find_result / is_unauthorized / gen_session_id）----


def _parse_ndjson(stdout: str) -> list[dict[str, Any]]:
    """逐行解析 NDJSON，跳过空行与无法解析的行。"""
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
    """取 type=="result" 那行（每个命令的结果行）。"""
    for obj in objs:
        if obj.get("type") == "result":
            return obj
    return None


def _is_unauthorized(obj: dict[str, Any]) -> bool:
    """未授权判定（照广成）：code==-1408，或 code 为负且 msg 含未授权/认证/token。"""
    code = obj.get("code")
    msg = str(obj.get("msg") or "")
    if code == _UNAUTHORIZED_CODE:
        return True
    if isinstance(code, int) and code < 0:
        return any(k in msg for k in ("未授权", "认证", "token"))
    return False


def _gen_session_id() -> str:
    """生成 {unix时间戳}-{6位随机字母数字} 格式 session-id（照广成）。"""
    ts = int(time.time())
    rand = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"{ts}-{rand}"


def _factory(ctx: Any, *, storage_dir: str | None = None) -> QuarkConnector:
    """工厂函数：注册到 CONNECTORS 注册表（quark 不落盘本地，storage_dir 仅签名一致）。"""
    return QuarkConnector(storage_dir=storage_dir)


# 模块加载时注册（照 xhs/xianyu 模式）
register_connector(_CONNECTOR_ID, _factory)
