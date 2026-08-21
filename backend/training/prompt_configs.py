"""§15.4 消融表里三行「提示词侧」配置的落地。

| 配置 | §15.4 那一行 | 本模块 |
|---|---|---|
| `zero_shot` | 14B 原始（零样本） | 一句通用系统提示词 + 工具表，**下界参照** |
| `production` | 14B 原始 + 6 组 few-shot | **当前生产配置**：`prompts/<组件>/system.md` + 工具表 |
| `optimized` | 14B + few-shot 提示工程再优化 | 生产提示词 + 本模块的强化段落 |

## ⚠️ 一处与 v6 对不上的实测事实

§15.1 / §15.4 把正式基线写成「14B 原始 + **6 组 few-shot**，提示词 ~2.4k token」。
**工具调用路径上没有 few-shot。** 全仓库唯一的 6 组 few-shot 在
`backend/planner/revision.py::FEW_SHOT`，只挂在**修订翻译**那一条路径上（360 token）。

实测的 2.4k 是另一回事 —— 它是 **system 提示词 + 工具 schema** 的合计
（按 `tool_calls_200` 的 200 条 valid 加权 = **2468 token**，与 §15.1 的「~2.4k」
数量级吻合，但构成完全不同：大头是 schema 而不是范例）：

| 组件 | system | 工具 schema | 合计 |
|---|---|---|---|
| route | 395 | 751 | 1146 |
| knowledge | 400 | 990 | 1390 |
| explain | 499 | 1325 | 1824 |
| diagnosis | 463 | 1590 | 2053 |
| extract | 509 | 2436 | 2945 |
| planner | 605 | 2553 | 3158 |

> **业务方 2026-08-20 裁定**：`production` 一行取**当前生产配置原样**（实测无
> few-shot），并在报告里注明 §15.4 的措辞与实现不符；§15.4 的「提示词 token 数
> 下降 ≥30%」门禁按 **system + 工具 schema 合计**立（基线 2468 → 门槛 ≤1728）。

## 为什么实验提示词放在代码里而不是 `prompts/`

`prompts/` 是**生产**提示词：进 Git、有锁文件、改了要换 `prompt_version` 并跑
该组件的 eval 子集（§7.7.1 第 8 行）。把三个消融配置塞进去，会让锁文件里出现
三份从没上线过的提示词，`prompt_version` 这个字段随即失去意义。

所以这里是**覆盖层**：`registry_for()` 拿生产提示词做底，按配置改写正文，
产出一个只在本次评测里存在的 `PromptRegistry`。配置 `optimized` 若通过准入，
再由人把它**升版号**搬进 `prompts/`，那才是上线动作。
"""

from __future__ import annotations

from typing import Final, Literal

from backend.core.config import get_settings
from backend.harness.prompts import Prompt, PromptRegistry
from backend.harness.types import ALL_COMPONENTS, ComponentName

#: 三种提示词配置。
PromptConfigName = Literal["zero_shot", "production", "optimized"]

ALL_PROMPT_CONFIGS: Final[tuple[PromptConfigName, ...]] = (
    "zero_shot",
    "production",
    "optimized",
)

#: `zero_shot` 的通用系统提示词 —— **不含任何本项目知识**。
#:
#: 它是下界参照：模型只知道「有一张工具表，按用户的任务挑一个调」。
#: 编号形态、实体表、禁令一概不给。与 `production` 的差值就是那份领域提示词
#: 值多少个点。
ZERO_SHOT_SYSTEM: Final[str] = "你是一个工具调用助手。根据用户给出的任务，调用最合适的工具。"

#: `optimized` 在生产提示词之后追加的强化段落（v6 §15.4「先试提示工程」那一行）。
#:
#: **五段各针对一类实测失败模式**，不是凭感觉堆的话术：
#:
#: | 段 | 针对 | 失败模式 |
#: |---|---|---|
#: | ① 只输出调用 | 模型先写一段说明再调工具 | `json_malformed` |
#: | ② 已知条件即全部 | 可选字段被填上编出来的值 | `entity_hallucination` |
#: | ③ 不确定就别填 | 必填字段给空串占位 | `missing_field` |
#: | ④ 形态照抄 schema | 单值写成数组、数组写成单值 | `type_error` |
#: | ⑤ 枚举逐字 | 近义词替换枚举值 | `enum_out_of_range` |
#:
#: 段落内容在拿到 `zero_shot` / `production` 两轮的失败模式分布之后定稿，
#: 迭代过程记在 `reports/模型评估报告.md`。
OPTIMIZED_SUFFIX: Final[str] = """
## 工具调用的五条硬性要求

1. **只输出工具调用本身**，不要在调用前后写任何说明、寒暄或理由。
   需要解释的场合由别的组件负责。

2. **「已知条件」里给了什么就填什么，没给的字段一律不填。**
   可选字段留空是正确答案；替用户补一个看起来合理的值不是。

3. **不确定的字段不要填占位值。** 空字符串 `""`、`"未知"`、`"N/A"`、`0`
   都不是「没填」，它们会被当成真实取值用下去。

4. **参数形态严格照工具 schema**：schema 写 `array` 就给数组（哪怕只有一项），
   写 `string` 就给字符串（不要包成数组），写 `object` 就给对象（不要给 JSON 字符串）。

5. **枚举字段逐字照抄 schema 里的取值**，不要换同义词、不要翻译、不要改大小写。

## 编号形态（位数不固定，别补零也别截断）

| 类别 | 形态 | 例 |
|---|---|---|
| 人员 | `P` + 数字 | `P04` |
| 飞机 | `AC` + 数字 | `AC73` |
| 课目 | `mission` + 大写字母 + `-` + 数字 | `missionC-1` |
| 跑道 | `RWY-` + 数字 | `RWY-2` |
| 周次 | 四位年 + `W` + 两位周 | `2026W02` |

编号只在「已知条件」里出现时才填。**没给编号就不要自己想一个** ——
先调 `resolve_person` / `resolve_aircraft` / `resolve_week` 把名称解析成编号。
""".strip()


def _body_for(config: PromptConfigName, base: Prompt) -> str:
    if config == "zero_shot":
        return ZERO_SHOT_SYSTEM
    if config == "production":
        return base.body
    return f"{base.body}\n\n{OPTIMIZED_SUFFIX}"


def registry_for(
    config: PromptConfigName,
    *,
    base: PromptRegistry | None = None,
    prompt_key: str = "system",
) -> PromptRegistry:
    """按配置产出一个覆盖过的 `PromptRegistry`。

    **`prompt_version` 保持原样不动**：它的模式是 `^v\\d+$`（锁文件与 trace 都按
    这个模式解析），塞不下 `v1-optimized` 这种后缀，而为了打标记去放宽模式，
    等于让全项目的版本号校验为一次评测让路。配置名改记在 `description` 上，
    评测结果文件本身也逐条带着 `config` 字段，分得开。
    """
    source = base or PromptRegistry.load()
    prompts: dict[str, Prompt] = {}
    for component in ALL_COMPONENTS:
        original = source.get(component, prompt_key)
        prompts[f"{component}/{prompt_key}"] = original.model_copy(
            update={
                "body": _body_for(config, original),
                "description": f"{original.description}（消融配置：{config}）",
            }
        )
    return PromptRegistry(prompts, get_settings().PROMPTS_DIR)


def describe(config: PromptConfigName) -> str:
    """配置的一句话说明，进评测结果文件的表头。"""
    return {
        "zero_shot": "14B 原始（零样本）：通用系统提示词 + 工具表，下界参照",
        "production": "14B 原始 + 当前生产提示词（实测无 few-shot）：§15.4 的正式基线",
        "optimized": "14B + 提示工程再优化：生产提示词 + 五条硬性要求 + 编号形态表",
    }[config]


def system_prompt_of(config: PromptConfigName, component: ComponentName) -> str:
    """取某配置下某组件的系统提示词正文（算 token 数用）。"""
    return registry_for(config).get(component).body


__all__ = [
    "ALL_PROMPT_CONFIGS",
    "OPTIMIZED_SUFFIX",
    "ZERO_SHOT_SYSTEM",
    "PromptConfigName",
    "describe",
    "registry_for",
    "system_prompt_of",
]
