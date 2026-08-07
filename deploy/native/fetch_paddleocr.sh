#!/usr/bin/env bash
# 预下载 PaddleOCR 中文模型权重到项目内目录，确保离线可用。
# PaddleOCR 首次调用会自行联网拉模型——在内网部署时那一步必然失败，
# 所以必须在装机阶段就把权重落到本地。
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"

export PADDLE_HOME PADDLE_PDX_CACHE_HOME PADDLEOCR_HOME
mkdir -p "$PADDLE_HOME"

conda run --no-capture-output -n "$FTS_PY_ENV" python - <<'PY'
import os
from pathlib import Path

home = Path(os.environ["PADDLE_HOME"])
from paddleocr import PaddleOCR

# 构造即触发模型下载（det + rec + cls 三件套，中文）
ocr = PaddleOCR(lang="ch")
print("PaddleOCR 初始化完成")

files = [p for p in home.rglob("*") if p.is_file()]
print(f"已落地 {len(files)} 个文件，合计 "
      f"{sum(p.stat().st_size for p in files) / 1e6:.1f} MB → {home}")
PY

fts_ok "PaddleOCR 中文模型已预下载到 $PADDLE_HOME"
