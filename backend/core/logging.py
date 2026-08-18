"""结构化日志 + trace_id 透传 + 人员身份脱敏（v6 §11.5「数据脱敏」）。

三件事：

1. **结构化**：structlog，`LOG_FORMAT=json` 出 JSON（生产/CI），
   `console` 出人眼友好格式（开发）。
2. **trace_id 透传**：用 :class:`contextvars.ContextVar` 存放，
   :func:`bind_trace_id` 绑定后，同一异步/线程上下文内的每条日志自动带上，
   不需要每个调用点手工传。
3. **脱敏**：`LOG_REDACT_PERSON=true` 时，把日志事件里的人员身份信息
   （姓名、`person_id`）替换为占位符。**只脱敏日志，不影响业务数据**——
   排班结果里的姓名必须原样保留，否则 Excel 就没法看了。
"""

from __future__ import annotations

import logging
import re
import sys
import uuid
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any, Final

import structlog
from structlog.types import EventDict, WrappedLogger

from backend.core.config import Settings, get_settings

_trace_id_var: ContextVar[str | None] = ContextVar("fts_trace_id", default=None)

#: 值需要脱敏的键名（大小写不敏感、匹配后缀）。
_PERSON_KEYS: Final[frozenset[str]] = frozenset(
    {
        "person_id",
        "person_ids",
        "name",
        "person_name",
        "crew",
        "instructor",
        "student",
        "user_name",
        "operator",
    }
)

#: `P\d+` 形态的人员主键，出现在自由文本里时一并脱敏。
#:
#: ⚠️ **不限位数**（M8 改正）。原先写的是 `\bP\d{2}\b`，只盖得住 `P01`~`P99`
#: —— 那是把 v6 §1.3 的**基准数据集规模**当成了系统上限（`Z-4`：编号只固定
#: 前缀、不限位数）。用户上传 120 人的花名册时，`P100` 往后的人在日志里就
#: **不再脱敏**了，而这件事没有任何症状，是静默失效。
_PERSON_ID_RE: Final[re.Pattern[str]] = re.compile(r"\bP\d+\b")


def new_trace_id() -> str:
    """生成一个新的 trace_id。"""
    return uuid.uuid4().hex


def bind_trace_id(trace_id: str | None = None) -> str:
    """把 trace_id 绑定到当前上下文，返回实际使用的值。"""
    tid = trace_id or new_trace_id()
    _trace_id_var.set(tid)
    return tid


def get_trace_id() -> str | None:
    return _trace_id_var.get()


def clear_trace_id() -> None:
    _trace_id_var.set(None)


def _inject_trace_id(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
    """把上下文里的 trace_id 注入每条日志。"""
    tid = _trace_id_var.get()
    if tid is not None:
        event_dict.setdefault("trace_id", tid)
    return event_dict


def _redact_value(value: Any, placeholder: str) -> Any:
    """递归脱敏一个值。"""
    if isinstance(value, str):
        return placeholder
    if isinstance(value, MutableMapping):
        return {k: _redact_value(v, placeholder) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_redact_value(v, placeholder) for v in value]
    return placeholder


def make_person_redactor(placeholder: str) -> Any:
    """构造一个 structlog processor：脱敏人员身份字段与文本中的 `Pnn`。"""

    def _redact(_logger: WrappedLogger, _name: str, event_dict: EventDict) -> EventDict:
        for key in list(event_dict.keys()):
            if key.lower() in _PERSON_KEYS:
                event_dict[key] = _redact_value(event_dict[key], placeholder)
            elif isinstance(event_dict[key], str):
                event_dict[key] = _PERSON_ID_RE.sub(placeholder, event_dict[key])
        return event_dict

    return _redact


def configure_logging(settings: Settings | None = None) -> None:
    """按配置初始化 structlog。可重复调用（幂等）。"""
    cfg = settings or get_settings()

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, cfg.LOG_LEVEL),
        force=True,
    )

    processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=False),
        _inject_trace_id,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]
    # 脱敏必须排在渲染器之前 —— 一旦渲染成字符串就没得改了
    if cfg.LOG_REDACT_PERSON:
        processors.append(make_person_redactor(cfg.LOG_REDACT_PLACEHOLDER))

    if cfg.LOG_FORMAT == "json":
        processors.append(structlog.processors.JSONRenderer(ensure_ascii=False))
    else:
        processors.append(structlog.dev.ConsoleRenderer(colors=False))

    # 走 stdlib LoggerFactory 而非 PrintLoggerFactory：`add_logger_name` 与
    # `filter_by_level` 都要求底层是 stdlib logger（PrintLogger 没有 .name），
    # 且经由 stdlib 后 basicConfig 的 level / handler 配置才真正生效。
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """取一个绑定了模块名的 logger。"""
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


__all__ = [
    "bind_trace_id",
    "clear_trace_id",
    "configure_logging",
    "get_logger",
    "get_trace_id",
    "make_person_redactor",
    "new_trace_id",
]
