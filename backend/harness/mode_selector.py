"""双模式调用的模式选择（v6 §7.7.1 第 2 行）。

> 主用 Ollama 原生 tool calling；当运行时统计显示某组件的解析失败率超阈值，
> 自动切换为「受约束 JSON 解码」模式（`format=<schema>`）。
> **模式由统计驱动，不写死在配置里。**

所以这里没有 `LLM_CALL_MODE=native` 这种开关，只有一个滑窗统计器：

- 每个组件独立一个**定长滑窗**（默认 20 次），窗内记 True/False；
- 样本数够（默认 ≥5）且失败率 ≥ 切换阈值（默认 0.30）→ 切 `constrained_json`；
- 切过去以后，失败率回落到恢复阈值（默认 0.10）以下才切回 `native`。

**两个阈值不是一个数**，这是有意的：只用一个阈值，失败率在阈值附近抖动时模式
会来回翻，而翻模式意味着提示词形态、输出形态、解析路径全变一遍，抖动会被放大
成两种失败交替出现。滞回（hysteresis）把这条路堵死。

切模式是有代价的：`constrained_json` 拿不到原生 tool call 的结构，得靠 schema
约束模型吐一个 `{"tool": ..., "arguments": {...}}`，表达力更弱；所以它是**降级**
手段，不是默认路径。统计好了就该切回去。
"""

from __future__ import annotations

from collections import deque
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings
from backend.harness.types import ComponentName
from backend.llm.types import CallMode

DEFAULT_WINDOW: Final[int] = 20
DEFAULT_SWITCH_THRESHOLD: Final[float] = 0.30
DEFAULT_RECOVER_THRESHOLD: Final[float] = 0.10
DEFAULT_MIN_SAMPLES: Final[int] = 5


class ModeStats(BaseModel):
    """某组件当前的模式与统计口径（进 trace，供事后复盘为什么切了）。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    component: ComponentName
    mode: CallMode
    window_size: int = Field(ge=0)
    failures: int = Field(ge=0)
    failure_rate: float = Field(ge=0.0, le=1.0)
    switched: bool = False


class ModeSelector:
    """按运行时解析失败率决定调用模式。"""

    def __init__(
        self,
        *,
        window: int = DEFAULT_WINDOW,
        switch_threshold: float = DEFAULT_SWITCH_THRESHOLD,
        recover_threshold: float = DEFAULT_RECOVER_THRESHOLD,
        min_samples: int = DEFAULT_MIN_SAMPLES,
    ) -> None:
        if not 0.0 < recover_threshold <= switch_threshold <= 1.0:
            raise ValueError(
                f"阈值必须满足 0 < 恢复({recover_threshold}) ≤ 切换({switch_threshold}) ≤ 1"
            )
        self._window = window
        self._switch = switch_threshold
        self._recover = recover_threshold
        self._min_samples = min_samples
        self._history: dict[ComponentName, deque[bool]] = {}
        self._mode: dict[ComponentName, CallMode] = {}

    @classmethod
    def from_settings(cls, cfg: Settings | None = None) -> ModeSelector:
        settings = cfg or get_settings()
        return cls(
            window=settings.HARNESS_MODE_WINDOW,
            switch_threshold=settings.HARNESS_MODE_SWITCH_THRESHOLD,
            recover_threshold=settings.HARNESS_MODE_RECOVER_THRESHOLD,
            min_samples=settings.HARNESS_MODE_MIN_SAMPLES,
        )

    # ── 决策 ─────────────────────────────────────────────────────────
    def pick(self, component: ComponentName) -> CallMode:
        """取当前模式。未攒够样本一律 `native`（主用原生 tool calling）。"""
        return self._mode.get(component, "native")

    def stats(self, component: ComponentName) -> ModeStats:
        window = self._history.get(component, deque())
        failures = sum(1 for ok in window if not ok)
        size = len(window)
        return ModeStats(
            component=component,
            mode=self.pick(component),
            window_size=size,
            failures=failures,
            failure_rate=(failures / size if size else 0.0),
            switched=self.pick(component) != "native",
        )

    # ── 统计 ─────────────────────────────────────────────────────────
    def report_success(self, component: ComponentName) -> CallMode:
        return self._record(component, ok=True)

    def report_failure(self, component: ComponentName) -> CallMode:
        return self._record(component, ok=False)

    def _record(self, component: ComponentName, *, ok: bool) -> CallMode:
        window = self._history.setdefault(component, deque(maxlen=self._window))
        window.append(ok)
        return self._reevaluate(component, window)

    def _reevaluate(self, component: ComponentName, window: deque[bool]) -> CallMode:
        if len(window) < self._min_samples:
            return self.pick(component)

        rate = sum(1 for ok in window if not ok) / len(window)
        current = self.pick(component)
        if current == "native" and rate >= self._switch:
            self._mode[component] = "constrained_json"
        elif current == "constrained_json" and rate <= self._recover:
            self._mode[component] = "native"
        return self.pick(component)

    def reset(self, component: ComponentName | None = None) -> None:
        """清空统计。跨请求不共享统计时用得上（默认是进程级共享的）。"""
        if component is None:
            self._history.clear()
            self._mode.clear()
            return
        self._history.pop(component, None)
        self._mode.pop(component, None)


__all__ = [
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_RECOVER_THRESHOLD",
    "DEFAULT_SWITCH_THRESHOLD",
    "DEFAULT_WINDOW",
    "ModeSelector",
    "ModeStats",
]
