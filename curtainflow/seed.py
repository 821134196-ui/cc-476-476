"""演示样例：一条启动命令载入串包、缩水、返工等典型场景。

客户信息全部为虚构数据。
"""
from datetime import datetime, timedelta

from . import db, photos
from .adapters import DeliveryAdapter, PaymentAdapter
from .reminders import ReminderWorker
from .service import CurtainService, DomainError, now_iso


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def build_demo(db_path="data/curtainflow.db", photo_dir="data/photos",
               delivery=None, payment=None):
    """重建数据库并灌入样例，返回 (service, summary)。"""
    conn = db.reset(db_path)
    delivery = delivery or DeliveryAdapter()
    payment = payment or PaymentAdapter()
    svc = CurtainService(conn, photo_dir, delivery=delivery, payment=payment)

    # ---------- 订单一：已送达，等待复装验收（主演示单） ----------
    o1 = svc.create_order(
        {"name": "林予安", "phone": "138-0000-1024",
         "address_code": "梧桐里3号201",
         "address": "（虚构）听松路梧桐里小区3号201室"},
        [{"name": "客厅", "windows": [{"code": "KT-01", "panels": [
            {"fabric": "棉麻混纺", "hook_count": 12, "care_req": "常温轻柔"},
            {"fabric": "棉麻混纺", "hook_count": 10, "care_req": "常温轻柔"},
        ]}]},
         {"name": "主卧", "windows": [{"code": "WO-01", "panels": [
             {"fabric": "高精密遮光布", "hook_count": 8},
             {"fabric": "高精密遮光布", "hook_count": 8},
         ]}]}],
        appoint_at=_iso(datetime.now() - timedelta(days=2)),
        note="南阳台窗帘有轻微褪色")
    svc.start_dismantle(o1["id"])
    tree1 = svc.order_tree(o1["id"])
    bag_ids = []
    for room in tree1["rooms"]:
        for win in room["windows"]:
            ids = [p["id"] for p in win["panels"]]
            bag_ids.append(svc.seal_bag(win["id"], ids)["id"])
    for bid in bag_ids:
        svc.ship_bag(bid)
        svc.receive_bag(bid)
    # 洗护中发现挂钩缺失一片 → 登记损伤 → 客服申请赔付 → 主管批准
    first_panel = tree1["rooms"][0]["windows"][0]["panels"][0]
    svc.report_damage(first_panel["id"], "hook_missing",
                      "拆洗清点发现金属挂钩缺失 2 个", role="washer")
    inc_hook = svc.list_incidents(o1["id"])[0]
    claim = svc.propose_claim(inc_hook["id"], 15.0, "金属挂钩2个补发",
                              role="cs")
    svc.decide_claim(claim["id"], True, role="cs_lead")
    # 全部洗烘
    q = svc.wash_queue()
    for t in q:
        svc.start_wash(t["panel_id"])
    for t in q:
        svc.finish_dry(t["panel_id"])
    # 按原窗位配包 → 批次 → 配送
    pkg_ids = []
    for room in tree1["rooms"]:
        for win in room["windows"]:
            ids = [p["id"] for p in win["panels"]]
            pkg_ids.append(svc.pack_package(win["id"], ids)["id"])
    batch = svc.create_batch(pkg_ids, vehicle="沪A·D3721")
    svc.dispatch_batch(batch["id"])
    svc.deliver_batch(batch["id"])

    # ---------- 订单二：洗护中 —— 缩水赔付 + 禁洗 + 返工 + 二次修补 ----------
    o2 = svc.create_order(
        {"name": "沈知遥", "phone": "139-0000-2048",
         "address_code": "海棠苑7栋502",
         "address": "（虚构）望海路海棠苑7栋502室"},
        [{"name": "书房", "windows": [{"code": "SF-01", "panels": [
            {"fabric": "真丝乔其", "hook_count": 6, "wash_forbidden": True,
             "care_req": "禁水洗，仅除尘挂晾"},
        ]}]},
         {"name": "儿童房", "windows": [{"code": "ET-01", "panels": [
             {"fabric": "全棉印花", "hook_count": 9},
         ]}, {"code": "ET-02", "panels": [
             {"fabric": "浅色雪尼尔", "hook_count": 11},
         ]}]}],
        appoint_at=_iso(datetime.now() - timedelta(days=1)))
    svc.start_dismantle(o2["id"])
    tree2 = svc.order_tree(o2["id"])
    b2 = []
    for room in tree2["rooms"]:
        for win in room["windows"]:
            b2.append(svc.seal_bag(
                win["id"], [p["id"] for p in win["panels"]])["id"])
    for bid in b2:
        svc.ship_bag(bid)
        svc.receive_bag(bid)  # 真丝乔其自动产生“布料禁洗”留痕
    et01 = tree2["rooms"][1]["windows"][0]["panels"][0]
    et02 = tree2["rooms"][1]["windows"][1]["panels"][0]
    silk = tree2["rooms"][0]["windows"][0]["panels"][0]

    # 缩水样例：全棉印花烘干后缩水 → 赔付申请 → 主管批准
    svc.start_wash(et01["id"])
    svc.report_damage(et01["id"], "shrink",
                      "烘干后纵向缩水约 3cm，需放边重做", role="washer")
    inc_shrink = next(i for i in svc.list_incidents(o2["id"])
                      if i["type"] == "shrink")
    c2 = svc.propose_claim(inc_shrink["id"], 120.0, "缩水放边重做补偿",
                           role="cs")
    svc.decide_claim(c2["id"], True, role="cs_lead")
    svc.finish_dry(et01["id"])

    # 返工样例：浅色雪尼尔染色 → 主管批准返工，退回洗护队列重洗
    svc.start_wash(et02["id"])
    svc.report_damage(et02["id"], "dye",
                      "与深色布件同洗导致下摆轻微串色", role="washer")
    inc_dye = next(i for i in svc.list_incidents(o2["id"])
                   if i["type"] == "dye")
    svc.approve_rework(inc_dye["id"], role="cs_lead")
    # 返工后的新洗护任务（重新洗烘，演示中完成它，帘片回到 ready）
    t_new = next(t for t in svc.wash_queue() if t["panel_id"] == et02["id"])
    svc.start_wash(t_new["panel_id"])
    svc.finish_dry(t_new["panel_id"])

    # 二次修补样例：真丝乔其边缘两次修补（自动留痕 second_repair）
    r1 = svc.create_repair(silk["id"], "下摆卷边松动，重新手工扦边",
                           role="washer")
    svc.finish_repair(r1["id"])
    r2 = svc.create_repair(silk["id"], "客户要求加衬固定，再次修补",
                           role="washer")
    svc.finish_repair(r2["id"])
    # 禁洗布料留在队列中做除尘挂晾，并制造一条洗护超时提醒
    conn.execute(
        "UPDATE wash_tasks SET due_at=? WHERE panel_id=? AND status='queued'",
        (_iso(datetime.now() - timedelta(hours=3)), silk["id"]))
    conn.commit()

    # ---------- 订单三：已送达，复装时发现串包 ----------
    o3 = svc.create_order(
        {"name": "周慕白", "phone": "137-0000-3072",
         "address_code": "青石板巷12号",
         "address": "（虚构）老城青石板巷12号院"},
        [{"name": "客餐厅", "windows": [{"code": "LR-01", "panels": [
            {"fabric": "亚麻原色", "hook_count": 14},
        ]}]},
         {"name": "茶室", "windows": [{"code": "TR-01", "panels": [
             {"fabric": "竹节棉", "hook_count": 7},
         ]}]}],
        appoint_at=_iso(datetime.now() - timedelta(days=1)))
    svc.start_dismantle(o3["id"])
    tree3 = svc.order_tree(o3["id"])
    wins3 = [(w, [p for p in w["panels"]])
             for r in tree3["rooms"] for w in r["windows"]]
    b3 = [svc.seal_bag(w["id"], [p["id"] for p in ps])["id"]
          for w, ps in wins3]
    for bid in b3:
        svc.ship_bag(bid)
        svc.receive_bag(bid)
    for t in svc.wash_queue():
        if t["order_code"] == o3["code"]:
            svc.start_wash(t["panel_id"])
    for t in svc.wash_queue():
        if t["order_code"] == o3["code"]:
            svc.finish_dry(t["panel_id"])
    pkgs3 = [svc.pack_package(w["id"], [p["id"] for p in ps])
             for w, ps in wins3]
    bat3 = svc.create_batch([p["id"] for p in pkgs3])
    svc.dispatch_batch(bat3["id"])
    svc.deliver_batch(bat3["id"])
    # 串包样例：把茶室帘片误扫进客餐厅配送包 → 拦截并留痕
    wrong_panel = wins3[1][1][0]
    try:
        svc.scan_reinstall(wrong_panel["id"], pkgs3[0]["id"])
    except DomainError:
        pass

    # ---------- 订单四：客户延期 ----------
    o4 = svc.create_order(
        {"name": "何晚晴", "phone": "136-0000-4096",
         "address_code": "柳岸居9号301",
         "address": "（虚构）滨河路柳岸居9号301室"},
        [{"name": "卧室", "windows": [{"code": "BR-01", "panels": [
            {"fabric": "天鹅绒", "hook_count": 10},
        ]}]}],
        appoint_at=_iso(datetime.now() + timedelta(hours=12)))
    svc.schedule_order(o4["id"], _iso(datetime.now() + timedelta(hours=12)))
    svc.postpone_order(o4["id"], _iso(datetime.now() + timedelta(hours=20)),
                       "客户临时出差，要求改期", role="cs")

    # ---------- 订单五：全流程完成，已验收关单并收款（费用复核样例） ----------
    o5 = svc.create_order(
        {"name": "顾南星", "phone": "135-0000-5120",
         "address_code": "竹影弄4号102",
         "address": "（虚构）翠竹路竹影弄4号102室"},
        [{"name": "客厅", "windows": [{"code": "LR-01", "panels": [
            {"fabric": "雪尼尔提花", "hook_count": 12},
        ]}]}],
        appoint_at=_iso(datetime.now() - timedelta(days=3)))
    svc.start_dismantle(o5["id"])
    tree5 = svc.order_tree(o5["id"])
    win5 = tree5["rooms"][0]["windows"][0]
    bag5 = svc.seal_bag(win5["id"], [p["id"] for p in win5["panels"]])
    svc.ship_bag(bag5["id"])
    svc.receive_bag(bag5["id"])
    t5 = next(t for t in svc.wash_queue() if t["order_code"] == o5["code"])
    svc.start_wash(t5["panel_id"])
    svc.finish_dry(t5["panel_id"])
    pkg5 = svc.pack_package(win5["id"], [p["id"] for p in win5["panels"]])
    b5b = svc.create_batch([pkg5["id"]])
    svc.dispatch_batch(b5b["id"])
    svc.deliver_batch(b5b["id"])
    for p in win5["panels"]:
        svc.scan_reinstall(p["id"], pkg5["id"])
        svc.accept_reinstall(p["id"], "ok", note="平整，挂钩齐全")
    svc.close_order(o5["id"], role="cs")
    svc.pay_order(o5["id"], role="finance")

    # 触发一次提醒（超时洗护 + 临近预约）
    worker = ReminderWorker(svc)
    fired = worker.tick()

    summary = {
        "orders": svc.list_orders(),
        "reinstall_order_id": o1["id"],
        "care_order_id": o2["id"],
        "misbag_order_id": o3["id"],
        "postpone_order_id": o4["id"],
        "closed_order_id": o5["id"],
        "reminders_fired": fired,
        "shipments": delivery.shipments,
        "payments": payment.txns,
    }
    return svc, summary


def print_summary(svc, summary):
    print("=" * 64)
    print("窗帘拆洗护与复装核验系统 · 样例数据已载入（客户信息均为虚构）")
    print("=" * 64)
    for o in summary["orders"]:
        print(f"  {o['code']}  {o['status']:<12} {o['customer_name']}"
              f"  {o['address_code']}  帘片 {o['panel_count']} 片")
    print("-" * 64)
    print(f"  复装对照演示单：{summary['reinstall_order_id']}  "
          f"（送达待验收，含挂钩缺失赔付）")
    print(f"  洗护队列演示单：{summary['care_order_id']}  "
          f"（缩水赔付 / 禁洗留痕 / 返工重洗 / 二次修补）")
    print(f"  串包留痕演示单：{summary['misbag_order_id']}")
    print(f"  客户延期演示单：{summary['postpone_order_id']}")
    print(f"  关单收款演示单：{summary['closed_order_id']}")
    print(f"  进程内提醒触发：{len(summary['reminders_fired'])} 条")
    print("=" * 64)


if __name__ == "__main__":
    svc, summary = build_demo()
    print_summary(svc, summary)
