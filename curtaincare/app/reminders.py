"""进程内任务提醒：后台线程轮询 SQLite，对洗护时限、修补时限与复装预约到期发提醒。

提醒去重键 rkey 写入 reminders 表，重启后不会重复打扰；可在界面确认(ack)。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from .db import Database

WASH_SLA_HOURS = 2.0      # 到厂后 2 小时须进入洗护
REPAIR_SLA_HOURS = 1.0    # 修补单 1 小时须完工
APPT_WINDOW_HOURS = 24.0  # 复装预约前 24 小时开始提醒


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


class ReminderEngine:
    def __init__(self, db: Database, interval: float = 20.0):
        self.db = db
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def scan_once(self) -> list[str]:
        """执行一轮检查，返回新生成的提醒消息。"""
        fired: list[str] = []
        nowdt = datetime.now(timezone.utc)
        with self.db.tx() as con:
            # 1) 洗护时限：到厂/洗护中超过 SLA
            rows = con.execute(
                "SELECT p.id pid, p.code, p.status, "
                "(SELECT MAX(pe.at) FROM piece_events pe WHERE pe.piece_id=p.id) last_at "
                "FROM pieces p WHERE p.status IN ('at_plant','in_wash')").fetchall()
            for r in rows:
                age = (nowdt - _parse(r["last_at"])).total_seconds() / 3600
                if age >= WASH_SLA_HOURS:
                    msg = f"洗护时限：{r['code']} 已在「{'到厂待洗' if r['status']=='at_plant' else '洗护中'}」{age:.1f} 小时，超过 {WASH_SLA_HOURS:.0f} 小时 SLA"
                    if self._emit(con, f"wash_sla:{r['pid']}", "wash_sla", msg):
                        fired.append(msg)

            # 2) 修补时限
            rows = con.execute(
                "SELECT rp.id rid, p.code, rp.created_at FROM repairs rp "
                "JOIN pieces p ON p.id=rp.piece_id WHERE rp.status='open'").fetchall()
            for r in rows:
                age = (nowdt - _parse(r["created_at"])).total_seconds() / 3600
                if age >= REPAIR_SLA_HOURS:
                    msg = f"修补时限：{r['code']} 的修补单 #{r['rid']} 已开工 {age:.1f} 小时，请尽快完工"
                    if self._emit(con, f"repair_sla:{r['rid']}", "repair_sla", msg):
                        fired.append(msg)

            # 3) 复装预约时限：24 小时内或已逾期，且未关闭
            rows = con.execute(
                "SELECT id, code, address_alias, appointment_at, status FROM orders "
                "WHERE appointment_at IS NOT NULL AND status!='completed'").fetchall()
            for r in rows:
                appt = _parse(r["appointment_at"])
                delta = (appt - nowdt).total_seconds() / 3600
                if delta <= APPT_WINDOW_HOURS:
                    when = f"还剩 {delta:.1f} 小时" if delta >= 0 else f"已逾期 {-delta:.1f} 小时"
                    msg = f"复装预约：订单 {r['code']}（{r['address_alias']}）{when}到达预约时间"
                    if self._emit(con, f"appointment:{r['id']}", "appointment", msg):
                        fired.append(msg)
        return fired

    def _emit(self, con, rkey: str, kind: str, message: str) -> bool:
        exists = con.execute("SELECT 1 FROM reminders WHERE rkey=?", (rkey,)).fetchone()
        if exists:
            return False
        con.execute(
            "INSERT OR IGNORE INTO reminders(rkey,kind,message,fired_at) VALUES(?,?,?,?)",
            (rkey, kind, message, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        return True

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.scan_once()
            except Exception:
                # 提醒线程不能因单轮错误退出
                pass
