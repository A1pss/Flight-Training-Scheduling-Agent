"""token 估算（v6 §7.7.1 的预算控制与上下文装配都要它）。

**先说清楚这是什么，免得被当成实测数**：本模块给的是**估算**，用于两件事——
装配上下文时决定裁到哪里、预算闸在请求发出**之前**拦一次。真实 token 数只有
模型自己知道，`OllamaProvider` 会在响应里带回 `prompt_eval_count` /
`eval_count`，Harness 拿到后**用实测值改写账本**（`BudgetLedger.settle`）。

所以口径是：**估算用于事前拦截，实测用于事后记账**。报告里出现的 token 数
一律取实测（铁律 6）；`mock` / `replay` 两态没有实测值，账本退回估算，
并在账本里标记 `estimated=True`，不许把它当实测数往上报。

估算规则（对 Qwen2.5 的 BPE 词表是个够用的近似）：

- CJK 字符：1 字 ≈ 1 token
- 其余（ASCII 单词、数字、标点）：4 字符 ≈ 1 token
- 每条消息额外 +4（role 与分隔符的固定开销）
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

#: 每条消息的固定开销（role 标记 + 分隔符）。
PER_MESSAGE_OVERHEAD: Final[int] = 4

_CJK_RANGES: Final[tuple[tuple[int, int], ...]] = (
    (0x3000, 0x303F),  # CJK 标点
    (0x3400, 0x4DBF),  # 扩展 A
    (0x4E00, 0x9FFF),  # 基本区
    (0xF900, 0xFAFF),  # 兼容表意
    (0xFF00, 0xFFEF),  # 全角形式
)


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


def estimate_tokens(text: str) -> int:
    """估算一段文本的 token 数。确定性函数：同输入必然同输出（铁律 9）。"""
    cjk = 0
    other = 0
    for ch in text:
        if _is_cjk(ch):
            cjk += 1
        else:
            other += 1
    return cjk + (other + 3) // 4


def estimate_messages(messages: Sequence[dict[str, str]]) -> int:
    """估算一组消息的 token 数。"""
    return sum(
        estimate_tokens(m.get("content", ""))
        + estimate_tokens(m.get("role", ""))
        + PER_MESSAGE_OVERHEAD
        for m in messages
    )


__all__ = ["PER_MESSAGE_OVERHEAD", "estimate_messages", "estimate_tokens"]
