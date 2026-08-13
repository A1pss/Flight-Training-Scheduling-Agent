"""工具注册表：契约 ↔ 实现的接线板（v6 §7.7）。

**契约在本窗口全部定稿，实现分属各自的里程碑**——检索工具是 M5、Planner 工具
是 M4-B、诊断工具接的是 M2-A 已经写好的 `solver/diagnose.py`。所以这里给的是
接线板而不是实现：`ToolSpec` 是唯一的入参真相，`register()` 把实现接上去。

三条注册期规则：

1. 只能注册 `TOOL_CATALOG` 里有的名字——工具表是契约，不是谁想加就能加的口子；
2. 注册前过一遍 `ToolACL.assert_registrable`——六个确定性节点与非 memory 的写
   工具在这一步就被挡住（v6 §7.7.2 最后两行）；
3. 没接线的工具**调用时抛 `ToolNotBoundError`**，不返回空结果。返回空结果的话，
   「M5 忘了接 `vector_search`」会表现成「检索没召回到东西」，排查方向全歪。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from backend.harness.acl import DEFAULT_ACL, ToolACL
from backend.harness.tools import TOOL_CATALOG
from backend.harness.types import ToolHandler, ToolSpec
from backend.llm.types import ToolSchema


class ToolNotBoundError(RuntimeError):
    """工具在目录里，但没有接上实现。

    这是**接线 bug**，不是运行时业务错误：某个里程碑交付时漏接了 handler。
    故意抛得响亮——静默降级会把它伪装成「工具返回了空」。
    """


class ToolRegistry:
    """工具目录 + 已接线的实现。"""

    def __init__(
        self,
        catalog: Mapping[str, ToolSpec] | None = None,
        acl: ToolACL | None = None,
    ) -> None:
        self._catalog: dict[str, ToolSpec] = dict(catalog or TOOL_CATALOG)
        self._acl = acl or DEFAULT_ACL
        self._handlers: dict[str, ToolHandler] = {}
        for spec in self._catalog.values():
            self._acl.assert_registrable(spec)

    # ── 接线 ─────────────────────────────────────────────────────────
    def register(self, name: str, handler: ToolHandler) -> None:
        spec = self.spec(name)
        self._acl.assert_registrable(spec)
        self._handlers[name] = handler

    def register_many(self, handlers: Mapping[str, ToolHandler]) -> None:
        for name, handler in handlers.items():
            self.register(name, handler)

    def unregister(self, name: str) -> None:
        self._handlers.pop(name, None)

    # ── 读 ───────────────────────────────────────────────────────────
    def spec(self, name: str) -> ToolSpec:
        try:
            return self._catalog[name]
        except KeyError as exc:
            raise ToolNotBoundError(
                f"未知工具 {name!r}：不在工具目录中（可用：{sorted(self._catalog)}）"
            ) from exc

    def handler(self, name: str) -> ToolHandler:
        spec = self.spec(name)
        try:
            return self._handlers[spec.name]
        except KeyError as exc:
            raise ToolNotBoundError(
                f"工具 {name!r} 在目录中但没有接上实现 —— 该里程碑的接线漏了"
            ) from exc

    def is_bound(self, name: str) -> bool:
        return name in self._handlers

    def bound_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._catalog))

    def schemas_for(self, names: tuple[str, ...]) -> tuple[ToolSchema, ...]:
        """导出给模型的工具声明。"""
        return tuple(self.spec(name).to_tool_schema() for name in names)


#: 进程级默认注册表。各里程碑在自己的装配点上调 `register()` 接线。
DEFAULT_REGISTRY: Final[ToolRegistry] = ToolRegistry()


__all__ = ["DEFAULT_REGISTRY", "ToolNotBoundError", "ToolRegistry"]
