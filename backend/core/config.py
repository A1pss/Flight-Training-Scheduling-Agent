"""集中配置（pydantic-settings）。

覆盖 `.env` 的全部键。每个键的默认值取自 v6：端口来自 §11.1，求解预算来自
§3.11，探针预算来自 §3.9.2，Harness 预算来自 §7.6，显存相关来自 §11.3。

**这里不做任何出网**：`EGRESS_ALLOWLIST` 只是把 §11.5 的 allowlist 做成可配置项，
真正的拦截在 :mod:`backend.core.http`。
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: 仓库根目录（本文件位于 <root>/backend/core/config.py）
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

ProviderName = Literal["ollama", "mock", "replay"]


class Settings(BaseSettings):
    """FTS 全局配置。实例通过 :func:`get_settings` 获取（进程内单例）。"""

    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=True,
    )

    # ── 应用 ─────────────────────────────────────────────────────────
    APP_ENV: Literal["dev", "ci", "prod"] = "dev"
    # 内网部署需从其它机器访问，绑 0.0.0.0 是设计意图（CLAUDE.md §9 的 uvicorn 命令）
    APP_HOST: str = "0.0.0.0"  # noqa: S104  # nosec B104
    APP_PORT: int = 8000
    FRONTEND_PORT: int = 8501
    TENANT_ID: str = "default"

    # ── 日志与脱敏（v6 §11.5）────────────────────────────────────────
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "json"
    LOG_REDACT_PERSON: bool = True
    LOG_REDACT_PLACEHOLDER: str = "***"

    # ── PostgreSQL 16（裸装独立实例，§11.1）──────────────────────────
    PG_HOST: str = "127.0.0.1"
    PG_PORT: int = 5433
    PG_USER: str = "fts"
    # 本机 initdb 实例用 trust 认证，此处只是占位默认值；真实口令走 .env（已 gitignore）
    PG_PASSWORD: str = "fts"  # noqa: S105
    PG_DATABASE: str = "fts"
    PG_DATA_DIR: Path = PROJECT_ROOT / ".data" / "pg"

    # ── Redis 7（§11.1）──────────────────────────────────────────────
    REDIS_HOST: str = "127.0.0.1"
    REDIS_PORT: int = 6380
    REDIS_DB: int = 0
    REDIS_DATA_DIR: Path = PROJECT_ROOT / ".data" / "redis"
    RQ_QUEUE: str = "fts"

    # ── Ollama（§11.1 / §11.2）───────────────────────────────────────
    OLLAMA_HOST: str = "127.0.0.1:11434"
    OLLAMA_MODELS: Path = PROJECT_ROOT / ".data" / "ollama"
    OLLAMA_NUM_PARALLEL: int = 4
    CUDA_VISIBLE_DEVICES: str = "3"

    # ── 三态 Provider（§11.2）────────────────────────────────────────
    LLM_PROVIDER: ProviderName = "mock"
    LLM_MODEL: str = "qwen2.5:14b-instruct-q4_K_M"
    LLM_MODEL_DIGEST: str = ""
    LLM_TEMPERATURE: float = Field(default=0.0, ge=0.0, le=2.0)
    LLM_NUM_CTX: int = 8192
    LLM_TIMEOUT_S: float = Field(default=120.0, gt=0)
    REPLAY_TRACE_DIR: Path = PROJECT_ROOT / "traces" / "accept_v1"
    MOCK_FIXTURE_DIR: Path = PROJECT_ROOT / "tests" / "fixtures" / "llm_stubs"

    # ── 求解预算（§3.11）─────────────────────────────────────────────
    SOLVER_WORKERS: int = Field(default=4, ge=1)
    SOLVER_SEED: int = 42
    # 常规档 2026-08-13 由 30 提到 60（v6 §3.11 `Z-13`）：基准周 18~21 s 证到
    # OPTIMAL，30 s 余量太薄，机器一忙就落到 FEASIBLE —— 而 FEASIBLE 不保证
    # 逐字节可复现（§3.11.1，顶着铁律 9）。求解器证完立刻返回，提预算零成本。
    SOLVER_TIME_LIMIT_S: float = Field(default=60.0, gt=0)
    SOLVER_RESCHEDULE_TIME_LIMIT_S: float = Field(default=120.0, gt=0)
    SOLVER_DIAGNOSE_TIME_LIMIT_S: float = Field(default=300.0, gt=0)

    # ── 探针预算池（§3.9.2，独立于 Harness 预算）────────────────────
    PROBE_TIME_LIMIT_S: float = Field(default=30.0, gt=0)
    PROBE_MAX_CALLS: int = Field(default=5, ge=0)
    PROBE_TOTAL_BUDGET_S: float = Field(default=120.0, gt=0)

    # ── Harness 预算（§7.7.1 第 4 行 / FTS-4003）────────────────────
    # 四条上限**照抄 v6 §7.7.1**：LLM 调用 ≤10、工具调用 ≤20、墙钟 ≤180s、
    # token ≤40k。M0 当时写的是 12 / 120k，与设计方案不符，M4-A 已改正。
    # `BudgetLimits` 对这四项设了 `le=` 上界：配置只能往严里调，调不松。
    HARNESS_MAX_LLM_CALLS: int = Field(default=10, ge=1, le=10)
    HARNESS_MAX_TOOL_CALLS: int = Field(default=20, ge=1, le=20)
    HARNESS_MAX_TOKENS: int = Field(default=40_000, ge=1, le=40_000)
    HARNESS_WALL_CLOCK_S: float = Field(default=180.0, gt=0, le=180.0)
    HARNESS_MAX_RETRIES: int = Field(default=2, ge=0, le=2)

    # ── Harness 上下文装配（§7.7.1 第 5 行）─────────────────────────
    #: 给模型输出留的 token 余量（输入预算 = LLM_NUM_CTX − 本项）
    HARNESS_RESERVE_OUTPUT_TOKENS: int = Field(default=1024, ge=1)
    #: 历史消息滑窗：只保留最近这么多条历史块
    HARNESS_HISTORY_WINDOW: int = Field(default=6, ge=1)

    # ── Harness 双模式调用的统计阈值（§7.7.1 第 2 行）───────────────
    # 注意这里配的是**阈值**，不是模式本身 —— 模式由运行时统计决定，
    # 配置里没有 `LLM_CALL_MODE` 这种开关，也不许加。
    HARNESS_MODE_WINDOW: int = Field(default=20, ge=1)
    HARNESS_MODE_SWITCH_THRESHOLD: float = Field(default=0.30, gt=0.0, le=1.0)
    HARNESS_MODE_RECOVER_THRESHOLD: float = Field(default=0.10, gt=0.0, le=1.0)
    HARNESS_MODE_MIN_SAMPLES: int = Field(default=5, ge=1)

    # ── Harness 结果缓存（§7.7.1 第 6 行）───────────────────────────
    #: 兜底 TTL；正常失效路径是快照生命周期结束时 `invalidate_snapshot()`
    HARNESS_CACHE_TTL_S: int = Field(default=86_400, ge=1)

    # ── 提示词（§7.7.1 第 8 行）─────────────────────────────────────
    PROMPTS_DIR: Path = PROJECT_ROOT / "prompts"

    # ── Planner（§7.3.3）────────────────────────────────────────────
    BLAST_RADIUS_THRESHOLD: int = Field(default=20, ge=0)
    SELF_CONSISTENCY_SAMPLES: int = Field(default=3, ge=1)

    # ── 规则与语义（§1.1）───────────────────────────────────────────
    RULESET_PATH: Path = PROJECT_ROOT / "rules" / "ruleset_v1.3.yaml"
    SEMANTICS_PATH: Path = PROJECT_ROOT / "rules" / "semantics.yaml"

    # ── 训练周期起点 ─────────────────────────────────────────────────
    # 这里**刻意没有** `DEFAULT_CYCLE_START` 之类的配置项。
    #
    # `training_progress.cycle_start` 只有两个来源：课目文件的「课程开始日期」列，
    # 或用户对 `Q_cycle_start` 的回答。都没有时管线**向用户提问并阻断**，不兜底。
    # 加一个默认值配置就等于给「悄悄填一个日期」开了后门，而这个值进主键、
    # 填错要迁移全表。详见 :mod:`backend.ingestion.questions`。

    # ── 检索与嵌入（§6.5 / §11.3）───────────────────────────────────
    BGE_M3_PATH: Path = PROJECT_ROOT / ".data" / "models" / "bge-m3"
    BGE_RERANKER_PATH: Path = PROJECT_ROOT / ".data" / "models" / "bge-reranker-v2-m3"
    CHROMA_PATH: Path = PROJECT_ROOT / ".data" / "chroma"
    RRF_K: int = 60
    RERANK_TOP_K: int = 5
    #: 嵌入实现。`bge` = 真模型（.data/models/bge-m3）；`hash` = 确定性哈希嵌入，
    #: 给 CI 用（那里没有 2.2GB 权重）。检索质量指标一律用 `bge` 跑。
    EMBED_PROVIDER: Literal["bge", "hash"] = "bge"
    #: **摄取期固定 CPU**：GPU 3 上常驻 Ollama，语料极小没必要抢显存（M1 隔离方案）
    EMBED_DEVICE: str = "cpu"

    # ── 摄取（§5 / §11.5）──────────────────────────────────────────
    PADDLEOCR_HOME: Path = PROJECT_ROOT / ".data" / "paddleocr"
    UPLOAD_MAX_BYTES: int = 50 * 1024 * 1024

    # ── 产物归档（§10.6）───────────────────────────────────────────
    PLANS_DIR: Path = PROJECT_ROOT / "data" / "plans"
    TRACES_DIR: Path = PROJECT_ROOT / "traces"
    SKILLS_DIR: Path = PROJECT_ROOT / "skills"

    # ── egress allowlist（§11.5 / §12.5.4）─────────────────────────
    #: 逗号分隔。支持字面主机名与 CIDR 网段。缺省仅本机 + RFC1918 内网段。
    EGRESS_ALLOWLIST: str = "127.0.0.1,localhost,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16"

    @field_validator("OLLAMA_HOST")
    @classmethod
    def _strip_scheme(cls, v: str) -> str:
        """`OLLAMA_HOST` 按 Ollama 惯例写作 `host:port`，容忍误填的 scheme。"""
        return v.removeprefix("http://").removeprefix("https://").rstrip("/")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def DATABASE_URL(self) -> str:
        return (
            f"postgresql+psycopg://{self.PG_USER}:{self.PG_PASSWORD}"
            f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DATABASE}"
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def REDIS_URL(self) -> str:
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def OLLAMA_BASE_URL(self) -> str:
        return f"http://{self.OLLAMA_HOST}"

    @property
    def egress_allowlist(self) -> tuple[str, ...]:
        """把逗号分隔的 allowlist 解析为条目元组。"""
        return tuple(item.strip() for item in self.EGRESS_ALLOWLIST.split(",") if item.strip())


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程内单例。测试中改配置请用 ``get_settings.cache_clear()``。"""
    return Settings()


__all__ = ["PROJECT_ROOT", "ProviderName", "Settings", "get_settings"]
