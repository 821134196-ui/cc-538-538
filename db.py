"""SQLite 连接与 schema。所有表结构集中在此，服务层只做业务逻辑。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
PHOTO_DIR = DATA_DIR / "photos"
DEFAULT_DB = DATA_DIR / "trees.db"

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 用户：居民(可认养)、园艺师(做计划/诊断)、主管(复核诊断)
CREATE TABLE IF NOT EXISTS users (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL CHECK (role IN ('resident','gardener','supervisor')),
    created_at  TEXT NOT NULL
);

-- 树木档案：编号、品种、位置(地图坐标 x/y，米)、健康状况、可认养期限
CREATE TABLE IF NOT EXISTS trees (
    id               TEXT PRIMARY KEY,                 -- T001 ...
    species          TEXT NOT NULL,
    x                REAL NOT NULL,
    y                REAL NOT NULL,
    health_status    TEXT NOT NULL DEFAULT 'healthy'
                     CHECK (health_status IN
                           ('healthy','monitor','sick_locked','recovering','transplanted','removed')),
    -- 专业健康结论，仅园艺师可发起变更、主管复核后生效
    adoptable_from   TEXT NOT NULL,                    -- ISO 日期
    adoptable_to     TEXT NOT NULL,
    photo_seed       INTEGER NOT NULL DEFAULT 1,
    notes            TEXT NOT NULL DEFAULT '',
    CHECK (adoptable_to > adoptable_from)
);

-- 移栽历史：预置案例 T004。位置以本表最新一行为准，trees.x/y 同步更新。
CREATE TABLE IF NOT EXISTS transplants (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id     TEXT NOT NULL REFERENCES trees(id),
    old_x       REAL NOT NULL,
    old_y       REAL NOT NULL,
    new_x       REAL NOT NULL,
    new_y       REAL NOT NULL,
    happened_on TEXT NOT NULL,
    reason      TEXT NOT NULL DEFAULT ''
);

-- 认养单：pending(占位,待支付) / active(已支付) / exited(认养人退出) /
--           expired(占位超时释放) / rejected / superseded
CREATE TABLE IF NOT EXISTS adoptions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id       TEXT NOT NULL REFERENCES trees(id),
    user_id       INTEGER NOT NULL REFERENCES users(id),
    care_mode     TEXT NOT NULL CHECK (care_mode IN ('watering','full_care')),
    start_date    TEXT NOT NULL,
    end_date      TEXT NOT NULL,
    status        TEXT NOT NULL,
    hold_expires  TEXT,                    -- pending 占位释放时间；其余为 NULL
    created_at    TEXT NOT NULL,
    paid_at       TEXT,
    exited_at     TEXT,
    CHECK (end_date > start_date)
);
CREATE INDEX IF NOT EXISTS idx_adopt_tree_dates ON adoptions(tree_id, start_date, end_date);
CREATE INDEX IF NOT EXISTS idx_adopt_user ON adoptions(user_id);
CREATE INDEX IF NOT EXISTS idx_adopt_status ON adoptions(status, hold_expires);

-- 模拟支付适配器流水
CREATE TABLE IF NOT EXISTS payments (
    id            TEXT PRIMARY KEY,
    adoption_id   INTEGER NOT NULL REFERENCES adoptions(id),
    amount        INTEGER NOT NULL,            -- 分
    status        TEXT NOT NULL DEFAULT 'paid' CHECK (status IN ('paid','refunded')),
    created_at    TEXT NOT NULL
);

-- 养护任务（浇水/修剪/病虫观察），状态随执行与事件流转
CREATE TABLE IF NOT EXISTS care_tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id       TEXT NOT NULL REFERENCES trees(id),
    adoption_id   INTEGER REFERENCES adoptions(id),
    kind          TEXT NOT NULL CHECK (kind IN ('water','prune','pest_check')),
    due_date      TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'scheduled'
                  CHECK (status IN ('scheduled','done','overdue','cancelled')),
    assignee      TEXT NOT NULL DEFAULT 'adopter'
                  CHECK (assignee IN ('adopter','gardener')),
    done_date     TEXT,
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_tree_due ON care_tasks(tree_id, due_date);
CREATE INDEX IF NOT EXISTS idx_task_status ON care_tasks(status, due_date);

-- 巡查照片，本地文件存储（data/photos/<id>.png）
CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id     TEXT NOT NULL REFERENCES trees(id),
    uploader_id INTEGER NOT NULL REFERENCES users(id),
    filename    TEXT NOT NULL,
    caption     TEXT NOT NULL DEFAULT '',
    taken_on    TEXT NOT NULL,
    brightness  REAL NOT NULL,                  -- 0..1，前端亮度校验后回传
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_photo_tree ON photos(tree_id, taken_on);

-- 异常报告：居民"误报枯萎"进入此处(source=resident_report)，需园艺师核实；
-- 园艺师提交的异常诊断(diagnosis)必须由主管复核(approve/reject)后才改健康状态。
CREATE TABLE IF NOT EXISTS anomaly_reports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id       TEXT NOT NULL REFERENCES trees(id),
    reporter_id   INTEGER NOT NULL REFERENCES users(id),
    kind          TEXT NOT NULL DEFAULT 'other'
                  CHECK (kind IN ('wilt','pest','disease','branch_damage','transplant_shock','other')),
    source        TEXT NOT NULL CHECK (source IN ('resident_report','gardener_diagnosis')),
    detail        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open','needs_review','approved','rejected','false_alarm')),
    diagnosis     TEXT NOT NULL DEFAULT '',      -- 园艺师核实/诊断意见
    gardener_id   INTEGER REFERENCES users(id),
    review_note   TEXT NOT NULL DEFAULT '',
    reviewer_id   INTEGER REFERENCES users(id),
    proposed_health TEXT,                        -- 诊断拟变更的健康状态
    created_at    TEXT NOT NULL,
    reviewed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_anomaly_status ON anomaly_reports(status);

-- 树木事件流：移栽/封控/退出/逾期/误报，供详情时间线与计划重算使用
CREATE TABLE IF NOT EXISTS tree_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tree_id     TEXT NOT NULL REFERENCES trees(id),
    type        TEXT NOT NULL CHECK (type IN
                ('transplant','lockdown','reopen','exit','overdue',
                 'false_report','diagnosis_approved','diagnosis_rejected','plan_update')),
    detail      TEXT NOT NULL DEFAULT '',
    actor_id    INTEGER REFERENCES users(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_tree ON tree_events(tree_id, created_at);

-- 模拟通知适配器的收件箱
CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id),
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    channel     TEXT NOT NULL DEFAULT 'sms',
    status      TEXT NOT NULL DEFAULT 'queued',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, id);

-- 年度反馈：认养人可查看的真实执行汇总
CREATE TABLE IF NOT EXISTS annual_reports (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    adoption_id  INTEGER NOT NULL REFERENCES adoptions(id),
    tree_id      TEXT NOT NULL REFERENCES trees(id),
    year         INTEGER NOT NULL,
    water_done   INTEGER NOT NULL DEFAULT 0,
    water_total  INTEGER NOT NULL DEFAULT 0,
    prune_done   INTEGER NOT NULL DEFAULT 0,
    prune_total  INTEGER NOT NULL DEFAULT 0,
    pest_done    INTEGER NOT NULL DEFAULT 0,
    pest_total   INTEGER NOT NULL DEFAULT 0,
    overdue_cnt  INTEGER NOT NULL DEFAULT 0,
    false_alarm_cnt INTEGER NOT NULL DEFAULT 0,
    summary      TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    UNIQUE(adoption_id, year)
);
"""


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    PHOTO_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=15, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 15000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
