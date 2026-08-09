"""修复层（v6 §1.5 + §5.2）—— 摄取管线的**强制步骤**，不是可选优化。

原始 PDF 的文本流里课目编号被硬换行截断：

```
missionA-1、missionA-2、mis
sionB-1、missionB-2、mission
C-1、missionC-2、missionF-1
```

直接切分或直接正则会产出 `mis`、`sionB-1` 这类脏 token，一路流进 PG 就是
外键失配。同时人员记录横跨 5~6 个物理行，`extract_tables()` 的返回需按
「主键列非空」重新聚合。

## 处理顺序（顺序本身是规格的一部分）

1. :func:`normalize_widths` —— 全半角归一化（NFKC）
2. :func:`strip_cjk_linebreaks` —— 中文之间的换行直接删（v6 §5.2 原样）
3. :func:`dehyphenate_linebreaks` —— 行尾连字符 + 换行 → 拼接
4. :func:`join_wrapped_words` —— 拉丁/数字 token 跨行拼接
5. :data:`TOKEN_PATTERNS` —— v6 §5.2 的五条正则，**原样落地**
6. :func:`normalize_separators` —— 分隔符归一化

**第 3 步为什么在第 5 步之前**：`aircraft.pdf` 的适配课目列里，`missionC-` 与
`1` 被拆在两行（原始字节即 `missionC-\\n1`）。第 3 步按标准的断词拼接规则去掉
行尾连字符后得到 `missionC1` —— 这正是 §5.5 X2 记录的那个变体 —— 再由
:data:`TOKEN_PATTERNS` 第 5 条还原成 `missionC-1`。**顺序倒过来 X2 就永远不会
出现，第 5 条正则也就成了死代码**，下次真出现 `missionC1` 反而没人兜。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

from backend.core.errors import IngestionError

#: 合法课目编号的唯一形态。后置断言以它为准。
MISSION_ID_RE = re.compile(r"mission[A-H]-\d")
#: 完整匹配（`fullmatch` 用）
MISSION_ID_FULL_RE = re.compile(r"mission[A-H]-\d")
#: 机号 / 人员编号
AIRCRAFT_ID_RE = re.compile(r"AC\d{2}")
PERSON_ID_RE = re.compile(r"P\d{2}")

#: v6 §5.2 的五条断词修复正则，**原样落地，顺序不得调整**。
#: 第 5 条是 v6 新增：`aircraft.pdf` 的适配课目列在断词拼接后写作 `missionC1`
#: （缺连字符），与 `missionC-1` 是同一课目，不修会导致外键失配（§5.5 X2）。
TOKEN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmis\s*\n?\s*sion([A-H])\s*-?\s*(\d)"), r"mission\1-\2"),
    (re.compile(r"\bmission\s*\n\s*([A-H])\s*-\s*(\d)"), r"mission\1-\2"),
    (re.compile(r"\bmi\s*\n?\s*ssion([A-H])-(\d)"), r"mission\1-\2"),
    (re.compile(r"\bssion([A-H])-(\d)"), r"mission\1-\2"),
    (re.compile(r"\bmission([A-H])(\d)\b"), r"mission\1-\2"),  # missionC1 → missionC-1
]

#: 中文字符之间的换行（v6 §5.2 原样）
_CJK_LINEBREAK_RE = re.compile(r"(?<=[一-鿿])\n(?=[一-鿿])")
#: 行尾连字符断词：字母 + `-` + 换行 + 数字。**刻意收窄**——若写成通用的 `-\n`，
#: 形如 `2026-01-\n09` 的日期会被拼成 `2026-0109`。
_DEHYPHEN_RE = re.compile(r"(?<=[A-Za-z])-\s*\n\s*(?=\d)")
#: 拉丁/数字 token 被硬换行拆开（`missi\nonB-1`、`missio\nnH-1`、`2026\n-01-07`）
_WRAPPED_WORD_RE = re.compile(r"(?<=[A-Za-z0-9])[ \t]*\n[ \t]*(?=[A-Za-z0-9\-])")
_WRAPPED_WORD_TAIL_RE = re.compile(r"(?<=[A-Za-z0-9\-])[ \t]*\n[ \t]*(?=[A-Za-z0-9])")
#: 列表分隔符：顿号 / 半角逗号 / 分号 / 中文逗号（NFKC 后后两者已成半角）
_LIST_SEPARATOR_RE = re.compile(r"[、,;]+")
#: 机型列表分隔符额外接受 `/`（`missions.pdf` 写作 `JL-8/JL-9`）
_TYPE_SEPARATOR_RE = re.compile(r"[、,;/]+")
#: 表示「无」的占位符
NULL_TOKENS = frozenset({"—", "-", "－", "", "无", "N/A", "NA", "null", "None"})


def normalize_widths(text: str) -> str:
    """全半角归一化。

    用 NFKC：全角字母数字与全角标点（`（）：，；`）一律折成半角，`、。【】≥`
    这些没有半角等价物的字符原样保留。归一化到**半角**而不是全角，是为了让
    日期、机号、时刻这些 ASCII 值只有一种形态。
    """
    return unicodedata.normalize("NFKC", text)


def strip_cjk_linebreaks(text: str) -> str:
    """中文字符之间的换行直接删（v6 §5.2 第一步，原样）。"""
    return _CJK_LINEBREAK_RE.sub("", text)


def dehyphenate_linebreaks(text: str) -> str:
    """行尾连字符断词拼接：`missionC-\\n1` → `missionC1`（随后由第 5 条正则还原）。"""
    return _DEHYPHEN_RE.sub("", text)


def join_wrapped_words(text: str) -> str:
    """把被硬换行拆开的拉丁/数字 token 拼回去。

    `missi\\nonB-1` → `missionB-1`；`missio\\nnH-1` → `missionH-1`；
    `2026\\n-01-07` → `2026-01-07`。这三种形态 :data:`TOKEN_PATTERNS` 都不覆盖
    （第 1~4 条只认 `mis`/`mission`/`mi`/`ssion` 四种切点），所以必须有这一步。
    """
    text = _WRAPPED_WORD_RE.sub("", text)
    return _WRAPPED_WORD_TAIL_RE.sub("", text)


def apply_token_patterns(text: str) -> str:
    """套用 v6 §5.2 的五条断词修复正则。"""
    for pat, rep in TOKEN_PATTERNS:
        text = pat.sub(rep, text)
    return text


def normalize_separators(text: str) -> str:
    """分隔符归一化：把列表分隔符统一成顿号，并压掉多余空白。"""
    text = _LIST_SEPARATOR_RE.sub("、", text)
    return re.sub(r"[ \t　]+", " ", text).strip()


def repair_linebreaks(text: str) -> str:
    """v6 §5.2 给出的那个函数，签名与行为原样保留。

    完整管线请用 :func:`repair_text` —— 它在本函数前后各补了一步（断词拼接、
    拉丁 token 拼接），覆盖 v6 五条正则照顾不到的切点。
    """
    text = strip_cjk_linebreaks(text)
    return apply_token_patterns(text)


def repair_text(text: str) -> str:
    """完整修复层：按模块文档的六步顺序处理自由文本。"""
    text = normalize_widths(text)
    text = strip_cjk_linebreaks(text)
    text = dehyphenate_linebreaks(text)
    text = join_wrapped_words(text)
    return apply_token_patterns(text)


def repair_cell(value: str | None) -> str:
    """修复单个表格单元格。

    单元格是**一个逻辑值**，PDF 渲染器造成的硬换行在这里没有任何语义，
    所以在 :func:`repair_text` 之后把残留换行一并压成空白 —— 形如
    `20周，每14天≥1\\n次` 的频率描述必须拼回一句话才能解析。
    """
    if value is None:
        return ""
    repaired = repair_text(value)
    repaired = re.sub(r"\s*\n\s*", "", repaired)
    return re.sub(r"[ \t　]+", " ", repaired).strip()


def is_null_token(value: str | None) -> bool:
    """PDF 里表示「无」的占位（`—`）判定。"""
    return value is None or value.strip() in NULL_TOKENS


def split_list(value: str | None, *, allow_slash: bool = False) -> list[str]:
    """把「a、b、c」这类列表单元格切成去重后的有序列表。

    `allow_slash` 用于机型列（`JL-8/JL-9`）—— 其它列不能拆 `/`，否则
    `IFR Route` 这类含斜杠的名字会被切碎。
    """
    cell = repair_cell(value)
    if is_null_token(cell):
        return []
    pattern = _TYPE_SEPARATOR_RE if allow_slash else _LIST_SEPARATOR_RE
    items: list[str] = []
    for raw in pattern.split(cell):
        item = raw.strip()
        if item and not is_null_token(item) and item not in items:
            items.append(item)
    return items


def extract_mission_tokens(value: str | None) -> list[str]:
    """从课目列表单元格里抽出课目编号，保持出现顺序、去重。

    **不做「顺手修一下」的容错**：切出来长什么样就返回什么样，残缺 token 由
    :func:`assert_no_orphan_tokens` 拦截并抛 `FTS-1003`。静默丢弃脏 token 等于
    「尽力而为的部分入库」，是铁律 7 明令禁止的。
    """
    return split_list(value)


def aggregate_rows(rows: Sequence[Sequence[str | None]], key_index: int = 0) -> list[list[str]]:
    """跨行单元格聚合：按「主键列非空」重新聚合物理行（v6 §1.5）。

    `pdfplumber.extract_tables()` 对横跨 5~6 个物理行的记录，可能返回主键列
    为空的续行。规则很简单：主键列有值 → 开一条新记录；主键列为空 → 把各列
    追加到上一条记录的对应列上。

    表头行（主键列文本等于 `编号` / `机号` / `课目编号` 这类）由调用方剥离，
    本函数不猜。
    """
    aggregated: list[list[str]] = []
    for row in rows:
        cells = ["" if c is None else str(c) for c in row]
        if not cells:
            continue
        key = cells[key_index].strip() if key_index < len(cells) else ""
        if key or not aggregated:
            aggregated.append(cells)
            continue
        previous = aggregated[-1]
        # 续行可能比首行短或长，按最大列数对齐
        while len(previous) < len(cells):
            previous.append("")
        for i, cell in enumerate(cells):
            if cell:
                previous[i] = f"{previous[i]}\n{cell}" if previous[i] else cell
    return aggregated


def assert_no_orphan_tokens(records: Iterable[dict[str, object]]) -> None:
    """摄取后置断言：不允许残缺课目编号流入数据库（v6 §5.2 原样）。

    抛 :class:`~backend.core.errors.IngestionError`（FTS-1003）阻断，
    **绝不静默降级**（铁律 7）。
    """
    bad: list[str] = []
    for record in records:
        missions = record.get("missions", [])
        if not isinstance(missions, list):
            continue
        for token in missions:
            text = str(token)
            if not MISSION_ID_FULL_RE.fullmatch(text):
                bad.append(text)
    if bad:
        raise IngestionError(
            f"发现残缺课目编号 {bad}，请检查 PDF 抽取修复层",
            details={"orphan_tokens": bad, "expected_pattern": MISSION_ID_FULL_RE.pattern},
            suggestions=[
                "检查 backend/ingestion/repair.py 的 TOKEN_PATTERNS 是否覆盖该切点",
                "确认 repair_cell() 已对该单元格执行（而非直接用 extract_tables 原值）",
            ],
        )


__all__ = [
    "AIRCRAFT_ID_RE",
    "MISSION_ID_FULL_RE",
    "MISSION_ID_RE",
    "NULL_TOKENS",
    "PERSON_ID_RE",
    "TOKEN_PATTERNS",
    "aggregate_rows",
    "apply_token_patterns",
    "assert_no_orphan_tokens",
    "dehyphenate_linebreaks",
    "extract_mission_tokens",
    "is_null_token",
    "join_wrapped_words",
    "normalize_separators",
    "normalize_widths",
    "repair_cell",
    "repair_linebreaks",
    "repair_text",
    "split_list",
    "strip_cjk_linebreaks",
]
