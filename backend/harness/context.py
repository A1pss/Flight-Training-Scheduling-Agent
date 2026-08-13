"""上下文装配（v6 §7.7.1 第 5 行）。

> 8K 窗口下，规则 + 快照 + 历史必然溢出。策略：**结构化数据只入摘要、明细由
> 工具按需取**；历史消息滑窗 + 关键决策固定保留；超限时按优先级裁剪并记 `WARN`。

四类块，优先级从高到低（数字越小越先保、越后被裁）：

| 优先级 | 块类型 | 处置 |
|---|---|---|
| 0 | `system` 提示词 | **钉住**，永不裁剪 |
| 10 | `decision` 关键决策 | **钉住**——用户说过「周三不排何超」，裁掉它等于失忆 |
| 20 | `summary` 结构化摘要 | 可裁，但裁之前先裁历史与证据 |
| 30 | `history` 历史消息 | 先滑窗（只留最近 N 轮），再按从旧到新裁 |
| 40 | `evidence` 检索片段 | 最先裁——它随时能再检索回来 |

**为什么结构化数据只入摘要**：一份基准周快照展开成 JSON 有几万 token，8K 窗口
放不下，硬塞就会把系统提示词挤掉。摘要只给「8 人 / 8 机 / 12 课目 / 本周 3 项
扰动」这种规模信息，模型要明细就调工具——这也正是工具表存在的理由。

**钉住的块加起来仍然超限**时不静默截断：记一条 `PINNED_OVERFLOW` 警告并如实
返回。这种情况意味着系统提示词或关键决策本身写爆了，是要人去改的 bug，
不是运行时该偷偷抹平的事。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.core.config import Settings, get_settings
from backend.core.logging import get_logger
from backend.harness.tokens import PER_MESSAGE_OVERHEAD, estimate_tokens

BlockKind = Literal["system", "decision", "summary", "history", "evidence"]

#: 各类块的默认优先级（越小越先保）。
DEFAULT_PRIORITY: Final[dict[BlockKind, int]] = {
    "system": 0,
    "decision": 10,
    "summary": 20,
    "history": 30,
    "evidence": 40,
}

#: 钉住不裁的块类型。
PINNED_KINDS: Final[frozenset[BlockKind]] = frozenset({"system", "decision"})

_log = get_logger(__name__)


class ContextBlock(BaseModel):
    """装配的最小单位。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: BlockKind
    content: str = Field(min_length=1)
    role: Literal["system", "user", "assistant"] = "user"
    #: 标签只用于日志与「裁掉了什么」的回执，不进消息体
    label: str = ""
    priority: int | None = None

    @property
    def effective_priority(self) -> int:
        return self.priority if self.priority is not None else DEFAULT_PRIORITY[self.kind]

    @property
    def pinned(self) -> bool:
        return self.kind in PINNED_KINDS

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.content) + PER_MESSAGE_OVERHEAD

    def as_message(self) -> dict[str, str]:
        return {"role": self.role, "content": self.content}


class AssembledContext(BaseModel):
    """装配结果。`dropped` / `warnings` 要进 trace——上下文裁过什么，事后必须查得到。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    messages: tuple[dict[str, str], ...] = ()
    tokens: int = 0
    budget: int = 0
    dropped: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


class ContextAssembler:
    """按 8K 窗口把块装配成消息列表。"""

    def __init__(
        self,
        *,
        num_ctx: int | None = None,
        reserve_output_tokens: int | None = None,
        history_window: int | None = None,
        settings: Settings | None = None,
    ) -> None:
        cfg = settings or get_settings()
        self._num_ctx = num_ctx if num_ctx is not None else cfg.LLM_NUM_CTX
        self._reserve = (
            reserve_output_tokens
            if reserve_output_tokens is not None
            else cfg.HARNESS_RESERVE_OUTPUT_TOKENS
        )
        self._history_window = (
            history_window if history_window is not None else cfg.HARNESS_HISTORY_WINDOW
        )

    @property
    def budget(self) -> int:
        """留给输入的 token 预算 = 上下文窗口 − 给输出留的余量。"""
        return max(self._num_ctx - self._reserve, 1)

    def assemble(self, blocks: Sequence[ContextBlock]) -> AssembledContext:
        kept = list(blocks)
        dropped: list[str] = []
        warnings: list[str] = []

        # ① 历史滑窗：先按轮次砍，再谈 token
        kept, slid = self._slide_history(kept)
        dropped.extend(slid)

        # ② token 裁剪：从优先级最低、最旧的开始
        budget = self.budget
        total = sum(b.tokens for b in kept)
        if total > budget:
            order = sorted(
                range(len(kept)),
                key=lambda i: (-kept[i].effective_priority, i),
            )
            for idx in order:
                if total <= budget:
                    break
                block = kept[idx]
                if block.pinned:
                    continue
                total -= block.tokens
                dropped.append(_describe(block))
                kept[idx] = _TOMBSTONE

            kept = [b for b in kept if b is not _TOMBSTONE]

        if dropped:
            warnings.append(f"上下文超限，已按优先级裁剪 {len(dropped)} 块")
            _log.warning(
                "context_trimmed",
                dropped=list(dropped),
                budget=budget,
                tokens=total,
            )

        if total > budget:
            warnings.append("PINNED_OVERFLOW：钉住的块本身已超预算，未做截断")
            _log.warning("context_pinned_overflow", tokens=total, budget=budget)

        return AssembledContext(
            messages=tuple(b.as_message() for b in kept),
            tokens=total,
            budget=budget,
            dropped=tuple(dropped),
            warnings=tuple(warnings),
        )

    def _slide_history(self, blocks: list[ContextBlock]) -> tuple[list[ContextBlock], list[str]]:
        history_idx = [i for i, b in enumerate(blocks) if b.kind == "history"]
        if len(history_idx) <= self._history_window:
            return blocks, []
        drop_idx = set(history_idx[: len(history_idx) - self._history_window])
        dropped = [_describe(blocks[i]) + "（滑窗）" for i in sorted(drop_idx)]
        return [b for i, b in enumerate(blocks) if i not in drop_idx], dropped


#: 裁剪时的占位（避免边遍历边删导致下标错位）。
_TOMBSTONE: Final[ContextBlock] = ContextBlock(kind="evidence", content="\x00tombstone")


def _describe(block: ContextBlock) -> str:
    return f"{block.kind}:{block.label}" if block.label else block.kind


def structured_summary(title: str, payload: Mapping[str, Any]) -> str:
    """把结构化数据压成一行行摘要（§7.7.1「结构化数据只入摘要」）。

    刻意只吐**规模与关键标记**，不吐明细：明细由工具按需取。列表只报条数与前
    三项，字典只报键数——这样一个快照进上下文是几十 token 而不是几万。
    """
    lines = [f"【{title}】"]
    for key, value in payload.items():
        lines.append(f"- {key}: {_summarize_value(value)}")
    lines.append("（以上为摘要，明细请调用相应工具按需获取）")
    return "\n".join(lines)


def _summarize_value(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        head = "、".join(str(v) for v in list(value)[:3])
        suffix = "…" if len(value) > 3 else ""
        return f"{len(value)} 项" + (f"（{head}{suffix}）" if value else "")
    if isinstance(value, Mapping):
        return f"{len(value)} 项"
    return str(value)


__all__ = [
    "DEFAULT_PRIORITY",
    "PINNED_KINDS",
    "AssembledContext",
    "BlockKind",
    "ContextAssembler",
    "ContextBlock",
    "structured_summary",
]
