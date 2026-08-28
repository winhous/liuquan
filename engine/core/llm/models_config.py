"""models.yaml 模型注册加载（详设-v0.1 §7.1；T8）。

结构（§7.1 原文）：

    models:
      default:
        provider: deepseek
        model: deepseek-chat
        base_url: env:DEEPSEEK_BASE_URL    # env: 前缀 = 启动时从环境变量读
        api_key: env:DEEPSEEK_API_KEY      # 文件里永不出现真值（R20）
        timeout_s: 30                      # 可省，默认 30
        reask_limit: 2                     # 可省，默认 2（§14）

拒载规则（不合规抛 ``ModelsConfigError``，启动失败——与 loader 的
「拒载 = 引擎不起」同哲学）：
1. yaml 语法坏
2. 顶层缺 ``models`` 键 / ``models`` 非映射 / 为空
3. 别名重复（yaml 默认静默覆盖重复键，本模块用严格 loader 专门检测）
4. 缺必填字段（provider/model/base_url/api_key 四者缺一拒载）
5. base_url/api_key 非 ``env:`` 前缀（出现真值形态，R20）拒载；
   env 引用格式坏（空变量名/非法变量名）拒载
6. env 引用的环境变量未设置或为空 -> 拒载（fail-fast，密钥唯一来源 .env）
7. timeout_s 非正 / reask_limit 为负（或类型非法）拒载

模型串（model）只存于本 YAML，代码永不内联（规范 R11）；换模型 = 改 YAML
零代码改动（验收断言 A7）。env 解析发生在加载期（启动时），解析后的真值
只进内存 ModelConfig，文件与审计永不落真值（R20/§8）。

.env 文件的实际装载（dotenv）属 CLI/入口任务，本模块只读 os.environ——
本文件在 engine/core/llm/ 豁免区内（lint P2 唯一合法持有密钥形态处）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml

__all__ = ["ModelConfig", "ModelRegistry", "ModelsConfigError", "load_models"]

_ENV_PREFIX = "env:"
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REQUIRED_FIELDS = ("provider", "model", "base_url", "api_key")
_DEFAULT_TIMEOUT_S = 30.0
_DEFAULT_REASK_LIMIT = 2


class ModelsConfigError(Exception):
    """models.yaml 拒载：加载不合规即抛，引擎不起（与 loader 拒载同哲学）。"""


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """一个模型别名的已解析配置（env: 引用已在加载期解析为真值）。"""

    alias: str
    provider: str
    model: str
    base_url: str
    api_key: str
    timeout_s: float
    reask_limit: int


class _UniqueKeyLoader(yaml.SafeLoader):
    """检测重复键的 YAML loader（yaml 默认静默覆盖重复键，违反正反拒载）。"""


def _construct_mapping_no_duplicates(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ModelsConfigError(f"models.yaml 重复键拒载：{key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_no_duplicates,
)


def _resolve_env_ref(
    value: Any, field: str, alias: str, path: Path
) -> str:
    """解析 env: 引用为环境变量真值；非 env: 形态/格式坏/未设置一律拒载。"""
    if not isinstance(value, str):
        raise ModelsConfigError(
            f"{path}: 别名 {alias!r} 的 {field} 必须是字符串（拒载），实际 {type(value).__name__}"
        )
    if not value.startswith(_ENV_PREFIX):
        raise ModelsConfigError(
            f"{path}: 别名 {alias!r} 的 {field} 必须用 env: 前缀引用环境变量"
            "（规范 R20：models.yaml 永不出现真值），实际出现真值形态（拒载）"
        )
    var_name = value[len(_ENV_PREFIX):]
    if not _ENV_NAME_RE.fullmatch(var_name):
        raise ModelsConfigError(
            f"{path}: 别名 {alias!r} 的 {field} 的 env: 引用不合法：{value!r}"
            "（须为 env:<变量名>，变量名 [A-Za-z_][A-Za-z0-9_]*）（拒载）"
        )
    resolved = os.environ.get(var_name)
    if not resolved:
        raise ModelsConfigError(
            f"{path}: 别名 {alias!r} 的 {field} 引用的环境变量 {var_name} 未设置或为空"
            "（拒载，fail-fast：密钥唯一来源 .env，规范 R20）"
        )
    return resolved


def _parse_alias(raw: Mapping[str, Any], alias: str, path: Path) -> ModelConfig:
    """单别名校验与构造；任何不合规抛 ModelsConfigError（整体拒载）。"""
    if not isinstance(raw, dict):
        raise ModelsConfigError(
            f"{path}: 别名 {alias!r} 的值必须是映射（拒载）"
        )
    missing = [f for f in _REQUIRED_FIELDS if f not in raw or raw[f] is None]
    if missing:
        raise ModelsConfigError(
            f"{path}: 别名 {alias!r} 缺必填字段 {missing}（拒载）"
        )
    timeout_raw = raw.get("timeout_s", _DEFAULT_TIMEOUT_S)
    reask_raw = raw.get("reask_limit", _DEFAULT_REASK_LIMIT)
    if (
        not isinstance(timeout_raw, (int, float))
        or isinstance(timeout_raw, bool)
        or timeout_raw <= 0
    ):
        raise ModelsConfigError(
            f"{path}: 别名 {alias!r} 的 timeout_s 必须为正数（拒载），实际 {timeout_raw!r}"
        )
    if (
        not isinstance(reask_raw, int)
        or isinstance(reask_raw, bool)
        or reask_raw < 0
    ):
        raise ModelsConfigError(
            f"{path}: 别名 {alias!r} 的 reask_limit 必须为非负整数（拒载），实际 {reask_raw!r}"
        )
    return ModelConfig(
        alias=alias,
        provider=str(raw["provider"]),
        model=str(raw["model"]),
        base_url=_resolve_env_ref(raw["base_url"], "base_url", alias, path),
        api_key=_resolve_env_ref(raw["api_key"], "api_key", alias, path),
        timeout_s=float(timeout_raw),
        reask_limit=int(reask_raw),
    )


class ModelRegistry:
    """模型别名注册表：别名 -> ModelConfig；resolve 未知别名抛 KeyError。"""

    def __init__(self, configs: Mapping[str, ModelConfig]) -> None:
        self._configs: dict[str, ModelConfig] = dict(configs)

    @classmethod
    def load(cls, path: Path) -> "ModelRegistry":
        """从 models.yaml 加载全册；任一拒载规则触发即抛 ModelsConfigError。"""
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ModelsConfigError(f"models.yaml 读取失败：{exc}（拒载）") from exc
        try:
            data = yaml.load(text, Loader=_UniqueKeyLoader)
        except ModelsConfigError:
            raise  # 重复键拒载（严格 loader 内部抛出），原样上抛
        except yaml.YAMLError as exc:
            raise ModelsConfigError(f"models.yaml YAML 语法坏（拒载）：{exc}") from exc
        if not isinstance(data, dict) or "models" not in data:
            raise ModelsConfigError(f"{path}: 顶层缺 models 映射（拒载）")
        models = data["models"]
        if not isinstance(models, dict):
            raise ModelsConfigError(f"{path}: models 必须是映射（拒载）")
        if not models:
            raise ModelsConfigError(f"{path}: models 为空，至少需要一个模型别名（拒载）")
        configs: dict[str, ModelConfig] = {}
        for alias, raw in models.items():
            if not isinstance(alias, str):
                raise ModelsConfigError(
                    f"{path}: 模型别名必须是字符串（拒载），实际 {alias!r}"
                )
            configs[alias] = _parse_alias(raw, alias, path)
        return cls(configs)

    def resolve(self, alias: str) -> ModelConfig:
        """按别名取配置；未知别名抛 KeyError（调用方按启动失败处理）。"""
        try:
            return self._configs[alias]
        except KeyError:
            registered = ", ".join(sorted(self._configs)) or "（无）"
            raise KeyError(
                f"未知模型别名 {alias!r}（models.yaml 已注册：{registered}）"
            ) from None

    @property
    def aliases(self) -> frozenset[str]:
        """已注册别名集合。"""
        return frozenset(self._configs)


def load_models(path: Path) -> ModelRegistry:
    """便捷入口：``ModelRegistry.load``。"""
    return ModelRegistry.load(path)
