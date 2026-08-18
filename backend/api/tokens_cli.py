"""API Token 的生成、散列与轮换（v6 §11.5「认证鉴权」/「机密管理」）。

```bash
# 新发一个 token（打印明文一次，之后只留散列）
python -m backend.api.tokens_cli new --user P01 --role director

# 把现有的明文配置整条转成散列形态
python -m backend.api.tokens_cli hash --tokens "$API_TOKENS"

# 给某个人换一把新 token，其余条目原样保留
python -m backend.api.tokens_cli rotate --user P01 --tokens "$API_TOKENS"
```

## 为什么明文只打印一次

`new` / `rotate` 把明文 token 打到 **stdout**，把要写进 `.env` 的散列条目打到
**stderr 之外的另一段**并标注清楚。明文不落任何文件——落文件就等于又造了一份
需要保护的东西，而它的生命周期是「交给本人，然后忘掉」。

## 为什么不直接改写 `.env`

`.env` 是运维手工维护的文件，里面还有别的键与注释。自动改写它意味着这个 CLI
要理解整份文件的格式，而它出错的后果是**全员登不上**。这里只负责算出那一行，
贴进去这个动作留给人——多花十秒，换掉一整类事故。
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from backend.api.security import (
    ENTRY_SEP,
    FIELD_SEP,
    HASH_PREFIX,
    TokenTable,
    hash_token,
    new_token,
)
from backend.planner.authority import normalize_role


def _entries(raw: str) -> list[tuple[str, str, str]]:
    """把 `API_TOKENS` 拆成 (第一段, user_id, role) 三元组列表。

    走一遍 :meth:`TokenTable.parse` 是刻意的：**格式错误在这里就要炸**，
    而不是等到某天进程启动时才炸。
    """
    TokenTable.parse(raw)  # 格式校验，产物不用
    out: list[tuple[str, str, str]] = []
    for chunk in raw.split(ENTRY_SEP):
        item = chunk.strip()
        if not item:
            continue
        head, user_id, role = (p.strip() for p in item.split(FIELD_SEP))
        out.append((head, user_id, role))
    return out


def _render(entries: Sequence[tuple[str, str, str]]) -> str:
    return ENTRY_SEP.join(FIELD_SEP.join(item) for item in entries)


def cmd_new(args: argparse.Namespace) -> int:
    role = normalize_role(args.role)
    token = new_token()
    entry = FIELD_SEP.join((hash_token(token), args.user, role))
    print("── 交给本人的明文 token（只显示这一次）──")
    print(token)
    print()
    print("── 追加到 .env 的 API_TOKENS 里 ──")
    print(entry)
    return 0


def cmd_hash(args: argparse.Namespace) -> int:
    converted: list[tuple[str, str, str]] = []
    changed = 0
    for head, user_id, role in _entries(args.tokens):
        if head.startswith(HASH_PREFIX):
            converted.append((head, user_id, role))
            continue
        converted.append((hash_token(head), user_id, role))
        changed += 1
    print(_render(converted))
    print(f"\n── 已把 {changed} 条明文条目转成散列 ──", file=sys.stderr)
    return 0


def cmd_rotate(args: argparse.Namespace) -> int:
    entries = _entries(args.tokens)
    if not any(user_id == args.user for _, user_id, _ in entries):
        print(f"API_TOKENS 里没有用户 {args.user}", file=sys.stderr)
        return 2
    token = new_token()
    rotated = [
        (hash_token(token), user_id, role) if user_id == args.user else (head, user_id, role)
        for head, user_id, role in entries
    ]
    print("── 交给本人的新明文 token（只显示这一次）──")
    print(token)
    print()
    print("── 替换 .env 里整行 API_TOKENS ──")
    print(_render(rotated))
    print(
        f"\n── {args.user} 的旧 token 在这一行生效后立即失效 ──",
        file=sys.stderr,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m backend.api.tokens_cli",
        description="FTS API Token 的生成 / 散列 / 轮换",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_new = sub.add_parser("new", help="新发一个 token")
    p_new.add_argument("--user", required=True, help="user_id，如 P01")
    p_new.add_argument("--role", required=True, help="viewer / scheduler / director / admin")
    p_new.set_defaults(func=cmd_new)

    p_hash = sub.add_parser("hash", help="把明文条目转成散列条目")
    p_hash.add_argument("--tokens", required=True, help="现有的 API_TOKENS 整串")
    p_hash.set_defaults(func=cmd_hash)

    p_rot = sub.add_parser("rotate", help="给某个用户换一把新 token")
    p_rot.add_argument("--user", required=True)
    p_rot.add_argument("--tokens", required=True, help="现有的 API_TOKENS 整串")
    p_rot.set_defaults(func=cmd_rotate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


__all__ = ["build_parser", "cmd_hash", "cmd_new", "cmd_rotate", "main"]


if __name__ == "__main__":  # pragma: no cover - 入口薄封装
    sys.exit(main())
