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

    # ── API 认证（v6 §9.1「认证鉴权」，业务方 2026-08-18 选定静态 Token）──
    #: `token:user_id:role` 三段式，多条用逗号分隔。role ∈ viewer/scheduler/director/admin。
    #:
    #: **默认空 = 全部拒绝，不是全部放行。** 全离线内网也不给「没配就不校验」
    #: 这个后门：那样一旦有人忘了配，鉴权这件事就在无人察觉的情况下没有了。
    #: 空表时 API 返回 401 并直说「服务端未配置 API_TOKENS」。
    API_TOKENS: str = ""
    #: 前端调用后端的地址。前端与后端同机部署，默认走本机回环
    API_BASE_URL: str = "http://127.0.0.1:8000"
    #: 前端自己用的 token（Streamlit 进程从环境读，不落浏览器存储）
    FRONTEND_API_TOKEN: str = ""
    #: 轮询间隔（v6 §8.1：低频轮询，无实时流式）
    FRONTEND_POLL_INTERVAL_S: float = Field(default=1.5, gt=0)

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
    #: 长任务的执行方式。`rq` = 入队给 worker（生产，v6 §9.2）；
    #: `inline` = 在请求线程里同步跑完（集成测试与单机排障用）。
    #: **默认是 rq** —— inline 会让一次排班把 HTTP 请求卡住几十秒
    JOB_RUNNER: Literal["rq", "inline"] = "rq"
    #: RQ 任务的执行超时。一次排班 = 求解 60 s + 校验 + 报表，900 s 是宽余量
    RQ_JOB_TIMEOUT_S: int = Field(default=900, gt=0)

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
    #: self-consistency 的采样温度（v6 §7.3.5 伪码里的 `temperature=0.7`）。
    #: 采样必须**有温度**，否则 n 次采到同一条，一致率恒为 1.0，信号一失效。
    SELF_CONSISTENCY_TEMPERATURE: float = Field(default=0.7, gt=0.0, le=2.0)

    # ── 置信度阈值（§7.3.5 / §7.5 `CONFIDENCE_THRESHOLD`）───────────
    # ⚠️ **这是未拟合期的保守占位值，不是校准结果**（铁律 6）。
    # v6 §7.3.5 要求它由「误执行率 ≤4%」在 §12.2 的 360 条标注数据上反推，
    # 那批数据 W11 才有。业务方 2026-08-13 选定 0.75 作为拟合前的默认值。
    # 做成配置项而不是常量，就是为了 W11/W13 拿到数反推后**改 .env 即可**。
    CONFIDENCE_THRESHOLD: float = Field(default=0.75, ge=0.0, le=1.0)
    #: 已拟合校准器的落盘位置。文件不存在时用未拟合的启发式回退（如实标记）。
    CALIBRATOR_PATH: Path = PROJECT_ROOT / "datasets" / "calibrator.json"

    # ── 图与 HITL（§7.5 / §9.2）─────────────────────────────────────
    #: `validate → solve` 的驳回回环上限（v6 §7.5 `MAX_ATTEMPTS`）。
    #: 正常情况下这条回环**一次都不该触发**——触发即 FTS-3003 CRITICAL。
    MAX_SOLVE_ATTEMPTS: int = Field(default=2, ge=1)
    #: `explain` 的 Critic 重写轮数上限（v6 §7.2.3「重写（≤N 轮）」）
    EXPLAIN_MAX_REWRITES: int = Field(default=1, ge=0, le=3)
    #: DiagnosisAgent 的自主轮数上限（探针预算另由 §3.9.2 的独立池管）
    DIAGNOSIS_MAX_ROUNDS: int = Field(default=3, ge=1)

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
    #: 融合后送进精排的候选条数（v6 §6.5.2：「对融合后 Top-20 精排 → Top-5」）
    FUSION_TOP_K: int = 20
    #: 三路召回各自的取数上限。精排只看融合后的前 `FUSION_TOP_K` 条
    ROUTE_TOP_K: int = 10
    #: 嵌入实现。`bge` = 真模型（.data/models/bge-m3）；`hash` = 确定性哈希嵌入，
    #: 给 CI 用（那里没有 2.2GB 权重）。检索质量指标一律用 `bge` 跑。
    EMBED_PROVIDER: Literal["bge", "hash"] = "bge"
    #: **摄取期固定 CPU**：GPU 3 上常驻 Ollama，语料极小没必要抢显存（M1 隔离方案）
    EMBED_DEVICE: str = "cpu"
    #: 精排实现。`bge` = bge-reranker-v2-m3（2.2GB 权重）；`lexical` = 确定性
    #: 词元重合度，给 CI 用。**替身会在 `RerankResult.provider` 里如实标注**。
    RERANK_PROVIDER: Literal["bge", "lexical"] = "bge"
    #: 精排设备。与嵌入同一条理由（GPU 3 上常驻 Ollama），默认 CPU
    RERANK_DEVICE: str = "cpu"
    #: 向量后端。`chroma` = v6 §6.1 指定的向量库；`memory` = 精确余弦（单测）
    VECTOR_BACKEND: Literal["chroma", "memory"] = "chroma"

    # ── 长期记忆（§6.2 / §6.4）──────────────────────────────────────
    #: KnowledgeAgent 的 ReAct 步数上限（v6 §7.2.2「步数上限 6」）
    KNOWLEDGE_MAX_STEPS: int = Field(default=6, ge=1, le=6)
    #: 情景记忆归档阈值：超过 N 个训练周期归档到冷表（v6 §6.4 遗忘策略）。
    #: 周期长度不在这里配 —— 它按快照里最长的 `cycle_weeks` 算，见
    #: `backend/memory/episodic.py::retention_cycle_weeks`。
    EPISODIC_RETENTION_CYCLES: int = Field(default=3, ge=1)

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
