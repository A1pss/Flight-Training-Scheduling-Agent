# FTS · 飞行训练排班系统

全离线内网部署的飞行训练排班系统。**主体是确定性工作流**：排班由 OR-Tools CP-SAT 求解，
由一套与求解器实现完全独立的校验器兜底。LLM 只做三件事 —— 把人话翻译成精确的求解输入、
在冲突时组织人类能懂的权衡、对结果给出可信解释。**LLM 不生成、不修改、不参与任何一条架次记录。**

- 常驻工作指令：[`CLAUDE.md`](CLAUDE.md)
- 权威设计：[`docs/FTS_飞行训练排班系统_工程化设计方案_v6.md`](docs/)
- 规格裁决：[`docs/SPEC_DECISIONS.md`](docs/SPEC_DECISIONS.md)
- 规格锁定：[`docs/M0_规格锁定.md`](docs/M0_规格锁定.md)

## 快速开始

```bash
conda activate schedule                 # Python 3.11
cp .env.example .env

bash deploy/native/init_pg.sh           # PG16 独立实例 → 127.0.0.1:5433，建库 fts
bash deploy/native/start_redis.sh       # Redis7    → 127.0.0.1:6380
bash deploy/native/install_ollama.sh    # 用户态解压安装（版本钉在 v0.6.8，见脚本注释）
bash deploy/native/start_ollama.sh      # Ollama    → 127.0.0.1:11434，绑 GPU 0
bash deploy/native/pull_models.sh       # qwen2.5:14b-instruct-q4_K_M
bash deploy/native/fetch_bge.sh         # bge-m3 + bge-reranker-v2-m3
bash deploy/native/fetch_paddleocr.sh   # PaddleOCR 中文模型（离线可用）

bash deploy/native/healthcheck.sh       # 全栈体检 —— 每个开发窗口的开工前置
```

## 质量门禁（CLAUDE.md §6，一条不过就不许推）

```bash
conda run -n schedule ruff check . --fix
conda run -n schedule ruff format .
conda run -n schedule mypy backend --strict
conda run -n schedule bandit -r backend -ll
conda run -n schedule lint-imports
conda run -n schedule pytest -q --cov=backend --cov-report=term-missing --cov-fail-under=80
```

## 三条依赖禁令（由 `lint-imports` 强制）

1. `validator/` 禁止 import `solver/` —— 双通道校验不得合流
2. `solver/` `nodes/` `validator/` 禁止 import `skills_loader/` —— Skill 影响不到排班结果
3. 除 `core/http.py` 外全仓库禁止 import `requests`/`httpx`/`urllib` —— egress 收口

## 三态 Provider

```bash
LLM_PROVIDER=ollama LLM_MODEL=qwen2.5:14b-instruct-q4_K_M   # 开发 = 上线
LLM_PROVIDER=mock                                            # 单测/CI，零 LLM 调用
LLM_PROVIDER=replay REPLAY_TRACE_DIR=./traces/accept_v1      # 回归，零 LLM 调用
```
