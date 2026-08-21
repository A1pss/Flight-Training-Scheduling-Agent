"""M7 的命令行入口。

```bash
# ① 工具调用基线（§12.5.1）—— 断点续跑，中断后原样重跑即可续上
CUDA_VISIBLE_DEVICES=3 LLM_PROVIDER=ollama \
  python -m backend.training.cli toolcall --config production --rounds 3

# ② 聚合成指标表（不调模型，随时可跑）
python -m backend.training.cli report --out reports/M7_基线.md

# ③ 提示词 token 实测（§15.4 门禁的量具）
LLM_PROVIDER=ollama python -m backend.training.cli prompt-tokens
```

**结果目录固定在 `reports/m7/`**，一个配置一个 JSONL。放 `reports/` 而不是
`traces/` 是因为它是**实验产物**要进收工报告，而不是可重放的运行轨迹。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Final, cast

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.datasets.loader import load_eval_dataset
from backend.llm.provider import build_provider
from backend.training.metrics import (
    FAILURE_MODE_ORDER,
    ValidMetrics,
    guardrail_metrics,
    valid_metrics,
    worst_tools,
)
from backend.training.prompt_configs import ALL_PROMPT_CONFIGS, PromptConfigName, describe
from backend.training.prompt_tokens import (
    component_weights,
    measure_config,
    weighted_gate_tokens,
)
from backend.training.rendering import ALL_RENDERINGS, RenderingName, describe_rendering
from backend.training.toolcall_eval import DATASET, load_outcomes, run_config

_log = get_logger(__name__)

#: 实验产物目录。
RESULT_DIR: Final[Path] = Path("reports/m7")


def outcome_path(config: str, rendering: str = "task") -> Path:
    """一种 (配置, 渲染口径) 一个文件。

    口径 A 沿用**不带后缀**的旧文件名 —— 它引入之前已经跑掉了两个小时，
    改名等于把那批结果的断点续跑作废重跑。
    """
    suffix = "" if rendering == "task" else f"__{rendering}"
    return RESULT_DIR / f"toolcall_{config}{suffix}.jsonl"


def tokens_path() -> Path:
    return RESULT_DIR / "prompt_tokens.json"


def _cmd_toolcall(args: argparse.Namespace) -> int:
    config = cast(PromptConfigName, args.config)
    rendering = cast(RenderingName, args.rendering)
    path = outcome_path(config, rendering)
    written = run_config(
        config,
        out_path=path,
        rounds=args.rounds,
        limit=args.limit,
        strata=tuple(args.strata),
        rendering=rendering,
    )
    print(f"配置 {config}（{describe(config)}）")
    print(f"渲染 {rendering}（{describe_rendering(rendering)}）")
    print(f"新写入 {written} 条 → {path}")
    return 0


def _cmd_prompt_tokens(args: argparse.Namespace) -> int:
    provider = build_provider()
    _manifest, items = load_eval_dataset(DATASET, require_approved=True)
    weights = component_weights([dict(i) for i in items])

    payload: dict[str, object] = {
        "model": get_settings().LLM_MODEL,
        "weights": weights,
        "configs": {},
    }
    configs = cast(dict[str, object], payload["configs"])
    for name in ALL_PROMPT_CONFIGS:
        measured = measure_config(provider, name)
        configs[name] = {
            "per_component": [
                {
                    "component": m.component,
                    "system_tokens": m.system_tokens,
                    "schema_tokens": m.schema_tokens,
                    "gate_tokens": m.gate_tokens,
                    "raw": {"full": m.pe_full, "nosys": m.pe_nosys, "notools": m.pe_notools},
                }
                for m in measured
            ],
            "weighted_gate_tokens": weighted_gate_tokens(measured, weights),
        }
        print(f"{name:12s} 加权门禁 token = {weighted_gate_tokens(measured, weights)}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"→ {args.out}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    lines: list[str] = ["# M7 第一步 · 工具调用基线实测", ""]
    lines.append(f"数据集 `{DATASET}` · 模型 `{get_settings().LLM_MODEL}`")
    lines.append("")
    lines.append("## 一、三种配置的主指标")
    lines.append("")
    lines.append(
        "| 配置 / 口径 | 调用数 | **一次通过率** | 最终通过率 | 重试系数 | 降级率 "
        "| 工具选择 | 参数精确 |"
    )
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")

    available: list[tuple[str, ValidMetrics]] = []
    for rendering in ALL_RENDERINGS:
        for name in ALL_PROMPT_CONFIGS:
            outcomes = load_outcomes(outcome_path(name, rendering))
            if not outcomes:
                continue
            m = valid_metrics(outcomes, name, rendering)
            if m.calls == 0:
                continue
            label = f"{name} / {rendering}"
            available.append((label, m))
            lines.append(
                f"| `{label}` | {m.calls} | **{m.first_pass_rate:.1%}** | {m.final_pass_rate:.1%} "
                f"| {m.retry_coefficient:.3f} | {m.degrade_rate:.1%} "
                f"| {m.tool_selection_rate:.1%} | {m.params_exact_rate:.1%} |"
            )

    if not available:
        print("还没有任何结果文件，先跑 `toolcall`")
        return 1

    lines.extend(["", "## 二、失败模式分布（首次尝试，§15.2 ⑥ 的直接输入）", ""])
    lines.append("| 配置 / 口径 | " + " | ".join(FAILURE_MODE_ORDER) + " | 合计 |")
    lines.append("|---|" + "---:|" * (len(FAILURE_MODE_ORDER) + 1))
    for label, metric in available:
        dist = metric.first_failure_modes
        cells = " | ".join(str(dist.get(k, 0)) for k in FAILURE_MODE_ORDER)
        lines.append(f"| `{label}` | {cells} | {sum(dist.values())} |")

    lines.extend(["", "## 三、确定性两层（越权 / 超预算）", ""])
    lines.append("| 配置 / 口径 | 越权拦截率 | 预算熔断正确率 |")
    lines.append("|---|---:|---:|")
    for label, metric in available:
        g = guardrail_metrics(
            load_outcomes(outcome_path(metric.config, metric.rendering)),
            metric.config,
            metric.rendering,
        )
        lines.append(
            f"| `{label}` | {g.acl_intercepted}/{g.acl_total} = {g.acl_intercept_rate:.1%} "
            f"| {g.budget_correct}/{g.budget_total} = {g.budget_trip_rate:.1%} |"
        )

    lines.extend(["", "## 四、一次通过率最低的工具（难负例挑样入口）", ""])
    for label, metric in available:
        lines.append(f"**`{label}`**：")
        for tool, rate in worst_tools(metric):
            lines.append(f"- `{tool}` {rate:.1%}")
        lines.append("")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n→ {args.out}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.training.cli", description="M7 数据合成与微调")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("toolcall", help="跑 §12.5.1 的工具调用基线")
    run.add_argument("--config", required=True, choices=list(ALL_PROMPT_CONFIGS))
    run.add_argument(
        "--rendering",
        default="task",
        choices=list(ALL_RENDERINGS),
        help="task = 口径 A（给已知条件）/ context = 口径 B（原样用 prompt_context）",
    )
    run.add_argument("--rounds", type=int, default=3)
    run.add_argument("--limit", type=int, default=None, help="只跑前 N 条（冒烟用）")
    run.add_argument(
        "--strata",
        nargs="+",
        default=["valid", "acl_violation", "budget_exhaustion"],
        choices=["valid", "acl_violation", "budget_exhaustion"],
    )
    run.set_defaults(func=_cmd_toolcall)

    tok = sub.add_parser("prompt-tokens", help="实测三种配置的提示词 token（§15.4 门禁）")
    tok.add_argument("--out", type=Path, default=tokens_path())
    tok.set_defaults(func=_cmd_prompt_tokens)

    rep = sub.add_parser("report", help="把结果聚合成指标表")
    rep.add_argument("--out", type=Path, default=RESULT_DIR / "baseline.md")
    rep.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":  # pragma: no cover —— 入口
    raise SystemExit(main())
