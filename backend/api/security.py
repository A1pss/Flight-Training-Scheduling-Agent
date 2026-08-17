"""认证与鉴权（v6 §9.1「认证鉴权」那一列）。

## 为什么是静态 Token

全离线内网部署，没有 IdP、没有 LDAP、没有外网 OIDC，而 v6 §9.1 一个用户管理
端点都没有。业务方 2026-08-18 选定：`.env` 里配 `token:user_id:role`，
`Authorization: Bearer <token>` 带上来。四个角色沿用既有的
`viewer / scheduler / director / admin`（`HumanDecision.role` 与
`planner/authority.py` 早就是这四个，不另起一套）。

## 三条不肯让步的地方

1. **没配 Token = 全部拒绝**，不是全部放行。「内网所以不校验」这种默认值，
   出事时没有任何痕迹说明鉴权曾经存在过。
2. **Token 比对用 `secrets.compare_digest`**，不用 `==`。字符串比较会在第一个
   不同的字节短路，时序差可测。
3. **角色不做默认**。`normalize_role` 认不出就抛（v6 §7.3.3 那条注释写得很清楚：
   默认高了凭空发权限，默认低了挡住合法操作还没人知道为什么）。

## 端点 → 最低角色

| 端点 | 最低角色 | 理由 |
|---|---|---|
| `GET /jobs` `/runs` `/plans` `/changeset` `/export` | `viewer` | 只读 |
| `POST /ingest` `/confirm` `/chat` `/schedule` `/reject` | `scheduler` | 会改数据或占资源 |
| `POST /approve` | `director` | 归档 + 推进进度 + 结算欠账，是本系统唯一不可撤销的写 |

`approve` 里若带 `authorized_tiers`，还要**逐档**再核一次
（`RELAX_TIER_AUTHORITY`：Tier 3 需训练主任）——角色够格进这个端点，不等于
够格批那一档。
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Final

from backend.core.config import Settings, get_settings
from backend.planner.authority import RELAX_TIER_AUTHORITY, ROLE_RANK, normalize_role
from backend.schemas.intent import UserRole

#: 三段式配置的分隔符。`,` 分条、`:` 分段。
ENTRY_SEP: Final[str] = ","
FIELD_SEP: Final[str] = ":"


@dataclass(frozen=True)
class Principal:
    """一次请求的调用者身份。"""

    user_id: str
    role: UserRole

    def can(self, minimum: UserRole) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[minimum]

    def can_authorize_tier(self, tier: int) -> bool:
        required = RELAX_TIER_AUTHORITY.get(tier)
        if required is None:
            return False
        return ROLE_RANK[self.role] >= ROLE_RANK[required]


class AuthError(Exception):
    """认证/鉴权失败。由 `backend/api/main.py` 的处理器翻成 401/403。

    **不派生自 `FTSError`**：v6 §9.3 的错误码表里没有为鉴权单列一个码，
    而硬塞进某个业务码会让日志里「谁没登录」和「谁的数据错了」混在一起。
    与 `skills_loader/loader.py::SkillError` 同一处置。
    """

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class TokenTable:
    """`token → Principal` 的只读表。进程启动时解析一次。"""

    def __init__(self, entries: dict[str, Principal]) -> None:
        self._entries = dict(entries)

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def empty(self) -> bool:
        return not self._entries

    @classmethod
    def parse(cls, raw: str) -> TokenTable:
        """解析 `token:user:role,token2:user2:role2`。

        **格式不对就抛**，不跳过那一条——跳过等于某个人的 token 悄悄失效，
        而他会在半夜发现自己登不上，且日志里什么都没有。
        """
        entries: dict[str, Principal] = {}
        for chunk in raw.split(ENTRY_SEP):
            item = chunk.strip()
            if not item:
                continue
            parts = item.split(FIELD_SEP)
            if len(parts) != 3:
                raise ValueError(f"API_TOKENS 条目格式必须是 token:user_id:role，实际 {item!r}")
            token, user_id, role = (p.strip() for p in parts)
            if not token or not user_id:
                raise ValueError(f"API_TOKENS 条目的 token 与 user_id 不得为空：{item!r}")
            entries[token] = Principal(user_id=user_id, role=normalize_role(role))
        return cls(entries)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> TokenTable:
        return cls.parse((settings or get_settings()).API_TOKENS)

    def resolve(self, token: str) -> Principal:
        """比对 token。**常数时间比对**，不匹配抛 401。"""
        if self.empty:
            raise AuthError(
                "服务端未配置 API_TOKENS，全部请求一律拒绝。"
                "请在 .env 里配置 `token:user_id:role`（v6 §9.1）",
                status_code=401,
            )
        for known, principal in self._entries.items():
            if secrets.compare_digest(known, token):
                return principal
        raise AuthError("Token 无效", status_code=401)


def require_role(principal: Principal, minimum: UserRole, *, action: str) -> None:
    """鉴权。不够格抛 403，**并把差距说清楚**（谁、是什么角色、要什么角色）。"""
    if not principal.can(minimum):
        raise AuthError(
            f"{principal.user_id}（{principal.role}）无权{action}，需要 {minimum} 及以上",
            status_code=403,
        )


def parse_bearer(header: str | None) -> str:
    """从 `Authorization: Bearer xxx` 里取 token。缺失或格式不对抛 401。"""
    if not header:
        raise AuthError("缺少 Authorization 头（需要 `Bearer <token>`）", status_code=401)
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("Authorization 头格式必须是 `Bearer <token>`", status_code=401)
    return token.strip()


__all__ = [
    "AuthError",
    "Principal",
    "TokenTable",
    "parse_bearer",
    "require_role",
]
