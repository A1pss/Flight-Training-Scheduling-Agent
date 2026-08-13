# prompts/ —— 提示词即代码（v6 §7.7.1 第 8 行）

> `README.md` 与以 `_` 开头的文件不被加载器当作提示词（见 `prompts.PromptRegistry.load`）。

**这里的每一份 `.md` 都是代码，不是文档。** 规矩三条：

1. **进 Git，改动走 PR。** 提示词改了等于组件行为改了，必须能 code review、能
   回滚、能与某次运行对应上。
2. **改正文必须递增 `prompt_version`。** `PROMPTS.lock.json` 记着每份提示词的
   版本与正文 sha256，`deploy/scripts/check_prompt_versions.sh` 在 CI 里核对：
   正文变了而版本号没动 → 构建失败。理由很实在：trace 里记的是版本号，同一个
   `v1` 对应过两份不同的正文，那条 trace 就再也复现不了。
3. **改了要跑该组件的 eval 子集。** CI 检测到 `prompts/**` 有改动时跑
   `pytest -m prompt_eval`，指标劣化即阻断合并。

## 与 `skills/` 的区别（**不要混**）

| | `prompts/` | `skills/` |
|---|---|---|
| 性质 | 代码 | 业务方可编辑的知识层 |
| 权威性 | 权威：定义组件怎么工作 | `authoritative: false`，**影响不到排班结果**（v6 §7.8.2） |
| 改动流程 | PR + 版本递增 + eval 子集 | 业务方直接改目录，无需发版 |
| 校验 | `check_prompt_versions.sh` | `skills_loader` 的 authoritative 校验 |

v6 §12.5.3 的 S1 是「篡改 skill，排班结果一个字节都不变」；提示词没有这条豁免，
它改了行为就是会变——所以才要版本治理。

## 文件形态

```markdown
---
component: route          # 六个组件之一：route/planner/extract/knowledge/diagnosis/explain
prompt_key: system        # 同一组件可有多份，按 key 区分
prompt_version: v1        # ^v\d+$，改正文必须递增
description: 一句话
---
正文……
```

## 改完之后

```bash
conda run -n schedule python deploy/scripts/prompt_lock.py sync         # 重算锁文件
bash deploy/scripts/check_prompt_versions.sh                      # 本地先自查
conda run -n schedule pytest -m prompt_eval -q                    # 跑 eval 子集
```
