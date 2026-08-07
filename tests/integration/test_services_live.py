"""裸装依赖服务的联通性集成测试（v6 §12.1）。

**本项目不用 testcontainers**——无 Docker 环境下它不可用。集成测试直连
5433/6380 上的裸装实例，用完即清（v6 §12.1 脚注）。

标 `integration` 的用例需要 `deploy/native/start_pg.sh` 与 `start_redis.sh`
已经跑过；标 `ollama` 的还需要 `start_ollama.sh` 且模型已拉取。
CI 只起 PG/Redis，不起 Ollama、不依赖 GPU。
"""

from __future__ import annotations

import uuid

import pytest

from backend.core.config import Settings
from backend.llm.ollama import OllamaProvider

CFG = Settings(_env_file=None)  # type: ignore[call-arg]


# ─────────────────────────────────────────────────────────────────────
# PostgreSQL 16 @ 127.0.0.1:5433
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_postgres_reachable_and_is_v16() -> None:
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(
            host=CFG.PG_HOST,
            port=CFG.PG_PORT,
            user=CFG.PG_USER,
            dbname=CFG.PG_DATABASE,
            connect_timeout=5,
        )
    except Exception as exc:
        pytest.skip(f"PG 不可连（先跑 deploy/native/start_pg.sh）：{exc}")

    with conn, conn.cursor() as cur:
        cur.execute("SHOW server_version")
        row = cur.fetchone()
        assert row is not None
        assert str(row[0]).startswith("16."), f"要求 PostgreSQL 16，实际 {row[0]}"
    conn.close()


@pytest.mark.integration
def test_postgres_temp_schema_roundtrip() -> None:
    """建临时 schema → 写读 → drop，验证事务与权限（测完即清）。"""
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = psycopg.connect(
            host=CFG.PG_HOST,
            port=CFG.PG_PORT,
            user=CFG.PG_USER,
            dbname=CFG.PG_DATABASE,
            connect_timeout=5,
        )
    except Exception as exc:
        pytest.skip(f"PG 不可连：{exc}")

    # 注意：psycopg3 的 `with conn:` 在退出时**关闭**连接，不能对同一个连接
    # 用两次 with —— 清理块会拿到一个已关闭的连接。这里手工管理事务。
    schema = f"fts_test_{uuid.uuid4().hex[:8]}"
    try:
        with conn.cursor() as cur:
            cur.execute(f'CREATE SCHEMA "{schema}"')
            cur.execute(f'CREATE TABLE "{schema}".probe (id int primary key, note text)')
            cur.execute(f'INSERT INTO "{schema}".probe VALUES (1, %s)', ("基准周 2026W02",))
            cur.execute(f'SELECT note FROM "{schema}".probe WHERE id = 1')
            row = cur.fetchone()
            assert row is not None and row[0] == "基准周 2026W02"
        conn.commit()
    finally:
        with conn.cursor() as cur:
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
        conn.close()


# ─────────────────────────────────────────────────────────────────────
# Redis 7 @ 127.0.0.1:6380
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.integration
def test_redis_reachable_and_is_v7() -> None:
    redis_mod = pytest.importorskip("redis")
    client = redis_mod.Redis(
        host=CFG.REDIS_HOST, port=CFG.REDIS_PORT, db=CFG.REDIS_DB, socket_connect_timeout=5
    )
    try:
        assert client.ping() is True
    except Exception as exc:
        pytest.skip(f"Redis 不可连（先跑 deploy/native/start_redis.sh）：{exc}")

    version = str(client.info("server")["redis_version"])
    assert version.startswith("7."), f"要求 Redis 7，实际 {version}"


@pytest.mark.integration
def test_redis_lock_roundtrip() -> None:
    """(tenant, week) 分布式锁的最小验证（v6 §9.2）。"""
    redis_mod = pytest.importorskip("redis")
    client = redis_mod.Redis(
        host=CFG.REDIS_HOST, port=CFG.REDIS_PORT, db=CFG.REDIS_DB, socket_connect_timeout=5
    )
    try:
        client.ping()
    except Exception as exc:
        pytest.skip(f"Redis 不可连：{exc}")

    key = f"fts:test:lock:{uuid.uuid4().hex[:8]}"
    try:
        assert client.set(key, "held", nx=True, ex=30) is True
        # 同一把锁不得被第二个持有者拿到 —— 防止两人同时排同一周
        assert client.set(key, "other", nx=True, ex=30) is None
    finally:
        client.delete(key)


# ─────────────────────────────────────────────────────────────────────
# Ollama @ 127.0.0.1:11434（需 GPU，CI 不跑）
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.ollama
def test_ollama_model_present() -> None:
    provider = OllamaProvider(CFG)
    try:
        models = provider.list_models()
    except Exception as exc:
        pytest.skip(f"Ollama 不可连（先跑 deploy/native/start_ollama.sh）：{exc}")
    assert CFG.LLM_MODEL in models, f"模型 {CFG.LLM_MODEL} 未拉取，实际 {models}"


@pytest.mark.ollama
def test_ollama_answers_in_chinese() -> None:
    """出口标准：`ollama run ... "你好"` 能出中文。"""
    provider = OllamaProvider(CFG)
    try:
        reply = provider.complete([{"role": "user", "content": "你好，请用中文回答：你是谁？"}])
    except Exception as exc:
        pytest.skip(f"Ollama 不可用：{exc}")

    assert reply.strip(), "返回为空"
    han = sum(1 for ch in reply if "一" <= ch <= "鿿")
    assert han >= 5, f"返回中文字符过少（{han} 个）：{reply[:120]}"


@pytest.mark.ollama
def test_ollama_digest_matches_env_example() -> None:
    """模型完整性：digest 与 `.env.example` 登记值一致（v6 §11.5 防模型被替换）。

    口径与 `deploy/native/healthcheck.sh` 一致：取 `OLLAMA_MODELS` 下该模型
    **manifest 文件的 sha256**，与 `ollama list` 的 ID 列同源。
    不用 `/api/show` 的 digest 字段——Ollama v0.6.8 根本不返回它。
    """
    import hashlib

    from backend.core.config import PROJECT_ROOT

    repo, _, tag = CFG.LLM_MODEL.partition(":")
    manifest = CFG.OLLAMA_MODELS / "manifests/registry.ollama.ai/library" / repo / tag
    if not manifest.is_file():
        pytest.skip(f"模型 manifest 不存在（未拉取）：{manifest}")
    actual = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()

    expected = ""
    for line in (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("LLM_MODEL_DIGEST="):
            expected = line.split("=", 1)[1].strip()
    if not expected:
        pytest.skip(".env.example 尚未登记 LLM_MODEL_DIGEST")
    assert actual == expected, f"模型 digest 不符：期望 {expected} 实际 {actual}"
