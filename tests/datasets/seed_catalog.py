"""`sft_seed` 的构造（v6 §15.2「种子数据（人工）」那一格）。

## 本窗口只备种子

§15.2 的流程图从「种子数据」出发，经指令扩写 → 学生自采样 → **确定性过滤** →
教师补硬样本 → 程序化生成 → 难负例挖掘 → 去重配比，最终约 3000 条 SFT 样本。
**那条管线是 W12 的事**，M9-A 只交种子本身：

| 种子 | 条数 | 来源 |
|---|---|---|
| 真实排班需求表述 | 60 | 从 `nl_360` 的排班/指定/重排三层里**确定性抽样** |
| 规则 | 14 | 从 `rules/ruleset_v1.3.yaml` **读出来** |
| 语义假设 S-01~S-13 | 13 | 从 `rules/semantics.yaml` **读出来** |
| 实体表 | 36 | 8 人 + 12 课目 + 8 机 + 6 空域 + 2 跑道 |

合计 **123 条**。

## 为什么规则与语义假设要读而不是抄

手抄会在下一次改规则时**悄悄分叉** —— 而合成数据是拿它们当事实用的：
一条抄错的约束会被扩写成几十条训练样本，然后模型学到的是那条错的。
从 yaml 读，改一次规则种子跟着变，`items.jsonl` 的哈希也跟着变，diff 里看得见。

## 60 条需求表述为什么从 nl_360 抽而不是另写

§15.2 说的是「**真实**排班需求表述」。`nl_360` 的排班三层本来就是照着真实交互
构造并经业务方复核过的 —— 另写一批等于把同一件事做两遍，还多一份要维护的口径。
抽样是确定性的（每层按固定步长取 20 条），换不出别的结果。
"""

from __future__ import annotations

from typing import Any, Final

import yaml

from backend.core.ruleset import get_ruleset
from backend.datasets.entities import AIRCRAFT, AIRSPACES, MISSIONS, PERSONS, RUNWAYS
from backend.datasets.loader import REPO_ROOT
from tests.datasets import nl_catalog

#: 三个排班相关层，各取 20 条 = 60
REQUEST_LAYERS: Final[tuple[str, ...]] = (
    "standard_schedule",
    "targeted_schedule",
    "disrupted_reschedule",
)
PER_LAYER: Final[int] = 20


def request_items() -> list[dict[str, Any]]:
    """60 条真实排班需求表述。**确定性抽样**：每层按固定步长取 20 条。"""
    rows: list[dict[str, Any]] = []
    for layer in REQUEST_LAYERS:
        pool = [r for r in nl_catalog.build() if r["layer"] == layer]
        step = max(1, len(pool) // PER_LAYER)
        picked = pool[::step][:PER_LAYER]
        for item in picked:
            slots = item["expected_slots"]
            rows.append(
                {
                    "item_id": f"SEED-REQ-{len(rows) + 1:03d}",
                    "kind": "request",
                    "text": item["utterance"],
                    "source_ref": item["item_id"],
                    "payload": {
                        "layer": layer,
                        "intent": item["expected_intent"],
                        "action": item["expected_action"],
                        "persons": slots["persons"],
                        "aircraft": slots["aircraft"],
                        "missions": slots["missions"],
                        "week": slots["week"],
                        "modifiers": [m["kind"] for m in slots["constraint_modifiers"]],
                    },
                }
            )
    return rows


def rule_items() -> list[dict[str, Any]]:
    """14 条规则，从 ruleset yaml 读。"""
    ruleset = get_ruleset()
    rows: list[dict[str, Any]] = []
    for rule_id, spec in sorted(ruleset.rules.items()):
        rows.append(
            {
                "item_id": f"SEED-RUL-{rule_id:03d}",
                "kind": "rule",
                "text": f"约束{rule_id}·{spec.title}（{spec.tier}，{spec.kind}）：{spec.statement}",
                "source_ref": f"rules/ruleset_v{ruleset.version}.yaml#{rule_id}",
                "payload": {
                    "rule_id": rule_id,
                    "title": spec.title,
                    "tier": spec.tier,
                    "kind": spec.kind,
                    "relaxable": bool(spec.relaxable),
                    "check_id": spec.check_id,
                },
            }
        )
    return rows


def semantic_items() -> list[dict[str, Any]]:
    """13 条语义假设 S-01~S-13，从 semantics yaml 读。"""
    payload = yaml.safe_load((REPO_ROOT / "rules" / "semantics.yaml").read_text(encoding="utf-8"))
    switches = payload["switches"]
    rows: list[dict[str, Any]] = []
    for number, key in enumerate(sorted(switches), start=1):
        switch = switches[key]
        rows.append(
            {
                "item_id": f"SEED-SEM-{number:03d}",
                "kind": "semantic",
                "text": (
                    f"{key}·{switch['topic']}：取值 {switch['value']}。"
                    f"{str(switch.get('rationale', '')).strip()}"
                ),
                "source_ref": f"rules/semantics.yaml#{key}",
                "payload": {
                    "switch_id": key,
                    "value": switch["value"],
                    "options": list(switch.get("options", [])),
                    "lands_in": list(switch.get("lands_in", [])),
                    "semantics_version": payload["semantics_version"],
                },
            }
        )
    return rows


def entity_items() -> list[dict[str, Any]]:
    """36 条实体：8 人 + 12 课目 + 8 机 + 6 空域 + 2 跑道。"""
    rows: list[dict[str, Any]] = []

    def add(text: str, payload: dict[str, Any]) -> None:
        rows.append(
            {
                "item_id": f"SEED-ENT-{len(rows) + 1:03d}",
                "kind": "entity",
                "text": text,
                "source_ref": "v6 §1.3 基准实体全景",
                "payload": payload,
            }
        )

    for pid, (name, role) in PERSONS.items():
        add(
            f"{pid}·{name}，身份为{role}。",
            {"entity_type": "person", "id": pid, "name": name, "role": role},
        )
    for mission, (minutes, freq, space) in MISSIONS.items():
        add(
            f"{mission}·时长 {minutes} 分钟，每 {freq} 天至少一次，绑定空域 {space}。",
            {
                "entity_type": "mission",
                "id": mission,
                "duration_min": minutes,
                "freq_days": freq,
                "airspace": space,
            },
        )
    for plane, kind in AIRCRAFT.items():
        add(f"{plane}·机型 {kind}。", {"entity_type": "aircraft", "id": plane, "type": kind})
    for space, capacity in AIRSPACES.items():
        add(
            f"{space}·同时段容量 {capacity}。",
            {"entity_type": "airspace", "id": space, "capacity": capacity},
        )
    for runway, types in RUNWAYS.items():
        add(
            f"{runway}·服务机型 {'、'.join(types)}。",
            {"entity_type": "runway", "id": runway, "serves": list(types)},
        )
    return rows


def build() -> list[dict[str, Any]]:
    """123 条 = 60 需求 + 14 规则 + 13 语义假设 + 36 实体。"""
    return [*request_items(), *rule_items(), *semantic_items(), *entity_items()]
