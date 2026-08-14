"""Skill 体系（v6 §7.8）：业务方可编辑的知识层。

```python
from backend.skills_loader import load_library, render_skills, route_for_component

library = load_library()                                   # skills/ 下全部 SKILL.md
names = route_for_component("explain")                     # 确定性路由，不问 LLM
context = render_skills(library, names)                    # 渐进式披露的第二档
```

> **Skill 只影响 LLM 组件如何「解析、解释、措辞」，绝不影响「排什么班」。**
> —— v6 §7.8.2

`solver/`、`nodes/`、`validator/` **禁止 import 本包**（`.importlinter` 禁令二）。
"""

from backend.skills_loader.loader import (
    DISCLAIMER,
    SKILL_FILENAME,
    Skill,
    SkillLibrary,
    SkillLoadError,
    SkillNotAuthoritativeError,
    discover,
    load_library,
    parse_skill,
    render_skills,
)
from backend.skills_loader.routes import (
    NO_SKILL_COMPONENTS,
    SKILL_ROUTES,
    all_routed_skills,
    diagnosis_conditions,
    ingest_conditions,
    missing_from_library,
    route_for_component,
    route_skills,
)

__all__ = [
    "DISCLAIMER",
    "NO_SKILL_COMPONENTS",
    "SKILL_FILENAME",
    "SKILL_ROUTES",
    "Skill",
    "SkillLibrary",
    "SkillLoadError",
    "SkillNotAuthoritativeError",
    "all_routed_skills",
    "diagnosis_conditions",
    "discover",
    "ingest_conditions",
    "load_library",
    "missing_from_library",
    "parse_skill",
    "render_skills",
    "route_for_component",
    "route_skills",
]
