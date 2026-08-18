"""导出 OpenAPI 文档（v6 §13 交付物「API 文档（OpenAPI）」）。

```bash
python deploy/scripts/export_openapi.py            # → docs/openapi.json
python deploy/scripts/export_openapi.py --check    # 只校验与仓库里那份一致
```

## 为什么要落一份到仓库

`/docs` 与 `/redoc` 是 FastAPI 自带的，**但那要求服务起着**。交付物清单里的
「API 文档」是给对接方在写代码时看的，不能要求他先把系统装起来。落一份
`docs/openapi.json` 进 Git 还有第二个好处：**端点契约的变化会出现在 diff 里**
——加一个字段、改一个枚举、把某个响应从 200 改成 202，全都看得见。

`--check` 供 CI 用：文档与代码分叉时红，逼人把导出跑一遍。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # pragma: no cover - 供 `bash` 直接调用时用
    sys.path.insert(0, str(REPO_ROOT))

from backend.api.main import create_app  # noqa: E402
from backend.core.config import Settings  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "docs" / "openapi.json"


def build_spec() -> dict[str, object]:
    """生成 OpenAPI。

    **用一份固定的配置建 app**，不读 `.env`：导出结果不该随本机环境变化
    （否则谁导出的文档就带谁的痕迹，diff 里全是噪声）。`API_TOKENS` 随便给一个
    ——它不进文档，只是为了让 app 建得起来。
    """
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        APP_ENV="ci",
        LLM_PROVIDER="mock",
        API_TOKENS="doc:P00:admin",
    )
    app = create_app(settings=settings)
    spec: dict[str, object] = app.openapi()
    return spec


def render(spec: dict[str, object]) -> str:
    # `sort_keys=True` + 固定缩进：同一份代码导出两次必须逐字节相同，否则
    # `--check` 会因为字典序抖动而误报。
    return json.dumps(spec, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出 OpenAPI 文档")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = parser.parse_args(argv)

    text = render(build_spec())
    if args.check:
        if not args.out.is_file():
            print(f"❌ {args.out} 不存在 —— 先跑一次导出", file=sys.stderr)
            return 1
        if args.out.read_text(encoding="utf-8") != text:
            print(
                f"❌ {args.out} 与代码不一致 —— 端点契约改了但文档没更新。"
                f"跑：python deploy/scripts/export_openapi.py",
                file=sys.stderr,
            )
            return 1
        print(f"✅ {args.out} 与代码一致")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    spec_paths = len(build_spec().get("paths", {}))  # type: ignore[union-attr,arg-type]
    print(f"✅ 已写出 {args.out}（{spec_paths} 条路径）")
    return 0


if __name__ == "__main__":  # pragma: no cover - 入口薄封装
    sys.exit(main())
