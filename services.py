"""业务服务层：认养互斥、占位释放、计划重算、异常复核、年度报告等全部规则。

时间通过 Clock 注入，测试可冻结/拨快时钟。
"""
from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime, timedelta

from adapters import NotificationAdapter, PaymentAdapter

HOLD_MINUTES = 30              # 未支付占位 30 分钟到期释放
LOCK_PLAN_GAP_DAYS = 14        # 移栽后暂停近期待办的天数
FEE_PER_MONTH_CENTS = 100      # 模拟：每月 1 元

CARE_KIND_LABEL = {"water": "浇水", "prune": "修剪", "pest_check": "病虫观察"}
HEALTH_LABEL = {
    "healthy": "健康",
    "monitor": "观察中",
    "sick_locked": "患病封控",
    "recovering": "复壮中",
    "transplanted": "已移栽",
    "removed": "已移除",
}
ADOPT_STATUS_LABEL = {
    "pending": "占位待支付",
    "active": "认养中",
    "exited": "已退出",
    "expired": "占位过期",
    "rejected": "已驳回",
}


class Clock:
    def __init__(self, fixed: datetime | None = None):
        self.fixed = fixed

    def now(self) -> datetime:
        return self.fixed or datetime.now()

    def today(self) -> date:
        return self.now().date()


CLOCK = Clock()


# ---------------------------------------------------------------- 内部工具

def _notify_role(conn: sqlite3.Connection, role: str, title: str, body: str,
                 now: datetime) -> None:
    notif = NotificationAdapter(conn)
    for row in conn.execute("SELECT id FROM users WHERE role=?", (role,)):
        notif.send(row["id"], title, body, _dt(now))


def _dt(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S")


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def _event(conn, tree_id: str, etype: str, detail: str, now: datetime,
           actor_id: int | None = None) -> None:
    conn.execute(
        "INSERT INTO tree_events(tree_id,type,detail,actor_id,created_at) "
        "VALUES (?,?,?,?,?)",
        (tree_id, etype, detail, actor_id, _dt(now)),
    )


def _tree_or_raise(conn, tree_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM trees WHERE id=?", (tree_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"树木 {tree_id} 不存在")
    return row


def _user_or_raise(conn, user_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if row is None:
        raise NotFoundError("用户不存在")
    return row


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class NotFoundError(ServiceError):
    def __init__(self, message: str):
        super().__init__(message, 404)


class AuthzError(ServiceError):
    def __init__(self, message: str = "无权操作"):
        super().__init__(message, 403)


# ---------------------------------------------------------------- 占位清扫

def sweep_expired_holds(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """把到期未支付的 pending 认养单置为 expired。返回释放数量。"""
    now = now or CLOCK.now()
    rows = conn.execute(
        "SELECT id, tree_id FROM adoptions "
        "WHERE status='pending' AND hold_expires <= ?",
        (_dt(now),),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE adoptions SET status='expired', hold_expires=NULL WHERE id=?",
            (r["id"],),
        )
        _event(conn, r["tree_id"], "plan_update",
               f"认养占位 #{r['id']} 超时未支付，名额释放", now)
    return len(rows)


def sweep_overdue(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """到期未执行的 scheduled 任务转 overdue，并记一次逾期事件、通知认养人。"""
    now = now or CLOCK.now()
    today = now.date().isoformat()
    rows = conn.execute(
        "SELECT t.*, a.user_id FROM care_tasks t "
        "LEFT JOIN adoptions a ON a.id=t.adoption_id "
        "WHERE t.status='scheduled' AND t.due_date < ?",
        (today,),
    ).fetchall()
    for r in rows:
        conn.execute("UPDATE care_tasks SET status='overdue' WHERE id=?", (r["id"],))
        _event(conn, r["tree_id"], "overdue",
               f"{CARE_KIND_LABEL[r['kind']]}任务(截止 {r['due_date']})逾期未执行", now)
        if r["user_id"]:
            NotificationAdapter(conn).send(
                r["user_id"], "养护任务逾期",
                f"您认养的 {r['tree_id']} 有一项{CARE_KIND_LABEL[r['kind']]}任务已逾期，请尽快补做。",
                _dt(now))
    return len(rows)


# ---------------------------------------------------------------- 认养申请

def _overlap_exists(conn, tree_id: str, start: date, end: date,
                    exclude_id: int | None = None) -> bool:
    sql = ("SELECT 1 FROM adoptions WHERE tree_id=? AND status IN ('pending','active') "
           "AND start_date < ? AND end_date > ?")
    params: list = [tree_id, end.isoformat(), start.isoformat()]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    return conn.execute(sql, params).fetchone() is not None


def apply_adoption(conn: sqlite3.Connection, user_id: int, tree_id: str,
                   start_date: str, end_date: str, care_mode: str,
                   now: datetime | None = None) -> dict:
    """居民提交认养申请。BEGIN IMMEDIATE 串行化并发申请，按提交时间定胜负。"""
    now = now or CLOCK.now()
    if care_mode not in ("watering", "full_care"):
        raise ServiceError("养护方式无效")
    start, end = _parse_date(start_date), _parse_date(end_date)
    if end <= start:
        raise ServiceError("认养期限必须为正")

    conn.execute("BEGIN IMMEDIATE")
    try:
        sweep_expired_holds(conn, now)
        user = _user_or_raise(conn, user_id)
        if user["role"] != "resident":
            raise ServiceError("仅居民可提交认养申请")
        tree = _tree_or_raise(conn, tree_id)
        if tree["health_status"] == "sick_locked":
            raise ServiceError("该树患病封控中，暂不接受认养")
        if tree["health_status"] == "removed":
            raise ServiceError("该树已移除")
        if not (tree["adoptable_from"] <= start_date and end_date <= tree["adoptable_to"]):
            raise ServiceError(
                f"申请期限须在可认养期限 {tree['adoptable_from']} ~ {tree['adoptable_to']} 内")
        # 同一用户在该树上已有未决申请
        dup = conn.execute(
            "SELECT id FROM adoptions WHERE tree_id=? AND user_id=? "
            "AND status IN ('pending','active')", (tree_id, user_id)).fetchone()
        if dup:
            raise ServiceError("您对该树已有进行中的认养记录", 409)
        if _overlap_exists(conn, tree_id, start, end):
            raise ServiceError("该期限已被占用，请选择其他日期", 409)

        hold_expires = now + timedelta(minutes=HOLD_MINUTES)
        cur = conn.execute(
            "INSERT INTO adoptions(tree_id,user_id,care_mode,start_date,end_date,"
            "status,hold_expires,created_at) VALUES (?,?,?,?,?,'pending',?,?)",
            (tree_id, user_id, care_mode, start_date, end_date,
             hold_expires.strftime("%Y-%m-%dT%H:%M:%S"), _dt(now)),
        )
        adoption_id = cur.lastrowid
        NotificationAdapter(conn).send(
            user_id, "认养名额已锁定",
            f"树木 {tree_id} 的 {start_date} ~ {end_date} 名额已为您保留 "
            f"{HOLD_MINUTES} 分钟，请尽快完成支付。",
            _dt(now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_adoption(conn, adoption_id, viewer_id=user_id)


def pay_adoption(conn: sqlite3.Connection, adoption_id: int, user_id: int,
                 now: datetime | None = None) -> dict:
    now = now or CLOCK.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        sweep_expired_holds(conn, now)
        row = conn.execute("SELECT * FROM adoptions WHERE id=?", (adoption_id,)).fetchone()
        if row is None:
            raise NotFoundError("认养单不存在")
        if row["user_id"] != user_id:
            raise AuthzError("只能支付自己的认养申请")
        if row["status"] == "expired":
            raise ServiceError("占位已过期，名额已释放，请重新申请", 409)
        if row["status"] != "pending":
            raise ServiceError("该认养单无需支付", 409)
        # 二次确认互斥（理论上 pending 已互斥，防御性校验）
        if _overlap_exists(conn, row["tree_id"], _parse_date(row["start_date"]),
                           _parse_date(row["end_date"]), exclude_id=adoption_id):
            conn.execute("UPDATE adoptions SET status='expired', hold_expires=NULL WHERE id=?",
                         (adoption_id,))
            conn.execute("COMMIT")
            raise ServiceError("名额冲突，占位已释放", 409)
        months = max(1, round((_parse_date(row["end_date"]) -
                               _parse_date(row["start_date"])).days / 30))
        PaymentAdapter(conn).charge(adoption_id, months * FEE_PER_MONTH_CENTS, _dt(now))
        conn.execute(
            "UPDATE adoptions SET status='active', hold_expires=NULL, paid_at=? WHERE id=?",
            (_dt(now), adoption_id))
        _event(conn, row["tree_id"], "plan_update",
               f"认养 #{adoption_id} 支付完成，园艺师制定养护计划", now, user_id)
        _generate_plan(conn, row, _dt(now), from_date=now.date())
        _notify_role(conn, "gardener", "新认养待安排",
                     f"树木 {row['tree_id']} 已有新认养（#{adoption_id}），养护计划已生成，请核对。",
                     now)
        NotificationAdapter(conn).send(
            row["user_id"], "认养成功",
            f"您已成功认养 {row['tree_id']}（{row['start_date']} ~ {row['end_date']}），"
            "可在养护日历查看任务。", _dt(now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_adoption(conn, adoption_id, viewer_id=user_id)


def exit_adoption(conn: sqlite3.Connection, adoption_id: int, user_id: int,
                  reason: str = "", now: datetime | None = None) -> dict:
    """认养人主动退出：退款、终止计划、释放后续名额。"""
    now = now or CLOCK.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM adoptions WHERE id=?", (adoption_id,)).fetchone()
        if row is None:
            raise NotFoundError("认养单不存在")
        if row["user_id"] != user_id:
            raise AuthzError("只能退出自己的认养")
        if row["status"] != "active":
            raise ServiceError("仅认养中的记录可以退出", 409)
        conn.execute(
            "UPDATE adoptions SET status='exited', exited_at=? WHERE id=?",
            (_dt(now), adoption_id))
        # 未执行的计划随退出终止（已执行/逾期记录保留）
        conn.execute(
            "DELETE FROM care_tasks WHERE adoption_id=? AND status='scheduled'",
            (adoption_id,))
        pay = conn.execute(
            "SELECT id FROM payments WHERE adoption_id=? AND status='paid' ORDER BY id",
            (adoption_id,)).fetchone()
        if pay:
            PaymentAdapter(conn).refund(pay["id"])
        _event(conn, row["tree_id"], "exit",
               f"认养人退出认养 #{adoption_id}，养护计划终止。{reason}", now, user_id)
        NotificationAdapter(conn).send(
            row["user_id"], "已退出认养",
            f"您对 {row['tree_id']} 的认养已终止，未执行周期的费用已原路退回（模拟）。",
            _dt(now))
        _notify_role(conn, "gardener", "认养人退出",
                     f"树木 {row['tree_id']} 认养 #{adoption_id} 已退出，计划终止，可开放新认养。", now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_adoption(conn, adoption_id, viewer_id=user_id)


# ---------------------------------------------------------------- 养护计划

def _generate_plan(conn: sqlite3.Connection, adoption: sqlite3.Row,
                   created_at: str, from_date: date | None = None) -> None:
    """按认养周期生成浇水(每14天)/修剪(春秋各一次)/病虫观察(每月)任务。

    from_date：只生成该日期之后的任务（支付/解封/移栽重算时传入"今天"或复壮期结束日，
    不补建过去的待办）；默认从认养起始日生成（供种子回溯历史）。
    已存在的任务（含 done/overdue 历史）不会重复生成。
    """
    tree_id, adoption_id = adoption["tree_id"], adoption["id"]
    care_mode = adoption["care_mode"]
    adopt_start = _parse_date(adoption["start_date"])
    end = _parse_date(adoption["end_date"])
    # 浇水节奏始终锚定认养起始日，重算时只过滤掉过早的日期，避免与既有任务错位重复
    start = adopt_start
    if from_date is not None and from_date > start:
        start = start + timedelta(days=14 * ((from_date - start).days // 14))
        if start < from_date:
            start += timedelta(days=14)

    existing = {
        (r["kind"], r["due_date"])
        for r in conn.execute(
            "SELECT kind, due_date FROM care_tasks WHERE tree_id=? AND adoption_id=?",
            (tree_id, adoption_id))
    }

    def add(kind: str, d: date, assignee: str, note: str = "") -> None:
        if d < start or d > end or (kind, d.isoformat()) in existing:
            return
        conn.execute(
            "INSERT INTO care_tasks(tree_id,adoption_id,kind,due_date,status,assignee,"
            "note,created_at) VALUES (?,?,?,?,'scheduled',?,?,?)",
            (tree_id, adoption_id, kind, d.isoformat(), assignee, note, created_at))

    water_assignee = "adopter" if care_mode == "watering" else "gardener"
    d = start
    while d <= end:
        add("water", d, water_assignee)
        d += timedelta(days=14)

    for month, label in ((3, "春季整形修剪"), (11, "冬前修剪")):
        # 落在认养周期内的春秋修剪日（跨年周期两年各取）
        for y in (start.year, end.year):
            dd = date(y, month, 15)
            add("prune", dd, "gardener", label)

    d = date(start.year, start.month, 10)
    while d <= end:
        add("pest_check", d, "gardener", "月度病虫观察")
        # 月末安全进位
        if d.month == 12:
            d = date(d.year + 1, 1, 10)
        else:
            d = date(d.year, d.month + 1, 10)


def complete_task(conn: sqlite3.Connection, task_id: int, user_id: int,
                  note: str = "", done_date: str | None = None,
                  now: datetime | None = None) -> dict:
    now = now or CLOCK.now()
    done_on = _parse_date(done_date) if done_date else now.date()
    task = conn.execute("SELECT * FROM care_tasks WHERE id=?", (task_id,)).fetchone()
    if task is None:
        raise NotFoundError("任务不存在")
    user = _user_or_raise(conn, user_id)
    adoption = None
    if task["adoption_id"]:
        adoption = conn.execute(
            "SELECT * FROM adoptions WHERE id=?", (task["adoption_id"],)).fetchone()
    if user["role"] == "resident":
        if not adoption or adoption["user_id"] != user_id:
            raise AuthzError("只能执行自己认养树木上的任务")
        if task["assignee"] != "adopter":
            raise AuthzError("修剪与病虫观察由园艺师执行")
    elif user["role"] not in ("gardener", "supervisor"):
        raise AuthzError()
    if task["status"] not in ("scheduled", "overdue"):
        raise ServiceError("该任务已结束，无需重复执行", 409)
    late = done_on.isoformat() > task["due_date"]
    full_note = note + ("（逾期补做）" if late and task["status"] == "overdue" else "")
    conn.execute(
        "UPDATE care_tasks SET status='done', done_date=?, note=? WHERE id=?",
        (done_on.isoformat(), full_note.strip(), task_id))
    if late:
        _event(conn, task["tree_id"], "plan_update",
               f"{CARE_KIND_LABEL[task['kind']]}任务逾期后已补做完成", now, user_id)
    return _task_dict(conn, task_id)


# ---------------------------------------------------------------- 树木事件

def transplant_tree(conn: sqlite3.Connection, tree_id: str, user_id: int,
                    new_x: float, new_y: float, reason: str,
                    happened_on: str | None = None,
                    now: datetime | None = None) -> dict:
    """树木移栽：更新坐标与档案、重算近期计划（移栽复壮期暂停 + 加密观察）。"""
    now = now or CLOCK.now()
    happened_on = happened_on or now.date().isoformat()
    user = _user_or_raise(conn, user_id)
    if user["role"] not in ("gardener", "supervisor"):
        raise AuthzError("仅园艺师/主管可登记移栽")
    conn.execute("BEGIN IMMEDIATE")
    try:
        tree = _tree_or_raise(conn, tree_id)
        old_x, old_y = tree["x"], tree["y"]
        conn.execute(
            "INSERT INTO transplants(tree_id,old_x,old_y,new_x,new_y,happened_on,reason)"
            " VALUES (?,?,?,?,?,?,?)",
            (tree_id, old_x, old_y, new_x, new_y, happened_on, reason))
        conn.execute(
            "UPDATE trees SET x=?, y=?, health_status='transplanted' WHERE id=?",
            (new_x, new_y, tree_id))
        _event(conn, tree_id, "transplant",
               f"树木移栽：({old_x:.0f},{old_y:.0f}) → ({new_x:.0f},{new_y:.0f})。{reason}",
               now, user_id)
        _replan_after_transplant(conn, tree_id, happened_on, now, user_id)
        # 通知当前认养人
        row = conn.execute(
            "SELECT user_id FROM adoptions WHERE tree_id=? AND status='active'",
            (tree_id,)).fetchone()
        if row:
            NotificationAdapter(conn).send(
                row["user_id"], "您认养的树木已移栽",
                f"树木 {tree_id} 已移栽至新位置，地图与养护计划已更新，复壮期暂停近期浇水。",
                _dt(now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_tree_detail(conn, tree_id, viewer_id=user_id)


def _replan_after_transplant(conn, tree_id, happened_on: str, now, actor_id) -> None:
    base = _parse_date(happened_on)
    gap_end = base + timedelta(days=LOCK_PLAN_GAP_DAYS)
    # 复壮期内的待办取消（仅删除从未执行的 scheduled）
    conn.execute(
        "DELETE FROM care_tasks WHERE tree_id=? AND status='scheduled' AND due_date <= ?",
        (tree_id, gap_end.isoformat()))
    # 加密的复壮观察
    for offset in (7, 21):
        d = (base + timedelta(days=offset)).isoformat()
        exists = conn.execute(
            "SELECT 1 FROM care_tasks WHERE tree_id=? AND kind='pest_check' AND due_date=? "
            "AND status != 'cancelled'", (tree_id, d)).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO care_tasks(tree_id,kind,due_date,status,assignee,note,created_at)"
                " VALUES (?, 'pest_check', ?, 'scheduled','gardener',?,?)",
                (tree_id, d, "移栽后复壮观察", _dt(now)))
    # 为进行中的认养补齐后续浇水计划（不补建复壮期及之前的待办）
    resume_from = gap_end + timedelta(days=1)
    for a in conn.execute(
            "SELECT * FROM adoptions WHERE tree_id=? AND status='active'", (tree_id,)):
        _generate_plan(conn, a, _dt(now), from_date=resume_from)
    _event(conn, tree_id, "plan_update",
           f"移栽后 {LOCK_PLAN_GAP_DAYS} 天复壮期暂停近期待办，新增第7/21天复壮观察",
           now, actor_id)


def lockdown_tree(conn: sqlite3.Connection, tree_id: str, user_id: int,
                  reason: str, now: datetime | None = None) -> dict:
    """患病封控：更新健康状态、冻结全部待办。健康结论走异常复核时也调用本函数。"""
    now = now or CLOCK.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        tree = _tree_or_raise(conn, tree_id)
        conn.execute("UPDATE trees SET health_status='sick_locked' WHERE id=?", (tree_id,))
        conn.execute(
            "DELETE FROM care_tasks WHERE tree_id=? AND status='scheduled'", (tree_id,))
        _event(conn, tree_id, "lockdown", f"患病封控，养护计划冻结。{reason}", now, user_id)
        row = conn.execute(
            "SELECT user_id FROM adoptions WHERE tree_id=? AND status='active'",
            (tree_id,)).fetchone()
        if row:
            NotificationAdapter(conn).send(
                row["user_id"], "树木封控通知",
                f"树木 {tree_id} 因病患封控，养护任务暂停，解封后计划自动恢复。", _dt(now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_tree_detail(conn, tree_id, viewer_id=user_id)


def reopen_tree(conn: sqlite3.Connection, tree_id: str, user_id: int,
                target_health: str = "recovering", now: datetime | None = None) -> dict:
    now = now or CLOCK.now()
    if target_health not in ("recovering", "healthy", "monitor"):
        raise ServiceError("解封目标状态无效")
    conn.execute("BEGIN IMMEDIATE")
    try:
        tree = _tree_or_raise(conn, tree_id)
        if tree["health_status"] != "sick_locked":
            raise ServiceError("仅封控中的树木可解封", 409)
        conn.execute("UPDATE trees SET health_status=? WHERE id=?",
                     (target_health, tree_id))
        for a in conn.execute(
                "SELECT * FROM adoptions WHERE tree_id=? AND status='active'", (tree_id,)):
            _generate_plan(conn, a, _dt(now), from_date=now.date())
        _event(conn, tree_id, "reopen", "封控解除，养护计划恢复", now, user_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return get_tree_detail(conn, tree_id, viewer_id=user_id)


# ---------------------------------------------------------------- 异常复核

def report_anomaly(conn: sqlite3.Connection, tree_id: str, user_id: int, kind: str,
                   detail: str, now: datetime | None = None) -> dict:
    """居民上报（如误报枯萎）。居民无权下结论，只进入待核实队列。"""
    now = now or CLOCK.now()
    user = _user_or_raise(conn, user_id)
    if user["role"] != "resident":
        raise ServiceError("该接口面向居民上报；园艺师请提交诊断")
    if kind not in ("wilt", "pest", "disease", "branch_damage", "other"):
        raise ServiceError("异常类型无效")
    _tree_or_raise(conn, tree_id)
    cur = conn.execute(
        "INSERT INTO anomaly_reports(tree_id,reporter_id,kind,source,detail,status,created_at)"
        " VALUES (?,?,?, 'resident_report', ?, 'open', ?)",
        (tree_id, user_id, kind, detail, _dt(now)))
    _notify_role(conn, "gardener", "居民上报树木异常",
                 f"树木 {tree_id} 收到居民{kind}上报，请现场核实。", now)
    return _anomaly_dict(conn, cur.lastrowid, viewer=user)


def submit_diagnosis(conn: sqlite3.Connection, report_id: int, gardener_id: int,
                     diagnosis: str, is_real: bool, proposed_health: str | None = None,
                     now: datetime | None = None) -> dict:
    """园艺师核实居民上报，或直接提交诊断。

    is_real=False：判定误报 → false_alarm，并安排一次复查（计划更新）。
    is_real=True ：填写诊断与拟变更状态 → needs_review，等待主管复核。
    """
    now = now or CLOCK.now()
    user = _user_or_raise(conn, gardener_id)
    if user["role"] not in ("gardener", "supervisor"):
        raise AuthzError("仅园艺师可提交诊断")
    report = conn.execute("SELECT * FROM anomaly_reports WHERE id=?", (report_id,)).fetchone()
    if report is None:
        raise NotFoundError("异常报告不存在")
    if report["status"] not in ("open", "rejected"):
        raise ServiceError("该报告已处理，不能重复提交", 409)

    if not is_real:
        conn.execute(
            "UPDATE anomaly_reports SET status='false_alarm', diagnosis=?, gardener_id=?,"
            "reviewed_at=? WHERE id=?",
            (diagnosis, gardener_id, _dt(now), report_id))
        _event(conn, report["tree_id"], "false_report",
               f"居民上报经核实为误报：{diagnosis}", now, gardener_id)
        # 误报也更新计划：7 天内安排一次复查观察
        d = (now.date() + timedelta(days=7)).isoformat()
        conn.execute(
            "INSERT INTO care_tasks(tree_id,kind,due_date,status,assignee,note,created_at)"
            " VALUES (?, 'pest_check', ?, 'scheduled','gardener', ?, ?)",
            (report["tree_id"], d, "误报枯萎后的现场复查", _dt(now)))
        _event(conn, report["tree_id"], "plan_update",
               "误报已记录，7 天后安排复查观察", now, gardener_id)
        NotificationAdapter(conn).send(
            report["reporter_id"], "上报核实结果",
            f"您对 {report['tree_id']} 的异常上报经现场核实为误报，树木状态正常，感谢参与。",
            _dt(now))
    else:
        if proposed_health not in HEALTH_LABEL:
            raise ServiceError("拟变更的健康状态无效")
        conn.execute(
            "UPDATE anomaly_reports SET status='needs_review', diagnosis=?, gardener_id=?,"
            "proposed_health=? WHERE id=?",
            (diagnosis, gardener_id, proposed_health, report_id))
        _notify_role(conn, "supervisor", "异常诊断待复核",
                     f"树木 {report['tree_id']} 的诊断（拟变更为"
                     f"{HEALTH_LABEL[proposed_health]}）等待主管复核。", now)
    return _anomaly_dict(conn, report_id, viewer=user)


def review_diagnosis(conn: sqlite3.Connection, report_id: int, supervisor_id: int,
                     approve: bool, review_note: str = "",
                     now: datetime | None = None) -> dict:
    """主管复核：批准后健康结论才生效（封控/解封联动计划）。"""
    now = now or CLOCK.now()
    user = _user_or_raise(conn, supervisor_id)
    if user["role"] != "supervisor":
        raise AuthzError("仅主管可复核诊断")
    conn.execute("BEGIN IMMEDIATE")
    try:
        report = conn.execute(
            "SELECT * FROM anomaly_reports WHERE id=?", (report_id,)).fetchone()
        if report is None:
            raise NotFoundError("异常报告不存在")
        if report["status"] != "needs_review":
            raise ServiceError("仅待复核的诊断可以审批", 409)
        if approve:
            target = report["proposed_health"] or "monitor"
            conn.execute("UPDATE trees SET health_status=? WHERE id=?",
                         (target, report["tree_id"]))
            conn.execute(
                "UPDATE anomaly_reports SET status='approved', review_note=?, reviewer_id=?,"
                "reviewed_at=? WHERE id=?",
                (review_note, supervisor_id, _dt(now), report_id))
            _event(conn, report["tree_id"], "diagnosis_approved",
                   f"主管批准诊断，健康状态变更为「{HEALTH_LABEL[target]}」。{review_note}",
                   now, supervisor_id)
            if target == "sick_locked":
                conn.execute(
                    "DELETE FROM care_tasks WHERE tree_id=? AND status='scheduled'",
                    (report["tree_id"],))
                _event(conn, report["tree_id"], "lockdown",
                       "依复核诊断执行封控，计划冻结", now, supervisor_id)
            elif target in ("recovering", "healthy", "monitor"):
                for a in conn.execute(
                        "SELECT * FROM adoptions WHERE tree_id=? AND status='active'",
                        (report["tree_id"],)):
                    _generate_plan(conn, a, _dt(now), from_date=now.date())
                _event(conn, report["tree_id"], "reopen",
                       "依复核诊断恢复/更新养护计划", now, supervisor_id)
        else:
            conn.execute(
                "UPDATE anomaly_reports SET status='rejected', review_note=?, reviewer_id=?,"
                "reviewed_at=? WHERE id=?",
                (review_note, supervisor_id, _dt(now), report_id))
            _event(conn, report["tree_id"], "diagnosis_rejected",
                   f"主管驳回诊断，维持原健康结论。{review_note}", now, supervisor_id)
        # 通知园艺师与上报人
        for uid in {report["gardener_id"], report["reporter_id"]}:
            if uid:
                NotificationAdapter(conn).send(
                    uid, "异常诊断复核结果",
                    f"树木 {report['tree_id']} 的诊断已{'批准' if approve else '驳回'}："
                    f"{review_note or '无补充说明'}", _dt(now))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return _anomaly_dict(conn, report_id, viewer=user)


# ---------------------------------------------------------------- 巡查照片

MIN_PHOTO_BRIGHTNESS = 0.42   # 照片须保持可辨识亮度


def add_photo(conn: sqlite3.Connection, tree_id: str, uploader_id: int, filename: str,
              caption: str, taken_on: str, brightness: float,
              now: datetime | None = None) -> dict:
    now = now or CLOCK.now()
    _tree_or_raise(conn, tree_id)
    _user_or_raise(conn, uploader_id)
    _parse_date(taken_on)
    if brightness < MIN_PHOTO_BRIGHTNESS:
        raise ServiceError(
            f"照片亮度过低（{brightness:.2f} < {MIN_PHOTO_BRIGHTNESS}），无法辨识，请补光后重拍")
    cur = conn.execute(
        "INSERT INTO photos(tree_id,uploader_id,filename,caption,taken_on,brightness,created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (tree_id, uploader_id, filename, caption, taken_on, brightness, _dt(now)))
    return _photo_dict(conn, cur.lastrowid, viewer=_user_or_raise(conn, uploader_id))


# ---------------------------------------------------------------- 年度报告

def generate_annual_report(conn: sqlite3.Connection, adoption_id: int, year: int,
                           viewer_id: int, now: datetime | None = None) -> dict:
    now = now or CLOCK.now()
    adoption = conn.execute(
        "SELECT * FROM adoptions WHERE id=?", (adoption_id,)).fetchone()
    if adoption is None:
        raise NotFoundError("认养单不存在")
    viewer = _user_or_raise(conn, viewer_id)
    if viewer["role"] == "resident" and adoption["user_id"] != viewer_id:
        raise AuthzError("只能查看自己认养的年度反馈")

    lo, hi = f"{year}-01-01", f"{year}-12-31"
    counts = {}
    for kind in ("water", "prune", "pest_check"):
        row = conn.execute(
            "SELECT COUNT(*) total, "
            "SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) done FROM care_tasks "
            "WHERE adoption_id=? AND kind=? AND due_date BETWEEN ? AND ?",
            (adoption_id, kind, lo, hi)).fetchone()
        counts[kind] = (row["done"] or 0, row["total"] or 0)
    overdue_cnt = conn.execute(
        "SELECT COUNT(*) c FROM care_tasks WHERE adoption_id=? "
        "AND due_date BETWEEN ? AND ? AND (status='overdue' OR "
        "(status='done' AND done_date > due_date))",
        (adoption_id, lo, hi)).fetchone()["c"]
    false_cnt = conn.execute(
        "SELECT COUNT(*) c FROM anomaly_reports WHERE tree_id=? AND reporter_id=? "
        "AND source='resident_report' AND status='false_alarm' AND created_at BETWEEN ? AND ?",
        (adoption["tree_id"], adoption["user_id"],
         f"{year}-01-01T00:00:00", f"{year}-12-31T23:59:59")).fetchone()["c"]

    tree = conn.execute("SELECT * FROM trees WHERE id=?", (adoption["tree_id"],)).fetchone()
    total = sum(t for _, t in counts.values())
    done = sum(d for d, _ in counts.values())
    rate = (done / total * 100) if total else 100.0
    summary = (
        f"{year} 年度，您认养的{tree['species']}（{tree['id']}）共安排养护任务 {total} 项，"
        f"实际完成 {done} 项，执行率 {rate:.0f}%；逾期 {overdue_cnt} 项。"
        f"当前专业健康结论为「{HEALTH_LABEL[tree['health_status']]}」。"
    )
    if false_cnt:
        summary += f"本年度您提交的异常上报中有 {false_cnt} 次经核实为误报，欢迎继续观察并拍照留存。"
    if tree["health_status"] == "transplanted":
        summary += "该树本年度经历移栽，已进入复壮观察期，计划已相应调整。"
    if rate >= 90 and overdue_cnt == 0:
        summary += "全年养护配合出色，树木生长状态良好，感谢您的陪伴。"

    conn.execute(
        "INSERT INTO annual_reports(adoption_id,tree_id,year,water_done,water_total,"
        "prune_done,prune_total,pest_done,pest_total,overdue_cnt,false_alarm_cnt,"
        "summary,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(adoption_id,year) DO UPDATE SET "
        "water_done=excluded.water_done, water_total=excluded.water_total,"
        "prune_done=excluded.prune_done, prune_total=excluded.prune_total,"
        "pest_done=excluded.pest_done, pest_total=excluded.pest_total,"
        "overdue_cnt=excluded.overdue_cnt, false_alarm_cnt=excluded.false_alarm_cnt,"
        "summary=excluded.summary, created_at=excluded.created_at",
        (adoption_id, adoption["tree_id"], year,
         *counts["water"], *counts["prune"], *counts["pest_check"],
         overdue_cnt, false_cnt, summary, _dt(now)))
    return _annual_dict(conn, adoption_id, year)


# ---------------------------------------------------------------- 序列化

def _is_staff(user: sqlite3.Row | None) -> bool:
    return bool(user and user["role"] in ("gardener", "supervisor"))


def get_tree_map(conn: sqlite3.Connection, viewer_id: int | None,
                 now: datetime | None = None) -> list[dict]:
    now = now or CLOCK.now()
    sweep_expired_holds(conn, now)
    viewer = conn.execute("SELECT * FROM users WHERE id=?", (viewer_id,)).fetchone()
    out = []
    for t in conn.execute("SELECT * FROM trees ORDER BY id"):
        active = conn.execute(
            "SELECT id,user_id,start_date,end_date FROM adoptions "
            "WHERE tree_id=? AND status='active' AND ? BETWEEN start_date AND end_date",
            (t["id"], now.date().isoformat())).fetchone()
        held = None if active else conn.execute(
            "SELECT id,user_id,start_date,end_date,hold_expires FROM adoptions "
            "WHERE tree_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
            (t["id"],)).fetchone()
        if active:
            adopt_state = "mine" if viewer and active["user_id"] == viewer["id"] else "active"
        elif held:
            adopt_state = "held_by_me" if viewer and held["user_id"] == viewer["id"] else "held"
        elif t["health_status"] in ("sick_locked", "removed"):
            adopt_state = "locked"
        else:
            adopt_state = "available"
        last_move = conn.execute(
            "SELECT old_x,old_y,new_x,new_y,happened_on FROM transplants "
            "WHERE tree_id=? ORDER BY id DESC LIMIT 1", (t["id"],)).fetchone()
        out.append({
            "id": t["id"], "species": t["species"], "x": t["x"], "y": t["y"],
            "health": t["health_status"], "health_label": HEALTH_LABEL[t["health_status"]],
            "adopt_state": adopt_state,
            "adoptable_from": t["adoptable_from"], "adoptable_to": t["adoptable_to"],
            "my_hold_seconds_left": (
                max(0, int((datetime.strptime(held["hold_expires"],
                        "%Y-%m-%dT%H:%M:%S") - now).total_seconds()))
                if held and viewer and held["user_id"] == viewer["id"] else None),
            "transplant": dict(last_move) if last_move else None,
        })
    return out


def _task_dict(conn, task_id: int) -> dict:
    t = conn.execute(
        "SELECT t.*, u.name AS adopter_name FROM care_tasks t "
        "LEFT JOIN adoptions a ON a.id=t.adoption_id "
        "LEFT JOIN users u ON u.id=a.user_id WHERE t.id=?", (task_id,)).fetchone()
    return dict(t) | {"kind_label": CARE_KIND_LABEL[t["kind"]]}


def get_calendar(conn: sqlite3.Connection, user_id: int, month: str | None = None) -> list[dict]:
    user = _user_or_raise(conn, user_id)
    sql = "SELECT t.* FROM care_tasks t"
    params: list = []
    if user["role"] == "resident":
        sql += (" JOIN adoptions a ON a.id=t.adoption_id WHERE a.user_id=?")
        params.append(user_id)
    else:
        sql += " WHERE 1=1"
    if month:
        _parse_date(month + "-01")
        sql += " AND substr(t.due_date,1,7)=?"
        params.append(month)
    sql += " ORDER BY t.due_date, t.id"
    rows = conn.execute(sql, params).fetchall()
    result = []
    for t in rows:
        d = dict(t)
        d["kind_label"] = CARE_KIND_LABEL[t["kind"]]
        d["can_complete"] = (
            user["role"] in ("gardener", "supervisor")
            or (user["role"] == "resident" and t["assignee"] == "adopter"))
        result.append(d)
    return result


def _photo_dict(conn, photo_id: int, viewer: sqlite3.Row | None) -> dict:
    p = conn.execute(
        "SELECT p.*, u.name AS uploader_name, u.role AS uploader_role FROM photos p "
        "JOIN users u ON u.id=p.uploader_id WHERE p.id=?", (photo_id,)).fetchone()
    d = dict(p)
    # 跨用户隐私：非员工看不到上传者身份
    if not _is_staff(viewer):
        d["uploader_id"] = None
        d["uploader_name"] = "巡查记录" if p["uploader_role"] == "gardener" else "居民记录"
    return d


def _anomaly_dict(conn, report_id: int, viewer: sqlite3.Row | None) -> dict:
    r = conn.execute(
        "SELECT ar.*, u.name AS reporter_name, g.name AS gardener_name, "
        "s.name AS reviewer_name FROM anomaly_reports ar "
        "JOIN users u ON u.id=ar.reporter_id "
        "LEFT JOIN users g ON g.id=ar.gardener_id "
        "LEFT JOIN users s ON s.id=ar.reviewer_id WHERE ar.id=?", (report_id,)).fetchone()
    d = dict(r)
    if not _is_staff(viewer) and (not viewer or viewer["id"] != r["reporter_id"]):
        # 其他居民：只保留健康公报，隐去上报人/诊断细节
        d["reporter_id"] = None
        d["reporter_name"] = "居民"
        d["detail"] = ""
        d["diagnosis"] = ""
        d["review_note"] = ""
    return d


def get_tree_detail(conn: sqlite3.Connection, tree_id: str,
                    viewer_id: int | None) -> dict:
    viewer = conn.execute("SELECT * FROM users WHERE id=?", (viewer_id,)).fetchone()
    t = _tree_or_raise(conn, tree_id)
    moves = conn.execute(
        "SELECT * FROM transplants WHERE tree_id=? ORDER BY happened_on", (tree_id,)).fetchall()
    tasks = conn.execute(
        "SELECT t.*, u.name AS adopter_name FROM care_tasks t "
        "LEFT JOIN adoptions a ON a.id=t.adoption_id "
        "LEFT JOIN users u ON u.id=a.user_id WHERE t.tree_id=? ORDER BY t.due_date",
        (tree_id,)).fetchall()
    task_out = []
    for tsk in tasks:
        d = dict(tsk)
        d["kind_label"] = CARE_KIND_LABEL[tsk["kind"]]
        # 真实执行记录对所有登录用户公开，但不暴露认养人身份
        if not _is_staff(viewer):
            d["adopter_name"] = None
        task_out.append(d)

    photos = [_photo_dict(conn, r["id"], viewer) for r in conn.execute(
        "SELECT id FROM photos WHERE tree_id=? ORDER BY taken_on DESC, id DESC",
        (tree_id,))]
    events = [dict(r) for r in conn.execute(
        "SELECT e.*, u.name AS actor_name FROM tree_events e "
        "LEFT JOIN users u ON u.id=e.actor_id WHERE e.tree_id=? ORDER BY e.id DESC LIMIT 50",
        (tree_id,))]
    if not _is_staff(viewer):
        for e in events:
            if not viewer or e["actor_id"] != viewer["id"]:
                e["actor_name"] = None

    cur = conn.execute(
        "SELECT * FROM adoptions WHERE tree_id=? AND status IN ('pending','active') "
        "ORDER BY id DESC", (tree_id,)).fetchall()
    adoption_out = None
    for a in cur:
        is_owner = bool(viewer and a["user_id"] == viewer["id"])
        if not is_owner and not _is_staff(viewer):
            adoption_out = {
                "id": None, "status": a["status"],
                "status_label": ADOPT_STATUS_LABEL[a["status"]],
                "start_date": a["start_date"], "end_date": a["end_date"],
                "anonymized": True,
            }
        else:
            adoption_out = _adoption_full(conn, a)
        break

    reports = []
    if _is_staff(viewer):
        rows = conn.execute(
            "SELECT id FROM anomaly_reports WHERE tree_id=? ORDER BY id DESC", (tree_id,))
        reports = [_anomaly_dict(conn, r["id"], viewer) for r in rows]
    elif viewer:
        rows = conn.execute(
            "SELECT id FROM anomaly_reports WHERE tree_id=? AND reporter_id=? ORDER BY id DESC",
            (tree_id, viewer["id"]))
        reports = [_anomaly_dict(conn, r["id"], viewer) for r in rows]

    return {
        "id": t["id"], "species": t["species"], "x": t["x"], "y": t["y"],
        "health_status": t["health_status"],
        "health_label": HEALTH_LABEL[t["health_status"]],
        "adoptable_from": t["adoptable_from"], "adoptable_to": t["adoptable_to"],
        "notes": t["notes"],
        "transplants": [dict(m) for m in moves],
        "tasks": task_out,
        "photos": photos,
        "events": events,
        "current_adoption": adoption_out,
        "anomalies": reports,
    }


def _adoption_full(conn: sqlite3.Connection, a: sqlite3.Row) -> dict:
    tree = conn.execute("SELECT species FROM trees WHERE id=?", (a["tree_id"],)).fetchone()
    user = conn.execute("SELECT name FROM users WHERE id=?", (a["user_id"],)).fetchone()
    pay = conn.execute(
        "SELECT id,amount,status FROM payments WHERE adoption_id=? ORDER BY id",
        (a["id"],)).fetchall()
    years = [r["year"] for r in conn.execute(
        "SELECT year FROM annual_reports WHERE adoption_id=? ORDER BY year", (a["id"],))]
    return {
        "id": a["id"], "tree_id": a["tree_id"], "species": tree["species"],
        "user_id": a["user_id"], "user_name": user["name"],
        "care_mode": a["care_mode"],
        "care_mode_label": "仅浇水协助" if a["care_mode"] == "watering" else "全程养护",
        "start_date": a["start_date"], "end_date": a["end_date"],
        "status": a["status"], "status_label": ADOPT_STATUS_LABEL[a["status"]],
        "hold_expires": a["hold_expires"], "created_at": a["created_at"],
        "paid_at": a["paid_at"], "exited_at": a["exited_at"],
        "payments": [dict(p) for p in pay], "report_years": years,
        "anonymized": False,
    }


def get_adoption(conn: sqlite3.Connection, adoption_id: int,
                 viewer_id: int | None) -> dict:
    a = conn.execute("SELECT * FROM adoptions WHERE id=?", (adoption_id,)).fetchone()
    if a is None:
        raise NotFoundError("认养单不存在")
    viewer = conn.execute("SELECT * FROM users WHERE id=?", (viewer_id,)).fetchone()
    if not viewer or (viewer["role"] == "resident" and a["user_id"] != viewer["id"]):
        raise AuthzError("无权查看该认养记录")
    return _adoption_full(conn, a)


def list_my_adoptions(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    _user_or_raise(conn, user_id)
    rows = conn.execute(
        "SELECT * FROM adoptions WHERE user_id=? ORDER BY id DESC", (user_id,))
    return [_adoption_full(conn, a) for a in rows]


def list_review_queue(conn: sqlite3.Connection, user_id: int) -> dict:
    user = _user_or_raise(conn, user_id)
    if user["role"] not in ("gardener", "supervisor"):
        raise AuthzError()
    open_rows = conn.execute(
        "SELECT id FROM anomaly_reports WHERE status='open' ORDER BY id")
    review_rows = conn.execute(
        "SELECT id FROM anomaly_reports WHERE status='needs_review' ORDER BY id")
    return {
        "role": user["role"],
        "open_reports": [_anomaly_dict(conn, r["id"], user) for r in open_rows],
        "review_queue": [_anomaly_dict(conn, r["id"], user) for r in review_rows],
    }


def _annual_dict(conn, adoption_id: int, year: int) -> dict:
    r = conn.execute(
        "SELECT * FROM annual_reports WHERE adoption_id=? AND year=?",
        (adoption_id, year)).fetchone()
    if r is None:
        raise NotFoundError("年度报告尚未生成")
    return dict(r)


def list_notifications(conn: sqlite3.Connection, user_id: int) -> list[dict]:
    _user_or_raise(conn, user_id)
    return [dict(r) for r in conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (user_id,))]
