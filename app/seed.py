"""预置演示数据：用户、树木地图、认养、养护记录、照片、异常复核与移栽案例。

用法：python -m app.seed [--db data/park.db] [--photos data/photos]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from .adapters import Clock
from .database import connect, init_db
from .services import Service, season_of

# 白色底 + 森林绿树冠 + 少量花色点缀，亮度保持明快可辨识。
PHOTO_SVG = """<svg xmlns="http://www.w3.org/2000/svg" width="640" height="420" viewBox="0 0 640 420">
<rect width="640" height="420" fill="#f4f8f2"/>
<rect y="300" width="640" height="120" fill="#dfead7"/>
<path d="M0 320 Q160 290 320 320 T640 318 V420 H0 Z" fill="#cfe0c2"/>
<rect x="298" y="250" width="44" height="90" rx="8" fill="#8a6a45"/>
<circle cx="320" cy="190" r="105" fill="#2e6b4f"/>
<circle cx="250" cy="160" r="62" fill="#3d7d5c"/>
<circle cx="392" cy="168" r="58" fill="#3d7d5c"/>
<circle cx="320" cy="120" r="55" fill="#4d8f6b"/>
<circle cx="{fx}" cy="{fy}" r="14" fill="#e58fb1"/>
<circle cx="{fx2}" cy="{fy2}" r="10" fill="#f0b94f"/>
<text x="24" y="44" font-family="sans-serif" font-size="26" fill="#1f5132">{title}</text>
<text x="24" y="392" font-family="sans-serif" font-size="20" fill="#45624f">{caption} · {date}</text>
</svg>"""


def _write_photo(photos_dir: Path, name: str, title: str, caption: str, d: date,
                 variant: int = 0) -> str:
    month_dir = photos_dir / d.strftime("%Y%m")
    month_dir.mkdir(parents=True, exist_ok=True)
    fx = 250 + variant * 31
    fy = 130 + (variant % 3) * 26
    svg = PHOTO_SVG.format(fx=fx % 480 + 120, fy=fy,
                           fx2=(fx + 90) % 440 + 90, fy2=(fy + 60) % 120 + 120,
                           title=title, caption=caption, date=d.isoformat())
    p = month_dir / f"{name}.svg"
    p.write_text(svg, encoding="utf-8")
    return f"photos/{d.strftime('%Y%m')}/{name}.svg"


def seed(db_path: Path, photos_dir: Path) -> None:
    if db_path.exists():
        db_path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        if p.exists():
            p.unlink()
    photos_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    init_db(conn)
    svc = Service(conn, clock=Clock(), photo_root=photos_dir)
    now = datetime.now().replace(microsecond=0)
    today = now.date()

    def ts(offset_days=0, hh=10, mm=0):
        return (now - timedelta(days=offset_days)).replace(hour=hh, minute=mm).isoformat(timespec="seconds")

    # ---------- 用户 ----------
    users = {
        "alice": ("林晓", "resident"),
        "bob": ("王然", "resident"),
        "chen": ("陈墨", "gardener"),
        "zhou": ("周岚", "supervisor"),
    }
    uid = {}
    for uname, (disp, role) in users.items():
        cur = conn.execute("INSERT INTO users(username,display_name,role) VALUES (?,?,?)",
                           (uname, disp, role))
        uid[uname] = cur.lastrowid

    # ---------- 树木：编号/品种/位置/树冠坐标（1000×640 地图） ----------
    trees = [
        # code, species, location, x, y, health, adoptable, note
        ("T001", "银杏", "东门主步道北侧 12 米", 150, 402, "健康", 1, "树龄约 38 年，秋季叶色金黄"),
        ("T002", "香樟", "湖心亭西坡", 360, 452, "健康", 1, "冠幅宽大，遮阴良好"),
        ("T003", "染井吉野樱", "樱花广场南端", 560, 392, "健康", 1, "春季花期 3 月下旬"),
        ("T004", "雪松", "北坡草坪高点", 760, 250, "患病封控", 0, "发现松材线虫病媒迹象，已封控"),
        ("T005", "悬铃木", "西门林荫道第 3 株", 880, 430, "健康", 1, "夏季修剪重点株"),
        ("T006", "国槐", "儿童活动场西侧", 640, 178, "移栽中", 0, "因步道改造移栽至南坡新址"),
        ("T007", "白玉兰", "南门花坛内侧", 250, 170, "健康", 1, "早春先花后叶"),
        ("T008", "水杉", "湿地木栈道东端", 470, 120, "观察", 1, "湿生环境，注意排水"),
    ]
    tid = {}
    tree_photos = {}
    for i, (code, species, loc, x, y, health, adoptable, note) in enumerate(trees):
        d = today - timedelta(days=120 + i * 12)
        rel = _write_photo(photos_dir, f"{code.lower()}-profile", f"{code} {species}",
                           "档案照片", d, variant=i)
        tree_photos[code] = rel
        cur = conn.execute(
            """INSERT INTO trees(code,species,location,x,y,health_status,adoptable,photo_path,note)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (code, species, loc, x, y, health, adoptable, rel, note))
        tid[code] = cur.lastrowid

    # ---------- 认养 ----------
    def add_adoption(code, uname, s, e, mode, status, created_offset, fee=None):
        aid = conn.execute(
            """INSERT INTO adoptions(tree_id,user_id,start_date,end_date,care_mode,
                                     status,created_at,paid_at,fee_cents,exit_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (tid[code], uid[uname], s.isoformat(), e.isoformat(), mode, status,
             ts(created_offset), ts(created_offset) if status in ("active", "exited") else None,
             fee if fee is not None else max((e - s).days, 1) * 10,
             "个人时间安排冲突" if status == "exited" else None),
        ).lastrowid
        if status in ("active", "exited"):
            conn.execute(
                "INSERT INTO payments(adoption_id,amount_cents,status,created_at) VALUES (?,?, 'paid',?)",
                (aid, max((e - s).days, 1) * 10, ts(created_offset)))
        return aid

    # 林晓：去年认养 T001（已退出），今年继续认养，自助养护
    a_old = add_adoption("T001", "alice", date(today.year - 1, 3, 1),
                         date(today.year, 2, 28), "自助养护", "exited", 580)
    a1 = add_adoption("T001", "alice", date(today.year, 3, 1),
                      date(today.year + 1, 2, 28), "自助养护", "active", 210)
    # 王然：委托园艺师养护樱花
    a2 = add_adoption("T003", "bob", date(today.year, 5, 1),
                      date(today.year, 11, 30), "委托园艺师", "active", 160)
    # 王然：T006 国槐认养人（移栽案例当事人）
    a3 = add_adoption("T006", "bob", date(today.year, 4, 1),
                      date(today.year + 1, 3, 31), "委托园艺师", "active", 190)
    # T008 水杉：5 分钟前提交、尚未支付的占位（名额锁定中，15 分钟到期释放）
    a_hold = add_adoption("T008", "bob", today, today + timedelta(days=90),
                          "自助养护", "pending_payment", 0)
    conn.execute("UPDATE adoptions SET created_at=? WHERE id=?",
                 ((now - timedelta(minutes=5)).isoformat(timespec="seconds"), a_hold))

    def event(tree_code, etype, payload, uname=None, offset=0):
        conn.execute(
            "INSERT INTO tree_events(tree_id,event_type,payload,actor_id,created_at) VALUES (?,?,?,?,?)",
            (tid[tree_code], etype, json.dumps(payload, ensure_ascii=False),
             uid.get(uname), ts(offset)))

    event("T001", "提交认养申请", {"adoption_id": a_old, "start": f"{today.year-1}-03-01"}, "alice", 580)
    event("T001", "支付完成，认养生效", {"adoption_id": a_old}, "alice", 580)
    event("T001", "认养人退出", {"adoption_id": a_old, "reason": "到期自然结束"}, "alice", 220)
    event("T001", "提交认养申请", {"adoption_id": a1}, "alice", 210)
    event("T001", "支付完成，认养生效", {"adoption_id": a1}, "alice", 210)
    event("T003", "支付完成，认养生效", {"adoption_id": a2}, "bob", 160)
    event("T006", "支付完成，认养生效", {"adoption_id": a3}, "bob", 190)

    # ---------- 养护计划 + 真实执行记录（去年覆盖四季，今年部分执行） ----------
    def add_task(code, kind, planned, adoption_id, status="待执行", note="常规计划"):
        return conn.execute(
            """INSERT INTO care_tasks(tree_id,adoption_id,kind,planned_date,season,status,note,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (tid[code], adoption_id, kind, planned.isoformat(), season_of(planned),
             status, note, ts(300))).lastrowid

    def add_completed_special(code, kind, planned, note, uname="chen"):
        """封控/复壮等专项任务：过去日期视为已执行并留记录，未来日期待执行。"""
        if planned < today:
            tid_ = add_task(code, kind, planned, None, status="已完成", note=note)
            conn.execute(
                """INSERT INTO care_logs(task_id,tree_id,executed_on,actor_id,summary,photo_path,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (tid_, tid[code], planned.isoformat(), uid[uname], note, None,
                 ts((today - planned).days, 11)))
            return tid_
        return add_task(code, kind, planned, None, note=note)

    def execute(task_id, code, uname, executed, summary, photo=None):
        conn.execute(
            "UPDATE care_tasks SET status='已完成' WHERE id=?", (task_id,))
        conn.execute(
            """INSERT INTO care_logs(task_id,tree_id,executed_on,actor_id,summary,photo_path,created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (task_id, tid[code], executed.isoformat(), uid[uname], summary,
             photo, ts((today - executed).days)))

    # 去年四季各若干条，挂在已退出的认养上
    seasonal = {
        "春": [(3, 6, "浇水", "开春返青水，土壤墒情良好"),
               (3, 20, "病虫观察", "未发现蚧壳虫活动"),
               (4, 10, "浇水", "花前补水")],
        "夏": [(6, 5, "浇水", "高温周补水，树冠无萎蔫"),
               (7, 8, "病虫观察", "诱虫灯未见异常"),
               (8, 18, "修剪", "疏除内膛交叉枝 3 处")],
        "秋": [(9, 12, "浇水", "秋灌保墒"),
               (10, 15, "病虫观察", "叶片黄化为正常季节现象"),
               (11, 6, "浇水", "封冻前最后一次透水")],
        "冬": [(12, 18, "修剪", "冬季整形修剪，树势均衡"),
               (1, 10, "病虫观察", "清园，刮除老翘皮")],
    }
    log_idx = 0
    for season_label, items in seasonal.items():
        for m, day, kind, summary in items:
            y = today.year - 1
            d0 = date(y, m, day)
            t = add_task("T001", kind, d0, a_old, status="已完成")
            log_idx += 1
            rel = _write_photo(photos_dir, f"t001-log-{log_idx}", "T001 银杏",
                               f"{kind}记录", d0, variant=log_idx)
            execute(t, "T001", "alice" if kind == "浇水" else "chen",
                    d0, summary, photo=rel)
            conn.execute(
                "INSERT INTO photos(tree_id,uploader_id,taken_on,path,caption,created_at) VALUES (?,?,?,?,?,?)",
                (tid["T001"], uid["alice"] if kind == "浇水" else uid["chen"],
                 d0.isoformat(), rel, f"{kind}：{summary}", ts((today - d0).days)))

    # 今年 T001 计划：已完成 1 次浇水、1 条逾期任务
    t = add_task("T001", "浇水", today - timedelta(days=40), a1, status="已完成", note="常规计划")
    execute(t, "T001", "alice", today - timedelta(days=40), "春季浇水完成，新芽萌发整齐",
            tree_photos["T001"])
    add_task("T001", "病虫观察", today - timedelta(days=6), a1, status="待执行",
             note="常规计划（每日巡检将标记逾期并重排）")
    add_task("T001", "浇水", today + timedelta(days=8), a1)
    add_task("T001", "病虫观察", today + timedelta(days=20), a1)
    # 樱花委托计划
    add_task("T003", "病虫观察", today - timedelta(days=3), a2, status="待执行", note="蚜虫监测")
    add_task("T003", "修剪", today + timedelta(days=15), a2, note="花后轻剪")
    add_task("T003", "浇水", today + timedelta(days=25), a2)

    # ---------- 案例一：T004 雪松 患病封控（主管已确认） ----------
    d_created = today - timedelta(days=21)
    d_reviewed = today - timedelta(days=20)
    diag1 = conn.execute(
        """INSERT INTO diagnoses(tree_id,reporter_id,reporter_kind,kind,detail,status,
                                 proposed_health,reviewer_id,review_note,created_at,reviewed_at)
           VALUES (?,?,'gardener','病害',
                   '取样发现松材线虫病媒昆虫活动痕迹，建议立即封控并每周病虫观察。',
                   '已确认','患病封控',?,?,?,?)""",
        (tid["T004"], uid["chen"], uid["zhou"], "情况属实，即日起封控，暂停浇水修剪等常规作业。",
         datetime.combine(d_created, datetime.min.time()).replace(hour=9).isoformat(timespec="seconds"),
         datetime.combine(d_reviewed, datetime.min.time()).replace(hour=14).isoformat(timespec="seconds")),
    ).lastrowid
    event("T004", "园艺师提交异常诊断", {"diagnosis_id": diag1, "kind": "病害"}, "chen", 21)
    event("T004", "异常诊断已确认", {"diagnosis_id": diag1, "kind": "病害"}, "zhou", 20)
    for i, days in enumerate((3, 10, 17, 24)):
        add_completed_special("T004", "病虫观察", d_reviewed + timedelta(days=days),
                              "封控期病虫观察（主管确认封控）")

    # ---------- 案例二：T006 国槐 移栽（本系统预置的移栽案例） ----------
    d_t1 = today - timedelta(days=10)
    d_t2 = today - timedelta(days=8)
    diag2 = conn.execute(
        """INSERT INTO diagnoses(tree_id,reporter_id,reporter_kind,kind,detail,status,
                                 proposed_health,reviewer_id,review_note,created_at,reviewed_at)
           VALUES (?,?,'gardener','移栽',
                   '东门至儿童活动场步道改造，树冠与新步道冲突，建议带土球移栽至南坡新址并设支撑。',
                   '已确认','移栽中',?,?,?,?)""",
        (tid["T006"], uid["chen"], uid["zhou"],
         "同意移栽方案。落实施肥腐熟、土球直径不小于胸径 8 倍，移栽后两周复壮观察。",
         datetime.combine(d_t1, datetime.min.time()).replace(hour=11).isoformat(timespec="seconds"),
         datetime.combine(d_t2, datetime.min.time()).replace(hour=10, minute=30).isoformat(timespec="seconds")),
    ).lastrowid
    event("T006", "园艺师提交异常诊断", {"diagnosis_id": diag2, "kind": "移栽"}, "chen", 10)
    event("T006", "异常诊断已确认", {"diagnosis_id": diag2, "kind": "移栽"}, "zhou", 8)
    # 原计划中止
    add_task("T006", "浇水", today - timedelta(days=2), a3, status="已取消",
             note="常规计划（树木移栽，计划中止）")
    add_task("T006", "修剪", today + timedelta(days=30), a3, status="已取消",
             note="常规计划（树木移栽，计划中止）")
    # 移栽后复壮观察（第一周已完成，第二周待执行）
    add_completed_special("T006", "病虫观察", d_t2 + timedelta(days=7),
                          "移栽后复壮观察（主管确认移栽）")
    add_completed_special("T006", "病虫观察", d_t2 + timedelta(days=14),
                          "移栽后复壮观察（主管确认移栽）")
    transplant_photo = _write_photo(photos_dir, "t006-transplant", "T006 国槐 移栽现场",
                                    "带土球移栽，三角支撑", d_t2, variant=6)
    conn.execute(
        "INSERT INTO photos(tree_id,uploader_id,taken_on,path,caption,created_at) VALUES (?,?,?,?,?,?)",
        (tid["T006"], uid["chen"], d_t2.isoformat(), transplant_photo,
         "移栽当日：吊运至南坡新址，浇透定根水", ts(8, 15)))

    # ---------- 待办：等待复核的两条记录（供演示审批/误报闭环） ----------
    d_pend = today - timedelta(days=1)
    # 园艺师诊断，待主管复核
    conn.execute(
        """INSERT INTO diagnoses(tree_id,reporter_id,reporter_kind,kind,detail,status,
                                 proposed_health,created_at)
           VALUES (?,?,'gardener','虫害','水杉枝梢发现少量红蜘蛛聚集点，建议加密观察并标记观察状态。',
                   '待复核','观察',?)""",
        (tid["T008"], uid["chen"],
         datetime.combine(d_pend, datetime.min.time()).replace(hour=16).isoformat(timespec="seconds")))
    event("T008", "园艺师提交异常诊断", {"kind": "虫害"}, "chen", 1)
    # 居民误报枯萎，待园艺师核查
    conn.execute(
        """INSERT INTO diagnoses(tree_id,reporter_id,reporter_kind,kind,detail,status,created_at)
           VALUES (?,?,'resident','病害',
                   '居民疑似枯萎报告：路过看到白玉兰叶子发蔫，担心枯死，请尽快来看。','待复核',?)""",
        (tid["T007"], uid["alice"],
         datetime.combine(d_pend, datetime.min.time()).replace(hour=18, minute=20).isoformat(timespec="seconds")))
    event("T007", "居民报告疑似枯萎", {}, "alice", 1)

    # ---------- 去年年度反馈 ----------
    conn.execute(
        """INSERT INTO annual_reports(tree_id,year,summary,watering,pruning,pest,alerts,created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (tid["T001"], today.year - 1,
         f"{today.year - 1} 年度，T001（银杏）全年完成浇水 6 次、修剪 2 次、病虫观察 4 次；"
         "确认异常 0 起，年末健康结论为「健康」。树势良好，秋季叶色正常。",
         6, 2, 4, 0, ts(30)))

    # ---------- 通知（模拟适配器收件箱） ----------
    for uname, title, body, off in [
        ("bob", "认养树木移栽通知", "您认养的 T006 国槐将进行移栽，原养护计划已调整，移栽后将安排复壮观察。", 8),
        ("zhou", "待复核：异常诊断", "园艺师提交了 虫害 诊断，请复核。", 1),
        ("chen", "居民枯萎报告待核查", "居民报告树木 #7 疑似枯萎，请现场核查。", 1),
        ("alice", f"{today.year - 1} 年度反馈已发布", "您认养的 T001 年度报告已生成，欢迎查看。", 30),
    ]:
        conn.execute(
            "INSERT INTO notifications(user_id,title,body,created_at) VALUES (?,?,?,?)",
            (uid[uname], title, body, ts(off)))

    conn.commit()
    conn.close()
    print(f"种子数据完成：数据库 {db_path}，照片目录 {photos_dir}")
    print("演示账号：alice/bob（居民） chen（园艺师） zhou（主管），登录页可一键切换")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/park.db")
    ap.add_argument("--photos", default="data/photos")
    args = ap.parse_args()
    seed(Path(args.db), Path(args.photos))


if __name__ == "__main__":
    main()
