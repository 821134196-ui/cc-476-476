"""SQLite 存储层：schema、连接与事务助手。所有帘片链路数据保存在 SQLite。"""
from __future__ import annotations

import sqlite3
import threading

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    customer_name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address_alias TEXT NOT NULL,          -- 住址代号
    address_detail TEXT NOT NULL,
    appointment_at TEXT,                  -- 复装预约时间 ISO
    status TEXT NOT NULL DEFAULT 'created',
    postponed INTEGER NOT NULL DEFAULT 0,
    fees_reviewed INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    name TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    UNIQUE(order_id, name)
);

CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    label TEXT NOT NULL,
    sort INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending/in_progress/reinstalling/accepted
    accepted_at TEXT,
    UNIQUE(room_id, label)
);

CREATE TABLE IF NOT EXISTS bags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    window_id INTEGER NOT NULL REFERENCES windows(id),
    seal_no TEXT UNIQUE NOT NULL,             -- 袋封签号（全局唯一）
    status TEXT NOT NULL DEFAULT 'sealed',    -- sealed/in_transit/received
    sealed_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    window_id INTEGER NOT NULL REFERENCES windows(id),
    seal_no TEXT UNIQUE NOT NULL,             -- 包封签号（全局唯一）
    status TEXT NOT NULL DEFAULT 'packing',   -- packing/sealed/delivering/delivered
    packed_at TEXT,
    delivery_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pieces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    code TEXT UNIQUE NOT NULL,                -- 帘片编号（全局唯一）
    fabric TEXT NOT NULL,                     -- 布料
    washable INTEGER NOT NULL DEFAULT 1,      -- 0 = 布料禁洗
    wash_req TEXT NOT NULL DEFAULT '',        -- 洗护要求
    hooks_expected INTEGER NOT NULL DEFAULT 0,
    hooks_counted INTEGER,                    -- 拆卸清点挂钩数
    hooks_returned INTEGER,                   -- 复装核对挂钩数
    status TEXT NOT NULL DEFAULT 'registered',
    bag_id INTEGER REFERENCES bags(id),       -- 单列外键：一片帘结构上只能入一袋
    package_id INTEGER REFERENCES packages(id),-- 单列外键：一片帘结构上只能入一个配送包
    photo_before TEXT,
    photo_after_wash TEXT,
    photo_reinstall TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    batch_no TEXT UNIQUE NOT NULL,            -- 配送批次号
    status TEXT NOT NULL DEFAULT 'scheduled', -- scheduled/in_transit/delivered/failed
    adapter_ref TEXT,
    result TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS piece_events (     -- 帘片全链路留痕
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id INTEGER NOT NULL REFERENCES pieces(id),
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS order_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS incidents (        -- 异常留痕
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    piece_id INTEGER REFERENCES pieces(id),
    type TEXT NOT NULL,  -- misbag串包/shrinkage缩水/dye染色/hooks_missing挂钩缺失/
                         -- banned_wash布料禁洗/delay客户延期/second_repair二次修补/uneven复装不平整
    note TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    resolved_note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS damages (          -- 洗护人员登记的损伤
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id INTEGER NOT NULL REFERENCES pieces(id),
    description TEXT NOT NULL,
    registered_by TEXT NOT NULL,
    at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'    -- pending/compensated/rework
);

CREATE TABLE IF NOT EXISTS repairs (          -- 修补记录（round>=2 即二次修补）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    piece_id INTEGER NOT NULL REFERENCES pieces(id),
    round INTEGER NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',      -- open/done
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS compensations (    -- 赔付：仅客服主管可批准
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    damage_id INTEGER NOT NULL REFERENCES damages(id),
    amount REAL NOT NULL,
    decided_by TEXT NOT NULL,
    decision TEXT NOT NULL,                   -- approved/rejected
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fees (             -- 费用复核
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    item TEXT NOT NULL,
    amount REAL NOT NULL,
    kind TEXT NOT NULL DEFAULT 'charge'       -- charge/discount/compensation
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount REAL NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL,                     -- success/failed
    ref TEXT,
    at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (        -- 进程内提醒的持久化（去重键）
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rkey TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,                       -- appointment/wash_sla/repair_sla
    message TEXT NOT NULL,
    fired_at TEXT NOT NULL,
    acked INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pieces_window ON pieces(window_id);
CREATE INDEX IF NOT EXISTS idx_events_piece ON piece_events(piece_id);
CREATE INDEX IF NOT EXISTS idx_incidents_order ON incidents(order_id);
"""


class Database:
    """线程安全的 SQLite 访问：每线程一条连接，提供即时事务。"""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self.init()

    def connect(self) -> sqlite3.Connection:
        con = getattr(self._local, "con", None)
        if con is None:
            con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys = ON")
            con.execute("PRAGMA busy_timeout = 30000")
            self._local.con = con
        return con

    def init(self) -> None:
        con = sqlite3.connect(self.path)
        try:
            con.executescript(SCHEMA)
            con.commit()
        finally:
            con.close()

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        return self.connect().execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple = ()) -> sqlite3.Row | None:
        return self.connect().execute(sql, args).fetchone()

    def tx(self):
        """即时事务上下文管理器（写操作串行化）。"""
        db = self

        class _Tx:
            def __enter__(self):
                db._write_lock.acquire()
                self.con = db.connect()
                self.con.execute("BEGIN IMMEDIATE")
                return self.con

            def __exit__(self, exc_type, exc, tb):
                try:
                    if exc_type is None:
                        self.con.execute("COMMIT")
                    else:
                        self.con.execute("ROLLBACK")
                finally:
                    db._write_lock.release()
                return False

        return _Tx()
