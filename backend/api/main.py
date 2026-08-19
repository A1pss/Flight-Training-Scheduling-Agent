"""FastAPI 应用（v6 §9.1 的 11 个端点 + 一个存活探针）。

```
uvicorn backend.api.main:app --host 0.0.0.0 --port 8000
```

## 端点一览（与 v6 §9.1 那张表逐行对应）

| 方法 | 路径 | 幂等键 | 最低角色 |
|---|---|---|---|
| POST | `/api/v1/ingest` | 文件 SHA256 | scheduler |
| GET | `/api/v1/ingest/{id}/changeset` | — | viewer |
| POST | `/api/v1/ingest/{id}/confirm` | 客户端 UUID / 请求体指纹 | scheduler |
| POST | `/api/v1/chat` | 客户端 UUID | scheduler |
| POST | `/api/v1/schedule` | 客户端 UUID / 请求体指纹 | scheduler |
| GET | `/api/v1/jobs/{job_id}` | — | viewer |
| GET | `/api/v1/runs/{trace_id}` | — | viewer |
| POST | `/api/v1/schedule/{id}/approve` | 客户端 UUID / (trace, 决策) | **director** |
| POST | `/api/v1/schedule/{id}/reject` | 同上 | scheduler |
| GET | `/api/v1/schedule/{id}/export` | — | viewer |
| GET | `/api/v1/plans?week=2026-W02` | — | viewer |

## 错误一律走同一个出口

四个处理器把 `FTSError` / `AuthError` / 请求校验失败 / 未预期异常统一翻成
v6 §9.3 的 `ErrorResponse`。**`trace_id` 一定有值**——它由中间件在进门时就生成，
不依赖任何业务逻辑，所以「报错时反而没有 trace_id」这种最让人抓狂的情况
不会发生。

## 中间件只做一件事

生成/透传 `X-Trace-Id`，绑到日志上下文，回写响应头。**不做鉴权**——鉴权是
依赖项，因为它要按端点区分角色；写在中间件里就只能一刀切。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.api.idempotency import IdempotencyStore
from backend.api.jobs import JobStore
from backend.api.locks import LockManager
from backend.api.routers import chat, ingest, jobs, plans, runs, schedule
from backend.api.runner import JobRunner, build_runner
from backend.api.security import AuthError, TokenTable
from backend.api.store import KeyValueStore, build_store
from backend.core.config import Settings, get_settings
from backend.core.errors import ErrorCode, ErrorResponse, FTSError, ScheduleLockedError
from backend.core.integrity import enforce_model_integrity
from backend.core.logging import bind_trace_id, configure_logging, get_logger, new_trace_id
from backend.core.ruleset import load_ruleset, load_semantics
from backend.schemas.api import HealthView

logger = get_logger(__name__)

API_PREFIX = "/api/v1"

#: 四个状态码写成字面量而不是 `starlette.status` 的常量：那边把
#: `HTTP_422_UNPROCESSABLE_ENTITY` 改名成了 `..._CONTENT` 并对旧名发
#: `StarletteDeprecationWarning`，而本仓库把 DeprecationWarning 当错误
#: （pyproject 的 filterwarnings）。数字本身是 RFC 定死的，不会再变。
HTTP_400_BAD_REQUEST = 400
HTTP_409_CONFLICT = 409
HTTP_422_UNPROCESSABLE_CONTENT = 422
HTTP_500_INTERNAL_SERVER_ERROR = 500

DESCRIPTION = """\
全离线内网部署的飞行训练排班系统。**排班由 CP-SAT 求解、由一套与求解器实现
完全独立的校验器兜底**；LLM 只做翻译、组织权衡与解释，**不生成、不修改任何一条
架次记录**（v6 §0.1）。

- 长任务一律异步：提交返回 `job_id`，轮询 `GET /jobs/{job_id}`（阶段 + 百分比），
  完成后一次性取 `GET /runs/{trace_id}`（方案 + 校验报告 + TraceEvent 全量）。
- 三态分离：`OPTIMAL/FEASIBLE` / `INFEASIBLE` / `UNKNOWN` 在错误码与数据模型上
  全程不混（铁律 8）。
- 认证：`Authorization: Bearer <token>`，角色 viewer < scheduler < director < admin。
"""


def _error_response(error: ErrorResponse, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=error.model_dump(mode="json"))


def _safe_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """把 Pydantic 的错误列表压成**可 JSON 序列化**的形状。

    `exc.errors()` 的 `ctx` 里可能装着原始的 `ValueError` 对象，直接塞进响应
    会让序列化炸掉 —— 于是一个本该 422 的请求变成 500。契约测试抓到过。
    """
    out: list[dict[str, Any]] = []
    for item in exc.errors()[:10]:
        out.append(
            {
                "loc": [str(part) for part in item.get("loc", ())],
                "msg": str(item.get("msg", "")),
                "type": str(item.get("type", "")),
            }
        )
    return out


def _bad_request(trace_id: str, errors: list[dict[str, Any]]) -> ErrorResponse:
    return ErrorResponse(
        code=ErrorCode.REQUIRED_INPUT_MISSING,
        message="请求参数不合法或缺失",
        severity="WARN",
        stage="ingest",
        details={"errors": errors},
        suggestions=["按 /docs 的契约修正请求体后重试"],
        trace_id=trace_id,
        retryable=True,
    )


def _status_for(exc: FTSError) -> int:
    """错误码 → HTTP 状态。**只在这里做一次映射。**"""
    if isinstance(exc, ScheduleLockedError):
        return HTTP_409_CONFLICT
    if exc.code in (ErrorCode.VALIDATOR_SOLVER_DISAGREE,):
        return HTTP_500_INTERNAL_SERVER_ERROR
    return HTTP_400_BAD_REQUEST


def create_app(
    *,
    settings: Settings | None = None,
    store: KeyValueStore | None = None,
    runner: JobRunner | None = None,
    session_factory: Any = None,
    today: date | None = None,
) -> FastAPI:
    """建 app。四个可注入点全在签名里，测试不需要 monkeypatch 任何模块级对象。"""
    cfg = settings or get_settings()
    configure_logging(cfg)

    # ★ 模型完整性（v6 §11.5）：**启动即校验，不匹配就不给起**。
    # 与 `healthcheck.sh` 共用 `backend.core.integrity` 的同一份判据（「双重校验」
    # 指的是两个时机，不是两套算法）。`mock` / `replay` 两态整体跳过 —— 那两条路
    # 一次都不碰 Ollama，拿一个用不上的 digest 卡住 CI 启动只会制造假红。
    digest_check = enforce_model_integrity(cfg)
    logger.info("model_integrity", detail=digest_check.render(), skipped=digest_check.skipped)

    app = FastAPI(
        title="FTS 飞行训练排班系统 API",
        version="1.0.0",
        description=DESCRIPTION,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url="/docs",
        redoc_url="/redoc",
    )

    backend_store = store or build_store(cfg)
    app.state.settings = cfg
    app.state.store = backend_store
    app.state.jobs = JobStore(backend_store)
    app.state.locks = LockManager(backend_store)
    app.state.idempotency = IdempotencyStore(backend_store)
    app.state.runner = runner or build_runner(cfg, backend_store)
    app.state.tokens = TokenTable.from_settings(cfg)

    # 机密管理（v6 §11.5）：明文 token 仍然能用（M6 交付的配置不能一升级就全废），
    # 但**必须说出来**。转成散列：`python -m backend.api.tokens_cli hash --tokens ...`
    plaintext = app.state.tokens.plaintext_users
    if plaintext:
        logger.warning(
            "API_TOKENS 中仍有明文口令",
            users=list(plaintext),
            hint='python -m backend.api.tokens_cli hash --tokens "$API_TOKENS"',
        )
    app.state.session_factory = session_factory
    app.state.today = today

    @app.middleware("http")
    async def trace_middleware(
        request: Request, call_next: Callable[[Request], Awaitable[Any]]
    ) -> Any:
        trace_id = request.headers.get("x-trace-id") or new_trace_id()
        bind_trace_id(trace_id)
        request.state.trace_id = trace_id
        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(FTSError)
    async def _fts_error(request: Request, exc: FTSError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "") or new_trace_id()
        logger.warning("业务错误", code=str(exc.code), path=request.url.path)
        return _error_response(exc.to_response(trace_id), _status_for(exc))

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, exc: AuthError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "") or new_trace_id()
        # 鉴权失败没有对应的 FTS 业务码（v6 §9.3 的 16 个码都是业务语义），
        # 用 FTS-4004（越权）承载：它的语义正是「你没有权限做这件事」
        return _error_response(
            ErrorResponse(
                code=ErrorCode.TOOL_PERMISSION_DENIED,
                message=exc.message,
                severity="ERROR",
                stage="intent",
                details={"violation": "http_auth", "status": exc.status_code},
                suggestions=["检查 Authorization 头与账号角色"],
                trace_id=trace_id,
                retryable=False,
            ),
            exc.status_code,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", "") or new_trace_id()
        return _error_response(
            _bad_request(trace_id, _safe_errors(exc)), HTTP_422_UNPROCESSABLE_CONTENT
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        """框架层抛的 `HTTPException`。

        **400 要拉回业务错误契约**：Starlette 在请求体压根解析不了时（畸形
        multipart、坏 JSON）自己抛一个 400 `{"detail": ...}`，而本 API 的 400
        在别处一律是 v6 §9.3 的 `ErrorResponse` —— 两种形状同码混用，前端就得
        为一个状态码写两套解析。契约测试正是这么照出来的。

        **404 / 405 / 415 保持框架原样**：那是路由层的拒绝（路径不存在、方法
        不对、媒体类型不对），请求还没进到任何业务代码，谈不上「哪条业务规则不
        满足」。硬给它们安一个 FTS 码只会污染错误码的语义。
        """
        trace_id = getattr(request.state, "trace_id", "") or new_trace_id()
        if exc.status_code == HTTP_400_BAD_REQUEST:
            return _error_response(_bad_request(trace_id, [{"detail": str(exc.detail)}]), 400)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        """未预期异常 → 500，**但不给它安一个 FTS 业务码**。

        v6 §9.3 的 16 个码全是业务语义（规则解析失败、不可行、越权……），
        没有一个是「服务端有 bug」。硬挑一个最像的（比如 FTS-5001）会造成两种
        真实伤害：查日志时把「Excel 写坏了」和「PG 连不上」混成一类；
        以及让 `retryable` 这一位撒谎。

        所以这里返回一个**形状不同**的载荷：`{"kind": "internal", ...}`。
        前端据 `kind` 分辨——有 `code` 的是业务错误、照 §9.3 展示；
        有 `kind` 的是服务端故障，只能把 trace_id 交给运维。

        ⚠️ **契约测试里出现 500 就是缺陷**（`tests/contract/` 会挂），
        这条处理器的存在不是为了让 500 变得可接受，是为了 500 发生时
        trace_id 还在。
        """
        trace_id = getattr(request.state, "trace_id", "") or new_trace_id()
        logger.error("未预期异常", path=request.url.path, error=repr(exc))
        return JSONResponse(
            status_code=HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "kind": "internal",
                "message": f"服务内部错误：{exc.__class__.__name__}",
                "trace_id": trace_id,
                "suggestions": ["把 trace_id 交给运维查日志"],
            },
        )

    for router in (
        ingest.router,
        chat.router,
        schedule.router,
        jobs.router,
        runs.router,
        plans.router,
    ):
        app.include_router(router, prefix=API_PREFIX)

    @app.get("/health", response_model=HealthView, tags=["运维"])
    def health() -> HealthView:
        """存活探针。**不连库、不连 Redis**（见 `HealthView` 的注释）。"""
        return HealthView(
            app_env=cfg.APP_ENV,
            ruleset_version=load_ruleset(cfg.RULESET_PATH).version,
            semantics_version=load_semantics(cfg.SEMANTICS_PATH).version,
            offline=True,
        )

    return app


#: uvicorn 的入口：`uvicorn backend.api.main:app`。
#: 建 app 不产生任何连接（Redis 客户端惰性连接、TokenTable 只解析字符串），
#: 所以 import 本模块在 CI 上也是安全的。
app = create_app()

__all__ = ["API_PREFIX", "app", "create_app"]
