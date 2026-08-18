"""`python -m backend.api.tokens_cli` —— Token 的生成 / 散列 / 轮换（v6 §11.5）。

这个 CLI 是**运维每次发账号都要用**的东西，而它出错的形态很难被发现：
散列算错 → 本人登不上；轮换时漏掉一条 → 别人被无声踢掉。所以三个子命令
逐个测，且**每条都验证「产物真的能用来登录」**（把打印出来的条目喂回
`TokenTable` 再 `resolve` 一次），而不只是比对字符串形状。
"""

from __future__ import annotations

import pytest

from backend.api.security import HASH_PREFIX, AuthError, TokenTable, hash_token, new_token
from backend.api.tokens_cli import main

VALID = "tok-a:P01:director,tok-b:P02:scheduler"


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _entry_line(stdout: str) -> str:
    """取输出里那一行 `…:user:role` 配置条目。"""
    for line in reversed(stdout.splitlines()):
        if line.count(":") >= 2 and not line.startswith("──"):
            return line.strip()
    raise AssertionError(f"输出里找不到配置条目：\n{stdout}")


# ═════════════════════════════════════════════════════════════════════
# new
# ═════════════════════════════════════════════════════════════════════
def test_new_prints_a_usable_token_and_a_hashed_entry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """★ 明文能登进去，且配置里存的是散列。"""
    code, out, _ = _run(capsys, "new", "--user", "P07", "--role", "director")
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip() and not line.startswith("──")]
    plaintext, entry = lines[0].strip(), lines[1].strip()

    assert entry.startswith(HASH_PREFIX), "配置条目必须是散列形态"
    assert plaintext not in entry, "明文不许出现在配置条目里"

    # 产物真的能用：拿这一行建表，用明文 resolve
    principal = TokenTable.parse(entry).resolve(plaintext)
    assert principal.user_id == "P07"
    assert principal.role == "director"


def test_new_rejects_an_unknown_role(capsys: pytest.CaptureFixture[str]) -> None:
    """角色不做默认 —— 认不出就抛，不悄悄给个 viewer。"""
    with pytest.raises(ValueError):
        _run(capsys, "new", "--user", "P07", "--role", "超级管理员")


def test_new_tokens_are_never_the_same(capsys: pytest.CaptureFixture[str]) -> None:
    first = _entry_line(_run(capsys, "new", "--user", "P07", "--role", "viewer")[1])
    second = _entry_line(_run(capsys, "new", "--user", "P07", "--role", "viewer")[1])
    assert first != second


# ═════════════════════════════════════════════════════════════════════
# hash
# ═════════════════════════════════════════════════════════════════════
def test_hash_converts_every_plaintext_entry(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _run(capsys, "hash", "--tokens", VALID)
    assert code == 0
    converted = out.strip()
    table = TokenTable.parse(converted)
    assert table.plaintext_users == (), "转换后不该还有明文条目"
    # ★ 原来的明文仍然能登进去 —— 散列只是换了存储形态，不是换了口令
    assert table.resolve("tok-a").user_id == "P01"
    assert table.resolve("tok-b").role == "scheduler"
    assert "2 条" in err


def test_hash_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    """已经是散列的条目再跑一遍不变 —— 否则重复执行会把人锁在外面。"""
    once = _run(capsys, "hash", "--tokens", VALID)[1].strip()
    twice = _run(capsys, "hash", "--tokens", once)[1].strip()
    assert once == twice
    assert "0 条" in _run(capsys, "hash", "--tokens", once)[2]


def test_hash_rejects_a_malformed_table(capsys: pytest.CaptureFixture[str]) -> None:
    """格式不对就抛，**不跳过那一条** —— 跳过等于某人的 token 悄悄失效。"""
    with pytest.raises(ValueError):
        _run(capsys, "hash", "--tokens", "只有两段:P01")


# ═════════════════════════════════════════════════════════════════════
# rotate
# ═════════════════════════════════════════════════════════════════════
def test_rotate_replaces_one_and_keeps_the_rest(capsys: pytest.CaptureFixture[str]) -> None:
    """★ 换 P01 的，P02 的原样保留（**别人不该被无声踢掉**）。"""
    code, out, err = _run(capsys, "rotate", "--user", "P01", "--tokens", VALID)
    assert code == 0
    lines = [line for line in out.splitlines() if line.strip() and not line.startswith("──")]
    new_plain, rendered = lines[0].strip(), lines[1].strip()

    table = TokenTable.parse(rendered)
    assert table.resolve(new_plain).user_id == "P01", "新 token 登不进去"
    assert table.resolve("tok-b").user_id == "P02", "没被轮换的人不该受影响"
    with pytest.raises(AuthError):
        table.resolve("tok-a")  # 旧的立即失效
    assert "P01" in err and "失效" in err


def test_rotate_refuses_an_unknown_user(capsys: pytest.CaptureFixture[str]) -> None:
    code, _, err = _run(capsys, "rotate", "--user", "P99", "--tokens", VALID)
    assert code == 2
    assert "P99" in err


def test_rotate_output_has_no_plaintext_left_in_the_config(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """轮换后那一条是散列的（其余条目保持原样，包括原本的明文）。"""
    out = _run(capsys, "rotate", "--user", "P01", "--tokens", VALID)[1]
    rendered = [line for line in out.splitlines() if line.strip() and not line.startswith("──")][1]
    rotated = next(item for item in rendered.split(",") if ":P01:" in item)
    assert rotated.startswith(HASH_PREFIX)


# ═════════════════════════════════════════════════════════════════════
# 散列函数本身
# ═════════════════════════════════════════════════════════════════════
def test_hash_token_is_stable_and_prefixed() -> None:
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc").startswith(HASH_PREFIX)
    assert len(hash_token("abc")) == len(HASH_PREFIX) + 64


def test_new_token_has_enough_entropy() -> None:
    """256 bit 随机串 —— 这正是「可以用裸 sha256 而不必慢哈希」的前提。"""
    token = new_token()
    assert len(token) >= 43
    assert len({new_token() for _ in range(20)}) == 20


def test_cli_requires_a_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main([])
