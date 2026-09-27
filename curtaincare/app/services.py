"""领域服务：订单全链路状态机、权限与业务规则。

角色：
  staff    上门拆卸 / 配送 / 复装人员
  wash     洗护人员（可登记损伤）
  cs_lead  客服主管（批准赔付与返工、复核费用）

关键规则：
  * 帘片编号、封签号、批次号全局唯一
  * 一片帘只能入一个袋 / 一个配送包（pieces.bag_id / package_id 上有 UNIQUE 约束）
  * 配包必须包含该窗位全部帘片且均已完工
  * 状态严格按顺序流转
  * 复装扫描并发只成功一次，窗位不可重复验收
  * 损伤由洗护人员登记，赔付 / 返工须客服主管批准
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .db import Database

ROLE_STAFF = "staff"
ROLE_WASH = "wash"
ROLE_CS = "cs_lead"

ROLE_NAMES = {
    ROLE_STAFF: "上门/配送员",
    ROLE_WASH: "洗护人员",
    ROLE_CS: "客服主管",
}

INCIDENT_TYPES = {
    "misbag": "帘片串包",
    "shrinkage": "缩水",
    "dye": "染色",
    "hooks_missing": "挂钩缺失",
    "banned_wash": "布料禁洗",
    "delay": "客户延期",
    "second_repair": "二次修补",
    "uneven": "复装不平整",
}


class DomainError(Exception):
    def __init__(self, msg: str, status: int = 400):
        super().__init__(msg)
        self.status = status


class AuthError(DomainError):
    def __init__(self, msg: str = "无权操作"):
        super().__init__(msg, 403)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- 状态机 -------------------------------------------------------------

# 帘片状态顺序（index 即阶段，部分允许返工回退）
PIECE_FLOW = [
    "registered", "removed", "bagged", "in_transit", "at_plant",
    "in_wash", "washed", "drying", "dry", "in_repair", "ready",
    "packed", "delivering", "reinstalling", "reinstalled",
]
PIECE_TRANSITIONS = {
    "registered": {"removed"},
    "removed": {"bagged"},
    "bagged": {"in_transit"},
    "in_transit": {"at_plant"},
    "at_plant": {"in_wash", "skipped_wash"},
    "in_wash": {"washed"},
    "washed": {"drying", "in_repair"},
    "drying": {"dry"},
    "dry": {"ready", "in_repair"},
    "skipped_wash": {"ready"},
    "in_repair": {"ready"},
    "ready": {"packed", "in_repair"},
    "packed": {"delivering"},
    "delivering": {"reinstalled"},
    "reinstalling": {"reinstalled"},
    "reinstalled": {"reinstalling"},  # 仅复装不平整返工
}

WINDOW_FLOW = ["pending", "removing", "removed", "at_plant", "in_wash",
               "ready", "packed", "delivering", "reinstalling", "accepted"]
WINDOW_TRANSITIONS = {
    "pending": {"removing", "removed"},
    "removing": {"removed"},
    "removed": {"at_plant"},
    "at_plant": {"in_wash"},
    "in_wash": {"ready"},
    "ready": {"packed"},
    "packed": {"delivering"},
    "delivering": {"reinstalling"},
    "reinstalling": {"accepted"},
    "accepted": set(),
}

ORDER_FLOW = ["created", "removing", "in_service", "delivering",
              "reinstalling", "completed"]
ORDER_TRANSITIONS = {
    "created": {"removing"},
    "removing": {"in_service"},
    "in_service": {"delivering", "reinstalling"},
    "delivering": {"reinstalling"},
    "reinstalling": {"completed"},
    "completed": set(),
}

PIECE_STATUS_CN = {
    "registered": "已登记", "removed": "已拆卸", "bagged": "已装袋",
    "in_transit": "运输中", "at_plant": "到厂待洗", "in_wash": "洗护中",
    "washed": "已洗净", "drying": "烘干中", "dry": "已烘干",
    "in_repair": "修补中", "ready": "待配包", "packed": "已配包",
    "delivering": "配送中", "reinstalling": "待复装调整",
    "reinstalled": "已复装", "skipped_wash": "禁洗已隔",
}
WINDOW_STATUS_CN = {
    "pending": "待拆卸", "removing": "拆卸中", "removed": "已装袋",
    "at_plant": "到厂", "in_wash": "洗护中", "ready": "待配包",
    "packed": "已配包", "delivering": "配送中",
    "reinstalling": "复装中", "accepted": "已验收",
}
ORDER_STATUS_CN = {
    "created": "已建单", "removing": "拆卸中", "in_service": "洗护中",
    "delivering": "配送中", "reinstalling": "复装验收中", "completed": "已关闭",
}


class Service:
    def __init__(self, db: Database):
        self.db = db

    # --- 基础工具 ---

    @staticmethod
    def _require(role: str, allowed: set[str]):
        if role not in allowed:
            raise AuthError(f"需要角色 {'/'.join(sorted(allowed))}，当前为 {ROLE_NAMES.get(role, role)}")

    def _get(self, table: str, row_id: int):
        row = self.db.one(f"SELECT * FROM {table} WHERE id=?", (row_id,))
        if row is None:
            raise DomainError(f"{table}#{row_id} 不存在", 404)
        return row

    def _piece(self, piece_id: int):
        return self._get("pieces", piece_id)

    def _window(self, window_id: int):
        return self._get("windows", window_id)

    def _advance(self, con, table: str, row_id: int, target: str,
                 transitions: dict, status_col="status"):
        row = con.execute(f"SELECT {status_col} FROM {table} WHERE id=?", (row_id,)).fetchone()
        cur = row[0]
        if cur == target:
            return cur
        if target not in transitions.get(cur, set()):
            raise DomainError(f"状态顺序错误：{table}#{row_id} 不能从「{cur}」转到「{target}」")
        con.execute(f"UPDATE {table} SET {status_col}=? WHERE id=?", (target, row_id))
        return cur

    def _piece_event(self, con, piece_id: int, kind: str, actor: str,
                     detail: str = "", at: str | None = None):
        con.execute(
            "INSERT INTO piece_events(piece_id,kind,actor,detail,at) VALUES(?,?,?,?,?)",
            (piece_id, kind, actor, detail, at or now()))

    def _order_event(self, con, order_id: int, kind: str, actor: str,
                     detail: str = "", at: str | None = None):
        con.execute(
            "INSERT INTO order_events(order_id,kind,actor,detail,at) VALUES(?,?,?,?,?)",
            (order_id, kind, actor, detail, at or now()))

    def _incident(self, con, order_id: int, type_: str, actor: str,
                  note: str = "", piece_id: int | None = None,
                  at: str | None = None, resolved: bool = False) -> int:
        if type_ not in INCIDENT_TYPES:
            raise DomainError(f"未知异常类型 {type_}")
        cur = con.execute(
            "INSERT INTO incidents(order_id,piece_id,type,note,actor,at,resolved)"
            " VALUES(?,?,?,?,?,?,?)",
            (order_id, piece_id, type_, note, actor, at or now(), 1 if resolved else 0))
        return cur.lastrowid

    def _seq_code(self, con, table: str, column: str, prefix: str, width: int = 4) -> str:
        n = con.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"] + 1
        return f"{prefix}{n:0{width}d}"

    # --- 建单 / 树形结构 ---

    def create_order(self, customer_name: str, phone: str, address_alias: str,
                     address_detail: str, appointment_at: str | None = None,
                     actor: str = ROLE_STAFF, note: str = "") -> int:
        with self.db.tx() as con:
            code = self._seq_code(con, "orders", "code", "CO-")
            cur = con.execute(
                "INSERT INTO orders(code,customer_name,phone,address_alias,address_detail,"
                "appointment_at,status,note,created_at) VALUES(?,?,?,?,?,?, 'created',?,?)",
                (code, customer_name, phone, address_alias, address_detail,
                 appointment_at, note, now()))
            oid = cur.lastrowid
            self._order_event(con, oid, "created", actor, f"建单 {code}")
            return oid

    def create_room(self, order_id: int, name: str) -> int:
        with self.db.tx() as con:
            n = con.execute("SELECT COUNT(*) c FROM rooms WHERE order_id=?", (order_id,)).fetchone()["c"]
            cur = con.execute(
                "INSERT INTO rooms(order_id,name,sort) VALUES(?,?,?)", (order_id, name, n))
            return cur.lastrowid

    def add_window(self, room_id: int, label: str) -> int:
        with self.db.tx() as con:
            room = con.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
            if room is None:
                raise DomainError("房间不存在", 404)
            n = con.execute("SELECT COUNT(*) c FROM windows WHERE room_id=?", (room_id,)).fetchone()["c"]
            cur = con.execute(
                "INSERT INTO windows(room_id,label,sort,status) VALUES(?,?,?, 'pending')",
                (room_id, label, n))
            wid = cur.lastrowid
            wcode = f"W{wid:04d}"
            con.execute("UPDATE windows SET label=label WHERE id=?", (wid,))
            # 窗位代号存到 label 前不方便，另用 events 不需要；窗位编号由 id 推导
            return wid

    def add_piece(self, window_id: int, fabric: str, hooks: int,
                  washable: bool = True, wash_req: str = "",
                  code: str | None = None, actor: str = ROLE_STAFF) -> int:
        """登记一片帘布。编号全局唯一（按窗位分组：W0001-01）。"""
        with self.db.tx() as con:
            w = con.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
            if w is None:
                raise DomainError("窗位不存在", 404)
            wcode = f"W{window_id:04d}"
            if code is None:
                n = con.execute("SELECT COUNT(*) c FROM pieces WHERE window_id=?",
                                (window_id,)).fetchone()["c"] + 1
                code = f"{wcode}-{n:02d}"
            try:
                cur = con.execute(
                    "INSERT INTO pieces(window_id,code,fabric,washable,wash_req,"
                    "hooks_expected,status,created_at) VALUES(?,?,?,?,?,?, 'registered',?)",
                    (window_id, code, fabric, 1 if washable else 0, wash_req,
                     hooks, now()))
            except Exception as e:  # UNIQUE 冲突
                raise DomainError(f"帘片编号已存在：{code}") from e
            pid = cur.lastrowid
            self._piece_event(con, pid, "registered", actor, f"{fabric} / 挂钩 {hooks}")
            return pid

    def set_appointment(self, order_id: int, appointment_at: str, actor=ROLE_CS):
        with self.db.tx() as con:
            con.execute("UPDATE orders SET appointment_at=? WHERE id=?",
                        (appointment_at, order_id))
            self._order_event(con, order_id, "appointment", actor, appointment_at)

    def postpone_order(self, order_id: int, days: int, reason: str, actor=ROLE_CS):
        """客户延期：留痕并顺延预约。"""
        self._require(actor, {ROLE_CS, ROLE_STAFF})
        with self.db.tx() as con:
            o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                raise DomainError("订单不存在", 404)
            new_appt = None
            if o["appointment_at"]:
                base = datetime.fromisoformat(o["appointment_at"])
                new_appt = (base + timedelta(days=days)).isoformat(timespec="seconds")
                con.execute("UPDATE orders SET appointment_at=?, postponed=postponed+1 WHERE id=?",
                            (new_appt, order_id))
            else:
                con.execute("UPDATE orders SET postponed=postponed+1 WHERE id=?", (order_id,))
            self._incident(con, order_id, "delay", actor,
                           f"延期 {days} 天：{reason}" + (f"；新预约 {new_appt}" if new_appt else ""))
            self._order_event(con, order_id, "postponed", actor, reason)

    # --- 上门拆卸 ---

    def start_removal(self, order_id: int, actor=ROLE_STAFF):
        self._require(actor, {ROLE_STAFF})
        with self.db.tx() as con:
            self._advance_order(con, order_id, "removing")
            self._order_event(con, order_id, "removal_start", actor)

    def attach_photo(self, piece_id: int, kind: str, path: str, actor=ROLE_STAFF):
        if kind not in ("before", "after_wash", "reinstall"):
            raise DomainError("照片类型错误")
        col = {"before": "photo_before", "after_wash": "photo_after_wash",
               "reinstall": "photo_reinstall"}[kind]
        with self.db.tx() as con:
            p = con.execute("SELECT id FROM pieces WHERE id=?", (piece_id,)).fetchone()
            if p is None:
                raise DomainError("帘片不存在", 404)
            con.execute(f"UPDATE pieces SET {col}=? WHERE id=?", (path, piece_id))
            self._piece_event(con, piece_id, f"photo_{kind}", actor, path)

    def count_hooks(self, piece_id: int, hooks: int, actor=ROLE_STAFF):
        """拆卸清点挂钩：记录数量 + 拆前照片齐备后方可流转到「已拆卸」。"""
        self._require(actor, {ROLE_STAFF})
        if hooks < 0:
            raise DomainError("挂钩数量不能为负")
        with self.db.tx() as con:
            p = self._piece_locked(con, piece_id)
            if not p["photo_before"]:
                raise DomainError(f"{p['code']} 缺少拆前照片，不可清点")
            con.execute("UPDATE pieces SET hooks_counted=? WHERE id=?", (hooks, piece_id))
            if p["status"] == "registered":
                self._advance(con, "pieces", piece_id, "removed", PIECE_TRANSITIONS)
                w = con.execute("SELECT status FROM windows WHERE id=?", (p["window_id"],)).fetchone()
                if w["status"] == "pending":
                    self._advance(con, "windows", p["window_id"], "removing", WINDOW_TRANSITIONS)
            self._piece_event(con, piece_id, "hooks_counted", actor, f"清点挂钩 {hooks}")
            if hooks < p["hooks_expected"]:
                oid = con.execute("SELECT o.id FROM rooms r JOIN windows w ON w.room_id=r.id "
                                  "JOIN orders o ON o.id=r.order_id WHERE w.id=?",
                                  (p["window_id"],)).fetchone()["id"]
                self._incident(con, oid, "hooks_missing", actor,
                               f"{p['code']} 拆卸时即少 {p['hooks_expected']-hooks} 个挂钩",
                               piece_id=piece_id)

    def seal_window_bag(self, window_id: int, actor=ROLE_STAFF) -> str:
        """按窗位装袋封签：该窗位全部帘片必须已清点挂钩并拍照，一袋封一个窗位。"""
        self._require(actor, {ROLE_STAFF})
        with self.db.tx() as con:
            w = con.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
            if w is None:
                raise DomainError("窗位不存在", 404)
            pieces = con.execute("SELECT * FROM pieces WHERE window_id=? ORDER BY code",
                                 (window_id,)).fetchall()
            if not pieces:
                raise DomainError("该窗位没有帘片，无法封袋")
            for p in pieces:
                if p["status"] != "removed":
                    raise DomainError(f"{p['code']} 尚未完成拆卸清点，不能装袋")
            exists = con.execute("SELECT id FROM bags WHERE window_id=?", (window_id,)).fetchone()
            if exists:
                raise DomainError("该窗位已封袋，禁止重复封袋")
            seal = self._seq_code(con, "bags", "seal_no", "SEAL-B-")
            cur = con.execute(
                "INSERT INTO bags(order_id,window_id,seal_no,status,sealed_at,created_at)"
                " SELECT r.order_id, ?, ?, 'sealed', ?, ? FROM rooms r WHERE r.id=?",
                (window_id, seal, now(), now(), w["room_id"]))
            bag_id = cur.lastrowid
            for p in pieces:
                # bag_id 是帘片上的单列外键：结构上不可能同时属于两个袋（串袋）
                con.execute("UPDATE pieces SET bag_id=? WHERE id=?", (bag_id, p["id"]))
                self._advance(con, "pieces", p["id"], "bagged", PIECE_TRANSITIONS)
                self._piece_event(con, p["id"], "bagged", actor, f"入袋 {seal}")
            self._advance(con, "windows", window_id, "removed", WINDOW_TRANSITIONS)
            oid = con.execute("SELECT order_id FROM bags WHERE id=?", (bag_id,)).fetchone()["order_id"]
            o = con.execute("SELECT status FROM orders WHERE id=?", (oid,)).fetchone()
            if o["status"] == "created":
                self._advance_order(con, oid, "removing")
            self._order_event(con, oid, "bag_sealed", actor,
                              f"窗位#{window_id} 封签 {seal}（{len(pieces)} 片）")
            return seal

    def transport_order(self, order_id: int, actor=ROLE_STAFF):
        """封签袋进入运输。"""
        self._require(actor, {ROLE_STAFF})
        with self.db.tx() as con:
            bags = con.execute("SELECT * FROM bags WHERE order_id=? AND status='sealed'",
                               (order_id,)).fetchall()
            if not bags:
                raise DomainError("没有待运输的封签袋")
            for b in bags:
                con.execute("UPDATE bags SET status='in_transit' WHERE id=?", (b["id"],))
                for p in con.execute("SELECT id FROM pieces WHERE bag_id=?", (b["id"],)):
                    self._advance(con, "pieces", p["id"], "in_transit", PIECE_TRANSITIONS)
            self._order_event(con, order_id, "transport", actor,
                              f"{len(bags)} 袋发运")

    def receive_order(self, order_id: int, actor=ROLE_WASH):
        """到厂接收。"""
        self._require(actor, {ROLE_WASH, ROLE_STAFF})
        with self.db.tx() as con:
            bags = con.execute("SELECT * FROM bags WHERE order_id=? AND status='in_transit'",
                               (order_id,)).fetchall()
            if not bags:
                raise DomainError("没有在途袋可接收")
            for b in bags:
                con.execute("UPDATE bags SET status='received' WHERE id=?", (b["id"],))
                for p in con.execute("SELECT * FROM pieces WHERE bag_id=?", (b["id"],)):
                    self._advance(con, "pieces", p["id"], "at_plant", PIECE_TRANSITIONS)
                self._advance(con, "windows", b["window_id"], "at_plant", WINDOW_TRANSITIONS)
            self._advance_order(con, order_id, "in_service")
            self._order_event(con, order_id, "received", actor, f"到厂 {len(bags)} 袋")

    def flag_misbag(self, piece_id: int, found_seal: str, note: str, actor=ROLE_WASH):
        """到厂拆袋发现串包：帘片出现在非本窗位封签袋中——留痕并隔离纠正。"""
        self._require(actor, {ROLE_WASH, ROLE_STAFF})
        with self.db.tx() as con:
            p = self._piece_locked(con, piece_id)
            if p["status"] not in ("in_transit", "at_plant"):
                raise DomainError("仅到厂拆袋阶段可登记串包")
            own = con.execute("SELECT seal_no FROM bags WHERE id=?", (p["bag_id"],)).fetchone()
            own_seal = own["seal_no"] if own else "-"
            if own_seal == found_seal:
                raise DomainError("封签号与本窗位一致，不构成串包")
            oid = con.execute("SELECT o.id FROM windows w JOIN rooms r ON r.id=w.room_id "
                              "JOIN orders o ON o.id=r.order_id WHERE w.id=?",
                              (p["window_id"],)).fetchone()["id"]
            iid = self._incident(con, oid, "misbag", actor,
                                 f"{p['code']} 应在 {own_seal}，却发现于 {found_seal}；{note}",
                                 piece_id=piece_id, resolved=True)
            con.execute("UPDATE incidents SET resolved_note=? WHERE id=?",
                        ("已隔离并归回本窗位队列", iid))
            self._piece_event(con, piece_id, "misbag", actor,
                              f"串包：{found_seal} → 归回 {own_seal}")

    # --- 洗护 ---

    def start_wash(self, piece_id: int, actor=ROLE_WASH, at: str | None = None):
        self._require(actor, {ROLE_WASH})
        p = self._piece(piece_id)
        if not p["washable"]:
            # 禁洗拦截：留痕单独提交（不能随异常回滚），再拒绝入洗
            with self.db.tx() as con:
                oid = self._order_id_of(con, piece_id)
                self._incident(con, oid, "banned_wash", actor,
                               f"{p['code']} 布料「{p['fabric']}」禁水洗，已拦下",
                               piece_id=piece_id)
                self._piece_event(con, piece_id, "banned_wash", actor, "禁洗拦截")
            raise DomainError(f"{p['code']} 布料禁洗，不可进入水洗（已登记留痕，请改走隔洗）")
        with self.db.tx() as con:
            p = self._piece_locked(con, piece_id)
            self._advance(con, "pieces", piece_id, "in_wash", PIECE_TRANSITIONS)
            self._set_window_status(con, p["window_id"], "in_wash")
            self._piece_event(con, piece_id, "wash_start", actor,
                              p["wash_req"] or "常规洗护", at=at)

    def skip_wash(self, piece_id: int, actor=ROLE_WASH, note: str = "禁洗布料，仅除尘隔理"):
        """禁洗帘片：不进水洗，除尘隔理后直接待配包。"""
        self._require(actor, {ROLE_WASH})
        with self.db.tx() as con:
            p = self._piece_locked(con, piece_id)
            self._advance(con, "pieces", piece_id, "skipped_wash", PIECE_TRANSITIONS)
            self._advance(con, "pieces", piece_id, "ready", PIECE_TRANSITIONS)
            self._piece_event(con, piece_id, "wash_skip", actor, note)

    def finish_wash(self, piece_id: int, actor=ROLE_WASH, at: str | None = None):
        self._require(actor, {ROLE_WASH})
        with self.db.tx() as con:
            self._advance(con, "pieces", piece_id, "washed", PIECE_TRANSITIONS)
            self._piece_event(con, piece_id, "washed", actor, at=at)

    def start_drying(self, piece_id: int, actor=ROLE_WASH, at: str | None = None):
        self._require(actor, {ROLE_WASH})
        with self.db.tx() as con:
            self._advance(con, "pieces", piece_id, "drying", PIECE_TRANSITIONS)
            self._piece_event(con, piece_id, "drying_start", actor, at=at)

    def finish_drying(self, piece_id: int, actor=ROLE_WASH, photo_path: str | None = None):
        self._require(actor, {ROLE_WASH})
        with self.db.tx() as con:
            self._advance(con, "pieces", piece_id, "dry", PIECE_TRANSITIONS)
            if photo_path:
                con.execute("UPDATE pieces SET photo_after_wash=? WHERE id=?",
                            (photo_path, piece_id))
            self._piece_event(con, piece_id, "dried", actor)

    def report_incident(self, piece_id: int, type_: str, note: str,
                        actor=ROLE_WASH, resolved: bool = False):
        """缩水 / 染色 / 挂钩缺失 等洗护异常留痕。"""
        self._require(actor, {ROLE_WASH, ROLE_CS, ROLE_STAFF})
        if type_ in ("misbag", "delay", "banned_wash", "second_repair", "uneven"):
            raise DomainError("该类异常由专门流程登记")
        with self.db.tx() as con:
            p = self._piece_locked(con, piece_id)
            oid = self._order_id_of(con, piece_id)
            iid = self._incident(con, oid, type_, actor, note, piece_id=piece_id,
                                 resolved=resolved)
            self._piece_event(con, piece_id, type_, actor, note)
            return iid

    def resolve_incident(self, incident_id: int, note: str, actor=ROLE_CS):
        with self.db.tx() as con:
            row = con.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
            if row is None:
                raise DomainError("异常记录不存在", 404)
            con.execute("UPDATE incidents SET resolved=1, resolved_note=? WHERE id=?",
                        (note, incident_id))

    def mark_ready(self, piece_id: int, actor=ROLE_WASH):
        """烘干完工 → 待配包。"""
        self._require(actor, {ROLE_WASH})
        with self.db.tx() as con:
            self._advance(con, "pieces", piece_id, "ready", PIECE_TRANSITIONS)
            self._piece_event(con, piece_id, "ready", actor)
            self._refresh_window_ready(con, piece_id)

    # --- 损伤 / 赔付 / 返工 / 修补 ---

    def register_damage(self, piece_id: int, description: str, actor=ROLE_WASH,
                        at: str | None = None) -> int:
        """洗护人员登记损伤。"""
        self._require(actor, {ROLE_WASH})
        with self.db.tx() as con:
            p = self._piece_locked(con, piece_id)
            cur = con.execute(
                "INSERT INTO damages(piece_id,description,registered_by,at,status)"
                " VALUES(?,?,?,?,'pending')",
                (piece_id, description, actor, at or now()))
            did = cur.lastrowid
            oid = self._order_id_of(con, piece_id)
            self._incident(con, oid, "dye", actor,
                           f"损伤登记 #{did}：{description}", piece_id=piece_id) \
                if False else None
            self._piece_event(con, piece_id, "damage_registered", actor,
                              f"损伤 #{did}：{description}")
            return did

    def decide_damage(self, damage_id: int, decision: str, actor: str,
                      amount: float = 0.0, note: str = ""):
        """客服主管批准赔付或返工。洗护人员无权。"""
        self._require(actor, {ROLE_CS})
        if decision not in ("compensate", "rework", "reject"):
            raise DomainError("decision 必须是 compensate / rework / reject")
        with self.db.tx() as con:
            d = con.execute("SELECT * FROM damages WHERE id=?", (damage_id,)).fetchone()
            if d is None:
                raise DomainError("损伤记录不存在", 404)
            if d["status"] != "pending":
                raise DomainError(f"损伤 #{damage_id} 已审批为「{d['status']}」，不可重复批准")
            con.execute(
                "INSERT INTO compensations(damage_id,amount,decided_by,decision,decided_at)"
                " VALUES(?,?,?,?,?)",
                (damage_id, amount if decision == "compensate" else 0.0,
                 actor, decision, now()))
            if decision == "compensate":
                if amount <= 0:
                    raise DomainError("赔付金额必须大于 0")
                con.execute("UPDATE damages SET status='compensated' WHERE id=?", (damage_id,))
                oid = self._order_id_of(con, d["piece_id"])
                con.execute(
                    "INSERT INTO fees(order_id,item,amount,kind) VALUES(?,?,?, 'compensation')",
                    (oid, f"帘片损伤赔付 #{damage_id}", -amount))
                self._piece_event(con, d["piece_id"], "compensated", actor,
                                  f"赔付 ¥{amount:.2f}：{note}")
            elif decision == "rework":
                con.execute("UPDATE damages SET status='rework' WHERE id=?", (damage_id,))
                self._open_repair(con, d["piece_id"],
                                  f"客服主管批准返工（损伤 #{damage_id}）：{note}", actor)
            else:
                con.execute("UPDATE damages SET status='rejected' WHERE id=?", (damage_id,))
                self._piece_event(con, d["piece_id"], "damage_rejected", actor, note)

    def open_repair(self, piece_id: int, note: str, actor=ROLE_WASH,
                    at: str | None = None) -> int:
        self._require(actor, {ROLE_WASH, ROLE_CS})
        with self.db.tx() as con:
            return self._open_repair(con, piece_id, note, actor, at=at)

    def _open_repair(self, con, piece_id: int, note: str, actor: str,
                     at: str | None = None) -> int:
        p = con.execute("SELECT * FROM pieces WHERE id=?", (piece_id,)).fetchone()
        if p is None:
            raise DomainError("帘片不存在", 404)
        if p["status"] not in ("washed", "dry", "ready"):
            raise DomainError(f"帘片当前状态「{PIECE_STATUS_CN.get(p['status'])}」不可修补")
        rounds = con.execute("SELECT COUNT(*) c FROM repairs WHERE piece_id=?",
                             (piece_id,)).fetchone()["c"]
        round_no = rounds + 1
        cur = con.execute(
            "INSERT INTO repairs(piece_id,round,note,status,created_by,created_at)"
            " VALUES(?,?,?, 'open',?,?)",
            (piece_id, round_no, note, actor, at or now()))
        rid = cur.lastrowid
        if p["status"] != "in_repair":
            self._advance(con, "pieces", piece_id, "in_repair", PIECE_TRANSITIONS)
        if round_no >= 2:
            oid = self._order_id_of(con, piece_id)
            self._incident(con, oid, "second_repair", actor,
                           f"{p['code']} 第 {round_no} 次修补：{note}", piece_id=piece_id)
        self._piece_event(con, piece_id, "repair_open", actor,
                          f"第 {round_no} 轮修补：{note}")
        return rid

    def finish_repair(self, repair_id: int, actor=ROLE_WASH, at: str | None = None):
        self._require(actor, {ROLE_WASH, ROLE_CS})
        with self.db.tx() as con:
            r = con.execute("SELECT * FROM repairs WHERE id=?", (repair_id,)).fetchone()
            if r is None:
                raise DomainError("修补记录不存在", 404)
            if r["status"] == "done":
                raise DomainError("该修补已完工")
            con.execute("UPDATE repairs SET status='done', finished_at=? WHERE id=?",
                        (at or now(), repair_id))
            p = con.execute("SELECT * FROM pieces WHERE id=?", (r["piece_id"],)).fetchone()
            open_another = con.execute(
                "SELECT COUNT(*) c FROM repairs WHERE piece_id=? AND status='open'",
                (r["piece_id"],)).fetchone()["c"]
            if open_another == 0:
                self._advance(con, "pieces", r["piece_id"], "ready", PIECE_TRANSITIONS)
                self._piece_event(con, r["piece_id"], "repair_done", actor,
                                  f"第 {r['round']} 轮完工")
                self._refresh_window_ready(con, r["piece_id"])

    # --- 配包 / 配送 ---

    def pack_window(self, window_id: int, actor=ROLE_WASH) -> str:
        """洗净烘干后按原窗位配包：必须集齐该窗位全部帘片，缺一不可。"""
        self._require(actor, {ROLE_WASH, ROLE_STAFF})
        with self.db.tx() as con:
            w = con.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
            if w is None:
                raise DomainError("窗位不存在", 404)
            pieces = con.execute("SELECT * FROM pieces WHERE window_id=? ORDER BY code",
                                 (window_id,)).fetchall()
            if not pieces:
                raise DomainError("窗位无帘片")
            not_ready = [p["code"] for p in pieces if p["status"] != "ready"]
            if not_ready:
                raise DomainError("配包不完整，以下帘片未完工：" + "、".join(not_ready))
            dup = con.execute("SELECT code FROM pieces WHERE window_id=? AND package_id IS NOT NULL",
                              (window_id,)).fetchall()
            if dup:
                raise DomainError("已有帘片归入其他配送包：" + "、".join(r["code"] for r in dup))
            seal = self._seq_code(con, "packages", "seal_no", "SEAL-P-")
            oid = con.execute("SELECT order_id FROM rooms WHERE id=?", (w["room_id"],)).fetchone()["order_id"]
            cur = con.execute(
                "INSERT INTO packages(order_id,window_id,seal_no,status,created_at)"
                " VALUES(?,?,?, 'packing',?)",
                (oid, window_id, seal, now()))
            pkg_id = cur.lastrowid
            for p in pieces:
                # package_id 是帘片上的单列外键：同一帘片结构上不可能归入两个配送包
                con.execute("UPDATE pieces SET package_id=? WHERE id=?", (pkg_id, p["id"]))
                self._advance(con, "pieces", p["id"], "packed", PIECE_TRANSITIONS)
                self._piece_event(con, p["id"], "packed", actor, f"配包 {seal}")
            return seal

    def seal_package(self, package_id: int, actor=ROLE_WASH):
        self._require(actor, {ROLE_WASH, ROLE_STAFF})
        with self.db.tx() as con:
            pkg = con.execute("SELECT * FROM packages WHERE id=?", (package_id,)).fetchone()
            if pkg is None:
                raise DomainError("配送包不存在", 404)
            self._advance_pkg(con, package_id, "sealed")
            con.execute("UPDATE packages SET packed_at=? WHERE id=?", (now(), package_id))
            self._advance(con, "windows", pkg["window_id"], "packed", WINDOW_TRANSITIONS)
            self._order_event(con, pkg["order_id"], "package_sealed", actor,
                              f"窗位#{pkg['window_id']} 包封签 {pkg['seal_no']}")

    def create_delivery(self, order_id: int, package_ids: list[int], adapter,
                        actor=ROLE_STAFF) -> str:
        """生成配送批次，调用模拟配送适配器；失败则不改变状态。"""
        self._require(actor, {ROLE_STAFF})
        if not package_ids:
            raise DomainError("请选择配送包")
        with self.db.tx() as con:
            for pid in package_ids:
                pkg = con.execute("SELECT * FROM packages WHERE id=?", (pid,)).fetchone()
                if pkg is None:
                    raise DomainError(f"配送包 #{pid} 不存在", 404)
                if pkg["order_id"] != order_id:
                    raise DomainError(f"{pkg['seal_no']} 不属于该订单")
                if pkg["status"] != "sealed":
                    raise DomainError(f"{pkg['seal_no']} 状态为「{pkg['status']}」，未封包不能配送")
            batch_no = self._seq_code(con, "deliveries", "batch_no", "DB-")
            result = adapter.dispatch(batch_no, package_ids)
            failed_reason = None
            if not result.get("ok"):
                # 失败结果同样落库留痕；在事务提交后再抛错，避免留痕被回滚
                con.execute(
                    "INSERT INTO deliveries(order_id,batch_no,status,adapter_ref,result,"
                    "created_at,updated_at) VALUES(?,?, 'failed',?,?,?,?)",
                    (order_id, batch_no, result.get("ref", ""),
                     f"配送适配器失败：{result.get('reason','')}", now(), now()))
                self._order_event(con, order_id, "delivery_failed", actor,
                                  f"{batch_no} {result.get('reason','')}")
                failed_reason = result.get("reason", "未知原因")
            else:
                cur = con.execute(
                    "INSERT INTO deliveries(order_id,batch_no,status,adapter_ref,result,"
                    "created_at,updated_at) VALUES(?,?, 'in_transit',?,?,?,?)",
                    (order_id, batch_no, result["ref"], str(result), now(), now()))
                did = cur.lastrowid
                for pid in package_ids:
                    con.execute("UPDATE packages SET status='delivering', delivery_id=? WHERE id=?",
                                (did, pid))
                    win = con.execute("SELECT window_id FROM packages WHERE id=?", (pid,)).fetchone()
                    self._advance(con, "windows", win["window_id"], "delivering", WINDOW_TRANSITIONS)
                    for p in con.execute("SELECT id FROM pieces WHERE package_id=?", (pid,)):
                        self._advance(con, "pieces", p["id"], "delivering", PIECE_TRANSITIONS)
                o = con.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
                if o["status"] == "in_service":
                    self._advance_order(con, order_id, "delivering")
                self._order_event(con, order_id, "delivery_out", actor,
                                  f"批次 {batch_no}：{len(package_ids)} 包")
        if failed_reason is not None:
            raise DomainError(f"配送适配器拒单：{failed_reason}")
        return batch_no

    def deliver_batch(self, batch_id: int, adapter, actor=ROLE_STAFF):
        """配送到户，适配器确认。"""
        self._require(actor, {ROLE_STAFF})
        with self.db.tx() as con:
            b = con.execute("SELECT * FROM deliveries WHERE id=?", (batch_id,)).fetchone()
            if b is None:
                raise DomainError("批次不存在", 404)
            if b["status"] != "in_transit":
                raise DomainError("批次不在途")
            result = adapter.confirm(b["batch_no"])
            if not result.get("ok"):
                con.execute("UPDATE deliveries SET status='failed', updated_at=? WHERE id=?",
                            (now(), batch_id))
                raise DomainError(f"配送确认失败：{result.get('reason','')}")
            con.execute("UPDATE deliveries SET status='delivered', result=?, updated_at=? WHERE id=?",
                        (str(result), now(), batch_id))
            for pkg in con.execute("SELECT * FROM packages WHERE delivery_id=?", (batch_id,)):
                con.execute("UPDATE packages SET status='delivered' WHERE id=?", (pkg["id"],))
                self._advance(con, "windows", pkg["window_id"], "reinstalling", WINDOW_TRANSITIONS)
            o = con.execute("SELECT status FROM orders WHERE id=?", (b["order_id"],)).fetchone()
            if o["status"] in ("delivering", "in_service"):
                self._advance_order(con, b["order_id"], "reinstalling")
            self._order_event(con, b["order_id"], "delivered", actor, f"批次 {b['batch_no']} 到户")

    # --- 复装验收 ---

    def scan_reinstall(self, piece_id: int, hooks_returned: int, actor=ROLE_STAFF,
                       photo_path: str | None = None) -> bool:
        """逐片复装扫描。

        并发只成功一次：条件 UPDATE（仅 delivering / reinstalling 可成功），
        rowcount==0 即重复扫描或状态不符。
        """
        self._require(actor, {ROLE_STAFF})
        with self.db.tx() as con:
            p = con.execute("SELECT * FROM pieces WHERE id=?", (piece_id,)).fetchone()
            if p is None:
                raise DomainError("帘片不存在", 404)
            if p["status"] not in ("delivering", "reinstalling"):
                if p["status"] == "reinstalled":
                    raise DomainError(f"{p['code']} 已复装，重复扫描被拒绝")
                raise DomainError(f"{p['code']} 当前「{PIECE_STATUS_CN.get(p['status'])}」，不可复装")
            cur = con.execute(
                "UPDATE pieces SET status='reinstalled', hooks_returned=?,"
                "photo_reinstall=COALESCE(?,photo_reinstall) "
                "WHERE id=? AND status IN ('delivering','reinstalling')",
                (hooks_returned, photo_path, piece_id))
            if cur.rowcount == 0:  # 被并发扫描抢先
                raise DomainError(f"{p['code']} 复装扫描冲突：已被其他工单扫描，本次失败")
            if hooks_returned < p["hooks_expected"]:
                oid = self._order_id_of(con, piece_id)
                self._incident(con, oid, "hooks_missing", actor,
                               f"{p['code']} 复装挂钩 {hooks_returned}，应 {p['hooks_expected']}，"
                               f"缺 {p['hooks_expected']-hooks_returned} 个",
                               piece_id=piece_id)
            self._piece_event(con, piece_id, "reinstalled", actor,
                              f"复装挂钩核对 {hooks_returned}/{p['hooks_expected']}")
            return True

    def flag_uneven(self, piece_id: int, note: str, actor=ROLE_STAFF):
        """复装不平整：留痕，帘片退回待调整，窗位不得验收。"""
        self._require(actor, {ROLE_STAFF, ROLE_CS})
        with self.db.tx() as con:
            p = self._piece_locked(con, piece_id)
            if p["status"] != "reinstalled":
                raise DomainError("仅已复装帘片可登记不平整")
            self._advance(con, "pieces", piece_id, "reinstalling", PIECE_TRANSITIONS)
            oid = self._order_id_of(con, piece_id)
            iid = self._incident(con, oid, "uneven", actor,
                                 f"{p['code']} 复装不平整：{note}", piece_id=piece_id)
            self._piece_event(con, piece_id, "uneven", actor, note)
            return iid

    def accept_window(self, window_id: int, actor=ROLE_STAFF):
        """窗位验收：全部帘片已复装、无未处理的不平整记录；不可重复验收。"""
        self._require(actor, {ROLE_STAFF, ROLE_CS})
        with self.db.tx() as con:
            w = con.execute("SELECT * FROM windows WHERE id=?", (window_id,)).fetchone()
            if w is None:
                raise DomainError("窗位不存在", 404)
            if w["status"] == "accepted":
                raise DomainError("该窗位已验收，禁止重复验收")
            if w["status"] != "reinstalling":
                raise DomainError(f"窗位当前「{WINDOW_STATUS_CN[w['status']]}」，不可验收")
            blockers: list[str] = []
            pending = con.execute(
                "SELECT code FROM pieces WHERE window_id=? AND status!='reinstalled'",
                (window_id,)).fetchall()
            if pending:
                blockers.append("仍有帘片未复装：" + "、".join(r["code"] for r in pending))
            uneven = con.execute(
                "SELECT i.id FROM incidents i JOIN pieces p ON p.id=i.piece_id "
                "WHERE p.window_id=? AND i.type='uneven' AND i.resolved=0",
                (window_id,)).fetchall()
            if uneven:
                blockers.append(f"存在 {len(uneven)} 条未处理的复装不平整记录")
            if blockers:
                raise DomainError("验收被驳回：" + "；".join(blockers))
            self._advance(con, "windows", window_id, "accepted", WINDOW_TRANSITIONS)
            con.execute("UPDATE windows SET accepted_at=? WHERE id=?", (now(), window_id))
            oid = con.execute("SELECT order_id FROM rooms WHERE id=?", (w["room_id"],)).fetchone()["order_id"]
            self._order_event(con, oid, "window_accepted", actor, f"窗位#{window_id} 验收通过")

    def close_order(self, order_id: int, actor=ROLE_CS):
        """所有窗位验收完成 + 费用复核完成，才可关闭订单。"""
        self._require(actor, {ROLE_CS, ROLE_STAFF})
        with self.db.tx() as con:
            o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if o is None:
                raise DomainError("订单不存在", 404)
            if o["status"] == "completed":
                raise DomainError("订单已关闭")
            total_w = con.execute("SELECT COUNT(*) c FROM windows w JOIN rooms r ON r.id=w.room_id "
                                  "WHERE r.order_id=?", (order_id,)).fetchone()["c"]
            done_w = con.execute("SELECT COUNT(*) c FROM windows w JOIN rooms r ON r.id=w.room_id "
                                 "WHERE r.order_id=? AND w.status='accepted'", (order_id,)).fetchone()["c"]
            if total_w == 0 or done_w < total_w:
                raise DomainError(f"仍有窗位未验收（{done_w}/{total_w}），不可关闭订单")
            if not o["fees_reviewed"]:
                raise DomainError("费用尚未复核，不可关闭订单")
            undelivered = con.execute(
                "SELECT COUNT(*) c FROM packages WHERE order_id=? AND status!='delivered'",
                (order_id,)).fetchone()["c"]
            if undelivered:
                raise DomainError("仍有配送包未送达")
            self._advance_order(con, order_id, "completed")
            con.execute("UPDATE orders SET closed_at=? WHERE id=?", (now(), order_id))
            self._order_event(con, order_id, "closed", actor, "全部窗位验收完成，订单关闭")

    # --- 费用 / 付款 ---

    def add_fee(self, order_id: int, item: str, amount: float, kind: str = "charge",
                actor=ROLE_CS):
        self._require(actor, {ROLE_CS, ROLE_STAFF})
        if kind not in ("charge", "discount", "compensation"):
            raise DomainError("费用类型错误")
        with self.db.tx() as con:
            con.execute("INSERT INTO fees(order_id,item,amount,kind) VALUES(?,?,?,?)",
                        (order_id, item, amount, kind))
            con.execute("UPDATE orders SET fees_reviewed=0 WHERE id=?", (order_id,))

    def review_fees(self, order_id: int, actor=ROLE_CS):
        self._require(actor, {ROLE_CS})
        with self.db.tx() as con:
            con.execute("UPDATE orders SET fees_reviewed=1 WHERE id=?", (order_id,))
            self._order_event(con, order_id, "fees_reviewed", actor, "费用复核通过")

    def pay(self, order_id: int, method: str, adapter, actor=ROLE_CS):
        self._require(actor, {ROLE_CS, ROLE_STAFF})
        with self.db.tx() as con:
            o = con.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not o["fees_reviewed"]:
                raise DomainError("费用未复核，不能收款")
            total = con.execute("SELECT COALESCE(SUM(amount),0) s FROM fees WHERE order_id=?",
                                (order_id,)).fetchone()["s"]
            if total <= 0:
                raise DomainError("应付金额为 0，无需支付")
            result = adapter.pay(order_id, total, method)
            # 无论成功失败都落库留痕；失败在事务提交后再抛错
            con.execute(
                "INSERT INTO payments(order_id,amount,method,status,ref,at) VALUES(?,?,?,?,?,?)",
                (order_id, total, method, result["status"], result.get("ref", ""), now()))
            self._order_event(con, order_id, "payment", actor,
                              f"{method} ¥{total:.2f} → {result['status']}")
        if result["status"] != "success":
            raise DomainError(f"支付适配器返回失败：{result.get('reason','')}")
        return result

    # --- 查询 ---

    def get_state(self):
        db = self.db
        orders = [dict(r) for r in db.q("SELECT * FROM orders ORDER BY id")]
        rooms = {o["id"]: [] for o in orders}
        for r in db.q("SELECT * FROM rooms ORDER BY order_id,sort"):
            rooms.setdefault(r["order_id"], []).append(dict(r))
        windows = {}
        pieces = {}
        for w in db.q("SELECT * FROM windows ORDER BY id"):
            windows[w["id"]] = dict(w)
        for p in db.q("SELECT * FROM pieces ORDER BY window_id,code"):
            d = dict(p)
            d["wcode"] = f"W{p['window_id']:04d}"
            pieces.setdefault(p["window_id"], []).append(d)
        bags = [dict(r) for r in db.q("SELECT * FROM bags ORDER BY id")]
        packages = [dict(r) for r in db.q("SELECT * FROM packages ORDER BY id")]
        deliveries = [dict(r) for r in db.q("SELECT * FROM deliveries ORDER BY id")]
        incidents = [dict(r) for r in db.q(
            "SELECT i.*, p.code piece_code FROM incidents i LEFT JOIN pieces p ON p.id=i.piece_id "
            "ORDER BY i.id")]
        repairs = [dict(r) for r in db.q(
            "SELECT rp.*, p.code piece_code FROM repairs rp JOIN pieces p ON p.id=rp.piece_id "
            "ORDER BY rp.id")]
        damages = [dict(r) for r in db.q(
            "SELECT d.*, p.code piece_code FROM damages d JOIN pieces p ON p.id=d.piece_id "
            "ORDER BY d.id")]
        fees = [dict(r) for r in db.q("SELECT * FROM fees ORDER BY id")]
        payments = [dict(r) for r in db.q("SELECT * FROM payments ORDER BY id")]
        reminders = [dict(r) for r in db.q(
            "SELECT * FROM reminders WHERE acked=0 ORDER BY id DESC LIMIT 30")]
        events = [dict(r) for r in db.q(
            "SELECT pe.*, p.code piece_code FROM piece_events pe "
            "JOIN pieces p ON p.id=pe.piece_id ORDER BY pe.id DESC LIMIT 60")]
        order_events = [dict(r) for r in db.q(
            "SELECT * FROM order_events ORDER BY id DESC LIMIT 40")]
        return {
            "orders": orders, "rooms": rooms, "windows": windows, "pieces": pieces,
            "bags": bags, "packages": packages, "deliveries": deliveries,
            "incidents": incidents, "repairs": repairs, "damages": damages,
            "fees": fees, "payments": payments, "reminders": reminders,
            "events": events, "order_events": order_events,
            "labels": {
                "piece": PIECE_STATUS_CN, "window": WINDOW_STATUS_CN,
                "order": ORDER_STATUS_CN, "incident": INCIDENT_TYPES,
                "role": ROLE_NAMES,
            },
        }

    # --- 内部辅助 ---

    def _piece_locked(self, con, piece_id: int):
        p = con.execute("SELECT * FROM pieces WHERE id=?", (piece_id,)).fetchone()
        if p is None:
            raise DomainError("帘片不存在", 404)
        return p

    def _order_id_of(self, con, piece_id: int) -> int:
        return con.execute(
            "SELECT o.id FROM pieces p JOIN windows w ON w.id=p.window_id "
            "JOIN rooms r ON r.id=w.room_id JOIN orders o ON o.id=r.order_id "
            "WHERE p.id=?", (piece_id,)).fetchone()["id"]

    def _advance_order(self, con, order_id: int, target: str):
        self._advance(con, "orders", order_id, target, ORDER_TRANSITIONS)

    def _advance_pkg(self, con, package_id: int, target: str):
        trans = {"packing": {"sealed"}, "sealed": {"delivering"},
                 "delivering": {"delivered"}, "delivered": set()}
        self._advance(con, "packages", package_id, target, trans)

    def _set_window_status(self, con, window_id: int, target: str):
        w = con.execute("SELECT status FROM windows WHERE id=?", (window_id,)).fetchone()
        if target in WINDOW_TRANSITIONS.get(w["status"], set()):
            con.execute("UPDATE windows SET status=? WHERE id=?", (target, window_id))

    def _refresh_window_ready(self, con, piece_id: int):
        p = con.execute("SELECT window_id FROM pieces WHERE id=?", (piece_id,)).fetchone()
        wid = p["window_id"]
        pending = con.execute(
            "SELECT COUNT(*) c FROM pieces WHERE window_id=? AND status NOT IN "
            "('ready','packed','delivering','reinstalled','reinstalling')", (wid,)).fetchone()["c"]
        w = con.execute("SELECT status FROM windows WHERE id=?", (wid,)).fetchone()
        if pending == 0 and w["status"] == "in_wash":
            self._advance(con, "windows", wid, "ready", WINDOW_TRANSITIONS)
