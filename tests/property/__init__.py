"""属性测试（v6 §12.1）。

两条核心不变量：

- `test_solver_output_always_passes_validator` —— 求解器只要出解，独立校验器必过。
  **反例即规格理解分歧 bug（FTS-3003，CRITICAL）**，按 CLAUDE.md §7 第 5 条停下来
  报告，不许自行改代码抹平。
- `test_validator_catches_injected_violations` —— 向合法计划注入单点违规，校验器
  必须定位到正确的规则编号。

场景生成器在 :mod:`tests.property.scenario`，违规注入在
:mod:`tests.property.injections`。
"""
