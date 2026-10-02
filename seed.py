"""预置演示数据：一条命令启动后即有移栽案例、待复核异常、逾期任务等完整场景。"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

import services
from db import PHOTO_DIR, connect, init_db
from imaging import generate_tree_photo

# (编号, 品种, x, y, 健康, 说明)
TREES = [
    ("T001", "香樟",   38, 42,  "healthy",     "园龄 32 年，冠幅饱满，是居民最常认养的老树。"),
    ("T002", "银杏",   70, 30,  "healthy",     "秋季叶色金黄，步道东段的标志性树木。"),
    ("T003", "悬铃木", 122, 44, "healthy",     "主干道行道树，遮阴良好；近期有虫食叶迹象待复核。"),
    ("T004", "紫薇",   96, 104, "transplanted","因北门施工于本月移栽至阳光草坪南侧，复壮观察中。"),
    ("T005", "樱花",   160, 36, "healthy",     "春季观花树种，花后需修剪控形。"),
    ("T006", "雪松",   44, 104, "recovering",  "夏初根系积水黄化，经复核进入复壮养护。"),
    ("T007", "桂花",   150, 78, "monitor",     "有居民反映疑似枯萎，待园艺师现场核实。"),
    ("T008", "红枫",   178, 110, "sick_locked","确诊炭疽病，已封控并冻结养护计划。"),
]


def _photo(conn, tree_id: str, uploader: int, seed: int, taken_on: date,
           caption: str) -> None:
    fname = f"{tree_id}-{seed}.png"
    path = PHOTO_DIR / fname
    brightness = generate_tree_photo(path, seed, caption)
    conn.execute(
        "INSERT INTO photos(tree_id,uploader_id,filename,caption,taken_on,brightness,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (tree_id, uploader, fname, caption, taken_on.isoformat(),
         round(brightness, 3), datetime.now().strftime("%Y-%m-%dT%H:%M:%S")))


def seed(conn: sqlite3.Connection) -> None:
    init_db(conn)
    today = date.today()
    jan1 = date(today.year, 1, 1)
    dec31 = date(today.year, 12, 31)
    next_dec = date(today.year + 1, 12, 31)
    now = datetime.now()
    iso = now.strftime("%Y-%m-%dT%H:%M:%S")

    # ---- 用户
    users = [
        (1, "李阿姨", "resident"),
        (2, "王居民", "resident"),
        (3, "周同学", "resident"),
        (10, "陈园艺", "gardener"),
        (20, "赵主管", "supervisor"),
    ]
    for uid, name, role in users:
        conn.execute("INSERT INTO users(id,name,role,created_at) VALUES (?,?,?,?)",
                     (uid, name, role, iso))

    # ---- 树木
    for tid, species, x, y, health, notes in TREES:
        conn.execute(
            "INSERT INTO trees(id,species,x,y,health_status,adoptable_from,adoptable_to,"
            "photo_seed,notes) VALUES (?,?,?,?,?,?,?,?,?)",
            (tid, species, x, y, health, jan1.isoformat(), next_dec.isoformat(),
             int(tid[1:]) * 7 + 3, notes))

    # ---- 认养与计划
    def paid_adoption(uid, tid, start, end, mode, created: datetime):
        cur = conn.execute(
            "INSERT INTO adoptions(tree_id,user_id,care_mode,start_date,end_date,status,"
            "hold_expires,created_at,paid_at) VALUES (?,?,?,?,?,'active',NULL,?,?)",
            (tid, uid, mode, start.isoformat(), end.isoformat(),
             created.strftime("%Y-%m-%dT%H:%M:%S"), created.strftime("%Y-%m-%dT%H:%M:%S")))
        aid = cur.lastrowid
        months = max(1, round((end - start).days / 30))
        conn.execute(
            "INSERT INTO payments(id,adoption_id,amount,status,created_at) VALUES (?,?,?,'paid',?)",
            (f"PAY-SEED{aid:04d}", aid, months * services.FEE_PER_MONTH_CENTS, iso))
        row = conn.execute("SELECT * FROM adoptions WHERE id=?", (aid,)).fetchone()
        services._generate_plan(conn, row, iso)
        return aid

    aid1 = paid_adoption(1, "T001", jan1, dec31, "full_care",
                         datetime(today.year, 1, 2, 9, 0))
    paid_adoption(1, "T004", date(today.year, 3, 1),
                  date(today.year + 1, 2, 28), "watering",
                  datetime(today.year, 2, 20, 10, 0))
    paid_adoption(2, "T005", date(today.year, 5, 1),
                  date(today.year, 11, 30), "watering",
                  datetime(today.year, 4, 25, 14, 0))

    # T001：历史任务已如实完成（个别延迟补做）；并保证存在一项 5 天前到期未执行的任务，
    # 启动清扫时自动转为"逾期"，演示逾期更新计划与通知。
    for t in conn.execute(
            "SELECT * FROM care_tasks WHERE adoption_id=? ORDER BY due_date", (aid1,)):
        d = date.fromisoformat(t["due_date"])
        if d <= today - timedelta(days=6) and t["status"] == "scheduled":
            late = d.weekday() == 4
            conn.execute(
                "UPDATE care_tasks SET status='done', done_date=?, note=? WHERE id=?",
                ((d + timedelta(days=2) if late else d).isoformat(),
                 "雨后补做（延迟）" if late else "按计划执行，生长正常", t["id"]))
    conn.execute(
        "INSERT INTO care_tasks(tree_id,adoption_id,kind,due_date,status,assignee,note,"
        "created_at) VALUES (?,?,'water',?,'scheduled','adopter','追加的夏季补水',?)",
        ("T001", aid1, (today - timedelta(days=5)).isoformat(), iso))

    # ---- T004 移栽案例（发生在 20 天前）：走完整业务流程，自动重算计划
    services.transplant_tree(
        conn, "T004", 10, 150, 96, "北门地下管网施工，避让根系保护范围",
        happened_on=(today - timedelta(days=20)).isoformat(),
        now=datetime.combine(today - timedelta(days=20), datetime.min.time()).replace(hour=9))
    # 第 7 天的复壮观察已由园艺师完成；第 21 天的观察（明天）仍待执行
    conn.execute(
        "UPDATE care_tasks SET status='done', done_date=due_date, "
        "note=note||'（已完成：新芽萌动，定植稳固）' "
        "WHERE tree_id='T004' AND note='移栽后复壮观察' AND due_date < ?",
        (today.isoformat(),))

    # ---- T006：早先获批的诊断 → 复壮中（展示已审批闭环）
    r = conn.execute(
        "INSERT INTO anomaly_reports(tree_id,reporter_id,kind,source,detail,status,diagnosis,"
        "gardener_id,proposed_health,review_note,reviewer_id,created_at,reviewed_at) "
        "VALUES (?,1,'disease','gardener_diagnosis','夏初连续降雨后下部枝叶黄化',"
        "'approved','排水不畅导致轻度根腐，建议松土透气+减少浇水频次',10,'recovering',"
        "'现场复核属实，同意复壮方案',20,?,?)",
        ("T006",
         datetime(today.year, 6, 3, 10, 0).strftime("%Y-%m-%dT%H:%M:%S"),
         datetime(today.year, 6, 4, 9, 30).strftime("%Y-%m-%dT%H:%M:%S"))).lastrowid
    conn.execute("UPDATE trees SET health_status='recovering' WHERE id='T006'")

    # ---- T008：确诊封控（已审批）
    conn.execute(
        "INSERT INTO anomaly_reports(tree_id,reporter_id,kind,source,detail,status,diagnosis,"
        "gardener_id,proposed_health,review_note,reviewer_id,created_at,reviewed_at) "
        "VALUES ('T008',2,'disease','resident_report','叶背出现褐色斑点并快速扩大',"
        "'approved','炭疽病确诊，需封控喷施药剂并清除病叶',10,'sick_locked',"
        "'同意封控，做好周边提示',20,?,?)",
        (datetime(today.year, 8, 12, 8, 0).strftime("%Y-%m-%dT%H:%M:%S"),
         datetime(today.year, 8, 13, 10, 0).strftime("%Y-%m-%dT%H:%M:%S")))
    conn.execute("UPDATE trees SET health_status='sick_locked' WHERE id='T008'")
    conn.execute("DELETE FROM care_tasks WHERE tree_id='T008' AND status='scheduled'")

    # ---- T003：园艺师诊断待主管复核（拟改"观察中"，不封控）
    conn.execute(
        "INSERT INTO anomaly_reports(tree_id,reporter_id,kind,source,detail,status,diagnosis,"
        "gardener_id,proposed_health,created_at) VALUES "
        "('T003',3,'pest','gardener_diagnosis','叶片出现不规则缺刻，查见少量刺蛾幼虫',"
        "'needs_review','轻度刺蛾危害，建议人工摘除+生物药剂，暂无封控必要',10,'monitor',?)",
        (iso,))

    # ---- T007：居民上报"枯萎"，尚未核实（留给园艺师演示；另一单历史误报在 T001）
    conn.execute(
        "INSERT INTO anomaly_reports(tree_id,reporter_id,kind,source,detail,status,created_at)"
        " VALUES ('T007',2,'wilt','resident_report','周末路过看到叶子发蔫下垂，怀疑枯萎', 'open', ?)",
        (iso,))
    # T001 历史误报 → false_alarm + 复查任务
    fa = conn.execute(
        "INSERT INTO anomaly_reports(tree_id,reporter_id,kind,source,detail,status,diagnosis,"
        "gardener_id,reviewed_at,created_at) VALUES "
        "('T001',2,'wilt','resident_report','盛夏午后枝叶萎蔫','false_alarm',"
        "'高温生理性蒸腾，非枯萎；傍晚已恢复挺立',10,?,?)",
        (iso, (now - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%S"))).lastrowid
    conn.execute(
        "INSERT INTO care_tasks(tree_id,kind,due_date,status,assignee,note,created_at) "
        "VALUES ('T001','pest_check',?,'done','gardener','误报后的现场复查（无异常）',?)",
        ((today - timedelta(days=33)).isoformat(), iso))
    conn.execute("UPDATE care_tasks SET done_date=due_date WHERE tree_id='T001' "
                 "AND note='误报后的现场复查（无异常）' AND status='done'")

    # ---- 名额场景：王居民对 T002 的 10 分钟占位（未支付，等待到期释放演示）；
    #      另有一条已过期的历史占位
    conn.execute(
        "INSERT INTO adoptions(tree_id,user_id,care_mode,start_date,end_date,status,"
        "hold_expires,created_at) VALUES ('T002',2,'watering',?,?, 'pending', ?, ?)",
        ((today + timedelta(days=5)).isoformat(),
         (today + timedelta(days=95)).isoformat(),
         (now + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S"), iso))
    conn.execute(
        "INSERT INTO adoptions(tree_id,user_id,care_mode,start_date,end_date,status,"
        "hold_expires,created_at) VALUES ('T003',3,'full_care',?,?, 'expired', NULL, ?)",
        ((today - timedelta(days=30)).isoformat(),
         (today + timedelta(days=60)).isoformat(),
         (now - timedelta(days=45)).strftime("%Y-%m-%dT%H:%M:%S")))

    # ---- 巡查照片（本地 PNG，亮度充足）
    _photo(conn, "T001", 10, 11, today - timedelta(days=60), "春季例行巡查")
    _photo(conn, "T001", 1, 12, today - timedelta(days=20), "认养人浇水后留影")
    _photo(conn, "T004", 10, 41, today - timedelta(days=21), "移栽前原址（北门施工区）")
    _photo(conn, "T004", 10, 42, today - timedelta(days=18), "移栽定植后复壮观察")
    _photo(conn, "T004", 1, 43, today - timedelta(days=6), "复壮期新芽（认养人摄）")
    _photo(conn, "T005", 2, 51, today - timedelta(days=10), "花后修剪前")
    _photo(conn, "T006", 10, 61, today - timedelta(days=12), "复壮养护：松土透气")
    _photo(conn, "T007", 2, 71, today - timedelta(days=1), "居民反映萎蔫时拍摄")
    _photo(conn, "T008", 10, 81, today - timedelta(days=8), "病叶特写，封控前")

    # ---- 年度反馈（截至当前的年度执行汇总）
    services.generate_annual_report(conn, aid1, today.year, viewer_id=1)


def is_seeded(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trees'").fetchone() \
        and conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"] > 0


def reset_and_seed(db_path: Path) -> None:
    """删除库文件与照片后重新预置（--reset 使用）。"""
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    for p in PHOTO_DIR.glob("*.png"):
        p.unlink()
    conn = connect(db_path)
    try:
        seed(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    from db import DEFAULT_DB
    if "--reset" in sys.argv:
        reset_and_seed(DEFAULT_DB)
        print("已重置并预置演示数据（含 T004 移栽案例）")
    else:
        c = connect(DEFAULT_DB)
        init_db(c)
        if not is_seeded(c):
            seed(c)
            print("演示数据已预置（含 T004 移栽案例）")
        else:
            print("数据已存在，跳过预置（如需重置：python3 seed.py --reset）")
        c.close()
