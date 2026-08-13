"""提示词锁文件的核对与同步（v6 §7.7.1 第 8 行）。

```bash
python deploy/scripts/prompt_lock.py check   # 核对，问题即非零退出（CI 用）
python deploy/scripts/prompt_lock.py sync    # 改完提示词后重写锁文件
```

**为什么要有锁文件**：trace 与 manifest 里记的是 `prompt_version`。如果有人改了
正文却没动版本号，同一个 `v1` 就对应过两份不同的提示词，那条 trace 从此不可复现。
锁文件把「正文 sha256」和「版本号」绑在一起，让这件事在 CI 上必然暴露。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from backend.harness.prompts import LOCK_FILENAME, PromptRegistry  # noqa: E402


def _lock_path(registry_root: Path) -> Path:
    return registry_root / LOCK_FILENAME


def check(root: Path) -> int:
    registry = PromptRegistry.load(root)
    lock_file = _lock_path(root)
    if not lock_file.is_file():
        print(f"❌ 锁文件缺失：{lock_file}")
        print("   跑 `python deploy/scripts/prompt_lock.py sync` 生成")
        return 1

    lock = json.loads(lock_file.read_text(encoding="utf-8"))
    problems = registry.diff_lock(lock)
    if problems:
        print("❌ 提示词与锁文件不一致：")
        for problem in problems:
            print(f"   - {problem}")
        print("   改了正文就要递增 prompt_version，并跑 `sync` 更新锁文件；")
        print("   然后跑 `pytest -m prompt_eval` 验证该组件的 eval 子集。")
        return 1

    missing = registry.missing_components()
    if missing:
        print(f"❌ 以下组件缺 system 提示词：{list(missing)}")
        return 1

    print(f"✅ {len(registry.refs())} 份提示词与锁文件一致：{', '.join(registry.refs())}")
    return 0


def sync(root: Path) -> int:
    registry = PromptRegistry.load(root)
    payload = registry.lock_payload()
    _lock_path(root).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"✅ 锁文件已更新，共 {len(payload)} 份提示词")
    for ref, entry in payload.items():
        print(f"   {ref} @ {entry['prompt_version']}  {entry['sha256'][:12]}")
    return 0


def main(argv: list[str]) -> int:
    action = argv[1] if len(argv) > 1 else "check"
    root = Path(argv[2]) if len(argv) > 2 else ROOT / "prompts"
    if action == "check":
        return check(root)
    if action == "sync":
        return sync(root)
    print(f"用法：{argv[0]} [check|sync] [prompts 目录]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
