"""领域服务：订单、拆卸、洗护、修补、配包配送、复装验收、费用复核的全部规则。

关键不变量（均有测试覆盖）：
- 帘片编号全库唯一（panels.code UNIQUE）
- 同一帘片不能归入两个配送包（panels.package_id 唯一占用 + 应用层校验）
- 状态只能沿定义好的顺序流转
- 复装验收并发只成功一次（reinstalls 部分唯一索引 + IMMEDIATE 事务）
- 赔付与返工批准仅客服主管（cs_lead）可操作
"""
import os
import sqlite3
import threading
from datetime import datetime, timedelta

from . import photos

# ---------------------------------------------------------------- 常量

ROLES = ("crew", "washer", "cs", "cs_lead", "finance")
ROLE_NAMES = {
    "crew": "拆装技师",
    "washer": "洗护员",
    "cs": "客服",
    "cs_lead": "客服主管",
    "finance": "财务",
}

# 帘片状态机
PANEL_FLOW = {
    "registered": ["bagged"],
    "bagged": ["in_transit"],
    "in_transit": ["at_shop"],
    "at_shop": ["washing"],
    "washing": ["drying"],
    "drying": ["ready"],
    "ready": ["packed"],
    "packed": ["delivering"],
    "delivering": ["delivered"],
    "delivered": ["reinstalled"],
    "reinstalled": ["accepted"],
    "accepted": [],
}

ORDER_FLOW = {
    "created": ["scheduled", "dismantling"],
    "scheduled": ["dismantling"],
    "dismantling": ["in_care"],
    "in_care": ["packing"],
    "packing": ["delivering"],
    "delivering": ["reinstalling"],
    "reinstalling": ["review"],
    "review": ["closed"],
    "closed": [],
}

# 异常类型（留痕）
INCIDENT_TYPES = {
    "misbag": "帘片串包",
    "shrink": "缩水",
    "dye": "染色",
    "hook_missing": "挂钩缺失",
    "wash_forbidden": "布料禁洗",
    "postpone": "客户延期",
    "second_repair": "二次修补",
    "uneven": "复装不平整",
}

# 洗护员可登记的损伤类型
DAMAGE_TYPES = ("shrink", "dye", "hook_missing", "uneven")

# 费用单价（虚构）
PRICE_PER_PANEL = 68.0
PRICE_PER_HOOK = 1.5
REPAIR_FEE = 30.0
REWORK_FEE = 20.0

# 提醒阈值
WASH_DUE_HOURS = 48          # 洗护任务时限
APPOINT_REMIND_HOURS = 24    # 上门预约提前提醒
UNACCEPTED_STALE_HOURS = 24  # 复装后未验收提醒


class DomainError(Exception):
    """业务规则冲突，携带 HTTP 状态码。"""

    def __init__(self, message, code=409):
        super().__init__(message)
        self.message = message
        self.code = code


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _row_to_dict(row):
    return {k: row[k] for k in row.keys()} if row is not None else None


class CurtainService:
    def __init__(self, conn, photo_dir, delivery=None, payment=None):
        self.conn = conn
        self.photo_dir = photo_dir
        self.delivery = delivery
        self.payment = payment
        self.lock = threading.RLock()
        os.makedirs(photo_dir, exist_ok=True)

    # ------------------------------------------------------------ 基础工具

    def _one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def _all(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def _require(self, row, what):
        if row is None:
            raise DomainError(f"{what}不存在", 404)
        return row

    def _require_role(self, role, allowed, action):
        if role not in allowed:
            names = "、".join(ROLE_NAMES[r] for r in allowed)
            raise DomainError(f"{action}需要角色：{names}", 403)

    def _advance_panel(self, panel, target):
        flow = PANEL_FLOW[panel["status"]]
        if target not in flow:
            raise DomainError(
                f"帘片 {panel['code']} 状态不能从 {panel['status']} 流转到 {target}")
        self.conn.execute(
            "UPDATE panels SET status=? WHERE id=?", (target, panel["id"]))

    def _panel(self, panel_id):
        return self._require(
            self._one("SELECT * FROM panels WHERE id=?", (panel_id,)), "帘片")

    def _panel_by_code(self, code):
        return self._require(
            self._one("SELECT * FROM panels WHERE code=?", (code,)), "帘片")

    def _order(self, order_id):
        return self._require(
            self._one("SELECT * FROM orders WHERE id=?", (order_id,)), "订单")

    def _set_order_status(self, order_id, target):
        order = self._order(order_id)
        if target not in ORDER_FLOW[order["status"]]:
            raise DomainError(
                f"订单状态不能从 {order['status']} 流转到 {target}")
        self.conn.execute(
            "UPDATE orders SET status=? WHERE id=?", (target, order_id))

    def _order_of_panel(self, panel):
        return self._one(
            """SELECT o.* FROM orders o
               JOIN rooms r ON r.order_id=o.id
               JOIN windows w ON w.room_id=r.id
               JOIN panels p ON p.window_id=w.id
               WHERE p.id=?""", (panel["id"],))

    def _log_incident(self, order_id, panel_id, itype, detail, by, status="open"):
        cur = self.conn.execute(
            """INSERT INTO incidents(order_id,panel_id,type,detail,status,created_by,created_at)
               VALUES(?,?,?,?,?,?,?)""",
            (order_id, panel_id, itype, detail, status, by, now_iso()))
        return cur.lastrowid

    # ------------------------------------------------------------ 订单与拆卸

    def create_order(self, customer, rooms, appoint_at=None, note=""):
        """customer: {name,phone,address_code,address}; rooms: [{name, windows:[{code, panels:[...]}]}]"""
        with self.lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO customers(name,phone,address_code,address) VALUES(?,?,?,?)",
                (customer["name"], customer["phone"],
                 customer["address_code"], customer["address"]))
            cid = cur.lastrowid
            seq = self._one("SELECT COUNT(*) c FROM orders")["c"] + 1
            code = f"ORD-{seq:04d}"
            ocur = self.conn.execute(
                "INSERT INTO orders(code,customer_id,status,appoint_at,created_at,note)"
                " VALUES(?,?,?,?,?,?)",
                (code, cid, "created", appoint_at, now_iso(), note))
            oid = ocur.lastrowid
            for room in rooms:
                rcur = self.conn.execute(
                    "INSERT INTO rooms(order_id,name) VALUES(?,?)", (oid, room["name"]))
                rid = rcur.lastrowid
                for win in room.get("windows", []):
                    wcur = self.conn.execute(
                        "INSERT INTO windows(room_id,code) VALUES(?,?)",
                        (rid, win["code"]))
                    wid = wcur.lastrowid
                    for i, panel in enumerate(win.get("panels", []), 1):
                        pcode = f"{code}-{win['code']}-P{i}"
                        self.conn.execute(
                            """INSERT INTO panels(window_id,code,fabric,hook_count,
                               care_req,wash_forbidden,status,created_at)
                               VALUES(?,?,?,?,?,?,'registered',?)""",
                            (wid, pcode, panel["fabric"], panel["hook_count"],
                             panel.get("care_req", ""),
                             1 if panel.get("wash_forbidden") else 0, now_iso()))
            return self.get_order(oid)

    def list_orders(self):
        rows = self._all(
            """SELECT o.*, c.name customer_name, c.address_code,
                      (SELECT COUNT(*) FROM panels p
                       JOIN windows w ON p.window_id=w.id
                       JOIN rooms r ON w.room_id=r.id
                       WHERE r.order_id=o.id) panel_count
               FROM orders o JOIN customers c ON c.id=o.customer_id
               ORDER BY o.id DESC""")
        return [_row_to_dict(r) for r in rows]

    def get_order(self, order_id):
        order = self._require(self._one(
            """SELECT o.*, c.name customer_name, c.phone, c.address_code, c.address
               FROM orders o JOIN customers c ON c.id=o.customer_id
               WHERE o.id=?""", (order_id,)), "订单")
        return _row_to_dict(order)

    def order_tree(self, order_id):
        """订单 → 房间 → 窗位 → 帘片 的树形清单。"""
        order = self.get_order(order_id)
        rooms = []
        for r in self._all("SELECT * FROM rooms WHERE order_id=? ORDER BY id",
                           (order_id,)):
            windows = []
            for w in self._all("SELECT * FROM windows WHERE room_id=? ORDER BY id",
                               (r["id"],)):
                panels = [_row_to_dict(p) for p in self._all(
                    "SELECT * FROM panels WHERE window_id=? ORDER BY id", (w["id"],))]
                for p in panels:
                    p["photo_before"] = photos.photo_path(
                        self.photo_dir, p["code"], "before")
                    p["photo_after"] = photos.photo_path(
                        self.photo_dir, p["code"], "after")
                accepted = self._window_accepted(w["id"])
                windows.append({**_row_to_dict(w), "panels": panels,
                                "accepted": accepted})
            rooms.append({**_row_to_dict(r), "windows": windows})
        order["rooms"] = rooms
        order["incidents"] = self.list_incidents(order_id)
        return order

    def _window_accepted(self, window_id):
        total = self._one(
            "SELECT COUNT(*) c FROM panels WHERE window_id=?", (window_id,))["c"]
        done = self._one(
            "SELECT COUNT(*) c FROM panels WHERE window_id=? AND status='accepted'",
            (window_id,))["c"]
        return total > 0 and total == done

    def schedule_order(self, order_id, appoint_at):
        with self.lock, self.conn:
            self.conn.execute(
                "UPDATE orders SET appoint_at=? WHERE id=?", (appoint_at, order_id))
            self._set_order_status(order_id, "scheduled")
            return self.get_order(order_id)

    def start_dismantle(self, order_id):
        with self.lock, self.conn:
            self._set_order_status(order_id, "dismantling")
            return self.get_order(order_id)

    def postpone_order(self, order_id, new_appoint_at, reason, role="cs"):
        """客户延期：留痕并把订单退回待预约。"""
        with self.lock, self.conn:
            self._require_role(role, ("cs", "cs_lead"), "登记客户延期")
            order = self._order(order_id)
            if order["status"] not in ("created", "scheduled", "dismantling"):
                raise DomainError("当前状态不可延期")
            self._log_incident(order_id, None, "postpone",
                               f"延期至 {new_appoint_at}：{reason}", role,
                               status="resolved")
            self.conn.execute(
                "UPDATE orders SET status='scheduled', appoint_at=? WHERE id=?",
                (new_appoint_at, order_id))
            return self.get_order(order_id)

    # ------------------------------------------------------------ 拆卸装袋

    def seal_bag(self, window_id, panel_ids, seal_no=None):
        """按窗位装袋封签：清点挂钩、生成封签号、帘片进入 bagged。"""
        with self.lock, self.conn:
            window = self._require(
                self._one("SELECT * FROM windows WHERE id=?", (window_id,)), "窗位")
            room = self._one("SELECT * FROM rooms WHERE id=?", (window["room_id"],))
            order = self._order(room["order_id"])
            if order["status"] == "scheduled":
                self._set_order_status(order["id"], "dismantling")
            panels = []
            for pid in panel_ids:
                p = self._panel(pid)
                if p["window_id"] != window_id:
                    raise DomainError(
                        f"帘片 {p['code']} 不属于该窗位，不能混入此袋")
                if p["status"] != "registered":
                    raise DomainError(f"帘片 {p['code']} 已装袋或已在流转中")
                panels.append(p)
            if not panels:
                raise DomainError("封袋至少需要一片帘布")
            if seal_no is None:
                seq = self._one("SELECT COUNT(*) c FROM bags")["c"] + 1
                seal_no = f"BAG-{order['code']}-{seq:03d}"
            cur = self.conn.execute(
                """INSERT INTO bags(order_id,window_id,seal_no,status,sealed_at)
                   VALUES(?,?,?,'sealed',?)""",
                (order["id"], window_id, seal_no, now_iso()))
            bag_id = cur.lastrowid
            for p in panels:
                self._advance_panel(p, "bagged")
                self.conn.execute(
                    "UPDATE panels SET bag_id=? WHERE id=?", (bag_id, p["id"]))
                photos.ensure_photo(self.photo_dir, p["code"], "before")
            return _row_to_dict(self._one("SELECT * FROM bags WHERE id=?", (bag_id,)))

    def list_bags(self, order_id):
        rows = self._all(
            """SELECT b.*, w.code window_code,
                      (SELECT COUNT(*) FROM panels WHERE bag_id=b.id) panel_count,
                      (SELECT GROUP_CONCAT(code) FROM panels WHERE bag_id=b.id) panel_codes
               FROM bags b JOIN windows w ON w.id=b.window_id
               WHERE b.order_id=? ORDER BY b.id""", (order_id,))
        return [_row_to_dict(r) for r in rows]

    def ship_bag(self, bag_id):
        with self.lock, self.conn:
            bag = self._require(self._one("SELECT * FROM bags WHERE id=?", (bag_id,)),
                                "运输袋")
            if bag["status"] != "sealed":
                raise DomainError("只有已封签的袋可以发出")
            self.conn.execute(
                "UPDATE bags SET status='in_transit', shipped_at=? WHERE id=?",
                (now_iso(), bag_id))
            for p in self._all("SELECT * FROM panels WHERE bag_id=?", (bag_id,)):
                self._advance_panel(p, "in_transit")
            return _row_to_dict(self._one("SELECT * FROM bags WHERE id=?", (bag_id,)))

    def receive_bag(self, bag_id):
        """到厂扫码：袋内帘片进入洗护队列（禁洗布料自动留痕）。"""
        with self.lock, self.conn:
            bag = self._require(self._one("SELECT * FROM bags WHERE id=?", (bag_id,)),
                                "运输袋")
            if bag["status"] != "in_transit":
                raise DomainError("运输袋不在运输途中")
            self.conn.execute(
                "UPDATE bags SET status='arrived', arrived_at=? WHERE id=?",
                (now_iso(), bag_id))
            due = (datetime.now() + timedelta(hours=WASH_DUE_HOURS)
                   ).isoformat(timespec="seconds")
            for p in self._all("SELECT * FROM panels WHERE bag_id=?", (bag_id,)):
                self._advance_panel(p, "at_shop")
                self.conn.execute(
                    """INSERT INTO wash_tasks(panel_id,status,queued_at,due_at)
                       VALUES(?,'queued',?,?)""", (p["id"], now_iso(), due))
                if p["wash_forbidden"]:
                    self._log_incident(
                        bag["order_id"], p["id"], "wash_forbidden",
                        f"{p['fabric']} 为禁洗布料，仅做除尘挂晾，禁止水洗", "system")
            order = self._order(bag["order_id"])
            if order["status"] == "dismantling":
                self._set_order_status(order["id"], "in_care")
            return _row_to_dict(self._one("SELECT * FROM bags WHERE id=?", (bag_id,)))

    # ------------------------------------------------------------ 洗护

    def wash_queue(self):
        rows = self._all(
            """SELECT t.*, p.code panel_code, p.fabric, p.hook_count, p.care_req,
                      p.wash_forbidden, p.status panel_status, o.code order_code
               FROM wash_tasks t
               JOIN panels p ON p.id=t.panel_id
               JOIN windows w ON w.id=p.window_id
               JOIN rooms r ON r.id=w.room_id
               JOIN orders o ON o.id=r.order_id
               WHERE t.status IN ('queued','washing','drying')
               ORDER BY t.due_at""")
        return [_row_to_dict(r) for r in rows]

    def _wash_task(self, panel_id, statuses):
        task = self._one(
            """SELECT * FROM wash_tasks WHERE panel_id=? AND status IN (%s)
               ORDER BY id DESC""" % ",".join("?" * len(statuses)),
            (panel_id, *statuses))
        return self._require(task, "洗护任务")

    def start_wash(self, panel_id, role="washer"):
        with self.lock, self.conn:
            self._require_role(role, ("washer",), "开始洗护")
            panel = self._panel(panel_id)
            if panel["wash_forbidden"]:
                raise DomainError(
                    f"帘片 {panel['code']} 的布料（{panel['fabric']}）禁止水洗")
            task = self._wash_task(panel_id, ("queued",))
            self._advance_panel(panel, "washing")
            self.conn.execute(
                "UPDATE wash_tasks SET status='washing', started_at=? WHERE id=?",
                (now_iso(), task["id"]))
            return _row_to_dict(self._one(
                "SELECT * FROM wash_tasks WHERE id=?", (task["id"],)))

    def finish_dry(self, panel_id, role="washer"):
        with self.lock, self.conn:
            self._require_role(role, ("washer",), "登记烘干")
            panel = self._panel(panel_id)
            task = self._wash_task(panel_id, ("queued", "washing"))
            # 禁洗布料走除尘挂晾：queued -> drying -> ready
            if panel["status"] == "at_shop":
                self._advance_panel(panel, "washing")
                self.conn.execute(
                    "UPDATE wash_tasks SET status='washing', started_at=? WHERE id=?",
                    (now_iso(), task["id"]))
            panel = self._panel(panel_id)
            self._advance_panel(panel, "drying")
            self._advance_panel(self._panel(panel_id), "ready")
            self.conn.execute(
                "UPDATE wash_tasks SET status='done', dried_at=? WHERE id=?",
                (now_iso(), task["id"]))
            self.conn.execute(
                "UPDATE panels SET wash_count=wash_count+1 WHERE id=?", (panel_id,))
            return _row_to_dict(self._panel(panel_id))

    def report_damage(self, panel_id, itype, detail, role="washer"):
        """洗护人员登记损伤（缩水/染色/挂钩缺失/复装不平整等），留痕待处理。"""
        with self.lock, self.conn:
            self._require_role(role, ("washer", "crew"), "登记损伤")
            if itype not in DAMAGE_TYPES:
                raise DomainError(f"不支持的损伤类型：{itype}")
            panel = self._panel(panel_id)
            order = self._order_of_panel(panel)
            iid = self._log_incident(order["id"], panel_id, itype, detail, role)
            return _row_to_dict(
                self._one("SELECT * FROM incidents WHERE id=?", (iid,)))

    # ------------------------------------------------------------ 修补与赔付

    def create_repair(self, panel_id, description, role="washer"):
        with self.lock, self.conn:
            self._require_role(role, ("washer", "cs", "cs_lead"), "登记修补")
            panel = self._panel(panel_id)
            order = self._order_of_panel(panel)
            rnd = self._one(
                "SELECT COALESCE(MAX(round),0)+1 r FROM repairs WHERE panel_id=?",
                (panel_id,))["r"]
            cur = self.conn.execute(
                """INSERT INTO repairs(panel_id,round,description,status,created_at)
                   VALUES(?,?,?,'open',?)""",
                (panel_id, rnd, description, now_iso()))
            if rnd >= 2:
                self._log_incident(order["id"], panel_id, "second_repair",
                                   f"第 {rnd} 次修补：{description}", role)
            self.conn.execute(
                """INSERT INTO fee_items(order_id,kind,label,amount,status,created_at)
                   VALUES(?,?,?,?,'pending',?)""",
                (order["id"], "repair",
                 f"帘片 {panel['code']} 第{rnd}次修补", REPAIR_FEE, now_iso()))
            return _row_to_dict(
                self._one("SELECT * FROM repairs WHERE id=?", (cur.lastrowid,)))

    def finish_repair(self, repair_id, role="washer"):
        with self.lock, self.conn:
            self._require_role(role, ("washer", "cs_lead"), "完成修补")
            repair = self._require(
                self._one("SELECT * FROM repairs WHERE id=?", (repair_id,)), "修补单")
            if repair["status"] != "open":
                raise DomainError("修补单已关闭")
            self.conn.execute(
                "UPDATE repairs SET status='done', done_at=? WHERE id=?",
                (now_iso(), repair_id))
            return _row_to_dict(
                self._one("SELECT * FROM repairs WHERE id=?", (repair_id,)))

    def list_repairs(self, order_id=None):
        sql = """SELECT rp.*, p.code panel_code, o.code order_code
                 FROM repairs rp
                 JOIN panels p ON p.id=rp.panel_id
                 JOIN windows w ON w.id=p.window_id
                 JOIN rooms r ON r.id=w.room_id
                 JOIN orders o ON o.id=r.order_id"""
        args = ()
        if order_id:
            sql += " WHERE o.id=?"
            args = (order_id,)
        rows = self._all(sql + " ORDER BY rp.id DESC", args)
        return [_row_to_dict(r) for r in rows]

    def propose_claim(self, incident_id, amount, reason, role="cs"):
        with self.lock, self.conn:
            self._require_role(role, ("cs", "cs_lead"), "提交赔付申请")
            incident = self._require(
                self._one("SELECT * FROM incidents WHERE id=?", (incident_id,)),
                "异常记录")
            cur = self.conn.execute(
                """INSERT INTO claims(incident_id,amount,reason,status,proposed_by,created_at)
                   VALUES(?,?,?,'pending',?,?)""",
                (incident_id, amount, reason, role, now_iso()))
            return _row_to_dict(
                self._one("SELECT * FROM claims WHERE id=?", (cur.lastrowid,)))

    def decide_claim(self, claim_id, approve, role):
        """赔付批准/驳回：仅客服主管。"""
        with self.lock, self.conn:
            self._require_role(role, ("cs_lead",), "审批赔付")
            claim = self._require(
                self._one("SELECT * FROM claims WHERE id=?", (claim_id,)), "赔付单")
            if claim["status"] != "pending":
                raise DomainError("赔付单已审批")
            status = "approved" if approve else "rejected"
            self.conn.execute(
                "UPDATE claims SET status=?, decided_by=?, decided_at=? WHERE id=?",
                (status, role, now_iso(), claim_id))
            incident = self._one(
                "SELECT * FROM incidents WHERE id=?", (claim["incident_id"],))
            if approve:
                self.conn.execute(
                    """INSERT INTO fee_items(order_id,kind,label,amount,status,created_at)
                       VALUES(?,?,?,?,'pending',?)""",
                    (incident["order_id"], "compensation",
                     f"赔付（{INCIDENT_TYPES.get(incident['type'], incident['type'])}）",
                     -abs(claim["amount"]), now_iso()))
                self.conn.execute(
                    "UPDATE incidents SET status='resolved', resolved_at=? WHERE id=?",
                    (now_iso(), incident["id"]))
            return _row_to_dict(
                self._one("SELECT * FROM claims WHERE id=?", (claim_id,)))

    def approve_rework(self, incident_id, role):
        """返工批准：仅客服主管。批准后帘片退回洗护队列重新洗烘。"""
        with self.lock, self.conn:
            self._require_role(role, ("cs_lead",), "批准返工")
            incident = self._require(
                self._one("SELECT * FROM incidents WHERE id=?", (incident_id,)),
                "异常记录")
            if incident["status"] != "open":
                raise DomainError("该异常已处理")
            panel = self._panel(incident["panel_id"])
            if panel["status"] in ("packed", "delivering", "delivered",
                                   "reinstalled", "accepted"):
                raise DomainError("帘片已进入配送/复装环节，不能返工")
            # 重置到洗护队列（作废旧任务）
            self.conn.execute(
                """UPDATE wash_tasks SET status='done'
                   WHERE panel_id=? AND status IN ('queued','washing','drying')""",
                (panel["id"],))
            self.conn.execute(
                "UPDATE panels SET status='at_shop' WHERE id=?", (panel["id"],))
            due = (datetime.now() + timedelta(hours=WASH_DUE_HOURS)
                   ).isoformat(timespec="seconds")
            self.conn.execute(
                """INSERT INTO wash_tasks(panel_id,status,queued_at,due_at)
                   VALUES(?,'queued',?,?)""", (panel["id"], now_iso(), due))
            self.conn.execute(
                "UPDATE incidents SET status='resolved', resolved_at=? WHERE id=?",
                (now_iso(), incident_id))
            self.conn.execute(
                """INSERT INTO fee_items(order_id,kind,label,amount,status,created_at)
                   VALUES(?,?,?,?,'pending',?)""",
                (incident["order_id"], "rework",
                 f"帘片 {panel['code']} 返工重洗", REWORK_FEE, now_iso()))
            return _row_to_dict(
                self._one("SELECT * FROM incidents WHERE id=?", (incident_id,)))

    def list_incidents(self, order_id=None):
        sql = """SELECT i.*, p.code panel_code, o.code order_code,
                        (SELECT COUNT(*) FROM claims c WHERE c.incident_id=i.id) claim_count
                 FROM incidents i
                 LEFT JOIN panels p ON p.id=i.panel_id
                 JOIN orders o ON o.id=i.order_id"""
        args = ()
        if order_id:
            sql += " WHERE i.order_id=?"
            args = (order_id,)
        rows = self._all(sql + " ORDER BY i.id DESC", args)
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d["type_label"] = INCIDENT_TYPES.get(d["type"], d["type"])
            out.append(d)
        return out

    def list_claims(self, status=None):
        sql = """SELECT c.*, i.type incident_type, i.panel_id, o.code order_code,
                        p.code panel_code
                 FROM claims c
                 JOIN incidents i ON i.id=c.incident_id
                 JOIN orders o ON o.id=i.order_id
                 LEFT JOIN panels p ON p.id=i.panel_id"""
        args = ()
        if status:
            sql += " WHERE c.status=?"
            args = (status,)
        rows = self._all(sql + " ORDER BY c.id DESC", args)
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d["type_label"] = INCIDENT_TYPES.get(d["incident_type"], d["incident_type"])
            out.append(d)
        return out

    # ------------------------------------------------------------ 配包与配送

    def pack_package(self, window_id, panel_ids, seal_no=None):
        """洗净烘干后按原窗位配包。同一帘片不能归入两个配送包。"""
        with self.lock, self.conn:
            window = self._require(
                self._one("SELECT * FROM windows WHERE id=?", (window_id,)), "窗位")
            room = self._one("SELECT * FROM rooms WHERE id=?", (window["room_id"],))
            order = self._order(room["order_id"])
            panels = []
            for pid in panel_ids:
                p = self._panel(pid)
                if p["window_id"] != window_id:
                    raise DomainError(
                        f"帘片 {p['code']} 不属于窗位 {window['code']}，禁止串包")
                if p["package_id"] is not None:
                    raise DomainError(
                        f"帘片 {p['code']} 已在配送包中，不能归入两个配送包")
                if p["status"] != "ready":
                    raise DomainError(
                        f"帘片 {p['code']} 状态为 {p['status']}，未就绪不能配包")
                panels.append(p)
            if not panels:
                raise DomainError("配送包至少需要一片帘布")
            if seal_no is None:
                seq = self._one("SELECT COUNT(*) c FROM packages")["c"] + 1
                seal_no = f"PKG-{order['code']}-{seq:03d}"
            cur = self.conn.execute(
                """INSERT INTO packages(order_id,window_id,seal_no,status,packed_at)
                   VALUES(?,?,?,'packed',?)""",
                (order["id"], window_id, seal_no, now_iso()))
            pkg_id = cur.lastrowid
            for p in panels:
                self._advance_panel(p, "packed")
                self.conn.execute(
                    "UPDATE panels SET package_id=? WHERE id=?", (pkg_id, p["id"]))
            if order["status"] == "in_care":
                self._set_order_status(order["id"], "packing")
            return _row_to_dict(
                self._one("SELECT * FROM packages WHERE id=?", (pkg_id,)))

    def list_packages(self, order_id=None):
        sql = """SELECT pk.*, w.code window_code, o.code order_code,
                        (SELECT COUNT(*) FROM panels WHERE package_id=pk.id) panel_count,
                        (SELECT GROUP_CONCAT(code) FROM panels WHERE package_id=pk.id) panel_codes,
                        (SELECT b.code FROM batches b JOIN batch_packages bp
                          ON bp.batch_id=b.id WHERE bp.package_id=pk.id) batch_code
                 FROM packages pk
                 JOIN windows w ON w.id=pk.window_id
                 JOIN orders o ON o.id=pk.order_id"""
        args = ()
        if order_id:
            sql += " WHERE pk.order_id=?"
            args = (order_id,)
        rows = self._all(sql + " ORDER BY pk.id DESC", args)
        return [_row_to_dict(r) for r in rows]

    def create_batch(self, package_ids, vehicle="沪A·D3721"):
        with self.lock, self.conn:
            pkgs = []
            for pid in package_ids:
                pkg = self._require(
                    self._one("SELECT * FROM packages WHERE id=?", (pid,)), "配送包")
                if pkg["status"] != "packed":
                    raise DomainError(f"配送包 {pkg['seal_no']} 状态为 {pkg['status']}")
                pkgs.append(pkg)
            if not pkgs:
                raise DomainError("配送批次至少包含一个配送包")
            seq = self._one("SELECT COUNT(*) c FROM batches")["c"] + 1
            code = f"BAT-{datetime.now():%Y%m%d}-{seq:03d}"
            cur = self.conn.execute(
                "INSERT INTO batches(code,status,vehicle,created_at) VALUES(?,?,?,?)",
                (code, "planned", vehicle, now_iso()))
            batch_id = cur.lastrowid
            for pkg in pkgs:
                self.conn.execute(
                    "INSERT INTO batch_packages(batch_id,package_id) VALUES(?,?)",
                    (batch_id, pkg["id"]))
            return _row_to_dict(
                self._one("SELECT * FROM batches WHERE id=?", (batch_id,)))

    def dispatch_batch(self, batch_id):
        """发车：调用配送模拟适配器，批次与包进入在途。"""
        with self.lock, self.conn:
            batch = self._require(
                self._one("SELECT * FROM batches WHERE id=?", (batch_id,)), "配送批次")
            if batch["status"] != "planned":
                raise DomainError("批次已发车")
            if self.delivery:
                self.delivery.create_shipment(batch["code"], batch["vehicle"])
            self.conn.execute(
                "UPDATE batches SET status='delivering' WHERE id=?", (batch_id,))
            for row in self._all(
                    "SELECT package_id FROM batch_packages WHERE batch_id=?",
                    (batch_id,)):
                pkg = self._one("SELECT * FROM packages WHERE id=?",
                                (row["package_id"],))
                self.conn.execute(
                    "UPDATE packages SET status='delivering' WHERE id=?", (pkg["id"],))
                for p in self._all("SELECT * FROM panels WHERE package_id=?",
                                   (pkg["id"],)):
                    self._advance_panel(p, "delivering")
                order = self._order(pkg["order_id"])
                if order["status"] == "packing":
                    self._set_order_status(order["id"], "delivering")
            return _row_to_dict(
                self._one("SELECT * FROM batches WHERE id=?", (batch_id,)))

    def deliver_batch(self, batch_id):
        """送达：适配器确认签收，包与帘片进入 delivered。"""
        with self.lock, self.conn:
            batch = self._require(
                self._one("SELECT * FROM batches WHERE id=?", (batch_id,)), "配送批次")
            if batch["status"] != "delivering":
                raise DomainError("批次未在配送中")
            if self.delivery:
                self.delivery.confirm(batch["code"])
            self.conn.execute(
                "UPDATE batches SET status='delivered', delivered_at=? WHERE id=?",
                (now_iso(), batch_id))
            for row in self._all(
                    "SELECT package_id FROM batch_packages WHERE batch_id=?",
                    (batch_id,)):
                pkg = self._one("SELECT * FROM packages WHERE id=?",
                                (row["package_id"],))
                self.conn.execute(
                    "UPDATE packages SET status='delivered', delivered_at=? WHERE id=?",
                    (now_iso(), pkg["id"]))
                for p in self._all("SELECT * FROM panels WHERE package_id=?",
                                   (pkg["id"],)):
                    self._advance_panel(p, "delivered")
                order = self._order(pkg["order_id"])
                if order["status"] == "delivering":
                    self._set_order_status(order["id"], "reinstalling")
            return _row_to_dict(
                self._one("SELECT * FROM batches WHERE id=?", (batch_id,)))

    def list_batches(self):
        rows = self._all("SELECT * FROM batches ORDER BY id DESC")
        out = []
        for r in rows:
            d = _row_to_dict(r)
            d["packages"] = [_row_to_dict(p) for p in self._all(
                """SELECT pk.*, w.code window_code FROM packages pk
                   JOIN batch_packages bp ON bp.package_id=pk.id
                   JOIN windows w ON w.id=pk.window_id
                   WHERE bp.batch_id=?""", (r["id"],))]
            out.append(d)
        return out

    # ------------------------------------------------------------ 复装与验收

    def scan_reinstall(self, panel_id, package_id, role="crew"):
        """复装扫描：校验帘片属于该配送包且属于原窗位，生成复装记录。

        并发安全：同一帘片重复扫描返回已有记录；验收通过有唯一索引兜底。
        """
        with self.lock, self.conn:
            self._require_role(role, ("crew",), "复装扫描")
            panel = self._panel(panel_id)
            pkg = self._require(
                self._one("SELECT * FROM packages WHERE id=?", (package_id,)),
                "配送包")
            if pkg["status"] != "delivered":
                raise DomainError("配送包未送达，不能复装")
            if panel["package_id"] != package_id:
                # 串包留痕：先提交留痕再拒绝扫描（异常不会回滚这条记录）
                order = self._order_of_panel(panel)
                self._log_incident(
                    order["id"], panel_id, "misbag",
                    f"复装扫描时发现帘片 {panel['code']} 不在配送包 "
                    f"{pkg['seal_no']} 中（实际包 "
                    f"{self._pkg_seal(panel['package_id'])}）", role)
                self.conn.commit()
                raise DomainError(
                    f"帘片 {panel['code']} 不属于配送包 {pkg['seal_no']}，"
                    "已按串包异常留痕")
            if panel["window_id"] != pkg["window_id"]:
                raise DomainError("帘片不属于该配送包对应的窗位")
            existing = self._one(
                "SELECT * FROM reinstalls WHERE panel_id=? AND package_id=?",
                (panel_id, package_id))
            if existing:
                if panel["status"] == "delivered":
                    # 不平整退回后重新扫描：推进到待验收并刷新扫描时间
                    self._advance_panel(panel, "reinstalled")
                    self.conn.execute(
                        "UPDATE reinstalls SET scanned_at=?, result='', note=''"
                        " WHERE id=?", (now_iso(), existing["id"]))
                    return _row_to_dict(self._one(
                        "SELECT * FROM reinstalls WHERE id=?", (existing["id"],)))
                return _row_to_dict(existing)
            if panel["status"] != "delivered":
                raise DomainError(
                    f"帘片 {panel['code']} 状态为 {panel['status']}，不能复装")
            cur = self.conn.execute(
                """INSERT INTO reinstalls(panel_id,package_id,scanned_at)
                   VALUES(?,?,?)""", (panel_id, package_id, now_iso()))
            self._advance_panel(panel, "reinstalled")
            return _row_to_dict(
                self._one("SELECT * FROM reinstalls WHERE id=?", (cur.lastrowid,)))

    def _pkg_seal(self, package_id):
        if package_id is None:
            return "无"
        pkg = self._one("SELECT seal_no FROM packages WHERE id=?", (package_id,))
        return pkg["seal_no"] if pkg else "无"

    def accept_reinstall(self, panel_id, result, note="", role="crew"):
        """复装验收：ok 通过；uneven 不平整则留痕并退回待复装。

        并发只成功一次：reinstalls 上 accepted=1 的部分唯一索引 +
        IMMEDIATE 事务，冲突方得到 409。
        """
        with self.lock:
            self._require_role(role, ("crew",), "复装验收")
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                panel = self._panel(panel_id)
                if panel["status"] != "reinstalled":
                    raise DomainError(
                        f"帘片 {panel['code']} 状态为 {panel['status']}，"
                        "不能重复验收")
                rec = self._require(self._one(
                    """SELECT * FROM reinstalls WHERE panel_id=?
                       ORDER BY id DESC""", (panel_id,)), "复装记录")
                if result == "ok":
                    self.conn.execute(
                        """UPDATE reinstalls SET accepted=1, accepted_at=?,
                           result='ok', note=? WHERE id=?""",
                        (now_iso(), note, rec["id"]))
                    self._advance_panel(panel, "accepted")
                    photos.ensure_photo(self.photo_dir, panel["code"], "after")
                elif result == "uneven":
                    order = self._order_of_panel(panel)
                    self._log_incident(
                        order["id"], panel_id, "uneven",
                        f"复装不平整：{note or '需重新调整'}", role)
                    self.conn.execute(
                        """UPDATE reinstalls SET result='uneven', note=? WHERE id=?""",
                        (note, rec["id"]))
                    # 退回待复装，允许重新扫描
                    self.conn.execute(
                        "UPDATE panels SET status='delivered' WHERE id=?",
                        (panel_id,))
                else:
                    raise DomainError(f"未知验收结果：{result}", 400)
                self.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.conn.rollback()
                raise DomainError(f"该帘片已验收通过，重复验收被拒绝（{exc}）")
            except Exception:
                self.conn.rollback()
                raise
            return _row_to_dict(self._panel(panel_id))

    def list_reinstalls(self, order_id=None):
        sql = """SELECT ri.*, p.code panel_code, p.hook_count, pk.seal_no,
                        w.code window_code, o.code order_code, o.id order_id
                 FROM reinstalls ri
                 JOIN panels p ON p.id=ri.panel_id
                 JOIN packages pk ON pk.id=ri.package_id
                 JOIN windows w ON w.id=p.window_id
                 JOIN rooms r ON r.id=w.room_id
                 JOIN orders o ON o.id=r.order_id"""
        args = ()
        if order_id:
            sql += " WHERE o.id=?"
            args = (order_id,)
        rows = self._all(sql + " ORDER BY ri.id DESC", args)
        return [_row_to_dict(r) for r in rows]

    def upload_photo(self, panel_id, kind, data, ext=".png"):
        """保存上传照片到本地目录；kind: before/after。"""
        with self.lock:
            panel = self._panel(panel_id)
            if kind not in ("before", "after"):
                raise DomainError("照片类型须为 before/after", 400)
            rel = photos.save_upload(self.photo_dir, panel["code"], kind, data, ext)
            return {"panel_code": panel["code"], "kind": kind, "path": rel}

    # ------------------------------------------------------------ 关单与费用

    def close_order(self, order_id, role="cs"):
        """所有窗位验收完成才可关闭订单。"""
        with self.lock, self.conn:
            self._require_role(role, ("cs", "cs_lead"), "关闭订单")
            order = self._order(order_id)
            if order["status"] not in ("reinstalling", "review"):
                raise DomainError(f"订单状态 {order['status']} 不能关闭")
            remaining = self._one(
                """SELECT COUNT(*) c FROM panels p
                   JOIN windows w ON w.id=p.window_id
                   JOIN rooms r ON r.id=w.room_id
                   WHERE r.order_id=? AND p.status != 'accepted'""",
                (order_id,))["c"]
            if remaining:
                raise DomainError(f"还有 {remaining} 片帘布未验收，订单不能关闭")
            open_incidents = self._one(
                "SELECT COUNT(*) c FROM incidents WHERE order_id=? AND status='open'",
                (order_id,))["c"]
            if open_incidents:
                raise DomainError(f"还有 {open_incidents} 条异常未处理，订单不能关闭")
            if order["status"] == "reinstalling":
                self._set_order_status(order_id, "review")
            self._set_order_status(order_id, "closed")
            self.conn.execute(
                "UPDATE orders SET closed_at=? WHERE id=?", (now_iso(), order_id))
            return self.get_order(order_id)

    def fee_review(self, order_id):
        """费用复核：基础洗护费 + 修补费 + 返工费 - 赔付。"""
        order = self._order(order_id)
        panels = self._all(
            """SELECT p.* FROM panels p
               JOIN windows w ON w.id=p.window_id
               JOIN rooms r ON r.id=w.room_id
               WHERE r.order_id=?""", (order_id,))
        base = sum(PRICE_PER_PANEL + p["hook_count"] * PRICE_PER_HOOK for p in panels)
        extras = [_row_to_dict(r) for r in self._all(
            "SELECT * FROM fee_items WHERE order_id=? ORDER BY id", (order_id,))]
        extra_total = sum(e["amount"] for e in extras)
        payments = [_row_to_dict(r) for r in self._all(
            "SELECT * FROM payments WHERE order_id=? ORDER BY id", (order_id,))]
        paid = sum(p["amount"] for p in payments if p["status"] == "success")
        return {
            "order": _row_to_dict(order),
            "panel_count": len(panels),
            "base_amount": round(base, 2),
            "extra_items": extras,
            "extra_total": round(extra_total, 2),
            "total": round(base + extra_total, 2),
            "payments": payments,
            "paid": round(paid, 2),
            "balance": round(base + extra_total - paid, 2),
        }

    def pay_order(self, order_id, role="finance"):
        """通过付款模拟适配器收取余款。"""
        with self.lock, self.conn:
            self._require_role(role, ("finance", "cs_lead"), "发起收款")
            review = self.fee_review(order_id)
            amount = review["balance"]
            if amount <= 0:
                raise DomainError("订单无待收余额")
            result = self.payment.charge(order_id, amount) if self.payment else {
                "status": "success", "txn_no": f"MOCK-{order_id}"}
            self.conn.execute(
                """INSERT INTO payments(order_id,amount,channel,status,txn_no,created_at)
                   VALUES(?,?,?,?,?,?)""",
                (order_id, amount, "mockpay", result["status"],
                 result.get("txn_no"), now_iso()))
            return {"payment": result, "review": self.fee_review(order_id)}

    # ------------------------------------------------------------ 提醒

    def due_reminders(self):
        """扫描当前应触发的提醒（洗护超时 / 预约临近 / 复装后未验收）。"""
        now = now_iso()
        soon = (datetime.now() + timedelta(hours=APPOINT_REMIND_HOURS)
                ).isoformat(timespec="seconds")
        stale = (datetime.now() - timedelta(hours=UNACCEPTED_STALE_HOURS)
                 ).isoformat(timespec="seconds")
        items = []
        for t in self._all(
                """SELECT t.id, t.due_at, p.code panel_code FROM wash_tasks t
                   JOIN panels p ON p.id=t.panel_id
                   WHERE t.status IN ('queued','washing') AND t.due_at < ?""",
                (now,)):
            items.append({"kind": "wash_overdue", "ref_type": "wash_task",
                          "ref_id": t["id"], "level": "warn",
                          "message": f"帘片 {t['panel_code']} 洗护超时（时限 {t['due_at']}）"})
        for o in self._all(
                """SELECT id, code, appoint_at FROM orders
                   WHERE status='scheduled' AND appoint_at IS NOT NULL
                   AND appoint_at < ?""", (soon,)):
            items.append({"kind": "appoint_soon", "ref_type": "order",
                          "ref_id": o["id"], "level": "info",
                          "message": f"订单 {o['code']} 预约上门时间临近（{o['appoint_at']}）"})
        for p in self._all(
                """SELECT p.id, p.code FROM panels p
                   JOIN reinstalls ri ON ri.panel_id=p.id
                   WHERE p.status='reinstalled' AND ri.accepted=0
                   AND ri.scanned_at < ?""", (stale,)):
            items.append({"kind": "accept_stale", "ref_type": "panel",
                          "ref_id": p["id"], "level": "warn",
                          "message": f"帘片 {p['code']} 复装后超过 {UNACCEPTED_STALE_HOURS} 小时未验收"})
        return items

    def fire_reminders(self):
        """把到期提醒写入 reminders 表（去重），返回新触发的条目。"""
        with self.lock, self.conn:
            fired = []
            for item in self.due_reminders():
                dup = self._one(
                    """SELECT id FROM reminders WHERE kind=? AND ref_type=?
                       AND ref_id=? AND message=?""",
                    (item["kind"], item["ref_type"], item["ref_id"],
                     item["message"]))
                if dup:
                    continue
                self.conn.execute(
                    """INSERT INTO reminders(kind,ref_type,ref_id,message,level,fired_at)
                       VALUES(?,?,?,?,?,?)""",
                    (item["kind"], item["ref_type"], item["ref_id"],
                     item["message"], item["level"], now_iso()))
                fired.append(item)
            return fired

    def list_reminders(self, limit=50):
        rows = self._all(
            "SELECT * FROM reminders ORDER BY id DESC LIMIT ?", (limit,))
        return [_row_to_dict(r) for r in rows]
