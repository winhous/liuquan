"""测试环境 Context provider 桩（详设-v0.1 §13 + 详设-v0.2 §7.5）。

Fake 实现类只住 tests/（规范 R12 + lint P3-4：生产目录出现 Fake/Stub 前缀类
= 拒载）；注册按环境加载：测试读 tests/fake_providers.yaml（生产加载
providers.yaml，路径按环境区分，生产误指桩的防线，详设 v0.1 §13）。

- FakeDemoGreeting：demo.greeting provider（v0.1 已有模式的独立实现，
  供 A10 类 Context 通道测试注入）
- FakeDemoInbox：demo 域「链喂给 AI 的数据集合」provider（决策 16 禁幻觉
  白名单来源，详设-v0.2 §7.5）——provide() 返回业务对象集合（对象带 id），
  whitelist() = 集合内全部对象 id：测试把它注入转交器做数据引用封闭性校验
  ——样本 A（ref_id 命中集合）通过；样本 B（集合外 ref_id）拒落（A21 反向）。

本文件在 P2 扫描对象内（tests/ 只有 fixtures/ 豁免）：零 URL/IP/sk- 字面量、
不读 os.environ、不给敏感名赋非空字面量。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

from models.workers import DemoGreeting

__all__ = [
    "DemoInboxData",
    "DemoInboxObject",
    "FakeDemoGreeting",
    "FakeDemoInbox",
    "load_providers",
    "whitelist_from",
]


class DemoInboxObject(BaseModel):
    """demo.inbox 数据集合里的业务对象（对象 id = 决策 16 白名单成员）。"""

    id: str
    kind: str
    text: str


class DemoInboxData(BaseModel):
    """demo.inbox provider 返回：链喂给 AI 的数据集合（对象 id = 白名单）。"""

    objects: list[DemoInboxObject]


class FakeDemoGreeting:
    """demo.greeting 桩（v0.1 §13 模式：Context 通道测试注入）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def provide(self, params: Any = None) -> DemoGreeting:
        self.calls += 1
        return DemoGreeting(message="hello demo")


class FakeDemoInbox:
    """demo 域「链喂给 AI 的数据集合」桩（决策 16 白名单来源，详设-v0.2 §7.5）。

    whitelist() = 数据集合内全部业务对象 id——测试把它注入转交器做禁幻觉
    数据引用封闭性校验：样本 A（ref_id 命中集合）通过；样本 B（集合外
    ref_id）拒落 + 链 FAILED（A21）。
    """

    def __init__(self) -> None:
        self.objects = [
            DemoInboxObject(id="msg-001", kind="message", text="买家询问物流时效"),
            DemoInboxObject(id="met-001", kind="metric", text="近 7 日转化率 3.2%"),
            DemoInboxObject(id="ord-001", kind="order_view", text="订单含 2 件未发货商品"),
            DemoInboxObject(id="lst-001", kind="listing", text="Listing 标题含侵权词"),
            DemoInboxObject(id="img-001", kind="image", text="主图含促销角标"),
        ]
        self.calls = 0

    async def provide(self, params: Any = None) -> DemoInboxData:
        self.calls += 1
        return DemoInboxData(objects=self.objects)

    def whitelist(self) -> set[str]:
        """白名单来源：数据集合内全部对象 id（决策 16 禁幻觉校验）。"""
        return {obj.id for obj in self.objects}


def load_providers(path: Path | None = None) -> dict[str, Any]:
    """读 tests/fake_providers.yaml -> {provider 实现标识: 桩实例}。

    注册按环境加载（详设 v0.1 §13）：测试加载本文件（生产加载
    providers.yaml）。yaml 值形如 `tests.fake_providers:FakeDemoInbox`——
    类名按本模块命名空间解析（tests/ 非包，点分路径是声明标识，不
    importlib 跨模块；类必须住本文件，P3-4 同款约束）。
    """
    path = path or Path(__file__).parent / "fake_providers.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict), "fake_providers.yaml 应为映射"
    out: dict[str, Any] = {}
    for impl_id, dotted in raw.items():
        cls_name = str(dotted).rsplit(":", 1)[-1]
        try:
            cls = globals()[cls_name]
        except KeyError as exc:  # pragma: no cover  # 注册指向不存在的类 = 配置错误
            raise ValueError(f"fake_providers.yaml 指向未知类 {cls_name!r}") from exc
        out[str(impl_id)] = cls()
    return out


def whitelist_from(providers: dict[str, Any]) -> set[str]:
    """从已加载 provider 实例取白名单：数据集合对象 id 并集（无 inbox 为空集）。"""
    whitelist: set[str] = set()
    for impl in providers.values():
        if hasattr(impl, "whitelist"):
            whitelist |= set(impl.whitelist())
    return whitelist
