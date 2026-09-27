#!/usr/bin/env python3
"""一条本地启动命令：

    python3 run.py            # 启动服务（首次自动建库并载入串包/缩水/返工样例）
    python3 run.py --reset    # 清空数据库与照片后重新载入样例
    python3 run.py --port 8080

环境要求：Python 3.10+，无第三方依赖。
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from app.db import Database          # noqa: E402
from app.seed import seed            # noqa: E402
from app.server import serve, PHOTO_DIR  # noqa: E402
from app.services import Service      # noqa: E402

DATA_DIR = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA_DIR, "curtaincare.db")


def main() -> None:
    ap = argparse.ArgumentParser(description="家政窗帘拆洗与房间复装核验系统")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--reset", action="store_true", help="清空数据并重新载入演示样例")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    args = ap.parse_args()

    os.makedirs(DATA_DIR, exist_ok=True)
    if args.reset:
        for p in (DB_PATH,):
            if os.path.exists(p):
                os.remove(p)
        if os.path.isdir(PHOTO_DIR):
            shutil.rmtree(PHOTO_DIR)
        print("✓ 已清空旧数据")

    db = Database(DB_PATH)
    svc = Service(db)
    seed(svc)
    n = db.one("SELECT COUNT(*) c FROM orders")["c"]
    print(f"✓ 数据库就绪：{DB_PATH}（{n} 个演示订单，含串包/缩水/二次返工样例）")
    serve(DB_PATH, host=args.host, port=args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
