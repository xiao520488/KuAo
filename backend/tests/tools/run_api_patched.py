# -*- coding: utf-8 -*-
"""用隔离库启动后端（不写 competitions.db）。

用法：KUO_DB_PATH=<库路径> KUO_PORT=8010 python run_api_patched.py
"""
import tempfile
import os
import sys

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))  # 仓库根
BACKEND = os.path.join(BASE, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import db  # noqa: E402

db.DB_PATH = os.environ.get("KUO_DB_PATH", os.path.join(tempfile.gettempdir(), "kuo-verify", "approve_api_test.db"))

import main  # noqa: E402
import uvicorn  # noqa: E402

uvicorn.run(main.app, host="127.0.0.1", port=int(os.environ.get("KUO_PORT", "8010")),
            log_level="info")