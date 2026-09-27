"""核心规则测试：编号唯一、配包完整、状态顺序、重复验收/并发扫描、赔付权限。"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.adapters import DeliveryAdapter, PaymentAdapter
from app.db import Database
from app.services import (AuthError, DomainError, Service,
                          ROLE_STAFF, ROLE_WASH, ROLE_CS)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Database(os.path.join(self.tmp.name, "t.db"))
        self.svc = Service(self.db)
        self.delivery = DeliveryAdapter()
        self.payment = PaymentAdapter()

    def tearDown(self):
        self.tmp.cleanup()

    # 快速造一个「两片帘、已到厂」的订单
    def make_order_at_plant(self, hooks=(10, 12)):
        s = self.svc
        oid = s.create_order("测试客户", "13000000000", "测试住址代号 A-1", "测试路 1 号")
        rid = s.create_room(oid, "主卧")
        wid = s.add_window(rid, "东窗")
        pids = [s.add_piece(wid, "棉麻", h) for h in hooks]
        s.start_removal(oid)
        for pid in pids:
            s.attach_photo(pid, "before", "/photos/x.svg")
            s.count_hooks(pid, hooks[pids.index(pid)])
        s.seal_window_bag(wid)
        s.transport_order(oid)
        s.receive_order(oid, actor=ROLE_WASH)
        return oid, wid, pids

    def wash_all(self, pids):
        for pid in pids:
            self.svc.start_wash(pid, actor=ROLE_WASH)
            self.svc.finish_wash(pid, actor=ROLE_WASH)
            self.svc.start_drying(pid, actor=ROLE_WASH)
            self.svc.finish_drying(pid, actor=ROLE_WASH)
            self.svc.mark_ready(pid, actor=ROLE_WASH)


class TestUniqueCodes(Base):
    """测试编号唯一：帘片编号、封签号、批次号。"""

    def test_piece_code_unique(self):
        oid = self.svc.create_order("甲", "1", "代号A", "地址")
        rid = self.svc.create_room(oid, "房")
        wid = self.svc.add_window(rid, "窗")
        self.svc.add_piece(wid, "棉", 8, code="W0001-01")
        with self.assertRaises(DomainError):
            self.svc.add_piece(wid, "棉", 8, code="W0001-01")

    def test_seal_and_batch_unique(self):
        o1, w1, p1 = self.make_order_at_plant()
        o2, w2, p2 = self.make_order_at_plant()
        seals = [r["seal_no"] for r in self.db.q("SELECT seal_no FROM bags")]
        self.assertEqual(len(seals), len(set(seals)), "袋封签号必须唯一")
        self.wash_all(p1)
        self.wash_all(p2)
        self.svc.pack_window(w1, actor=ROLE_WASH)
        self.svc.pack_window(w2, actor=ROLE_WASH)
        pkg_seals = [r["seal_no"] for r in self.db.q("SELECT seal_no FROM packages")]
        self.assertEqual(len(pkg_seals), len(set(pkg_seals)), "包封签号必须唯一")
        self.assertNotEqual(set(seals) & set(pkg_seals), set(seals), "袋/包号段应区分")
        self.svc.seal_package(1, actor=ROLE_WASH)
        self.svc.seal_package(2, actor=ROLE_WASH)
        self.svc.create_delivery(o1, [1], self.delivery)
        self.svc.create_delivery(o2, [2], self.delivery)
        batches = [r["batch_no"] for r in self.db.q("SELECT batch_no FROM deliveries")]
        self.assertEqual(len(batches), len(set(batches)), "配送批次号必须唯一")


class TestPackIntegrity(Base):
    """配包完整：必须集齐窗位全部帘片；同一帘片不能归入两个配送包。"""

    def test_pack_requires_all_pieces_ready(self):
        oid, wid, pids = self.make_order_at_plant()
        # 只洗一片，另一片还在待洗
        self.svc.start_wash(pids[0], actor=ROLE_WASH)
        self.svc.finish_wash(pids[0], actor=ROLE_WASH)
        self.svc.start_drying(pids[0], actor=ROLE_WASH)
        self.svc.finish_drying(pids[0], actor=ROLE_WASH)
        self.svc.mark_ready(pids[0], actor=ROLE_WASH)
        with self.assertRaises(DomainError) as cm:
            self.svc.pack_window(wid, actor=ROLE_WASH)
        self.assertIn("配包不完整", str(cm.exception))

    def test_piece_cannot_join_two_packages(self):
        oid, wid, pids = self.make_order_at_plant()
        self.wash_all(pids)
        self.svc.pack_window(wid, actor=ROLE_WASH)
        # 同一窗位重复配包 → 拒绝（帘片已归入配送包）
        with self.assertRaises(DomainError):
            self.svc.pack_window(wid, actor=ROLE_WASH)
        # 结构保证：package_id 是帘片上的单列外键，一片帘只能指向一个包
        row = self.db.one("SELECT package_id FROM pieces WHERE id=?", (pids[0],))
        self.assertEqual(row["package_id"], 1)
        cols = [c[1] for c in self.db.q("PRAGMA table_info(pieces)")]
        self.assertEqual(cols.count("package_id"), 1, "帘片只有一个 package_id 列，无法归入两包")

    def test_bag_unique_per_piece(self):
        oid, wid, pids = self.make_order_at_plant()
        # 同一窗位重复封袋 → 拒绝
        with self.assertRaises(DomainError):
            self.svc.seal_window_bag(wid)
        # 结构保证：bag_id 单列，一片帘只能在一个袋中
        cols = [c[1] for c in self.db.q("PRAGMA table_info(pieces)")]
        self.assertEqual(cols.count("bag_id"), 1)


class TestStateOrder(Base):
    """状态顺序：非法跳转被拒绝。"""

    def test_piece_flow_order(self):
        oid, wid, pids = self.make_order_at_plant()
        # 未洗直接烘干
        with self.assertRaises(DomainError):
            self.svc.start_drying(pids[0], actor=ROLE_WASH)
        # 未复装直接验收窗位
        with self.assertRaises(DomainError):
            self.svc.accept_window(wid)
        # 未拆卸清点不能封袋
        oid2 = self.svc.create_order("乙", "2", "代号B", "地址")
        r2 = self.svc.create_room(oid2, "房")
        w2 = self.svc.add_window(r2, "窗")
        self.svc.add_piece(w2, "棉", 8)
        with self.assertRaises(DomainError):
            self.svc.seal_window_bag(w2)

    def test_banned_wash_blocked(self):
        s = self.svc
        oid = s.create_order("丙", "3", "代号C", "地址")
        rid = s.create_room(oid, "书房")
        wid = s.add_window(rid, "北窗")
        pid = s.add_piece(wid, "真丝", 8, washable=False)
        s.start_removal(oid)
        s.attach_photo(pid, "before", "/photos/x.svg")
        s.count_hooks(pid, 8)
        s.seal_window_bag(wid)
        s.transport_order(oid)
        s.receive_order(oid, actor=ROLE_WASH)
        with self.assertRaises(DomainError) as cm:
            s.start_wash(pid, actor=ROLE_WASH)
        self.assertIn("禁洗", str(cm.exception))
        # 留痕存在
        rows = self.db.q("SELECT * FROM incidents WHERE type='banned_wash'")
        self.assertEqual(len(rows), 1)
        # 隔洗通道可用
        s.skip_wash(pid, actor=ROLE_WASH)
        self.assertEqual(self.svc._piece(pid)["status"], "ready")

    def test_close_requires_all_windows_accepted(self):
        oid, wid, pids = self.make_order_at_plant()
        self.wash_all(pids)
        self.svc.pack_window(wid, actor=ROLE_WASH)
        self.svc.seal_package(1, actor=ROLE_WASH)
        self.svc.create_delivery(oid, [1], self.delivery)
        self.svc.deliver_batch(1, self.delivery)
        # 未验收 → 不可关闭
        with self.assertRaises(DomainError):
            self.svc.close_order(oid, actor=ROLE_CS)
        for pid in pids:
            self.svc.scan_reinstall(pid, 10)
        self.svc.accept_window(wid)
        # 未复核费用 → 仍不可关闭
        with self.assertRaises(DomainError):
            self.svc.close_order(oid, actor=ROLE_CS)
        self.svc.add_fee(oid, "服务费", 100.0)
        self.svc.review_fees(oid, actor=ROLE_CS)
        self.svc.close_order(oid, actor=ROLE_CS)
        self.assertEqual(self.db.one("SELECT status FROM orders WHERE id=?", (oid,))["status"],
                         "completed")


class TestReinstallAndAccept(Base):
    """重复验收与并发复装扫描只成功一次。"""

    def _make_reinstalling(self):
        oid, wid, pids = self.make_order_at_plant()
        self.wash_all(pids)
        self.svc.pack_window(wid, actor=ROLE_WASH)
        self.svc.seal_package(1, actor=ROLE_WASH)
        self.svc.create_delivery(oid, [1], self.delivery)
        self.svc.deliver_batch(1, self.delivery)
        return oid, wid, pids

    def test_duplicate_scan_rejected(self):
        oid, wid, pids = self._make_reinstalling()
        self.svc.scan_reinstall(pids[0], 10)
        with self.assertRaises(DomainError) as cm:
            self.svc.scan_reinstall(pids[0], 10)
        self.assertIn("重复扫描", str(cm.exception))

    def test_concurrent_scan_only_one_wins(self):
        oid, wid, pids = self._make_reinstalling()
        results = []

        def scan():
            try:
                self.svc.scan_reinstall(pids[0], 10)
                results.append("ok")
            except DomainError:
                results.append("conflict")

        threads = [threading.Thread(target=scan) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count("ok"), 1, "并发复装扫描只能成功一次")
        self.assertEqual(results.count("conflict"), 5)

    def test_double_accept_rejected(self):
        oid, wid, pids = self._make_reinstalling()
        for pid in pids:
            self.svc.scan_reinstall(pid, 10)
        self.svc.accept_window(wid)
        with self.assertRaises(DomainError) as cm:
            self.svc.accept_window(wid)
        self.assertIn("重复验收", str(cm.exception))

    def test_uneven_blocks_acceptance(self):
        oid, wid, pids = self._make_reinstalling()
        for pid in pids:
            self.svc.scan_reinstall(pid, 10)
        self.svc.flag_uneven(pids[0], "左高右低约 2cm")
        with self.assertRaises(DomainError) as cm:
            self.svc.accept_window(wid)
        self.assertIn("不平整", str(cm.exception))
        # 调整复装并销项后可验收
        self.svc.scan_reinstall(pids[0], 10)
        iid = self.db.one("SELECT id FROM incidents WHERE type='uneven'")["id"]
        self.svc.resolve_incident(iid, "已调平", actor=ROLE_CS)
        self.svc.accept_window(wid)


class TestDamagePermission(Base):
    """赔付权限：洗护人员可登记损伤，赔付/返工只能由客服主管批准。"""

    def test_washer_can_register_but_not_approve(self):
        oid, wid, pids = self.make_order_at_plant()
        self.svc.start_wash(pids[0], actor=ROLE_WASH)
        did = self.svc.register_damage(pids[0], "勾丝 2cm", actor=ROLE_WASH)
        # 洗护人员无权批准
        with self.assertRaises(AuthError):
            self.svc.decide_damage(did, "compensate", actor=ROLE_WASH, amount=80.0)
        # 上门人员也无权
        with self.assertRaises(AuthError):
            self.svc.decide_damage(did, "rework", actor=ROLE_STAFF)
        # 客服主管批准赔付 → 生成负费用
        self.svc.decide_damage(did, "compensate", actor=ROLE_CS, amount=80.0, note="确认")
        fee = self.db.one("SELECT * FROM fees WHERE order_id=? AND kind='compensation'", (oid,))
        self.assertEqual(fee["amount"], -80.0)
        # 不可重复批准
        with self.assertRaises(DomainError):
            self.svc.decide_damage(did, "compensate", actor=ROLE_CS, amount=80.0)

    def test_rework_decision_opens_repair_and_second_repair_logged(self):
        oid, wid, pids = self.make_order_at_plant()
        self.svc.start_wash(pids[0], actor=ROLE_WASH)
        self.svc.finish_wash(pids[0], actor=ROLE_WASH)
        did = self.svc.register_damage(pids[0], "掉扣 1 枚", actor=ROLE_WASH)
        self.svc.decide_damage(did, "rework", actor=ROLE_CS, note="返工补扣")
        self.assertEqual(self.svc._piece(pids[0])["status"], "in_repair")
        self.svc.finish_repair(1, actor=ROLE_WASH)
        # 第二次修补 → 二次修补留痕
        self.svc.open_repair(pids[0], "再次加固", actor=ROLE_WASH)
        rows = self.db.q("SELECT * FROM incidents WHERE type='second_repair'")
        self.assertEqual(len(rows), 1)

    def test_payment_requires_review_and_adapter(self):
        oid, wid, pids = self.make_order_at_plant()
        self.svc.add_fee(oid, "服务费", 136.0)
        with self.assertRaises(DomainError):
            self.svc.pay(oid, "微信支付", self.payment, actor=ROLE_CS)
        self.svc.review_fees(oid, actor=ROLE_CS)
        self.payment.fail_next = "模拟：余额不足"
        with self.assertRaises(DomainError):
            self.svc.pay(oid, "微信支付", self.payment, actor=ROLE_CS)
        r = self.svc.pay(oid, "微信支付", self.payment, actor=ROLE_CS)
        self.assertEqual(r["status"], "success")


class TestMisbagAndDelay(Base):
    def test_misbag_logged_and_delay_postpones(self):
        oid, wid, pids = self.make_order_at_plant()
        self.svc.flag_misbag(pids[0], "SEAL-B-9999", "错装他袋", actor=ROLE_WASH)
        rows = self.db.q("SELECT * FROM incidents WHERE type='misbag'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["resolved"], 1)
        # 客户延期
        self.svc.set_appointment(oid, "2026-09-28T09:00:00+00:00", actor=ROLE_CS)
        self.svc.postpone_order(oid, 2, "客户出差", actor=ROLE_CS)
        o = self.db.one("SELECT * FROM orders WHERE id=?", (oid,))
        self.assertEqual(o["postponed"], 1)
        self.assertIn("09-30", o["appointment_at"])
        self.assertEqual(len(self.db.q("SELECT * FROM incidents WHERE type='delay'")), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
