"""领域服务层：认养互斥、占位释放、计划联动、异常复核、年报与权限边界。

所有写操作在事务内完成；SQLite 单库，BEGIN IMMEDIATE 串行化并发申请。
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from .adapters import Clock, MockNotificationAdapter, MockPaymentAdapter

HOLD_TTL_MINUTES = 15

STAFF_ROLES = ("gardener", "supervisor", "admin")


class ServiceError(Exception):
    """业务规则错误（消息可直接展示给用户）。"""


class PermissionDenied(ServiceError):
    """跨用户/越权访问错误（HTTP 403）。"""


def _d(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def season_of(d: date) -> str:
    return {12: "冬", 1: "冬", 2: "冬", 3: "春", 4: "春", 5: "春",
            6: "夏", 7: "夏", 8: "夏", 9: "秋", 10: "秋", 11: "秋"}[d.month]


class Service:
    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None,
                 photo_root: str | Path = "data/photos", hold_ttl_minutes: int = HOLD_TTL_MINUTES):
        conn.isolation_level = None  # 显式事务
        self.conn = conn
        self.clock = clock or Clock()
        self.payment = MockPaymentAdapter(conn, self.clock)
        self.notify = MockNotificationAdapter(conn, self.clock)
        self.photo_root = Path(photo_root)
        self.photo_root.mkdir(parents=True, exist_ok=True)
        self.hold_ttl = timedelta(minutes=hold_ttl_minutes)

    # ---------- 用户 ----------
    def get_user(self, user_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def get_user_by_name(self, username: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()

    def list_users(self, role: str | None = None) -> list[sqlite3.Row]:
        if role:
            return self.conn.execute("SELECT * FROM users WHERE role=? ORDER BY id", (role,)).fetchall()
        return self.conn.execute("SELECT * FROM users ORDER BY id").fetchall()

    # ---------- 树木 ----------
    def list_trees(self) -> list[sqlite3.Row]:
        today = self.clock.today()
        rows = self.conn.execute("SELECT * FROM trees ORDER BY code").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["adoption_state"] = self._adoption_state(r["id"], today)
            out.append(d)
        return out

    def _adoption_state(self, tree_id: int, today: str) -> dict:
        """认养状态（与专业健康状态分开展示）。"""
        cur_active = self.conn.execute(
            """SELECT end_date FROM adoptions
               WHERE tree_id=? AND status='active'
                 AND start_date <= ? AND end_date > ?
               ORDER BY start_date LIMIT 1""",
            (tree_id, today, today),
        ).fetchone()
        if cur_active:
            return {"state": "已认养", "until": cur_active["end_date"]}
        # 未支付占位（按提交时间先后）优先提示名额锁定中
        hold = self.conn.execute(
            """SELECT start_date, end_date, MIN(created_at) AS created_at FROM adoptions
               WHERE tree_id=? AND status='pending_payment' AND end_date > ?
               GROUP BY start_date, end_date ORDER BY created_at LIMIT 1""",
            (tree_id, today),
        ).fetchone()
        if hold:
            return {"state": "名额锁定中", "from": hold["start_date"],
                    "until": hold["end_date"]}
        nxt = self.conn.execute(
            """SELECT start_date, end_date FROM adoptions
               WHERE tree_id=? AND status='active' AND start_date > ?
               ORDER BY start_date LIMIT 1""",
            (tree_id, today),
        ).fetchone()
        if nxt:
            return {"state": "已被预约", "from": nxt["start_date"], "until": nxt["end_date"]}
        return {"state": "可认养", "until": None}

    def get_tree(self, tree_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM trees WHERE id=?", (tree_id,)).fetchone()
        if not row:
            raise ServiceError("树木不存在")
        return row

    def tree_care_records(self, tree_id: int) -> dict:
        """真实执行记录，按执行季节排列。"""
        logs = self.conn.execute(
            """SELECT l.*, t.kind, t.planned_date AS planned_date,
                      t.season AS planned_season, u.display_name AS actor_name
               FROM care_logs l
               JOIN care_tasks t ON t.id=l.task_id
               JOIN users u ON u.id=l.actor_id
               WHERE l.tree_id=? ORDER BY l.executed_on""",
            (tree_id,),
        ).fetchall()
        grouped: dict[str, list] = {"春": [], "夏": [], "秋": [], "冬": []}
        for l in logs:
            grouped[season_of(_d(l["executed_on"]))].append(dict(l))
        return grouped

    def tree_events(self, tree_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT e.*, u.display_name AS actor_name FROM tree_events e
               LEFT JOIN users u ON u.id=e.actor_id
               WHERE e.tree_id=? ORDER BY e.id""",
            (tree_id,),
        ).fetchall()

    # ---------- 认养申请：互斥 + 按提交时间锁定 ----------
    def release_expired_holds(self) -> int:
        """未支付占位到期释放。返回释放条数。"""
        cutoff = (self.clock.now() - self.hold_ttl).isoformat(timespec="seconds")
        rows = self.conn.execute(
            "SELECT id, user_id, tree_id FROM adoptions "
            "WHERE status='pending_payment' AND created_at <= ?",
            (cutoff,),
        ).fetchall()
        for r in rows:
            self.conn.execute(
                "UPDATE adoptions SET status='expired', released_at=? WHERE id=?",
                (self.clock.now().isoformat(timespec="seconds"), r["id"]),
            )
            self.conn.execute(
                "UPDATE care_tasks SET status='已取消', note=note||'（占位到期释放）' "
                "WHERE adoption_id=? AND status='待执行'",
                (r["id"],),
            )
            self._event(r["tree_id"], "占位到期释放", {"adoption_id": r["id"]})
            self.notify.send(r["user_id"], "认养占位已释放",
                             "您的认养申请未在支付时限内完成支付，名额已释放，可重新申请。")
        if rows:
            self.conn.commit()
        return len(rows)

    def apply_adoption(self, tree_id: int, user_id: int, start_date: str,
                       end_date: str, care_mode: str) -> int:
        if care_mode not in ("自助养护", "委托园艺师"):
            raise ServiceError("养护方式无效")
        s, e = _d(start_date), _d(end_date)
        if e <= s:
            raise ServiceError("认养期限无效：结束日期必须晚于开始日期")
        if s < self.clock.now().date():
            raise ServiceError("认养开始日期不能早于今天")

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self._expire_holds_locked()
            tree = self.conn.execute(
                "SELECT * FROM trees WHERE id=?", (tree_id,)).fetchone()
            if not tree:
                raise ServiceError("树木不存在")
            if not tree["adoptable"]:
                raise ServiceError("该树木暂未开放认养")
            if tree["health_status"] in ("患病封控", "移栽中"):
                raise ServiceError(f"树木当前为「{tree['health_status']}」状态，暂停认养")
            clash = self.conn.execute(
                """SELECT id, status, start_date, end_date FROM adoptions
                   WHERE tree_id=? AND status IN ('active','pending_payment')
                     AND start_date < ? AND end_date > ?
                   ORDER BY created_at LIMIT 1""",
                (tree_id, end_date, start_date),
            ).fetchone()
            if clash:
                who = "已有认养" if clash["status"] == "active" else "名额已被更早提交的申请锁定"
                raise ServiceError(
                    f"认养期限冲突：{who}（{clash['start_date']} ~ {clash['end_date']}），"
                    "同一棵树的认养期限不可重叠")
            fee = max((e - s).days, 1) * 10  # 0.10 元/天（模拟）
            cur = self.conn.execute(
                """INSERT INTO adoptions
                   (tree_id,user_id,start_date,end_date,care_mode,status,created_at,fee_cents)
                   VALUES (?,?,?,?,?, 'pending_payment', ?, ?)""",
                (tree_id, user_id, start_date, end_date, care_mode,
                 self.clock.now().isoformat(timespec="seconds"), fee),
            )
            adoption_id = cur.lastrowid
            self._event_locked(tree_id, "提交认养申请",
                               {"adoption_id": adoption_id, "start": start_date,
                                "end": end_date, "care_mode": care_mode}, user_id)
            self.conn.commit()
            return adoption_id
        except Exception:
            self.conn.rollback()
            raise

    def _expire_holds_locked(self) -> None:
        cutoff = (self.clock.now() - self.hold_ttl).isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE adoptions SET status='expired', released_at=? "
            "WHERE status='pending_payment' AND created_at <= ?",
            (self.clock.now().isoformat(timespec="seconds"), cutoff),
        )

    def pay_adoption(self, adoption_id: int, user_id: int) -> dict:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            a = self.conn.execute(
                "SELECT * FROM adoptions WHERE id=?", (adoption_id,)).fetchone()
            if not a:
                raise ServiceError("申请不存在")
            if a["user_id"] != user_id:
                raise PermissionDenied("无权操作他人的认养申请")
            if a["status"] == "expired":
                raise ServiceError("占位已到期释放，请重新申请")
            if a["status"] != "pending_payment":
                raise ServiceError("该申请无需支付")
            # 支付瞬间再查一次是否已超时（TTL 以创建时间为准）
            if self.clock.now() - datetime.fromisoformat(a["created_at"]) > self.hold_ttl:
                self.conn.execute(
                    "UPDATE adoptions SET status='expired', released_at=? WHERE id=?",
                    (self.clock.now().isoformat(timespec="seconds"), adoption_id))
                self._event_locked(a["tree_id"], "占位到期释放",
                                   {"adoption_id": adoption_id}, user_id)
                self.conn.commit()
                raise ServiceError("占位已到期释放，请重新申请")
            res = self.payment.charge(adoption_id, a["fee_cents"])
            self.conn.execute(
                "UPDATE adoptions SET status='active', paid_at=? WHERE id=?",
                (res["paid_at"], adoption_id))
            self._event_locked(a["tree_id"], "支付完成，认养生效",
                               {"adoption_id": adoption_id}, user_id)
            self.notify.send(user_id, "认养支付成功",
                             f"您认养的树木已生效（{a['start_date']} ~ {a['end_date']}），"
                             "养护计划将由园艺师安排。")
            self.conn.commit()
            return res
        except Exception:
            self.conn.rollback()
            raise

    def exit_adoption(self, adoption_id: int, user_id: int, reason: str) -> None:
        """认养人主动退出：释放未来期限，计划随之更新。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            a = self.conn.execute(
                "SELECT * FROM adoptions WHERE id=?", (adoption_id,)).fetchone()
            if not a:
                raise ServiceError("认养记录不存在")
            if a["user_id"] != user_id:
                raise PermissionDenied("无权操作他人的认养记录")
            if a["status"] != "active":
                raise ServiceError("仅生效中的认养可以退出")
            now = self.clock.now().isoformat(timespec="seconds")
            today = self.clock.today()
            self.conn.execute(
                "UPDATE adoptions SET status='exited', released_at=?, exit_reason=? WHERE id=?",
                (now, reason, adoption_id))
            self.conn.execute(
                "UPDATE care_tasks SET status='已取消', note=note||'（认养人退出）' "
                "WHERE adoption_id=? AND status='待执行' AND planned_date >= ?",
                (adoption_id, today))
            self._event_locked(a["tree_id"], "认养人退出",
                               {"adoption_id": adoption_id, "reason": reason}, user_id)
            self.notify.send(user_id, "已退出认养", "您的退出申请已登记，后续养护任务已取消。")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def my_adoptions(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT a.*, t.code, t.species, t.location FROM adoptions a
               JOIN trees t ON t.id=a.tree_id
               WHERE a.user_id=? ORDER BY a.created_at DESC""",
            (user_id,),
        ).fetchall()

    def get_adoption(self, adoption_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            """SELECT a.*, t.code, t.species, t.location FROM adoptions a
               JOIN trees t ON t.id=a.tree_id WHERE a.id=?""",
            (adoption_id,)).fetchone()
        if not row:
            raise ServiceError("认养记录不存在")
        return row

    def require_owner_or_staff(self, adoption_id: int, user: sqlite3.Row) -> sqlite3.Row:
        a = self.get_adoption(adoption_id)
        if a["user_id"] != user["id"] and user["role"] not in STAFF_ROLES:
            raise PermissionDenied("无权查看该认养记录")
        return a

    # ---------- 养护计划 ----------
    def add_task(self, actor: sqlite3.Row, tree_id: int, kind: str,
                 planned_date: str, note: str = "", adoption_id: int | None = None) -> int:
        if actor["role"] not in STAFF_ROLES:
            raise ServiceError("仅园艺师可制定养护计划")
        if kind not in ("浇水", "修剪", "病虫观察"):
            raise ServiceError("任务类型无效")
        _d(planned_date)  # 校验格式
        cur = self.conn.execute(
            """INSERT INTO care_tasks
               (tree_id,adoption_id,kind,planned_date,season,status,note,created_at)
               VALUES (?,?,?,?,?, '待执行', ?,?)""",
            (tree_id, adoption_id, kind, planned_date,
             season_of(_d(planned_date)), note,
             self.clock.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()
        return cur.lastrowid

    def plan_for_adoption(self, actor: sqlite3.Row, adoption_id: int) -> int:
        """园艺师按认养期限生成一版常规计划：春夏秋每月浇水2次、病虫观察1次；冬季修剪1次。"""
        if actor["role"] not in STAFF_ROLES:
            raise ServiceError("仅园艺师可制定养护计划")
        a = self.get_adoption(adoption_id)
        s, e = _d(a["start_date"]), _d(a["end_date"])
        n = 0
        cur = s
        while cur < e:
            m = season_of(cur)
            if m in ("春", "夏", "秋"):
                for day in (2, 16):
                    d0 = cur.replace(day=min(day, 28))
                    if s <= d0 < e:
                        n += self._insert_task(a["tree_id"], adoption_id, "浇水", d0.isoformat(), "常规计划")
                obs = cur.replace(day=min(10, 28))
                if s <= obs < e:
                    n += self._insert_task(a["tree_id"], adoption_id, "病虫观察", obs.isoformat(), "常规计划")
            elif m == "冬" and cur.month == 12:
                d0 = cur.replace(day=15)
                if s <= d0 < e:
                    n += self._insert_task(a["tree_id"], adoption_id, "修剪", d0.isoformat(), "冬季修剪")
            if cur.month == 12:
                cur = cur.replace(year=cur.year + 1, month=1)
            else:
                cur = cur.replace(month=cur.month + 1)
        self.conn.commit()
        return n

    def _insert_task(self, tree_id, adoption_id, kind, planned_date, note) -> int:
        cur = self.conn.execute(
            """INSERT INTO care_tasks
               (tree_id,adoption_id,kind,planned_date,season,status,note,created_at)
               VALUES (?,?,?,?,?, '待执行', ?,?)""",
            (tree_id, adoption_id, kind, planned_date, season_of(_d(planned_date)),
             note, self.clock.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid

    def list_tasks(self, viewer: sqlite3.Row, tree_id: int | None = None,
                   status: str | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT t.*, tr.code, tr.species, a.care_mode AS care_mode, "
               "a.user_id AS adoption_user_id FROM care_tasks t "
               "JOIN trees tr ON tr.id=t.tree_id "
               "LEFT JOIN adoptions a ON a.id=t.adoption_id")
        where, params = [], []
        if viewer["role"] not in STAFF_ROLES:
            # 居民可见：自己认养产生的任务 + 自己认养树上的园方专项任务
            # （如移栽复壮观察、封控期观察，adoption_id 为空）。
            where.append(
                "(EXISTS(SELECT 1 FROM adoptions a WHERE a.id=t.adoption_id "
                "AND a.user_id=? AND a.status IN ('active','exited')) "
                "OR (t.adoption_id IS NULL AND EXISTS(SELECT 1 FROM adoptions a2 "
                "WHERE a2.tree_id=t.tree_id AND a2.user_id=? "
                "AND a2.status IN ('active','exited'))))")
            params.extend([viewer["id"], viewer["id"]])
        if tree_id:
            where.append("t.tree_id=?")
            params.append(tree_id)
        if status:
            where.append("t.status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY t.planned_date"
        return self.conn.execute(sql, params).fetchall()

    def complete_task(self, actor: sqlite3.Row, task_id: int, summary: str,
                      photo_path: str | None = None) -> int:
        """登记真实执行记录。自助养护模式下认养人可登记，其余需园艺师。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            t = self.conn.execute("SELECT * FROM care_tasks WHERE id=?", (task_id,)).fetchone()
            if not t:
                raise ServiceError("任务不存在")
            if t["status"] in ("已完成", "已取消"):
                raise ServiceError(f"任务已{t['status']}，不能重复执行")
            if actor["role"] not in STAFF_ROLES:
                a = self.conn.execute(
                    "SELECT * FROM adoptions WHERE id=? AND user_id=?",
                    (t["adoption_id"], actor["id"])).fetchone()
                if not a or a["care_mode"] != "自助养护":
                    raise ServiceError("该任务由园艺师执行")
            today = self.clock.today()
            cur = self.conn.execute(
                """INSERT INTO care_logs
                   (task_id,tree_id,executed_on,actor_id,summary,photo_path,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (task_id, t["tree_id"], today, actor["id"], summary, photo_path,
                 self.clock.now().isoformat(timespec="seconds")),
            )
            overdue = today > t["planned_date"]
            self.conn.execute("UPDATE care_tasks SET status='已完成' WHERE id=?", (task_id,))
            if overdue:
                self._event_locked(t["tree_id"], "逾期任务补执行",
                                   {"task_id": task_id, "planned": t["planned_date"]}, actor["id"])
            self.conn.commit()
            return cur.lastrowid
        except Exception:
            self.conn.rollback()
            raise

    def sweep_overdue(self) -> int:
        """逾期任务：标记、通知，并自动重排一次（计划更新）。"""
        today = self.clock.today()
        rows = self.conn.execute(
            "SELECT * FROM care_tasks WHERE status='待执行' AND planned_date < ?",
            (today,),
        ).fetchall()
        for t in rows:
            self.conn.execute("UPDATE care_tasks SET status='已逾期' WHERE id=?", (t["id"],))
            new_date = (_d(t["planned_date"]) + timedelta(days=7)).isoformat()
            self._insert_task(t["tree_id"], t["adoption_id"], t["kind"], new_date,
                             f"原 {t['planned_date']} 任务逾期，自动重排")
            self._event(t["tree_id"], "养护任务逾期",
                        {"task_id": t["id"], "kind": t["kind"],
                         "planned": t["planned_date"], "rescheduled": new_date})
            if t["adoption_id"]:
                a = self.conn.execute(
                    "SELECT user_id FROM adoptions WHERE id=?", (t["adoption_id"],)).fetchone()
                if a:
                    self.notify.send(a["user_id"], "养护任务逾期提醒",
                                     f"{t['kind']}（计划 {t['planned_date']}）已逾期，"
                                     f"已为您重排至 {new_date}。")
            for g in self.list_users("gardener"):
                self.notify.send(g["id"], "任务逾期",
                                 f"树木 #{t['tree_id']} 的{t['kind']}任务逾期，已重排。")
        if rows:
            self.conn.commit()
        return len(rows)

    # ---------- 异常诊断与复核 ----------
    def submit_diagnosis(self, gardener: sqlite3.Row, tree_id: int, kind: str,
                         detail: str, proposed_health: str | None = None) -> int:
        """园艺师提交异常诊断，进入主管复核。注意：提交不等于修改健康结论。"""
        if gardener["role"] != "gardener":
            raise ServiceError("仅园艺师可提交异常诊断")
        if kind not in ("病害", "虫害", "枯萎误报", "移栽", "其他"):
            raise ServiceError("异常类型无效")
        if proposed_health and proposed_health not in ("健康", "观察", "患病封控", "移栽中"):
            raise ServiceError("拟更新的健康状态无效")
        cur = self.conn.execute(
            """INSERT INTO diagnoses
               (tree_id,reporter_id,reporter_kind,kind,detail,status,proposed_health,created_at)
               VALUES (?,?,'gardener',?,?, '待复核', ?,?)""",
            (tree_id, gardener["id"], kind, detail, proposed_health,
             self.clock.now().isoformat(timespec="seconds")),
        )
        diag_id = cur.lastrowid
        self._event(tree_id, "园艺师提交异常诊断",
                    {"diagnosis_id": diag_id, "kind": kind}, gardener["id"])
        for s in self.list_users("supervisor"):
            self.notify.send(s["id"], "待复核：异常诊断",
                             f"园艺师提交了 {kind} 诊断，请复核。")
        self.conn.commit()
        return diag_id

    def report_withering(self, resident: sqlite3.Row, tree_id: int, detail: str) -> int:
        """居民报告疑似枯萎：由园艺师现场核查（不是专业健康结论）。"""
        if resident["role"] != "resident":
            raise ServiceError("该入口仅供居民报告")
        cur = self.conn.execute(
            """INSERT INTO diagnoses
               (tree_id,reporter_id,reporter_kind,kind,detail,status,created_at)
               VALUES (?,?,'resident','病害',?, '待复核',?)""",
            (tree_id, resident["id"], f"居民疑似枯萎报告：{detail}",
             self.clock.now().isoformat(timespec="seconds")),
        )
        diag_id = cur.lastrowid
        self._event(tree_id, "居民报告疑似枯萎",
                    {"diagnosis_id": diag_id}, resident["id"])
        for g in self.list_users("gardener"):
            self.notify.send(g["id"], "居民枯萎报告待核查",
                             f"居民报告树木 #{tree_id} 疑似枯萎，请现场核查。")
        self.conn.commit()
        return diag_id

    def list_diagnoses(self, viewer: sqlite3.Row, status: str | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT d.*, t.code, t.species, u.display_name AS reporter_name FROM diagnoses d "
               "JOIN trees t ON t.id=d.tree_id JOIN users u ON u.id=d.reporter_id")
        where, params = [], []
        if status:
            where.append("d.status=?")
            params.append(status)
        if viewer["role"] == "resident":
            where.append("d.reporter_id=?")
            params.append(viewer["id"])
        if where:
            sql += " WHERE " + " AND ".join(where)
        return self.conn.execute(sql + " ORDER BY d.id DESC", params).fetchall()

    def get_diagnosis(self, diag_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            """SELECT d.*, t.code, t.species, u.display_name AS reporter_name,
                      r.display_name AS reviewer_name
               FROM diagnoses d JOIN trees t ON t.id=d.tree_id
               JOIN users u ON u.id=d.reporter_id
               LEFT JOIN users r ON r.id=d.reviewer_id WHERE d.id=?""",
            (diag_id,)).fetchone()
        if not row:
            raise ServiceError("诊断记录不存在")
        return row

    def review_diagnosis(self, reviewer: sqlite3.Row, diag_id: int,
                         approve: bool, note: str) -> None:
        """复核入口：园艺师诊断→必须主管；居民报告→园艺师核查。复核后联动计划。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            d = self.conn.execute("SELECT * FROM diagnoses WHERE id=?", (diag_id,)).fetchone()
            if not d:
                raise ServiceError("诊断记录不存在")
            if d["status"] != "待复核":
                raise ServiceError("该诊断已复核")
            if d["reporter_kind"] == "gardener" and reviewer["role"] != "supervisor":
                raise ServiceError("园艺师的异常诊断须由主管复核")
            if d["reporter_kind"] == "resident" and reviewer["role"] != "gardener":
                raise ServiceError("居民报告由园艺师现场核查")

            now = self.clock.now().isoformat(timespec="seconds")
            new_status = "已确认" if approve else "已驳回"
            self.conn.execute(
                "UPDATE diagnoses SET status=?, reviewer_id=?, review_note=?, reviewed_at=? WHERE id=?",
                (new_status, reviewer["id"], note, now, diag_id))

            if approve:
                if d["reporter_kind"] == "resident":
                    # 园艺师现场核查确认确有异常：不能直接改健康结论，
                    # 转为园艺师诊断提交主管复核。
                    self.conn.execute(
                        """INSERT INTO diagnoses
                           (tree_id,reporter_id,reporter_kind,kind,detail,status,
                            proposed_health,created_at)
                           VALUES (?,?,'gardener','病害',?, '待复核','观察',?)""",
                        (d["tree_id"], reviewer["id"],
                         f"居民枯萎报告经现场核查属实：{d['detail']}；核查意见：{note}", now))
                    for s in self.list_users("supervisor"):
                        self.notify.send(s["id"], "待复核：异常诊断",
                                         "居民枯萎报告经园艺师核查属实，已转主管复核。")
                    self._event_locked(d["tree_id"], "居民报告核查属实，转主管复核",
                                       {"source_diagnosis_id": diag_id}, reviewer["id"])
                else:
                    # 主管确认后，专业健康结论才允许变更——这是全系统唯一的健康结论写入路径。
                    if d["proposed_health"]:
                        self.conn.execute("UPDATE trees SET health_status=? WHERE id=?",
                                          (d["proposed_health"], d["tree_id"]))
                    if d["kind"] == "移栽":
                        self._apply_transplant_locked(d["tree_id"], note, reviewer["id"])
                    elif d["kind"] == "病害" and d["proposed_health"] == "患病封控":
                        self._apply_lockdown_locked(d["tree_id"], note, reviewer["id"])
                    self._event_locked(d["tree_id"], "异常诊断已确认",
                                       {"diagnosis_id": diag_id, "kind": d["kind"]}, reviewer["id"])
            else:
                if d["reporter_kind"] == "resident":
                    # 居民误报枯萎：登记误报事件并更新关怀计划。
                    self._apply_false_alarm_locked(d["tree_id"], reviewer["id"])
                    self._event_locked(d["tree_id"], "居民误报枯萎",
                                       {"diagnosis_id": diag_id, "note": note}, reviewer["id"])
                else:
                    self._event_locked(d["tree_id"], "异常诊断已驳回",
                                       {"diagnosis_id": diag_id}, reviewer["id"])
            self.notify.send(d["reporter_id"],
                             "异常复核结果" if d["reporter_kind"] == "gardener" else "枯萎报告核查结果",
                             f"您提交的记录复核结果：{new_status}。{note}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _apply_transplant_locked(self, tree_id: int, note: str, actor_id: int) -> None:
        today = self.clock.today()
        self.conn.execute(
            "UPDATE care_tasks SET status='已取消', note=note||'（树木移栽，计划中止）' "
            "WHERE tree_id=? AND status='待执行' AND planned_date>=?",
            (tree_id, today))
        # 移栽后两周安排复壮观察
        for days in (7, 14):
            self._insert_task(tree_id, None, "病虫观察",
                              (_d(today) + timedelta(days=days)).isoformat(),
                              f"移栽后复壮观察（{note or '主管确认移栽'}）")
        adopters = self.conn.execute(
            "SELECT DISTINCT user_id FROM adoptions WHERE tree_id=? AND status='active'",
            (tree_id,)).fetchall()
        for a in adopters:
            self.notify.send(a["user_id"], "认养树木移栽通知",
                             "您认养的树木将进行移栽，原养护计划已调整，移栽后将安排复壮观察。")

    def _apply_lockdown_locked(self, tree_id: int, note: str, actor_id: int) -> None:
        today = self.clock.today()
        self.conn.execute(
            "UPDATE care_tasks SET status='已取消', note=note||'（患病封控，暂停常规养护）' "
            "WHERE tree_id=? AND status='待执行' AND planned_date>=? AND kind IN ('浇水','修剪')",
            (tree_id, today))
        # 封控期加密病虫观察：连续四周每周一次
        for days in (3, 10, 17, 24):
            self._insert_task(tree_id, None, "病虫观察",
                              (_d(today) + timedelta(days=days)).isoformat(),
                              f"封控期病虫观察（{note or '主管确认封控'}）")
        adopters = self.conn.execute(
            "SELECT DISTINCT user_id FROM adoptions WHERE tree_id=? AND status='active'",
            (tree_id,)).fetchall()
        for a in adopters:
            self.notify.send(a["user_id"], "认养树木患病封控通知",
                             "您认养的树木已患病封控，常规养护暂停，改为每周病虫观察。")

    def _apply_false_alarm_locked(self, tree_id: int, gardener_id: int) -> None:
        today = _d(self.clock.today())
        self._insert_task(tree_id, None, "浇水", (today + timedelta(days=1)).isoformat(),
                          "居民误报枯萎：现场核查正常，补水一次")
        self._insert_task(tree_id, None, "病虫观察", (today + timedelta(days=3)).isoformat(),
                          "居民误报枯萎：安排一次复查")

    # ---------- 照片 ----------
    def save_photo(self, tree_id: int, uploader_id: int, filename: str,
                   data: bytes, caption: str, taken_on: str | None = None) -> int:
        self.get_tree(tree_id)
        ext = Path(filename).suffix.lower()
        if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg"):
            raise ServiceError("仅支持 jpg/png/gif/webp/svg 照片")
        name = f"{uuid.uuid4().hex}{ext}"
        month_dir = self.photo_root / self.clock.today()[:7].replace("-", "")
        month_dir.mkdir(parents=True, exist_ok=True)
        (month_dir / name).write_bytes(data)
        rel = str((month_dir / name).relative_to(self.photo_root.parent))
        cur = self.conn.execute(
            """INSERT INTO photos(tree_id,uploader_id,taken_on,path,caption,created_at)
               VALUES (?,?,?,?,?,?)""",
            (tree_id, uploader_id, taken_on or self.clock.today(), rel, caption,
             self.clock.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_photos(self, tree_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT p.*, u.display_name AS uploader_name FROM photos p
               JOIN users u ON u.id=p.uploader_id
               WHERE p.tree_id=? ORDER BY p.taken_on DESC, p.id DESC""",
            (tree_id,),
        ).fetchall()

    # ---------- 年度反馈 ----------
    def generate_annual_report(self, actor: sqlite3.Row, tree_id: int, year: int) -> int:
        if actor["role"] not in STAFF_ROLES:
            raise ServiceError("年度报告由园方编制")
        start, end = f"{year}-01-01", f"{year}-12-31"
        agg = self.conn.execute(
            """SELECT t.kind, COUNT(*) c FROM care_logs l JOIN care_tasks t ON t.id=l.task_id
               WHERE l.tree_id=? AND l.executed_on BETWEEN ? AND ? GROUP BY t.kind""",
            (tree_id, start, end)).fetchall()
        counts = {r["kind"]: r["c"] for r in agg}
        alerts = self.conn.execute(
            "SELECT COUNT(*) c FROM diagnoses WHERE tree_id=? AND status='已确认' "
            "AND created_at BETWEEN ? AND ?", (tree_id, start + "T00:00:00", end + "T23:59:59")).fetchone()["c"]
        tree = self.get_tree(tree_id)
        summary = (f"{year} 年度，{tree['code']}（{tree['species']}）全年完成浇水 {counts.get('浇水',0)} 次、"
                   f"修剪 {counts.get('修剪',0)} 次、病虫观察 {counts.get('病虫观察',0)} 次；"
                   f"确认异常 {alerts} 起，年末健康结论为「{tree['health_status']}」。")
        self.conn.execute(
            """INSERT INTO annual_reports(tree_id,year,summary,watering,pruning,pest,alerts,created_at)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(tree_id,year) DO UPDATE SET
                 summary=excluded.summary, watering=excluded.watering, pruning=excluded.pruning,
                 pest=excluded.pest, alerts=excluded.alerts, created_at=excluded.created_at""",
            (tree_id, year, summary, counts.get("浇水", 0), counts.get("修剪", 0),
             counts.get("病虫观察", 0), alerts, self.clock.now().isoformat(timespec="seconds")),
        )
        for a in self.conn.execute(
                "SELECT DISTINCT user_id FROM adoptions WHERE tree_id=? AND status IN ('active','exited')",
                (tree_id,)).fetchall():
            self.notify.send(a["user_id"], f"{year} 年度反馈已发布",
                             f"您认养的 {tree['code']} 年度报告已生成，欢迎查看。")
        self.conn.commit()
        return year

    def can_view_report(self, user: sqlite3.Row, tree_id: int, year: int) -> bool:
        if user["role"] in STAFF_ROLES:
            return True
        return self.conn.execute(
            """SELECT 1 FROM adoptions
               WHERE tree_id=? AND user_id=? AND status IN ('active','exited')
                 AND start_date <= ? AND end_date >= ? LIMIT 1""",
            (tree_id, user["id"], f"{year}-12-31", f"{year}-01-01"),
        ).fetchone() is not None

    def get_report(self, user: sqlite3.Row, tree_id: int, year: int) -> sqlite3.Row:
        if not self.can_view_report(user, tree_id, year):
            raise PermissionDenied("无权查看该年度反馈：仅该树认养人可查看")
        row = self.conn.execute(
            "SELECT * FROM annual_reports WHERE tree_id=? AND year=?",
            (tree_id, year)).fetchone()
        if not row:
            raise ServiceError("年度报告尚未发布")
        return row

    def list_reports(self, user: sqlite3.Row) -> list[sqlite3.Row]:
        sql = ("SELECT r.*, t.code, t.species FROM annual_reports r JOIN trees t ON t.id=r.tree_id")
        if user["role"] not in STAFF_ROLES:
            sql += (" WHERE EXISTS(SELECT 1 FROM adoptions a WHERE a.tree_id=r.tree_id "
                    "AND a.user_id=? AND a.status IN ('active','exited'))")
            return self.conn.execute(sql + " ORDER BY r.year DESC", (user["id"],)).fetchall()
        return self.conn.execute(sql + " ORDER BY r.year DESC").fetchall()

    # ---------- 事件 ----------
    def _event(self, tree_id, event_type, payload: dict, actor_id=None) -> None:
        self.conn.execute(
            "INSERT INTO tree_events(tree_id,event_type,payload,actor_id,created_at) VALUES (?,?,?,?,?)",
            (tree_id, event_type, json.dumps(payload, ensure_ascii=False), actor_id,
             self.clock.now().isoformat(timespec="seconds")))
        self.conn.commit()

    def _event_locked(self, tree_id, event_type, payload: dict, actor_id=None) -> None:
        self.conn.execute(
            "INSERT INTO tree_events(tree_id,event_type,payload,actor_id,created_at) VALUES (?,?,?,?,?)",
            (tree_id, event_type, json.dumps(payload, ensure_ascii=False), actor_id,
             self.clock.now().isoformat(timespec="seconds")))
