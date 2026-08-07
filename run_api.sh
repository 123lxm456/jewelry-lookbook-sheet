#!/bin/bash
# 进入项目目录
cd /www/wwwroot/picture.deedface.com/jewelry-lookbook-sheet
# 注入环境变量
export WEB_HOST=0.0.0.0
export WEB_PORT=8000
# 确保 API 服务和 run_workflow.sh 子进程使用同一套 Python 依赖。
export PATH="/www/wwwroot/picture.deedface.com/venv/bin:$PATH"
export PYTHON_BIN="/www/wwwroot/picture.deedface.com/venv/bin/python"
export IMAGE_GEN_CLI="${IMAGE_GEN_CLI:-/www/wwwroot/picture.deedface.com/jewelry-lookbook-sheet/scripts/image_gen_api.py}"

exec /www/wwwroot/picture.deedface.com/venv/bin/python -m uvicorn app:app \
  --app-dir /www/wwwroot/picture.deedface.com/jewelry-lookbook-sheet \
  --host 127.0.0.1 \
  --port 8000
