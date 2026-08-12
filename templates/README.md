# templates/ —— Excel 模板与版式基准

| 文件 | 是什么 |
|---|---|
| `plan_workbook_template.xlsx` | 四表模板空壳：工作表名与顺序固定、Sheet 1~3 表头就位、Sheet 4 七个区块标题与列名就位、**无任何数据行** |
| `版式基准抽取清单.md` | 模板的设计依据（业务方 2026-08-13 确认的归档副本，源文件在 `docs/`） |

## 模板怎么生成、怎么用

```bash
conda run -n schedule python -m backend.report.template   # 重新生成 plan_workbook_template.xlsx
```

模板**不是**「一张画好的空表往里填行」——Sheet 1~3 的表头是逐个分组重复的
（每个星期 / 每个人 / 每架飞机各带一行表头，见版式基准 image 1 与 image 4），
所以真正的模板是 `backend/report/template.py::template_contract()` 那份**契约**：
工作表名与顺序、三张表的表头、七个区块的标题与列名、区块 1 的必需字段标签、
列宽与字体。本 xlsx 由同一份契约生成，用途有二：

1. **给业务方看版式**（不用跑代码就能打开看）；
2. 让「产物的表头逐字匹配模板」这句话有个可比的实体
   （`tests/unit/test_report_excel.py::test_headers_match_template_verbatim`）。

契约本身来自 `backend/validator/workbook.py` 的模块级常量 —— 那边是**回读**，
渲染层是**写出**，两边共用一份常量，才不会出现「写出改了、回读没改」的漂移。

## 内容一律不采信

`data/origin/image 1~4.png` 只作**版式**基准。模板与代码里没有任何一条来自图的架次数据：
图中出现的 `missionD-2` / `Range Route 1|2` / `Large Area C` 在实体表里根本不存在，
且图中 **17 处**架次违反硬约束（逐条算式见抽取清单 §4）。
把它们当黄金用例的期望输出是 `CLAUDE.md §11` 明列的反模式。

## 着色

业务方 2026-08-13 裁定 **不着色**：Sheet 1~3 全白底黑字。
唯一的底纹是 Sheet 4 区块标题行的 `#DDEBF6` —— 那是 v6 §10.4 的强制项，与版式图无关。
image 4 里按课目类别着色的那套色板实测值保留在抽取清单 §1.4，供日后要恢复时取用。
