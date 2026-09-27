"""SQLite 存储层：连接管理与建表。所有状态唯一性约束都落在数据库层。"""
import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    phone TEXT NOT NULL,
    address_code TEXT NOT NULL,
    address TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'created',
    appoint_at TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    note TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    name TEXT NOT NULL,
    UNIQUE(order_id, name)
);

CREATE TABLE IF NOT EXISTS windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    code TEXT NOT NULL,
    UNIQUE(room_id, code)
);

CREATE TABLE IF NOT EXISTS panels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    window_id INTEGER NOT NULL REFERENCES windows(id),
    code TEXT NOT NULL UNIQUE,
    fabric TEXT NOT NULL,
    hook_count INTEGER NOT NULL,
    care_req TEXT DEFAULT '',
    wash_forbidden INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'registered',
    bag_id INTEGER,
    package_id INTEGER,
    wash_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    window_id INTEGER NOT NULL REFERENCES windows(id),
    seal_no TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'sealed',
    sealed_at TEXT,
    shipped_at TEXT,
    arrived_at TEXT
);

CREATE TABLE IF NOT EXISTS wash_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id),
    status TEXT NOT NULL DEFAULT 'queued',
    queued_at TEXT NOT NULL,
    started_at TEXT,
    dried_at TEXT,
    due_at TEXT
);

CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    panel_id INTEGER REFERENCES panels(id),
    type TEXT NOT NULL,
    detail TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_by TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS repairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id),
    round INTEGER NOT NULL,
    description TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    done_at TEXT,
    UNIQUE(panel_id, round)
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(id),
    amount REAL NOT NULL,
    reason TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    proposed_by TEXT DEFAULT '',
    decided_by TEXT,
    decided_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    window_id INTEGER NOT NULL REFERENCES windows(id),
    seal_no TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'packing',
    packed_at TEXT,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL DEFAULT 'planned',
    vehicle TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    delivered_at TEXT
);

CREATE TABLE IF NOT EXISTS batch_packages (
    batch_id INTEGER NOT NULL REFERENCES batches(id),
    package_id INTEGER NOT NULL REFERENCES packages(id),
    PRIMARY KEY (batch_id, package_id)
);

CREATE TABLE IF NOT EXISTS reinstalls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id INTEGER NOT NULL REFERENCES panels(id),
    package_id INTEGER NOT NULL REFERENCES packages(id),
    scanned_at TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0,
    accepted_at TEXT,
    result TEXT DEFAULT '',
    note TEXT DEFAULT ''
);

-- 同一帘片只可能有一次验收通过的复装记录（并发扫描只成功一次）
CREATE UNIQUE INDEX IF NOT EXISTS uq_reinstall_accepted
    ON reinstalls(panel_id) WHERE accepted = 1;

CREATE TABLE IF NOT EXISTS fee_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    kind TEXT NOT NULL,
    label TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id),
    amount REAL NOT NULL,
    channel TEXT DEFAULT 'mockpay',
    status TEXT NOT NULL,
    txn_no TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    ref_type TEXT NOT NULL,
    ref_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    fired_at TEXT NOT NULL
);
"""


def connect(db_path):
    """打开（必要时创建）数据库并保证表结构存在。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def reset(db_path):
    """删除数据库文件，返回全新连接。"""
    if os.path.exists(db_path):
        os.remove(db_path)
    return connect(db_path)
