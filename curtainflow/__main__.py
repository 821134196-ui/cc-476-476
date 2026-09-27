"""一条本地启动命令：

    python3 -m curtainflow            # 重建样例数据并启动服务于 127.0.0.1:8765
    python3 -m curtainflow --port 9000
    python3 -m curtainflow --no-demo  # 使用现有数据库，不重建样例
"""
import argparse

from . import db
from .adapters import DeliveryAdapter, PaymentAdapter
from .reminders import ReminderWorker
from .seed import build_demo, print_summary
from .server import create_server
from .service import CurtainService


def main():
    parser = argparse.ArgumentParser(prog="curtainflow",
                                     description="家政窗帘拆洗与房间复装核验系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--db", default="data/curtainflow.db")
    parser.add_argument("--photos", default="data/photos")
    parser.add_argument("--no-demo", action="store_true",
                        help="不重建演示样例，直接使用现有数据库")
    args = parser.parse_args()

    delivery = DeliveryAdapter()
    payment = PaymentAdapter()

    if args.no_demo:
        conn = db.connect(args.db)
        svc = CurtainService(conn, args.photos, delivery=delivery,
                             payment=payment)
        print(f"使用现有数据库 {args.db}（未重建样例）")
    else:
        svc, summary = build_demo(args.db, args.photos, delivery, payment)
        print_summary(svc, summary)

    worker = ReminderWorker(svc, interval=60)
    worker.start()

    httpd = create_server(svc, worker, args.photos, args.host, args.port)
    url = f"http://{args.host}:{args.port}"
    print(f"\n服务已启动：{url}  （Ctrl+C 停止）")
    print("角色在页面右上角切换：拆装技师 / 洗护员 / 客服 / 客服主管 / 财务\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        worker.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
