"""数据库连接与建表。SQLite 单文件，外键开启，WAL 以减少读写互斥。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('resident','gardener','supervisor','admin'))
);

CREATE TABLE IF NOT EXISTS trees (
    id              INTEGER PRIMARY KEY,
    code            TEXT NOT NULL UNIQUE,          -- 编号
    species         TEXT NOT NULL,                 -- 品种
    location        TEXT NOT NULL,                 -- 位置（文字）
    x               REAL NOT NULL,                 -- 地图坐标（树冠中心点）
    y               REAL NOT NULL,
    health_status   TEXT NOT NULL DEFAULT '健康'   -- 专业健康结论，仅园艺师可提案、主管复核后生效
                    CHECK (health_status IN ('健康','观察','患病封控','移栽中')),
    adoptable       INTEGER NOT NULL DEFAULT 1,    -- 是否在可认养开放名单
    photo_path      TEXT,
    note            TEXT NOT NULL DEFAULT ''
);

-- 认养：同一棵树的认养区间不得重叠，由服务层事务保证。
CREATE TABLE IF NOT EXISTS adoptions (
    id              INTEGER PRIMARY KEY,
    tree_id         INTEGER NOT NULL REFERENCES trees(id),
    user_id         INTEGER NOT NULL REFERENCES users(id),
    start_date      TEXT NOT NULL,
    end_date        TEXT NOT NULL,
    care_mode       TEXT NOT NULL CHECK (care_mode IN ('自助养护','委托园艺师')),
    status          TEXT NOT NULL
                    CHECK (status IN ('pending_payment','active','exited','rejected','expired')),
    created_at      TEXT NOT NULL,
    paid_at         TEXT,
    released_at     TEXT,
    fee_cents       INTEGER NOT NULL DEFAULT 0,
    exit_reason     TEXT,
    CHECK (end_date > start_date)
);
CREATE INDEX IF NOT EXISTS idx_adoptions_tree_dates ON adoptions(tree_id, start_date, end_date);

-- 园艺师养护计划与真实执行记录（浇水 / 修剪 / 病虫观察）。
CREATE TABLE IF NOT EXISTS care_tasks (
    id          INTEGER PRIMARY KEY,
    tree_id     INTEGER NOT NULL REFERENCES trees(id),
    adoption_id INTEGER REFERENCES adoptions(id),
    kind        TEXT NOT NULL CHECK (kind IN ('浇水','修剪','病虫观察')),
    planned_date TEXT NOT NULL,
    season      TEXT NOT NULL CHECK (season IN ('春','夏','秋','冬')),
    status      TEXT NOT NULL DEFAULT '待执行'
                CHECK (status IN ('待执行','已完成','已逾期','已取消')),
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_care_tree_date ON care_tasks(tree_id, planned_date);

-- 执行日志（真实执行记录），可附巡查照片。
CREATE TABLE IF NOT EXISTS care_logs (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES care_tasks(id),
    tree_id     INTEGER NOT NULL REFERENCES trees(id),
    executed_on TEXT NOT NULL,
    actor_id    INTEGER NOT NULL REFERENCES users(id),
    summary     TEXT NOT NULL,
    photo_path  TEXT,
    created_at  TEXT NOT NULL
);

-- 巡查照片（独立于任务也可上传，例如居民/园艺师巡查）。
CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    tree_id     INTEGER NOT NULL REFERENCES trees(id),
    uploader_id INTEGER NOT NULL REFERENCES users(id),
    taken_on    TEXT NOT NULL,
    path        TEXT NOT NULL,
    caption     TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- 异常诊断：园艺师提交，主管复核；居民的“枯萎误报”走 reporter 角色区分。
CREATE TABLE IF NOT EXISTS diagnoses (
    id           INTEGER PRIMARY KEY,
    tree_id      INTEGER NOT NULL REFERENCES trees(id),
    reporter_id  INTEGER NOT NULL REFERENCES users(id),
    reporter_kind TEXT NOT NULL CHECK (reporter_kind IN ('gardener','resident')),
    kind         TEXT NOT NULL CHECK (kind IN ('病害','虫害','枯萎误报','移栽','其他')),
    detail       TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT '待复核'
                 CHECK (status IN ('待复核','已确认','已驳回')),
    proposed_health TEXT,        -- 若确认，拟更新的专业健康结论
    reviewer_id  INTEGER REFERENCES users(id),
    review_note  TEXT,
    created_at   TEXT NOT NULL,
    reviewed_at  TEXT
);

-- 树木事件流水：移栽 / 封控 / 退出 / 逾期 / 误报 …，驱动计划更新与时间线。
CREATE TABLE IF NOT EXISTS tree_events (
    id          INTEGER PRIMARY KEY,
    tree_id     INTEGER NOT NULL REFERENCES trees(id),
    event_type  TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',
    actor_id    INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_tree ON tree_events(tree_id, created_at);

-- 年度反馈（按树 × 自然年），认养人可查看。
CREATE TABLE IF NOT EXISTS annual_reports (
    id          INTEGER PRIMARY KEY,
    tree_id     INTEGER NOT NULL REFERENCES trees(id),
    year        INTEGER NOT NULL,
    summary     TEXT NOT NULL,
    watering    INTEGER NOT NULL DEFAULT 0,
    pruning     INTEGER NOT NULL DEFAULT 0,
    pest        INTEGER NOT NULL DEFAULT 0,
    alerts      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE(tree_id, year)
);

-- 模拟支付流水。
CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY,
    adoption_id INTEGER NOT NULL REFERENCES adoptions(id),
    amount_cents INTEGER NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('paid','refunded')),
    created_at  TEXT NOT NULL
);

-- 模拟通知收件箱。
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    read        INTEGER NOT NULL DEFAULT 0
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
