"""待澄清问题（Open Question）—— 缺必需输入时**向用户提问**，不猜、不兜底。

与 :mod:`backend.ingestion.conflicts` 的区别：

- **Conflict** = 两个来源对同一件事说法不一致，两侧取值都在，要人裁决选哪个
- **OpenQuestion** = 排班必需的某个值**根本没人提供**，没有可选项，要人直接给

两者都会阻断落库、都走人工确认门禁，但形态不同：冲突有 `value_a` / `value_b`，
问题只有一个「答什么类型」的声明。

## 为什么不设静默默认值

`training_progress.cycle_start`（课程周期起点）是主键的一部分，用错了整表要迁移。
`CLAUDE.md` 铁律 5「不假设实验数据」与铁律 10「有疑问就问，不要猜」在这里是同一
件事：**上传的文件没写、对话里也没说，就必须问**，而不是悄悄填一个看起来合理的
日期然后一路跑下去。

## 三条来源的优先级

1. **文件**：课目表里的「课程开始日期」列（见
   :data:`backend.ingestion.parsers.missions.CYCLE_START_COLUMNS`）
2. **对话/命令行**：用户显式给出（`--cycle-start`，或 W9 的 UI 表单）
3. **都没有** → 生成一条 :class:`OpenQuestion`，人工门禁拒绝放行并把问题抛给用户

只要 ① 有，就永远走不到 ③ —— **换一批带日期的数据，一行代码都不用改。**
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final, Literal

from backend.core.errors import RequiredInputMissingError
from backend.ingestion.schema import IngestedFacts

#: 答案的类型声明，门禁据此校验用户给的值
ValueKind = Literal["date", "text", "int"]

#: 课程周期起点问题的固定 id（W9 的 UI 与 CLI 都按它认这条问题）
QID_CYCLE_START: Final[str] = "Q_cycle_start"

#: 排班必需的实体类 → (面向用户的文档名, 该类在 IngestedFacts 上的字段名)。
#:
#: **跑道不在其中**：业务方 2026-08-10 确认跑道数据基本确定、不随每次上传变化，
#: 维持 `rules/semantics.yaml` S-05 的配置形态。
#: **规则条文也不在其中**：排班约束永远来自 `rules/` 下的版本化文件（v6 §5.4
#: 第 3 层），上传的规则原文只进 Chroma 供检索与解释引用，缺了不影响排班。
REQUIRED_ENTITY_DOCS: Final[tuple[tuple[str, str, str], ...]] = (
    ("persons", "人员档案", "飞行人员的编号/姓名/身份/机型资质/已完成课目"),
    ("aircraft", "飞机资源", "机号/机型/座位/每日可用窗/周转时间/维护计划/适配课目"),
    ("missions", "课目标准", "课目编号/名称/时长/周期与频率/先修/带飞/机型/空域"),
    ("airspaces", "空域资源", "空域编号/名称/同时段容量（通常与飞机资源同一份文件）"),
)


@dataclass(frozen=True)
class OpenQuestion:
    """一个必须由人回答才能继续的问题。"""

    question_id: str
    topic: str
    #: 面向用户的中文问题原文，直接可以显示给业务方
    question: str
    #: 为什么非答不可 —— 让用户明白拒答的后果，而不是被一个弹窗挡住
    why_it_matters: str
    value_kind: ValueKind
    #: 这条问题**怎么才能解决**：
    #: - `answer` = 用户给一个值就行（如课程开始日期）
    #: - `upload` = 少了一整类数据，**给什么值都没用，必须补传文件**
    resolution: Literal["answer", "upload"] = "answer"
    #: 该答案影响的范围，例如 {"missions": "missionA-1、missionA-2"}
    applies_to: dict[str, str] = field(default_factory=dict)
    #: 可供参考的线索（**不是默认值**，门禁不会替用户选）
    hints: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class QuestionAnswer:
    """用户对某条问题的回答。"""

    question_id: str
    value: str
    answered_by: str
    #: 答案来源：`file` 走不到这里；`prompt` = 对话/命令行；`ui` = W9 表单
    source: Literal["prompt", "ui", "baseline"] = "prompt"
    note: str = ""


def parse_answer(question: OpenQuestion, answer: QuestionAnswer) -> Any:
    """按问题声明的类型把答案解析成 Python 值，解析不了就抛 FTS-1004。"""
    raw = answer.value.strip()
    if not raw:
        raise RequiredInputMissingError(
            f"问题 {question.question_id} 的答案为空",
            details={"question_id": question.question_id},
        )
    if question.value_kind == "date":
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise RequiredInputMissingError(
                f"问题 {question.question_id} 的答案 {raw!r} 不是合法日期（需 YYYY-MM-DD）",
                details={"question_id": question.question_id, "value": raw},
            ) from exc
    if question.value_kind == "int":
        if not raw.lstrip("-").isdigit():
            raise RequiredInputMissingError(
                f"问题 {question.question_id} 的答案 {raw!r} 不是整数",
                details={"question_id": question.question_id, "value": raw},
            )
        return int(raw)
    return raw


def detect_missing_inputs(facts: IngestedFacts) -> list[OpenQuestion]:
    """排班必需的实体类里，哪一类**一条记录都没有** → 请用户补传文件。

    这一步要跑在引用完整性校验**之前**：少传一份课目文件时，引用完整性会因为
    每个人的「已完成课目」都指向不存在的课目而吐出一屏错误，而真正的原因只有
    一句话 —— 「你没传课目标准文件」。先说这句话，比让人从 9 条外键错误里
    自己反推有用得多。
    """
    questions: list[OpenQuestion] = []
    for attr, doc_name, columns in REQUIRED_ENTITY_DOCS:
        if getattr(facts, attr):
            continue
        questions.append(
            OpenQuestion(
                question_id=f"Q_missing_{attr}",
                topic=f"缺少{doc_name}",
                question=(
                    f"本次上传里没有检测到**{doc_name}**的任何记录，无法排班。\n"
                    f"请补传一份包含以下内容的文件：{columns}"
                ),
                why_it_matters=(
                    f"没有{doc_name}就没有对应实体，排班的候选枚举无从谈起。\n"
                    "系统**不会**拿上一次快照或基准数据顶替 —— 那样排出来的计划"
                    "看着正常，实际是照着别人的数据排的。"
                ),
                value_kind="text",
                resolution="upload",
                applies_to={"entity": attr, "document": doc_name},
                hints=[
                    "若该类数据和别的类在同一份文件里（如空域常与飞机资源同文件），"
                    "确认那份文件已上传且表头能被识别",
                ],
            )
        )
    return questions


def detect_open_questions(
    facts: IngestedFacts, *, provided: Sequence[str] = ()
) -> list[OpenQuestion]:
    """扫出这批数据里「没人提供、又必须有」的输入。

    `provided` 是调用方已经从对话/命令行拿到答案的 question_id，
    这些不再重复问。
    """
    questions: list[OpenQuestion] = []
    already = set(provided)

    if QID_CYCLE_START not in already:
        missing = [m.mission_id for m in facts.missions if m.cycle_start is None]
        if missing and facts.missions:
            questions.append(
                OpenQuestion(
                    question_id=QID_CYCLE_START,
                    topic="课程周期起点",
                    question=(
                        "这批课目的**训练周期从哪一天开始**？（请给一个日期，格式 YYYY-MM-DD）\n"
                        f"以下 {len(missing)} 门课目的文件里都没有「课程开始日期」这一列："
                        f"{'、'.join(missing)}"
                    ),
                    why_it_matters=(
                        "周期起点决定 12/16/20 周训练周期从哪天起算，也是 "
                        "training_progress 主键的一部分（v6 §6.3）。填错要迁移全表，"
                        "所以系统不替你猜。\n"
                        "若课目文件里加上「课程开始日期」列，以后就不会再问这个问题。"
                    ),
                    value_kind="date",
                    applies_to={"missions": "、".join(missing)},
                    hints=[
                        "若各门课目起点不同，请在课目文件里加「课程开始日期」列，逐行填写",
                        f"接受的列名：{'、'.join(_CYCLE_START_COLUMN_HINT)}",
                    ],
                )
            )
    return questions


#: 只用于提示文案，真正的列名清单在 parsers.missions 里
_CYCLE_START_COLUMN_HINT = (
    "课程开始日期",
    "课程起始日期",
    "周期起点",
    "周期开始日期",
    "开始日期",
    "起始日期",
)


#: 基准数据集的既有裁决 —— 与 §5.5 的 `ADJUDICATIONS` 同一口径：
#: **已经问过、业务方已经答过的问题**，记录在案，让基准快照能非交互重跑（铁律 9）。
#: 换任何一批新数据，`question_id` 相同但数据不同时**仍然会问**，因为
#: `--baseline` 之外的路径不会去读这张表。
BASELINE_ANSWERS: Final[dict[str, QuestionAnswer]] = {
    QID_CYCLE_START: QuestionAnswer(
        question_id=QID_CYCLE_START,
        value="2026-01-05",
        answered_by="业务方 Alps",
        source="baseline",
        note=(
            "2026-08-09 裁定：四份基准 PDF 未提供课程开始日期，取基准周周一 2026-01-05（v6 §1.2.3）"
        ),
    ),
}


__all__ = [
    "BASELINE_ANSWERS",
    "QID_CYCLE_START",
    "REQUIRED_ENTITY_DOCS",
    "OpenQuestion",
    "QuestionAnswer",
    "ValueKind",
    "detect_missing_inputs",
    "detect_open_questions",
    "parse_answer",
]
