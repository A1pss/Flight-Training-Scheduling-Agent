"""松弛档位的授权门槛（v6 §3.10 + §7.3.3 的 `check_authority`）。

v6 §3.10 的松弛阶梯与 §3.9.3 的提案表一起，把「谁能批哪一档」定死了：

| 档 | 动作 | 触及规则 | 等级 | 授权 |
|---|---|---|---|---|
| Tier 0 | 全硬约束 | — | — | 无需授权 |
| Tier 1 | 约束13 频率窗口降级为软目标 | 13 | R2 | 排班员 |
| Tier 2 | Tier1 + 约束3 整体降级为软目标（D-6 重定义） | 3, 13 | R2 | 排班员 |
| Tier 3 | Tier2 + 经授权放宽 R1（约束10/11/12） | 10, 11, 12 | R1 | **训练主任** |

**R0 不在这张表里，这是刻意的**：约束 1/2/4/5/6/7/8/9 绝不可松弛，
「代码层硬编码禁止，UI 不提供选项」。所以本模块没有「R0 需要谁批」这个问题
——它不是权限问题，没有任何角色能批。
`RelaxationProposal` 的契约层已经把 `rule_tier == "R0"` 直接判非法。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from backend.schemas.intent import UserRole

#: RBAC 四角色的排序（v6 §11.5 / §7.4 `user_role`）。数字越大权限越高。
ROLE_RANK: Final[dict[UserRole, int]] = {
    "viewer": 0,
    "scheduler": 1,
    "director": 2,
    "admin": 3,
}

#: 中文角色名 ↔ 枚举（`CheckAuthorityParams.actor_role` 用中文，黑板用英文）
ROLE_ALIASES: Final[dict[str, UserRole]] = {
    "查看者": "viewer",
    "排班员": "scheduler",
    "训练主任": "director",
    "管理员": "admin",
}

ROLE_LABELS: Final[dict[UserRole, str]] = {v: k for k, v in ROLE_ALIASES.items()}

#: 档位 → 所需最低角色（v6 §3.10 / §3.9.3 的「授权」列）
RELAX_TIER_AUTHORITY: Final[dict[int, UserRole]] = {
    0: "viewer",
    1: "scheduler",
    2: "scheduler",
    3: "director",
}

#: 各档触及的规则编号，写进 Sheet 4 的松弛记录
TIER_RULES: Final[dict[int, tuple[int, ...]]] = {
    0: (),
    1: (13,),
    2: (3, 13),
    3: (3, 10, 11, 12, 13),
}

TIER_ACTIONS: Final[dict[int, str]] = {
    0: "全硬约束",
    1: "约束13 的频率窗口降级为软目标（允许欠账，最大化完成度）",
    2: "Tier1 + 约束3「A 类每周必飞」整体降级为软目标",
    3: "Tier2 + 经授权放宽 R1（约束10/11/12）",
}


@dataclass(frozen=True)
class AuthorityCheck:
    """一次授权核对的结果。`reason` 原样进 `open_questions` 与审计。"""

    tier: int
    actor_role: UserRole
    required_role: UserRole
    granted: bool
    reason: str


def normalize_role(role: UserRole | str) -> UserRole:
    """中文角色名或英文枚举 → 枚举。认不出就抛，**不默认成排班员**。

    默认成任何一个角色都是错的：默认高了等于凭空发权限，默认低了等于把合法
    操作挡住而且没人知道为什么。
    """
    if role in ROLE_RANK:
        return role
    resolved = ROLE_ALIASES.get(role)
    if resolved is None:
        raise ValueError(
            f"未知角色 {role!r}，合法取值：{sorted(ROLE_RANK)} 或 {sorted(ROLE_ALIASES)}"
        )
    return resolved


def required_role_for(tier: int) -> UserRole:
    """某档松弛所需的最低角色。档位越界即抛——不存在的档位没有默认授权。"""
    try:
        return RELAX_TIER_AUTHORITY[tier]
    except KeyError as exc:
        raise ValueError(f"松弛档位必须在 0~3，实际 {tier}") from exc


def check_authority(tier: int, actor_role: str) -> AuthorityCheck:
    """核对某角色能否预授权某一档（v6 §7.3.3 第 ② 步）。"""
    role = normalize_role(actor_role)
    required = required_role_for(tier)
    granted = ROLE_RANK[role] >= ROLE_RANK[required]
    if granted:
        reason = f"Tier{tier} 需 {ROLE_LABELS[required]}，当前角色 {ROLE_LABELS[role]}，已授权"
    else:
        reason = (
            f"Tier{tier} 需 {ROLE_LABELS[required]} 授权，"
            f"当前角色 {ROLE_LABELS[role]} 权限不足，已移出预授权"
        )
    return AuthorityCheck(
        tier=tier, actor_role=role, required_role=required, granted=granted, reason=reason
    )


def authorized_tiers(tiers: list[int], actor_role: str) -> tuple[list[int], list[str]]:
    """按角色过滤预授权档位，返回 (放行的档位, 被移出的理由)。

    **被移出的档位必须留下理由**（v6 §7.3.3 把它们塞进 `open_questions`）：
    静默丢掉的话，用户会看到一个「按 Tier 3 排」的请求被当成 Tier 0 跑出
    INFEASIBLE，却完全不知道是权限被挡了。
    """
    kept: list[int] = []
    reasons: list[str] = []
    for tier in sorted(set(tiers)):
        result = check_authority(tier, actor_role)
        if result.granted:
            kept.append(tier)
        else:
            reasons.append(result.reason)
    return kept, reasons


__all__ = [
    "RELAX_TIER_AUTHORITY",
    "ROLE_ALIASES",
    "ROLE_LABELS",
    "ROLE_RANK",
    "TIER_ACTIONS",
    "TIER_RULES",
    "AuthorityCheck",
    "authorized_tiers",
    "check_authority",
    "normalize_role",
    "required_role_for",
]
