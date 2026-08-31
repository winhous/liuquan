"""工序注册表全册加载 + L1-L9 校验 + 拒载（详设-v0.1 §4.4、§6.1；T4b）。

目录布局（详设 §11）：
- workers:    engine/registry/workers/<域>/<工序>/worker.yaml    （单文件单声明）
- chains:     engine/registry/chains/<域>/<链>/chain.yaml        （单文件单声明）
- context:    engine/registry/context/<域>.yaml                  （列表）
- events:     engine/registry/events/<域>.yaml                   （列表）
- actions:    engine/registry/actions/<域>.yaml                  （列表，ActionDeclaration）
- L9 登记:    engine/registry/model_hashes.yaml                  （write_hashes 生成，提交入库）

公共 API：
- ``load_registry(repo_root, *, models_yaml_path=None) -> Registry``：全册加载 +
  L1-L9 校验；不合规抛 ``RegistryLoadError``（聚合全部违规，每条一行
  「[L#] 文件 详情」）。models_yaml_path 缺省 = repo_root/models.yaml。
- ``validate(repo_root) -> list[str]``：不抛，返回违规行列表（空 = 合规），
  供 registry-check 命令打印。
- ``write_hashes(repo_root) -> Path``：重写 model_hashes.yaml（供
  registry-check --write-hashes 调用，T12b 接 CLI）。

校验规则（详设 §4.4 表；每条一正一反测试）：
- L1 id 全局唯一、snake_case：工序/链/provider 三表各自 + 跨表；事件
  event_type、Action action_id 各自表内唯一。snake_case 由 schema pattern
  强制（pydantic 报错由本模块归口 [L1]）。
- L2 domain 在枚举内（schema 用 policy.Domain，同源）；工序 context 引用的
  provider 必须已登记且同域（v0.1 越域直接拒，§5.2）；Action domain 契约为
  str，loader 补枚举校验。
- L3 input/output/params/returns/payload/output_model 的 model 名必须在
  models.workers / models.contract / models 可 import（顺序搜索），且须为
  Pydantic Model（R2 输出即类型）；import 不到 = 拒载。
- L4 ``model:`` 必须引用 models.yaml 已注册别名（复用 T8 的
  ``load_models``）；模型串内联（如 deepseek-chat）不在别名表 = 拒载；
  models.yaml 缺失 = 拒载「未配置模型注册」（任务语义：缺失即拒载）。
- L5 链内 worker 必须已注册；链域与全部工序域一致。
- L6 链步骤 input 表达式静态可解析、只指向前序（steps[n] 的 n < 当前 step）、
  字段存在（用 pydantic model_fields 静态路径检查，含 task.input 与
  steps[n].output 两侧）。类型兼容性只做存在级，不做深类型对等（见技术决策）。
- L7 risk: transaction 拒载（schema Literal 已拒，归口 [L7]；Action 同）。
- L8 prompt 文件、config_dir 目录必须存在（相对工序目录；解析后须仍在仓库内）。
- L9 Model 结构 hash 与工序 version 联动：write_hashes 记录每个被工序引用的
  Model 的 {version, hash}；load 时 hash 不匹配 / 记录缺失 / version 不一致
  均拒载。hash = 字段名+类型注解的稳定序列化 sha256。
- L10（补充，§6.3 原文「loader 校验」）Action target 锁死 tm.proposal。
- 事件 trigger_chain 引用未登记的链 = 拒载（R22：全册引用断裂 = 拒载）。

技术决策（记录，供变更日志）：
- L6 只做「字段存在」级静态检查（详设 §4.2 的「类型对得上」v0.1 不实现深
  类型对等；缺省 input / task.input 直传不校验目标 Model 兼容性——运行期
  INIT 相位由 Policy 兜底）。
- L9 范围 = 工序引用的 Model（input/output，version 联动主体是工序）；
  链 input / provider params|returns / 事件 payload / Action output 不参与
  version 联动（它们没有可 bump 的 version 字段，hash 记录不覆盖）。
  同一 Model 被多个工序引用时记录其中最大 version，各引用工序 version 必须
  一致（v0.1 无共享 Model，约束不触发）。
- context params（如 input.customer_id）暂不做静态校验（不在 L1-L9 规则表），
  运行期取数容错。
- L4 复用 ``engine.core.llm.models_config.load_models``（T8）：models.yaml 的
  env: 引用在加载期解析，环境变量未配置 = ModelsConfigError = 拒载（fail-fast
  与「拒载 = 引擎不起」同哲学；registry-check 的 .env 装载属 CLI 任务 T12b）。
- P1/P2 自检：本模块不出现业务专名字面量（域名单词经 policy.Domain 枚举值
  运行期拼接，不写死）；不出现 URL/IP/密钥形态；不读环境变量。
"""

from __future__ import annotations

import hashlib
import importlib
import re
import types
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml
from pydantic import BaseModel, ValidationError

from engine.core.llm.models_config import ModelsConfigError, load_models
from engine.core.policy import Domain
from models.contract.action import ActionDeclaration
from .schema import (
    ChainDeclaration,
    ContextProviderDeclaration,
    EventDeclaration,
    WorkerDeclaration,
)

__all__ = [
    "Registry",
    "RegistryLoadError",
    "load_registry",
    "model_structure_hash",
    "validate",
    "write_hashes",
]

# L1 规则原文：^[a-z][a-z0-9_]*$（schema 同源强制；此处为归口信息）
_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# L6 表达式：task.input(.字段) 与 steps[n].output(.字段)
_TASK_REF_RE = re.compile(r"^task\.input(?:\.(.+))?$")
_STEP_REF_RE = re.compile(r"^steps\[(\d+)\]\.output(?:\.(.+))?$")
# L3 Model 搜索顺序（详设：models.workers / models.contract / models 包内同名类）
_MODEL_SEARCH_MODULES = ("models.workers", "models.contract", "models")
# L2 域枚举提示（运行期由枚举拼接，不写死业务词）
_DOMAIN_LIST = "/".join(d.value for d in Domain)
# L10 Action target 合法值（详设 §6.3 + 详设-v0.3 §5.4 + 详设-v0.4 §10.3：
# v0.3 增 crm.todo_candidate——crm.candidate 候选消费者落 crm.todo_candidate，决策 19/26；
# v0.4 增 tm.task——tm.schedule 提醒消费者落 tm.task，决策 37-1）
_ACTION_TARGETS = frozenset({"tm.proposal", "crm.todo_candidate", "tm.task"})


class RegistryLoadError(Exception):
    """注册表拒载：聚合全部违规，每条一行「[L#] 文件 详情」（引擎不起）。"""

    def __init__(self, violations: Iterable[str]) -> None:
        self.violations: list[str] = list(violations)
        super().__init__("\n".join(self.violations))


@dataclass(frozen=True)
class Registry:
    """全册加载成功结果（load_registry 返回；失败即抛 RegistryLoadError）。"""

    workers: dict[str, WorkerDeclaration]
    chains: dict[str, ChainDeclaration]
    context_providers: dict[str, ContextProviderDeclaration]
    events: dict[str, EventDeclaration]
    actions: dict[str, ActionDeclaration]
    valid: bool = True


@dataclass(frozen=True)
class _CheckResult:
    violations: list[str]
    registry: Registry | None


@dataclass
class _Decl:
    kind: str
    obj: BaseModel
    rel: str
    line: int
    file: Path


# ---- L9：Model 结构 hash ----


def _canonical_type(tp: Any) -> str:
    """类型注解 -> 稳定字符串（L9 hash 输入；同环境内确定）。

    review 修复（2026-08-28）：规范化等价写法——`Optional[str]` / `str | None` /
    `Union[str, None]` / `None | str` 是同一语义类型，必须产出同一 hash（原实现
    各自不同，纯语法重构会误报「结构已变更」逼无意义 bump version）。
    """
    origin = typing.get_origin(tp)
    if origin is typing.Annotated:
        # 元数据不算结构（Annotated[X, ...] -> X）
        return _canonical_type(typing.get_args(tp)[0])
    if origin is typing.Union or origin is types.UnionType:
        args = typing.get_args(tp)
        non_none = [a for a in args if a is not type(None)]
        has_none = len(non_none) != len(args)
        if len(non_none) == 1:
            inner = _canonical_type(non_none[0])
            return f"Optional[{inner}]" if has_none else inner
        body = ",".join(sorted(_canonical_type(a) for a in non_none))
        return f"Optional[Union[{body}]]" if has_none else f"Union[{body}]"
    if origin is typing.Literal:
        parts = ",".join(sorted(repr(a) for a in typing.get_args(tp)))
        return f"Literal[{parts}]"
    if origin is None:
        if isinstance(tp, type):
            return tp.__name__
        return str(tp)
    args = typing.get_args(tp)
    args_str = ",".join(_canonical_type(a) for a in args)
    origin_name = getattr(origin, "__name__", None) or str(origin)
    return f"{origin_name}[{args_str}]"


def model_structure_hash(model_cls: type[BaseModel]) -> str:
    """Model 结构摘要：字段名+类型注解的稳定序列化 -> sha256（L9 hash）。"""
    parts = [
        f"{name}:{_canonical_type(field.annotation)}"
        for name, field in model_cls.model_fields.items()
    ]
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _unwrap_model(ann: Any) -> type[BaseModel] | None:
    """注解展开 Optional/Annotated 后返回 BaseModel 子类（否则 None）。"""
    while True:
        origin = typing.get_origin(ann)
        if origin is typing.Union or origin is types.UnionType:
            args = [a for a in typing.get_args(ann) if a is not type(None)]
            if len(args) == 1:
                ann = args[0]
                continue
            return None
        if origin is typing.Annotated:
            ann = typing.get_args(ann)[0]
            continue
        break
    if isinstance(ann, type) and issubclass(ann, BaseModel):
        return ann
    return None


def _load_hash_records(
    path: Path,
) -> tuple[dict[str, dict[str, Any]] | None, str | None]:
    """读 model_hashes.yaml；文件缺失 = (None, None)（每个被引用 Model 报未登记）。"""
    if not path.is_file():
        return None, None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        return None, f"[L9] model_hashes.yaml 解析失败（fail-closed）：{exc}"
    if not isinstance(data, dict) or "models" not in data:
        return None, "[L9] model_hashes.yaml 缺 models 映射（fail-closed）"
    models = data["models"]
    if not isinstance(models, dict):
        return None, "[L9] model_hashes.yaml 的 models 必须是映射（fail-closed）"
    records: dict[str, dict[str, Any]] = {}
    for name, raw in models.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            return None, f"[L9] model_hashes.yaml 记录形态非法：{name!r}"
        version = raw.get("version")
        digest = raw.get("hash")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            return None, f"[L9] model_hashes.yaml 中 {name!r} 的 version 必须为正整数"
        if not isinstance(digest, str) or not digest:
            return None, f"[L9] model_hashes.yaml 中 {name!r} 的 hash 必须为非空字符串"
        records[name] = {"version": version, "hash": digest}
    return records, None


# ---- L3：Model import（顺序搜索 + 缓存）----


def _resolve_model(
    name: str, cache: dict[str, tuple[type[BaseModel] | None, str | None]]
) -> tuple[type[BaseModel] | None, str | None]:
    """按名在三个 models 包内找同名 Pydantic Model；import 不到 = (None, 原因)。"""
    cached = cache.get(name)
    if cached is not None:
        return cached
    for module_name in _MODEL_SEARCH_MODULES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        attr = getattr(module, name, None)
        if attr is None:
            continue
        if isinstance(attr, type) and issubclass(attr, BaseModel):
            cache[name] = (attr, None)
            return attr, None
        reason = f"{module_name}.{name} 存在但不是 Pydantic Model（R2：输出即类型）"
        cache[name] = (None, reason)
        return None, reason
    reason = f"{_MODEL_SEARCH_MODULES[0]} / {_MODEL_SEARCH_MODULES[1]} / {_MODEL_SEARCH_MODULES[2]} 中均找不到 {name}"
    cache[name] = (None, reason)
    return None, reason


def _cached_model(
    cache: dict[str, tuple[type[BaseModel] | None, str | None]], name: str
) -> type[BaseModel] | None:
    entry = cache.get(name)
    return entry[0] if entry is not None else None


# ---- YAML 解析（带行号，违规信息尽量带文件与行号）----


def _rel(root: Path, path: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _find_files(root: Path, sub: str, name: str | None) -> list[Path]:
    base = root / "engine" / "registry" / sub
    if not base.is_dir():
        return []
    if name is None:
        return sorted(base.glob("*.yaml"))
    return sorted(base.rglob(name))


def _parse_yaml_file(path: Path) -> tuple[Any | None, str | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"读取失败：{exc}"
    try:
        return yaml.safe_load(text), None
    except yaml.YAMLError as exc:
        return None, f"YAML 解析失败：{exc}"


def _top_level_key_line(path: Path, key: str) -> int:
    """单声明文件：顶层映射里 key 对应标量的行号（尽力而为）。"""
    try:
        node = yaml.compose(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return 0
    if not isinstance(node, yaml.MappingNode):
        return 0
    for k, v in node.value:
        if isinstance(k, yaml.ScalarNode) and k.value == key:
            return v.start_mark.line + 1
    return 0


def _list_item_key_lines(path: Path, key: str) -> list[int]:
    """列表声明文件：每项的 key 行号（与列表项一一对应）。"""
    try:
        node = yaml.compose(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return []
    if not isinstance(node, yaml.SequenceNode):
        return []
    lines: list[int] = []
    for item in node.value:
        found = 0
        if isinstance(item, yaml.MappingNode):
            for k, v in item.value:
                if isinstance(k, yaml.ScalarNode) and k.value == key:
                    found = v.start_mark.line + 1
                    break
        lines.append(found)
    return lines


def _validate(
    schema_cls: type[BaseModel], data: Any, rel: str, line: int
) -> tuple[BaseModel | None, list[str]]:
    """schema 校验；pydantic 报错按字段归口 L1/L2/L7，其余 [L0] 结构。"""
    try:
        return schema_cls.model_validate(data), []
    except ValidationError as exc:
        vios: list[str] = []
        for err in exc.errors():
            loc = tuple(err.get("loc") or ())
            raw = err.get("input")
            if "domain" in loc:
                vios.append(
                    f"[L2] {rel}:{line} domain 不在枚举内（{_DOMAIN_LIST}），实际 {raw!r}"
                )
            elif "risk" in loc:
                if raw == "transaction":
                    vios.append(
                        f"[L7] {rel}:{line} risk: transaction 拒载"
                        "（一期无对外真实事务，详设 §5.1）"
                    )
                else:
                    vios.append(
                        f"[L7] {rel}:{line} risk 必须为 read/suggest/write"
                        f"（transaction 一律拒载），实际 {raw!r}"
                    )
            elif "id" in loc:
                vios.append(
                    f"[L1] {rel}:{line} id 不合规（须匹配 snake_case {_ID_RE.pattern}），实际 {raw!r}"
                )
            else:
                field = ".".join(str(x) for x in loc) or "<顶层>"
                vios.append(f"[L0] {rel}:{line} schema 校验失败：{field} {err.get('msg', '')}")
        return None, vios


def _parse_single(
    path: Path,
    root: Path,
    schema_cls: type[BaseModel],
    kind: str,
    id_key: str,
    violations: list[str],
) -> list[_Decl]:
    rel = _rel(root, path)
    data, err = _parse_yaml_file(path)
    if err is not None:
        violations.append(f"[L0] {rel}: {err}")
        return []
    if data is None:
        return []
    if not isinstance(data, dict):
        violations.append(f"[L0] {rel}: {kind} 声明必须是映射（单文件单声明）")
        return []
    line = _top_level_key_line(path, id_key)
    obj, vios = _validate(schema_cls, data, rel, line)
    violations.extend(vios)
    if obj is None:
        return []
    return [_Decl(kind, obj, rel, line, path)]


def _parse_list(
    path: Path,
    root: Path,
    schema_cls: type[BaseModel],
    kind: str,
    id_key: str,
    violations: list[str],
) -> list[_Decl]:
    rel = _rel(root, path)
    data, err = _parse_yaml_file(path)
    if err is not None:
        violations.append(f"[L0] {rel}: {err}")
        return []
    if data is None:
        return []
    if not isinstance(data, list):
        violations.append(f"[L0] {rel}: {kind} 声明文件必须是列表（一文件多项声明）")
        return []
    lines = _list_item_key_lines(path, id_key)
    out: list[_Decl] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            violations.append(f"[L0] {rel}: {kind} 第 {i + 1} 项必须是映射")
            continue
        line = lines[i] if i < len(lines) else 0
        obj, vios = _validate(schema_cls, item, rel, line)
        violations.extend(vios)
        if obj is not None:
            out.append(_Decl(kind, obj, rel, line, path))
    return out


def _parse_workers(root: Path, violations: list[str]) -> list[_Decl]:
    out: list[_Decl] = []
    for path in _find_files(root, "workers", "worker.yaml"):
        out.extend(_parse_single(path, root, WorkerDeclaration, "工序", "id", violations))
    return out


# ---- 规则检查 ----


def _check_l1(violations: list[str], entries: list[tuple[str, str, str, int]]) -> None:
    """L1：同表/跨表 id 重复（entries: (kind, id, rel, line)）。"""
    seen: dict[str, list[tuple[str, str, int]]] = {}
    for kind, id_, rel, line in entries:
        seen.setdefault(id_, []).append((kind, rel, line))
    for id_, occ in seen.items():
        if len(occ) < 2:
            continue
        kinds = {k for k, _, _ in occ}
        scope = "跨表" if len(kinds) > 1 else "同表"
        first_kind, first_rel, first_line = occ[0]
        for kind, rel, line in occ[1:]:
            violations.append(
                f"[L1] {rel}:{line} {kind} id {id_!r} 重复"
                f"（{scope}，已见于 {first_rel}:{first_line} 的 {first_kind} 声明）"
            )


def _check_expr(
    expr: str,
    step_index: int,
    chain_decl: _Decl,
    worker_by_id: dict[str, _Decl],
    cache: dict[str, tuple[type[BaseModel] | None, str | None]],
) -> list[str]:
    """L6：单条 input 引用表达式静态检查。"""
    vios: list[str] = []
    d = chain_decl
    m = _TASK_REF_RE.match(expr)
    if m is not None:
        path = m.group(1)
        fields = path.split(".") if path else []
        cls = _cached_model(cache, d.obj.input.model)
        if cls is None:
            return vios  # L3 已报
        vios.extend(_resolve_path(cls, fields, expr, d))
        return vios
    m = _STEP_REF_RE.match(expr)
    if m is not None:
        idx = int(m.group(1))
        if idx >= step_index:
            vios.append(
                f"[L6] {d.rel}:{d.line} 链 {d.obj.id} step {step_index} 引用了非前序步"
                f" steps[{idx}].output（steps[n] 的 n 必须小于当前 step 序号）"
            )
            return vios
        path = m.group(2)
        fields = path.split(".") if path else []
        w = worker_by_id.get(d.obj.steps[idx].worker)
        if w is None:
            return vios  # L5 已报
        cls = _cached_model(cache, w.obj.output.model)
        if cls is None:
            return vios  # L3 已报
        vios.extend(_resolve_path(cls, fields, expr, d))
        return vios
    vios.append(
        f"[L6] {d.rel}:{d.line} 链 {d.obj.id} step {step_index} 的 input 表达式不可静态解析："
        f"{expr!r}（只支持 task.input(.字段) 与 steps[n].output(.字段)）"
    )
    return vios


def _resolve_path(
    model_cls: type[BaseModel], fields: list[str], expr: str, chain_decl: _Decl
) -> list[str]:
    """L6：沿 model_fields 静态下钻字段路径，字段不存在/不能下钻 = 拒载。"""
    vios: list[str] = []
    d = chain_decl
    current = model_cls
    for i, seg in enumerate(fields):
        if seg not in current.model_fields:
            vios.append(
                f"[L6] {d.rel}:{d.line} 链 {d.obj.id} 表达式 {expr!r} 引用的字段 {seg!r}"
                f" 不存在于 Model {current.__name__}"
            )
            return vios
        ann = current.model_fields[seg].annotation
        nxt = _unwrap_model(ann)
        if nxt is None and i < len(fields) - 1:
            vios.append(
                f"[L6] {d.rel}:{d.line} 链 {d.obj.id} 表达式 {expr!r} 的字段 {seg!r}"
                " 不是 Model，无法继续下钻"
            )
            return vios
        if nxt is not None:
            current = nxt
    return vios


def _check_l9(
    violations: list[str],
    root: Path,
    workers: list[_Decl],
    cache: dict[str, tuple[type[BaseModel] | None, str | None]],
) -> None:
    """L9：Model 结构 hash 与工序 version 联动校验。"""
    hashes_path = root / "engine" / "registry" / "model_hashes.yaml"
    records, err = _load_hash_records(hashes_path)
    if err is not None:
        violations.append(err)
        return
    if records is None:
        records = {}
    for d in workers:
        for model_name in {d.obj.input.model, d.obj.output.model}:
            cls = _cached_model(cache, model_name)
            if cls is None:
                continue  # L3 已报
            rec = records.get(model_name)
            if rec is None:
                violations.append(
                    f"[L9] {d.rel}:{d.line} Model {model_name!r} 未登记结构摘要"
                    "——请先运行 registry-check --write-hashes"
                )
                continue
            if rec["hash"] != model_structure_hash(cls):
                violations.append(
                    f"[L9] {d.rel}:{d.line} Model {model_name!r} 结构已变更（hash 不匹配）"
                    "——请 bump 工序 version 并重跑 registry-check --write-hashes"
                )
            elif rec["version"] != d.obj.version:
                violations.append(
                    f"[L9] {d.rel}:{d.line} 工序 {d.obj.id} version={d.obj.version}"
                    f" 与 Model {model_name!r} 登记 version={rec['version']} 不一致"
                    "——请重跑 registry-check --write-hashes"
                )


# ---- 主流程 ----


def _check(root: Path, models_yaml_path: Path | None) -> _CheckResult:
    violations: list[str] = []
    workers = _parse_workers(root, violations)
    chains: list[_Decl] = []
    for path in _find_files(root, "chains", "chain.yaml"):
        chains.extend(_parse_single(path, root, ChainDeclaration, "链", "id", violations))
    providers: list[_Decl] = []
    for path in _find_files(root, "context", None):
        providers.extend(
            _parse_list(path, root, ContextProviderDeclaration, "Context provider", "id", violations)
        )
    events: list[_Decl] = []
    for path in _find_files(root, "events", None):
        events.extend(_parse_list(path, root, EventDeclaration, "事件", "event_type", violations))
    actions: list[_Decl] = []
    for path in _find_files(root, "actions", None):
        actions.extend(_parse_list(path, root, ActionDeclaration, "Action", "action_id", violations))

    worker_by_id = {d.obj.id: d for d in workers}
    chain_by_id = {d.obj.id: d for d in chains}
    provider_by_id = {d.obj.id: d for d in providers}

    # ---- L1 ----
    _check_l1(
        violations,
        [(d.kind, d.obj.id, d.rel, d.line) for d in workers + chains + providers],
    )
    _check_l1(violations, [("事件", d.obj.event_type, d.rel, d.line) for d in events])
    _check_l1(violations, [("Action", d.obj.action_id, d.rel, d.line) for d in actions])

    # ---- L2：工序 context 引用 provider 同域；Action domain 枚举（契约 str 补校验）----
    for d in workers:
        w = d.obj
        for ref in w.context:
            provider = provider_by_id.get(ref.id)
            if provider is None:
                violations.append(
                    f"[L2] {d.rel}:{d.line} 工序 {w.id} 的 context 引用了未登记的 provider {ref.id!r}"
                )
            elif provider.obj.domain != w.domain:
                violations.append(
                    f"[L2] {d.rel}:{d.line} 工序 {w.id}（域 {w.domain.value}）的 context"
                    f" 跨域引用了 provider {ref.id!r}（域 {provider.obj.domain.value}）"
                )
    for d in actions:
        try:
            Domain(d.obj.domain)
        except ValueError:
            violations.append(
                f"[L2] {d.rel}:{d.line} Action {d.obj.action_id} 的 domain {d.obj.domain!r}"
                " 不在枚举内"
            )

    # ---- L3：Model 可 import ----
    cache: dict[str, tuple[type[BaseModel] | None, str | None]] = {}
    model_refs: list[tuple[str, str, int, str]] = []
    for d in workers:
        model_refs.append((f"工序 {d.obj.id} input", d.rel, d.line, d.obj.input.model))
        model_refs.append((f"工序 {d.obj.id} output", d.rel, d.line, d.obj.output.model))
    for d in chains:
        model_refs.append((f"链 {d.obj.id} input", d.rel, d.line, d.obj.input.model))
    for d in providers:
        model_refs.append((f"Context {d.obj.id} params", d.rel, d.line, d.obj.params.model))
        model_refs.append((f"Context {d.obj.id} returns", d.rel, d.line, d.obj.returns.model))
    for d in events:
        model_refs.append((f"事件 {d.obj.event_type} payload", d.rel, d.line, d.obj.payload_model))
    for d in actions:
        model_refs.append((f"Action {d.obj.action_id} output", d.rel, d.line, d.obj.output_model))
    for kind, rel, line, model_name in model_refs:
        cls, reason = _resolve_model(model_name, cache)
        if cls is None:
            violations.append(
                f"[L3] {rel}:{line} {kind} 引用的 Model {model_name!r} 不可导入（{reason}）"
            )

    # ---- L4：model 别名必须注册在 models.yaml（复用 T8 load_models）----
    label = "models.yaml" if models_yaml_path is None else str(models_yaml_path)
    models_path = models_yaml_path if models_yaml_path is not None else root / "models.yaml"
    if not models_path.is_file():
        violations.append(f"[L4] {label}: 未配置模型注册（models.yaml 缺失，拒载）")
    else:
        try:
            model_registry = load_models(models_path)
        except ModelsConfigError as exc:
            violations.append(f"[L4] {label}: models.yaml 拒载：{exc}")
        else:
            aliases = model_registry.aliases
            for d in workers:
                if d.obj.model not in aliases:
                    violations.append(
                        f"[L4] {d.rel}:{d.line} 工序 {d.obj.id} 的 model 别名 {d.obj.model!r}"
                        " 未注册于 models.yaml（模型串内联拒载：只允许引用已注册别名）"
                    )

    # ---- L5：链内 worker 已注册 + 链域一致 ----
    for d in chains:
        for step in d.obj.steps:
            w = worker_by_id.get(step.worker)
            if w is None:
                violations.append(
                    f"[L5] {d.rel}:{d.line} 链 {d.obj.id} 引用了未登记的 worker {step.worker!r}"
                )
            elif w.obj.domain != d.obj.domain:
                violations.append(
                    f"[L5] {d.rel}:{d.line} 链 {d.obj.id} 域 {d.obj.domain.value} 与工序"
                    f" {step.worker} 域 {w.obj.domain.value} 不一致"
                )

    # ---- L6：链步骤 input 表达式 ----
    for d in chains:
        for i, step in enumerate(d.obj.steps):
            if step.input is None:
                continue  # 省略 input = 整链入参直传（§4.2）
            target = worker_by_id.get(step.worker)
            if isinstance(step.input, str):
                violations.extend(_check_expr(step.input, i, d, worker_by_id, cache))
            elif isinstance(step.input, dict):
                if target is None:
                    continue  # L5 已报，取不到 input Model
                tcls = _cached_model(cache, target.obj.input.model)
                if tcls is None:
                    continue  # L3 已报
                for field_name, expr in step.input.items():
                    if field_name not in tcls.model_fields:
                        violations.append(
                            f"[L6] {d.rel}:{d.line} 链 {d.obj.id} step {i} 的 input 字段"
                            f" {field_name!r} 不存在于工序 {step.worker} 的 input Model"
                            f" {tcls.__name__}"
                        )
                        continue
                    violations.extend(_check_expr(expr, i, d, worker_by_id, cache))

    # ---- L7：schema Literal 已拒（_validate 归口 [L7]），loader 无额外判定 ----

    # ---- L8：prompt / config_dir 存在（相对工序目录，且须在仓库内）----
    for d in workers:
        wdir = d.file.parent
        prompt_path = (wdir / d.obj.prompt).resolve()
        if not prompt_path.is_file():
            violations.append(
                f"[L8] {d.rel}:{d.line} 工序 {d.obj.id} 的 prompt 文件不存在：{d.obj.prompt!r}"
            )
        elif not prompt_path.is_relative_to(root):
            violations.append(
                f"[L8] {d.rel}:{d.line} 工序 {d.obj.id} 的 prompt 路径越出仓库：{d.obj.prompt!r}"
            )
        if d.obj.config_dir is not None:
            cfg_path = (wdir / d.obj.config_dir).resolve()
            if not cfg_path.is_dir():
                violations.append(
                    f"[L8] {d.rel}:{d.line} 工序 {d.obj.id} 的 config_dir 目录不存在："
                    f"{d.obj.config_dir!r}"
                )
            elif not cfg_path.is_relative_to(root):
                violations.append(
                    f"[L8] {d.rel}:{d.line} 工序 {d.obj.id} 的 config_dir 路径越出仓库："
                    f"{d.obj.config_dir!r}"
                )

    # ---- L9 ----
    _check_l9(violations, root, workers, cache)

    # ---- L10（§6.3）Action target 白名单 ----
    for d in actions:
        if d.obj.target not in _ACTION_TARGETS:
            violations.append(
                f"[L10] {d.rel}:{d.line} Action {d.obj.action_id} 的 target {d.obj.target!r}"
                f" 非法——合法值 {sorted(_ACTION_TARGETS)}（详设 §6.3 + 详设-v0.3 §5.4）"
            )

    # ---- 事件 trigger_chain 引用断裂（R22：全册引用断裂 = 拒载）----
    for d in events:
        if d.obj.trigger_chain is not None and d.obj.trigger_chain not in chain_by_id:
            violations.append(
                f"[L0] {d.rel}:{d.line} 事件 {d.obj.event_type} 的 trigger_chain"
                f" {d.obj.trigger_chain!r} 引用未登记的链"
            )

    violations.sort()
    registry = None
    if not violations:
        registry = Registry(
            workers={d.obj.id: d.obj for d in workers},
            chains={d.obj.id: d.obj for d in chains},
            context_providers={d.obj.id: d.obj for d in providers},
            events={d.obj.event_type: d.obj for d in events},
            actions={d.obj.action_id: d.obj for d in actions},
        )
    return _CheckResult(violations, registry)


def validate(repo_root, *, models_yaml_path=None) -> list[str]:
    """全册校验，不抛：返回违规行列表（空 = 合规），供 registry-check 打印。"""
    root = Path(repo_root).resolve()
    models_path = None if models_yaml_path is None else Path(models_yaml_path).resolve()
    return _check(root, models_path).violations


def load_registry(repo_root, *, models_yaml_path=None) -> Registry:
    """全册加载 + L1-L9 校验；不合规抛 RegistryLoadError（聚合全部违规）。"""
    root = Path(repo_root).resolve()
    models_path = None if models_yaml_path is None else Path(models_yaml_path).resolve()
    result = _check(root, models_path)
    if result.violations:
        raise RegistryLoadError(result.violations)
    assert result.registry is not None
    return result.registry


def write_hashes(repo_root, *, models_yaml_path=None) -> Path:
    """重写 model_hashes.yaml（L9 登记文件，提交入库；供 --write-hashes 调用）。

    只依赖工序声明的 input/output Model：解析工序（schema 级）→ import Model →
    计算结构 hash → 写 {model: {version: 引用工序最大 version, hash}}。
    """
    root = Path(repo_root).resolve()
    violations: list[str] = []
    workers = _parse_workers(root, violations)
    if violations:
        raise RegistryLoadError(violations)
    versions_by_model: dict[str, set[int]] = {}
    for d in workers:
        for model_name in (d.obj.input.model, d.obj.output.model):
            versions_by_model.setdefault(model_name, set()).add(d.obj.version)
    cache: dict[str, tuple[type[BaseModel] | None, str | None]] = {}
    records: dict[str, dict[str, Any]] = {}
    for model_name in sorted(versions_by_model):
        cls, reason = _resolve_model(model_name, cache)
        if cls is None:
            raise RegistryLoadError(
                [f"[L9] 无法为 Model {model_name!r} 计算结构摘要：{reason}"]
            )
        records[model_name] = {
            "version": max(versions_by_model[model_name]),
            "hash": model_structure_hash(cls),
        }
    hashes_path = root / "engine" / "registry" / "model_hashes.yaml"
    hashes_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 自动生成文件：registry-check --write-hashes 重写，勿手改（详设 §4.4 L9）\n"
        "# hash = Model 字段名+类型结构摘要；version = 引用工序 version\n"
        "# （同一 Model 被多个工序引用时，记录为其中最大 version，各工序须一致）\n"
    )
    body = yaml.safe_dump({"models": records}, sort_keys=False, allow_unicode=True)
    hashes_path.write_text(header + body, encoding="utf-8")
    return hashes_path
