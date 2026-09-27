"""规则测试：编号唯一 / 配包完整 / 状态顺序 / 重复验收 / 赔付权限。

运行：python3 -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from curtainflow import db
from curtainflow.adapters import DeliveryAdapter, PaymentAdapter
from curtainflow.service import CurtainService, DomainError


def make_service():
    tmp = tempfile.mkdtemp(prefix="curtain-test-")
    conn = db.connect(os.path.join(tmp, "t.db"))
    svc = CurtainService(conn, os.path.join(tmp, "photos"),
                         delivery=DeliveryAdapter(), payment=PaymentAdapter())
    return svc


def make_order(svc, panels_per_window=((2,), (1,))):
    """建一个两窗位订单并走完全部拆卸/运输/洗护流程，返回 (order, tree)。"""
    rooms = []
    for wi, counts in enumerate(panels_per_window, 1):
        rooms.append({"name": f"房间{wi}", "windows": [
            {"code": f"W{wi:02d}", "panels": [
                {"fabric": "棉麻", "hook_count": 8} for _ in counts]}]})
    order = svc.create_order(
        {"name": "测试客户", "phone": "130-0000-0000",
         "address_code": "测试地址代号A1", "address": "（虚构）测试路1号"},
        rooms)
    svc.start_dismantle(order["id"])
    tree = svc.order_tree(order["id"])
    for room in tree["rooms"]:
        for win in room["windows"]:
            bag = svc.seal_bag(win["id"], [p["id"] for p in win["panels"]])
            svc.ship_bag(bag["id"])
            svc.receive_bag(bag["id"])
    for t in svc.wash_queue():
        svc.start_wash(t["panel_id"])
    for t in svc.wash_queue():
        svc.finish_dry(t["panel_id"])
    return svc.get_order(order["id"]), svc.order_tree(order["id"])


def pack_and_deliver(svc, tree):
    """全部窗位配包并送达，返回 {window_id: package}。"""
    pkgs = {}
    for room in tree["rooms"]:
        for win in room["windows"]:
            pkgs[win["id"]] = svc.pack_package(
                win["id"], [p["id"] for p in win["panels"]])
    batch = svc.create_batch([p["id"] for p in pkgs.values()])
    svc.dispatch_batch(batch["id"])
    svc.deliver_batch(batch["id"])
    return pkgs


class TestUniqueCodes(unittest.TestCase):
    """测试编号唯一：帘片编号全库唯一，重复编号被拒绝。"""

    def test_panel_codes_unique_across_orders(self):
        svc = make_service()
        svc.create_order(
            {"name": "甲", "phone": "1", "address_code": "A", "address": "x"},
            [{"name": "客厅", "windows": [{"code": "KT-01", "panels": [
                {"fabric": "棉", "hook_count": 6}]}]}])
        svc.create_order(
            {"name": "乙", "phone": "2", "address_code": "B", "address": "y"},
            [{"name": "客厅", "windows": [{"code": "KT-01", "panels": [
                {"fabric": "棉", "hook_count": 6}]}]}])
        codes = [r["code"] for r in svc.conn.execute("SELECT code FROM panels")]
        self.assertEqual(len(codes), len(set(codes)), "帘片编号必须全库唯一")

    def test_duplicate_code_rejected_by_db(self):
        svc = make_service()
        svc.create_order(
            {"name": "甲", "phone": "1", "address_code": "A", "address": "x"},
            [{"name": "客厅", "windows": [{"code": "KT-01", "panels": [
                {"fabric": "棉", "hook_count": 6}]}]}])
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            svc.conn.execute(
                "INSERT INTO panels(window_id,code,fabric,hook_count,created_at)"
                " VALUES(1,'ORD-0001-KT-01-P1','棉',6,'now')")

    def test_seal_numbers_unique(self):
        svc = make_service()
        order, tree = make_order(svc)
        seals = [r["seal_no"] for r in svc.conn.execute("SELECT seal_no FROM bags")]
        self.assertEqual(len(seals), len(set(seals)))


class TestPackaging(unittest.TestCase):
    """配包完整：同一帘片不能归入两个配送包；串包被拦截并留痕。"""

    def test_panel_cannot_join_two_packages(self):
        svc = make_service()
        order, tree = make_order(svc)
        win = tree["rooms"][0]["windows"][0]
        svc.pack_package(win["id"], [p["id"] for p in win["panels"]])
        with self.assertRaises(DomainError) as ctx:
            svc.pack_package(win["id"], [p["id"] for p in win["panels"]])
        self.assertIn("不能归入两个配送包", str(ctx.exception))

    def test_cross_window_panel_rejected(self):
        svc = make_service()
        order, tree = make_order(svc)
        w1 = tree["rooms"][0]["windows"][0]
        w2 = tree["rooms"][1]["windows"][0]
        foreign = w2["panels"][0]["id"]
        with self.assertRaises(DomainError) as ctx:
            svc.pack_package(w1["id"], [foreign])
        self.assertIn("禁止串包", str(ctx.exception))

    def test_misbag_scan_leaves_incident(self):
        svc = make_service()
        order, tree = make_order(svc)
        pkgs = pack_and_deliver(svc, tree)
        w1 = tree["rooms"][0]["windows"][0]
        w2 = tree["rooms"][1]["windows"][0]
        wrong_panel = w2["panels"][0]["id"]
        with self.assertRaises(DomainError):
            svc.scan_reinstall(wrong_panel, pkgs[w1["id"]]["id"])
        incidents = svc.list_incidents(order["id"])
        self.assertTrue(any(i["type"] == "misbag" for i in incidents),
                        "串包扫描必须留痕")

    def test_package_covers_all_window_panels(self):
        svc = make_service()
        order, tree = make_order(svc)
        pkgs = pack_and_deliver(svc, tree)
        for room in tree["rooms"]:
            for win in room["windows"]:
                pkg = pkgs[win["id"]]
                count = svc.conn.execute(
                    "SELECT COUNT(*) c FROM panels WHERE package_id=?",
                    (pkg["id"],)).fetchone()["c"]
                self.assertEqual(count, len(win["panels"]),
                                 "配送包必须完整覆盖本窗位全部帘片")


class TestStatusOrder(unittest.TestCase):
    """状态顺序：非法流转被拒绝。"""

    def test_cannot_wash_before_bagging(self):
        svc = make_service()
        order = svc.create_order(
            {"name": "甲", "phone": "1", "address_code": "A", "address": "x"},
            [{"name": "客厅", "windows": [{"code": "KT-01", "panels": [
                {"fabric": "棉", "hook_count": 6}]}]}])
        tree = svc.order_tree(order["id"])
        pid = tree["rooms"][0]["windows"][0]["panels"][0]["id"]
        with self.assertRaises(DomainError):
            svc.start_wash(pid)

    def test_cannot_pack_before_ready(self):
        svc = make_service()
        order = svc.create_order(
            {"name": "甲", "phone": "1", "address_code": "A", "address": "x"},
            [{"name": "客厅", "windows": [{"code": "KT-01", "panels": [
                {"fabric": "棉", "hook_count": 6}]}]}])
        svc.start_dismantle(order["id"])
        tree = svc.order_tree(order["id"])
        win = tree["rooms"][0]["windows"][0]
        with self.assertRaises(DomainError) as ctx:
            svc.pack_package(win["id"], [p["id"] for p in win["panels"]])
        self.assertIn("未就绪", str(ctx.exception))

    def test_cannot_accept_before_scan(self):
        svc = make_service()
        order, tree = make_order(svc)
        pkgs = pack_and_deliver(svc, tree)
        pid = tree["rooms"][0]["windows"][0]["panels"][0]["id"]
        with self.assertRaises(DomainError):
            svc.accept_reinstall(pid, "ok")

    def test_cannot_close_with_unaccepted_panels(self):
        svc = make_service()
        order, tree = make_order(svc)
        pkgs = pack_and_deliver(svc, tree)
        with self.assertRaises(DomainError) as ctx:
            svc.close_order(order["id"], role="cs")
        self.assertIn("未验收", str(ctx.exception))

    def test_wash_forbidden_panel_blocked(self):
        svc = make_service()
        order = svc.create_order(
            {"name": "甲", "phone": "1", "address_code": "A", "address": "x"},
            [{"name": "客厅", "windows": [{"code": "KT-01", "panels": [
                {"fabric": "真丝", "hook_count": 6, "wash_forbidden": True}]}]}])
        svc.start_dismantle(order["id"])
        tree = svc.order_tree(order["id"])
        win = tree["rooms"][0]["windows"][0]
        bag = svc.seal_bag(win["id"], [p["id"] for p in win["panels"]])
        svc.ship_bag(bag["id"])
        svc.receive_bag(bag["id"])
        pid = win["panels"][0]["id"]
        with self.assertRaises(DomainError) as ctx:
            svc.start_wash(pid)
        self.assertIn("禁止水洗", str(ctx.exception))
        # 禁洗留痕已自动生成
        types = [i["type"] for i in svc.list_incidents(order["id"])]
        self.assertIn("wash_forbidden", types)


class TestDuplicateAcceptance(unittest.TestCase):
    """重复验收：顺序与并发场景下都只成功一次。"""

    def _delivered_panel(self):
        svc = make_service()
        order, tree = make_order(svc)
        pkgs = pack_and_deliver(svc, tree)
        win = tree["rooms"][0]["windows"][0]
        pid = win["panels"][0]["id"]
        svc.scan_reinstall(pid, pkgs[win["id"]]["id"])
        return svc, order, pid

    def test_sequential_double_accept_rejected(self):
        svc, order, pid = self._delivered_panel()
        svc.accept_reinstall(pid, "ok")
        with self.assertRaises(DomainError) as ctx:
            svc.accept_reinstall(pid, "ok")
        self.assertEqual(ctx.exception.code, 409)

    def test_concurrent_accept_only_one_succeeds(self):
        svc, order, pid = self._delivered_panel()
        results, errors = [], []

        def worker():
            try:
                svc.accept_reinstall(pid, "ok")
                results.append(1)
            except DomainError:
                errors.append(1)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(results), 1, "并发验收只能成功一次")
        self.assertEqual(len(errors), 7)
        accepted = svc.conn.execute(
            "SELECT COUNT(*) c FROM reinstalls WHERE panel_id=? AND accepted=1",
            (pid,)).fetchone()["c"]
        self.assertEqual(accepted, 1)

    def test_concurrent_scan_creates_one_record(self):
        svc = make_service()
        order, tree = make_order(svc)
        pkgs = pack_and_deliver(svc, tree)
        win = tree["rooms"][0]["windows"][0]
        pid = win["panels"][0]["id"]
        pkg_id = pkgs[win["id"]]["id"]

        def worker():
            svc.scan_reinstall(pid, pkg_id)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        rows = svc.conn.execute(
            "SELECT COUNT(*) c FROM reinstalls WHERE panel_id=?", (pid,)
        ).fetchone()["c"]
        self.assertEqual(rows, 1, "并发复装扫描只成功一次（仅一条扫描记录）")

    def test_uneven_returns_to_delivered_and_can_rescan(self):
        svc, order, pid = self._delivered_panel()
        svc.accept_reinstall(pid, "uneven", note="右垂")
        panel = svc._panel(pid)
        self.assertEqual(panel["status"], "delivered")
        types = [i["type"] for i in svc.list_incidents(order["id"])]
        self.assertIn("uneven", types)
        # 可重新扫描并验收
        pkg_id = panel["package_id"]
        svc.scan_reinstall(pid, pkg_id)
        svc.accept_reinstall(pid, "ok")
        self.assertEqual(svc._panel(pid)["status"], "accepted")


class TestClaimPermission(unittest.TestCase):
    """赔付权限：仅客服主管可批准赔付与返工。"""

    def _damage_claim(self):
        svc = make_service()
        order, tree = make_order(svc)
        pid = tree["rooms"][0]["windows"][0]["panels"][0]["id"]
        svc.report_damage(pid, "shrink", "缩水2cm", role="washer")
        inc = svc.list_incidents(order["id"])[0]
        claim = svc.propose_claim(inc["id"], 50.0, "缩水补偿", role="cs")
        return svc, order, inc, claim

    def test_non_lead_cannot_decide_claim(self):
        svc, order, inc, claim = self._damage_claim()
        for role in ("crew", "washer", "cs", "finance"):
            with self.assertRaises(DomainError) as ctx:
                svc.decide_claim(claim["id"], True, role=role)
            self.assertEqual(ctx.exception.code, 403)

    def test_lead_can_approve_and_fee_recorded(self):
        svc, order, inc, claim = self._damage_claim()
        svc.decide_claim(claim["id"], True, role="cs_lead")
        review = svc.fee_review(order["id"])
        self.assertTrue(any(i["kind"] == "compensation" and i["amount"] == -50.0
                            for i in review["extra_items"]))
        self.assertEqual(svc.list_incidents(order["id"])[0]["status"], "resolved")

    def test_non_lead_cannot_approve_rework(self):
        svc = make_service()
        order, tree = make_order(svc)
        pid = tree["rooms"][0]["windows"][0]["panels"][0]["id"]
        svc.report_damage(pid, "dye", "串色", role="washer")
        inc = svc.list_incidents(order["id"])[0]
        with self.assertRaises(DomainError) as ctx:
            svc.approve_rework(inc["id"], role="cs")
        self.assertEqual(ctx.exception.code, 403)
        svc.approve_rework(inc["id"], role="cs_lead")
        self.assertEqual(svc._panel(pid)["status"], "at_shop")

    def test_second_repair_auto_logged(self):
        svc = make_service()
        order, tree = make_order(svc)
        pid = tree["rooms"][0]["windows"][0]["panels"][0]["id"]
        svc.create_repair(pid, "第一次", role="washer")
        svc.create_repair(pid, "第二次", role="washer")
        types = [i["type"] for i in svc.list_incidents(order["id"])]
        self.assertIn("second_repair", types)


class TestCloseAndFees(unittest.TestCase):
    """关单与费用复核的完整性。"""

    def test_full_flow_close_and_pay(self):
        svc = make_service()
        order, tree = make_order(svc)
        pkgs = pack_and_deliver(svc, tree)
        for room in tree["rooms"]:
            for win in room["windows"]:
                for p in win["panels"]:
                    svc.scan_reinstall(p["id"], pkgs[win["id"]]["id"])
                    svc.accept_reinstall(p["id"], "ok")
        closed = svc.close_order(order["id"], role="cs")
        self.assertEqual(closed["status"], "closed")
        review = svc.fee_review(order["id"])
        self.assertGreater(review["total"], 0)
        res = svc.pay_order(order["id"], role="finance")
        self.assertEqual(res["payment"]["status"], "success")
        self.assertEqual(res["review"]["balance"], 0)

    def test_postpone_logged(self):
        svc = make_service()
        order = svc.create_order(
            {"name": "甲", "phone": "1", "address_code": "A", "address": "x"},
            [{"name": "客厅", "windows": [{"code": "KT-01", "panels": [
                {"fabric": "棉", "hook_count": 6}]}]}])
        svc.postpone_order(order["id"], "2026-10-01T09:00:00", "客户出差",
                           role="cs")
        types = [i["type"] for i in svc.list_incidents(order["id"])]
        self.assertIn("postpone", types)


if __name__ == "__main__":
    unittest.main()
