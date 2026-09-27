"""种子数据：虚构客户与三类典型异常样例（串包、缩水、二次返工）。

全部通过 Service 业务接口写入，保证链路状态合法；
照片为本地生成的 SVG 占位图，存放于 photos/ 目录。
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from .services import Service, ROLE_STAFF, ROLE_WASH, ROLE_CS

PHOTO_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "photos")

# 亚麻白 / 布艺蓝 / 暖灰 三色占位照片
_STAGE_STYLE = {
    "before": ("#f6f2ea", "#4a6b8a", "拆前"),
    "after_wash": ("#eef3f7", "#3d5a76", "洗后"),
    "reinstall": ("#f1efe9", "#6b6f72", "复装后"),
}


def make_photo(piece_code: str, kind: str) -> str:
    """生成一张 SVG 占位照片，返回 /photos/ 相对路径。"""
    os.makedirs(PHOTO_DIR, exist_ok=True)
    bg, fg, label = _STAGE_STYLE[kind]
    name = f"{piece_code}-{kind}.svg"
    path = os.path.join(PHOTO_DIR, name)
    folds = "".join(
        f'<path d="M{40+i*46} 30 q 10 90 -6 180" stroke="{fg}" stroke-width="7" '
        f'fill="none" opacity="{0.35 + 0.08*(i % 3):.2f}" stroke-linecap="round"/>'
        for i in range(7))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="360" height="260">'
           f'<rect width="360" height="260" fill="{bg}"/>'
           f'<rect x="0" y="0" width="360" height="26" fill="{fg}" opacity="0.85"/>'
           f'{folds}'
           f'<rect x="0" y="226" width="360" height="34" fill="{fg}" opacity="0.12"/>'
           f'<text x="14" y="18" font-family="monospace" font-size="13" fill="#ffffff">{piece_code}</text>'
           f'<text x="14" y="248" font-family="sans-serif" font-size="15" fill="{fg}">{label}照片</text>'
           f'</svg>')
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    return f"/photos/{name}"


def _backdate(db, hours: float, where_sql: str, args: tuple = ()):
    """把部分事件时间拨到过去，用于演示时限提醒。"""
    past = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with db.tx() as con:
        con.execute(f"UPDATE piece_events SET at=? WHERE {where_sql}", (past, *args))


def seed(svc: Service) -> None:
    db = svc.db
    if db.one("SELECT id FROM orders LIMIT 1"):
        return  # 已有数据，不重复播种

    # ============ 订单一：复装验收中（串包 + 缩水 + 二次返工 + 挂钩缺失） ============
    o1 = svc.create_order("林晚晴", "13800001234", "湖畔雅居 3-2-501",
                          "湖畔雅居小区 3 栋 2 单元 501 室",
                          appointment_at=(datetime.now(timezone.utc) + timedelta(hours=20))
                          .isoformat(timespec="seconds"),
                          note="客户要求主卧遮光帘低温洗护")
    r1 = svc.create_room(o1, "主卧")
    r2 = svc.create_room(o1, "客厅")
    w1 = svc.add_window(r1, "主卧东窗")
    w2 = svc.add_window(r2, "客厅落地窗")

    p1 = svc.add_piece(w1, "高精密遮光布", 12, wash_req="低温轻柔，不可漂白")
    p2 = svc.add_piece(w1, "雪尼尔纱帘", 10, wash_req="中性洗剂")
    p3 = svc.add_piece(w2, "棉麻混纺", 14, wash_req="常规洗护")
    p4 = svc.add_piece(w2, "棉麻混纺", 14, wash_req="常规洗护")
    p5 = svc.add_piece(w2, "提花绒布", 16, wash_req="低温烘干")

    svc.start_removal(o1)
    for pid in (p1, p2, p3, p4, p5):
        svc.attach_photo(pid, "before", make_photo(f"W{svc._piece(pid)['window_id']:04d}-{pid:02d}", "before"))
    svc.count_hooks(p1, 12)
    svc.count_hooks(p2, 10)
    svc.count_hooks(p3, 13)   # 少 1 个 → 挂钩缺失留痕
    svc.count_hooks(p4, 14)
    svc.count_hooks(p5, 16)
    svc.seal_window_bag(w1)
    svc.seal_window_bag(w2)
    svc.transport_order(o1)
    svc.receive_order(o1, actor=ROLE_WASH)

    # 串包样例：p2 的纱帘被错装进客厅袋（SEAL-B-0002）
    svc.flag_misbag(p2, "SEAL-B-0002", "到厂拆袋发现纱帘混入客厅落地窗袋，已隔离归队", actor=ROLE_WASH)

    for pid in (p1, p2, p3, p4, p5):
        svc.start_wash(pid, actor=ROLE_WASH)
        svc.finish_wash(pid, actor=ROLE_WASH)

    # 缩水样例：p3 洗后缩水
    svc.report_incident(p3, "shrinkage", "洗后幅宽缩 4cm，已按低温程序复烘定型", actor=ROLE_WASH)

    for pid in (p1, p2, p3, p4, p5):
        svc.start_drying(pid, actor=ROLE_WASH)
        svc.finish_drying(pid, actor=ROLE_WASH,
                          photo_path=make_photo(f"W{svc._piece(pid)['window_id']:04d}-{pid:02d}", "after_wash"))

    # 二次返工样例：p5 提花绒布两次修补
    svc.open_repair(p5, "下摆脱线 8cm，第一轮缝合", actor=ROLE_WASH)
    svc.finish_repair(1, actor=ROLE_WASH)
    svc.open_repair(p5, "复查发现挂钩袢松动，第二轮加固", actor=ROLE_WASH)  # → second_repair 留痕
    svc.finish_repair(2, actor=ROLE_WASH)

    for pid in (p1, p2, p3, p4):
        svc.mark_ready(pid, actor=ROLE_WASH)
    svc.pack_window(w1, actor=ROLE_WASH)
    svc.pack_window(w2, actor=ROLE_WASH)
    svc.seal_package(1, actor=ROLE_WASH)
    svc.seal_package(2, actor=ROLE_WASH)

    from .adapters import DeliveryAdapter
    adapter = DeliveryAdapter()
    batch = svc.create_delivery(o1, [1, 2], adapter)
    svc.deliver_batch(1, adapter)

    # 复装：w1 两片完成，w2 完成两片、一片待装
    svc.scan_reinstall(p1, 12, photo_path=make_photo("W0001-01", "reinstall"))
    svc.scan_reinstall(p2, 10, photo_path=make_photo("W0001-02", "reinstall"))
    svc.scan_reinstall(p3, 13, photo_path=make_photo("W0002-03", "reinstall"))
    svc.scan_reinstall(p4, 14, photo_path=make_photo("W0002-04", "reinstall"))
    svc.accept_window(w1)  # 主卧已验收，客厅还差一片

    svc.add_fee(o1, "拆洗服务费（5 片）", 5 * 68.0, "charge")
    svc.add_fee(o1, "挂钩补配 ×1", 3.0, "charge")
    svc.add_fee(o1, "老客户折扣", -30.0, "discount")

    # ============ 订单二：洗护中（禁洗布料 + 染色 + 损伤待审批） ============
    o2 = svc.create_order("周明轩", "13900005678", "云溪别院 12-1-302",
                          "云溪别院 12 栋 1 单元 302 室",
                          appointment_at=(datetime.now(timezone.utc) + timedelta(days=2))
                          .isoformat(timespec="seconds"))
    r3 = svc.create_room(o2, "书房")
    w3 = svc.add_window(r3, "书房北窗")
    p6 = svc.add_piece(w3, "真丝绡", 8, washable=False, wash_req="禁水洗，仅除尘")
    p7 = svc.add_piece(w3, "涤纶遮光帘", 10, wash_req="常规洗护")

    svc.start_removal(o2)
    for pid in (p6, p7):
        svc.attach_photo(pid, "before", make_photo(f"W0003-{pid:02d}", "before"))
    svc.count_hooks(p6, 8)
    svc.count_hooks(p7, 10)
    svc.seal_window_bag(w3)
    svc.transport_order(o2)
    svc.receive_order(o2, actor=ROLE_WASH)

    # 禁洗样例：洗护员误操作被系统拦下并留痕，随后改走隔洗
    try:
        svc.start_wash(p6, actor=ROLE_WASH)
    except Exception:
        pass
    svc.skip_wash(p6, actor=ROLE_WASH)

    svc.start_wash(p7, actor=ROLE_WASH)
    # 染色样例 + 损伤登记（待客服主管审批赔付）；该片停留在洗护中以演示 SLA 提醒
    svc.report_incident(p7, "dye", "与深色织物同缸，边角轻微串色", actor=ROLE_WASH)
    svc.register_damage(p7, "右下角 3cm 串色痕迹，无法完全去除", actor=ROLE_WASH)

    svc.add_fee(o2, "拆洗服务费（2 片）", 2 * 68.0, "charge")

    # ============ 订单三：刚建单待拆卸（演示预约提醒） ============
    o3 = svc.create_order("苏蔓", "13700009012", "梧桐里 7-3-201",
                          "梧桐里公寓 7 栋 3 单元 201 室",
                          appointment_at=(datetime.now(timezone.utc) + timedelta(hours=5))
                          .isoformat(timespec="seconds"),
                          note="两只猫，上门请提前电话")
    r4 = svc.create_room(o3, "次卧")
    w4 = svc.add_window(r4, "次卧飘窗")
    svc.add_piece(w4, "亚麻纱", 9, wash_req="轻柔洗护")
    svc.add_piece(w4, "全遮光涂层布", 9, wash_req="不可高温")

    # ============ 订单四：已完成闭环（含赔付与收款） ============
    o4 = svc.create_order("韩志强", "13600003456", "汀兰苑 2-1-801",
                          "汀兰苑 2 栋 1 单元 801 室",
                          appointment_at=(datetime.now(timezone.utc) - timedelta(days=1))
                          .isoformat(timespec="seconds"))
    r5 = svc.create_room(o4, "主卧")
    w5 = svc.add_window(r5, "主卧南窗")
    p8 = svc.add_piece(w5, "绒面遮光帘", 12, wash_req="低温洗护")
    svc.start_removal(o4)
    svc.attach_photo(p8, "before", make_photo("W0005-08", "before"))
    svc.count_hooks(p8, 12)
    svc.seal_window_bag(w5)
    svc.transport_order(o4)
    svc.receive_order(o4, actor=ROLE_WASH)
    svc.start_wash(p8, actor=ROLE_WASH)
    svc.finish_wash(p8, actor=ROLE_WASH)
    svc.start_drying(p8, actor=ROLE_WASH)
    svc.finish_drying(p8, actor=ROLE_WASH, photo_path=make_photo("W0005-08", "after_wash"))
    svc.mark_ready(p8, actor=ROLE_WASH)
    svc.pack_window(w5, actor=ROLE_WASH)
    svc.seal_package(3, actor=ROLE_WASH)
    svc.create_delivery(o4, [3], DeliveryAdapter())
    svc.deliver_batch(2, DeliveryAdapter())
    svc.scan_reinstall(p8, 12, photo_path=make_photo("W0005-08", "reinstall"))
    svc.accept_window(w5)
    svc.add_fee(o4, "拆洗服务费（1 片）", 68.0, "charge")
    did = svc.register_damage(p8, "边缘轻微起球", actor=ROLE_WASH)
    svc.decide_damage(did, "compensate", actor=ROLE_CS, amount=50.0, note="客户认可")
    svc.review_fees(o4, actor=ROLE_CS)
    from .adapters import PaymentAdapter
    svc.pay(o4, "微信支付", PaymentAdapter(), actor=ROLE_CS)
    svc.close_order(o4, actor=ROLE_CS)

    # 让订单二的洗护停留时间超过 SLA，触发进程内提醒演示
    _backdate(db, 3.0, "piece_id IN (SELECT id FROM pieces WHERE window_id=?)", (w3,))
