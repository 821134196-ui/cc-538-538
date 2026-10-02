"""端到端业务测试：期限互斥(含并发)、占位释放、异常审批、跨用户隐私，
另覆盖预置移栽案例、逾期清扫、认养退出。

运行：python3 -m unittest -v   或   python3 tests/test_all.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import services
from db import connect, init_db
import seed as seed_mod
from server import build_server


class DBTestBase(unittest.TestCase):
    seeded = False

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "test.db"
        self.photo_dir = Path(self._tmp.name) / "photos"
        self.photo_dir.mkdir()
        self.conn = connect(self.db_path)
        init_db(self.conn)
        if self.seeded:
            # 让种子照片也写入临时目录
            self._orig_photo_dir = seed_mod.PHOTO_DIR
            seed_mod.PHOTO_DIR = self.photo_dir
            seed_mod.seed(self.conn)
        else:
            self._minimal_users()
        self.today = date.today()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()
        if self.seeded:
            seed_mod.PHOTO_DIR = self._orig_photo_dir

    def _minimal_users(self):
        now = datetime(2026, 1, 1, 9, 0).strftime("%Y-%m-%dT%H:%M:%S")
        for uid, name, role in [(1, "李阿姨", "resident"), (2, "王居民", "resident"),
                                (3, "周同学", "resident"), (10, "陈园艺", "gardener"),
                                (20, "赵主管", "supervisor")]:
            self.conn.execute(
                "INSERT INTO users(id,name,role,created_at) VALUES (?,?,?,?)",
                (uid, name, role, now))

    def add_tree(self, tid="T100", health="healthy", x=50.0, y=50.0):
        self.conn.execute(
            "INSERT INTO trees(id,species,x,y,health_status,adoptable_from,adoptable_to,"
            "photo_seed) VALUES (?,?,?,?,?,'2026-01-01','2027-12-31',1)",
            (tid, "测试树", x, y, health))

    def apply(self, uid, tid, start, end, mode="watering", now=None):
        return services.apply_adoption(self.conn, uid, tid, start, end, mode, now=now)


# --------------------------------------------------------------- 1. 期限互斥

class TestAdoptionMutex(DBTestBase):
    def setUp(self):
        super().setUp()
        self.add_tree()

    def test_overlapping_dates_rejected(self):
        s, e = "2026-03-01", "2026-06-01"
        a1 = self.apply(1, "T100", s, e)
        self.assertEqual(a1["status"], "pending")
        # 完全重叠
        with self.assertRaises(services.ServiceError) as cm:
            self.apply(2, "T100", s, e)
        self.assertEqual(cm.exception.status, 409)
        # 部分重叠（头部相交）
        with self.assertRaises(services.ServiceError):
            self.apply(2, "T100", "2026-02-01", "2026-03-10")
        # 包含关系
        with self.assertRaises(services.ServiceError):
            self.apply(2, "T100", "2026-04-01", "2026-04-10")

    def test_adjacent_and_disjoint_ranges_allowed(self):
        self.apply(1, "T100", "2026-03-01", "2026-06-01")
        # 首尾相接不重叠（end == next.start 允许）
        self.apply(2, "T100", "2026-06-01", "2026-09-01")
        # 更早的空档
        self.apply(3, "T100", "2026-01-05", "2026-03-01")

    def test_active_blocks_too(self):
        a = self.apply(1, "T100", "2026-03-01", "2026-06-01",
                       now=datetime(2026, 3, 1, 9, 0))
        services.pay_adoption(self.conn, a["id"], 1, now=datetime(2026, 3, 1, 9, 5))
        with self.assertRaises(services.ServiceError):
            self.apply(2, "T100", "2026-05-01", "2026-05-10")

    def test_lockdown_tree_not_adoptable(self):
        self.add_tree("T101", health="sick_locked")
        with self.assertRaises(services.ServiceError):
            self.apply(1, "T101", "2026-03-01", "2026-06-01")

    def test_concurrent_applications_only_one_wins(self):
        """并发申请按提交时间锁定名额：N 线程抢同一期限，恰好一单成功。"""
        results, winners, errors = [], [], []
        barrier = threading.Barrier(8)

        def worker(uid):
            c = connect(self.db_path)
            try:
                barrier.wait()
                a = services.apply_adoption(
                    c, uid, "T100", "2026-07-01", "2026-10-01", "watering")
                winners.append(a["id"])
            except Exception as exc:
                errors.append(exc)
            finally:
                c.close()

        threads = [threading.Thread(target=worker, args=(uid,))
                   for uid in (1, 2, 3, 11, 12, 13, 14, 15)]
        # 10/11 等不是 resident —— 为保证"恰好一单"，全部用居民
        threads = [threading.Thread(target=worker, args=(uid,))
                   for uid in (1, 2, 3, 4, 5, 6, 7, 8)]
        # 补建居民 4..8
        for uid in range(4, 9):
            self.conn.execute(
                "INSERT INTO users(id,name,role,created_at) VALUES (?,?,'resident','2026-01-01T00:00:00')",
                (uid, f"居民{uid}"))
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(winners), 1, f"应有且仅有 1 个名额，实际 {len(winners)}")
        self.assertEqual(len(errors), 7)
        self.assertTrue(all(e.status == 409 for e in errors))
        pending = self.conn.execute(
            "SELECT COUNT(*) c FROM adoptions WHERE tree_id='T100' AND status='pending'"
        ).fetchone()["c"]
        self.assertEqual(pending, 1)


# --------------------------------------------------------------- 2. 占位释放

class TestHoldRelease(DBTestBase):
    def setUp(self):
        super().setUp()
        self.add_tree()
        self.t0 = datetime(2026, 3, 1, 9, 0)

    def test_hold_expires_and_slot_reopens(self):
        a = self.apply(2, "T100", "2026-03-10", "2026-06-10", now=self.t0)
        # 未到期：别人不能占
        with self.assertRaises(services.ServiceError):
            self.apply(1, "T100", "2026-03-10", "2026-06-10", now=self.t0 + timedelta(minutes=29))
        # 30 分钟后清扫：释放
        released = services.sweep_expired_holds(self.conn, self.t0 + timedelta(minutes=31))
        self.assertEqual(released, 1)
        row = self.conn.execute("SELECT status FROM adoptions WHERE id=?", (a["id"],)).fetchone()
        self.assertEqual(row["status"], "expired")
        # 名额重新开放，另一居民可申请
        a2 = self.apply(1, "T100", "2026-03-10", "2026-06-10", now=self.t0 + timedelta(minutes=32))
        self.assertEqual(a2["status"], "pending")

    def test_pay_after_expiry_rejected(self):
        a = self.apply(2, "T100", "2026-03-10", "2026-06-10", now=self.t0)
        services.sweep_expired_holds(self.conn, self.t0 + timedelta(minutes=31))
        with self.assertRaises(services.ServiceError) as cm:
            services.pay_adoption(self.conn, a["id"], 2, now=self.t0 + timedelta(minutes=32))
        self.assertEqual(cm.exception.status, 409)

    def test_pay_in_time_activates_and_plans(self):
        a = self.apply(1, "T100", "2026-03-01", "2026-05-01", now=self.t0)
        out = services.pay_adoption(self.conn, a["id"], 1, now=self.t0 + timedelta(minutes=10))
        self.assertEqual(out["status"], "active")
        n = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE adoption_id=?", (a["id"],)).fetchone()["c"]
        self.assertGreater(n, 0)  # 浇水/修剪/病虫观察计划已生成


# --------------------------------------------------------------- 3. 异常审批

class TestAnomalyReview(DBTestBase):
    def setUp(self):
        super().setUp()
        self.add_tree()
        self.t0 = datetime(2026, 3, 1, 9, 0)

    def _report(self):
        return services.report_anomaly(
            self.conn, "T100", 2, "wilt", "叶子发蔫下垂", now=self.t0)

    def test_resident_cannot_diagnose_or_change_health(self):
        r = self._report()
        with self.assertRaises(services.AuthzError):
            services.submit_diagnosis(self.conn, r["id"], 2, "我说它病了", True,
                                      "sick_locked", now=self.t0)
        # 健康状态未被居民上报改变
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id='T100'").fetchone()["health_status"],
            "healthy")

    def test_false_report_workflow_adds_followup(self):
        r = self._report()
        out = services.submit_diagnosis(
            self.conn, r["id"], 10, "高温生理性萎蔫，非枯萎", False, now=self.t0)
        self.assertEqual(out["status"], "false_alarm")
        followup = self.conn.execute(
            "SELECT 1 FROM care_tasks WHERE tree_id='T100' AND note LIKE '%误报%复查%'"
        ).fetchone()
        self.assertIsNotNone(followup)
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id='T100'").fetchone()["health_status"],
            "healthy")

    def test_diagnosis_requires_supervisor_two_level(self):
        r = self._report()
        services.submit_diagnosis(
            self.conn, r["id"], 10, "确诊根腐，建议封控", True,
            "sick_locked", now=self.t0)
        # 待复核期间健康状态尚未改变
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id='T100'").fetchone()["health_status"],
            "healthy")
        # 居民与园艺师都不能复核
        with self.assertRaises(services.AuthzError):
            services.review_diagnosis(self.conn, r["id"], 2, True, now=self.t0)
        with self.assertRaises(services.AuthzError):
            services.review_diagnosis(self.conn, r["id"], 10, True, now=self.t0)
        # 主管驳回 → 维持原状
        r2 = self._report()
        services.submit_diagnosis(
            self.conn, r2["id"], 10, "另一处疑似问题", True, "monitor", now=self.t0)
        services.review_diagnosis(self.conn, r2["id"], 20, False, "证据不足", now=self.t0)
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id='T100'").fetchone()["health_status"],
            "healthy")
        # 主管批准封控 → 状态生效、计划冻结
        services.review_diagnosis(self.conn, r["id"], 20, True, "同意封控", now=self.t0)
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id='T100'").fetchone()["health_status"],
            "sick_locked")
        scheduled = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE tree_id='T100' AND status='scheduled'"
        ).fetchone()["c"]
        self.assertEqual(scheduled, 0)

    def test_staff_only_queue(self):
        with self.assertRaises(services.AuthzError):
            services.list_review_queue(self.conn, 2)
        q = services.list_review_queue(self.conn, 10)
        self.assertIn("open_reports", q)


# --------------------------------------------------------------- 4. 跨用户隐私

class TestPrivacy(DBTestBase):
    seeded = True

    def test_other_users_adoption_hidden_at_service(self):
        # 种子中李阿姨(1) 认养 T001
        aid = self.conn.execute(
            "SELECT id FROM adoptions WHERE tree_id='T001' AND user_id=1 AND status='active'"
        ).fetchone()["id"]
        with self.assertRaises(services.AuthzError):
            services.get_adoption(self.conn, aid, viewer_id=2)
        with self.assertRaises(services.AuthzError):
            services.generate_annual_report(self.conn, aid, self.today.year, viewer_id=2)
        # 本人可见
        self.assertEqual(services.get_adoption(self.conn, aid, viewer_id=1)["id"], aid)

    def test_tree_detail_anonymized_for_other_resident(self):
        # 第三方居民周同学(3)：看不到认养人、照片上传者与他人异常记录
        other = services.get_tree_detail(self.conn, "T001", viewer_id=3)
        a = other["current_adoption"]
        self.assertTrue(a["anonymized"])
        self.assertIsNone(a["id"])
        self.assertNotIn("李阿姨", json.dumps(a, ensure_ascii=False))
        # 照片不暴露上传者身份
        self.assertTrue(other["photos"])
        for p in other["photos"]:
            self.assertIsNone(p["uploader_id"])
        # 事件流不暴露他人姓名
        for e in other["events"]:
            if e["actor_id"] != 3:
                self.assertIsNone(e["actor_name"])
        # 别人的异常记录不可见（T001 的误报上报人是居民 2）
        self.assertEqual(other["anomalies"], [])

        # 上报人本人可以看到自己的上报与处理结论
        reporter = services.get_tree_detail(self.conn, "T001", viewer_id=2)
        self.assertTrue(any(x["id"] == 5 for x in reporter["anomalies"]))

        # 认养本人视角：信息完整
        mine = services.get_tree_detail(self.conn, "T001", viewer_id=1)
        self.assertFalse(mine["current_adoption"]["anonymized"])
        self.assertEqual(mine["current_adoption"]["user_name"], "李阿姨")

    def test_resident_calendar_only_own_tasks(self):
        mine = {t["tree_id"] for t in services.get_calendar(self.conn, 1)}
        self.assertIn("T001", mine)
        # 王居民(2) 只认养了 T005，日历中不得出现李阿姨的 T001 任务
        other_t001 = [t for t in services.get_calendar(self.conn, 2) if t["tree_id"] == "T001"]
        self.assertEqual(other_t001, [])

    def test_resident_cannot_complete_staff_task(self):
        prune = self.conn.execute(
            "SELECT id FROM care_tasks WHERE tree_id='T001' AND kind='prune' LIMIT 1"
        ).fetchone()
        with self.assertRaises(services.AuthzError):
            services.complete_task(self.conn, prune["id"], 2)


# --------------------------------------------------------------- 5. 预置场景

class TestSeededScenarios(DBTestBase):
    seeded = True

    def test_transplant_case_present_and_replanned(self):
        t = self.conn.execute("SELECT * FROM trees WHERE id='T004'").fetchone()
        self.assertEqual(t["health_status"], "transplanted")
        move = self.conn.execute(
            "SELECT * FROM transplants WHERE tree_id='T004' ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNotNone(move)
        self.assertNotEqual((move["old_x"], move["old_y"]), (move["new_x"], move["new_y"]))
        # 第 7/21 天复壮观察
        obs = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE tree_id='T004' AND note LIKE '%复壮观察%'"
        ).fetchone()["c"]
        self.assertEqual(obs, 2)
        # 复壮期（移栽后 14 天）内没有待执行任务
        move_day = date.fromisoformat(move["happened_on"])
        gap_end = (move_day + timedelta(days=14)).isoformat()
        bad = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE tree_id='T004' AND status='scheduled' "
            "AND due_date <= ?", (gap_end,)).fetchone()["c"]
        self.assertEqual(bad, 0)

    def test_startup_sweep_marks_seeded_overdue(self):
        n = services.sweep_overdue(self.conn)
        self.assertGreaterEqual(n, 1)
        od = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE status='overdue'").fetchone()["c"]
        self.assertGreaterEqual(od, 1)

    def test_seeded_review_queue_has_pending_diagnosis(self):
        q = services.list_review_queue(self.conn, 20)
        ids = {r["id"] for r in q["review_queue"]}
        self.assertIn(3, ids)  # T003 刺蛾诊断待主管复核

    def test_seed_idempotent(self):
        self.assertTrue(seed_mod.is_seeded(self.conn))


# --------------------------------------------------------------- 6. 退出与逾期补做

class TestExitAndOverdue(DBTestBase):
    def setUp(self):
        super().setUp()
        self.add_tree()
        a = self.apply(1, "T100", "2026-03-01", "2026-08-01", now=datetime(2026, 3, 1, 9))
        self.aid = a["id"]
        services.pay_adoption(self.conn, self.aid, 1, now=datetime(2026, 3, 1, 9, 2))

    def test_exit_cancels_plan_and_refunds(self):
        before = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE adoption_id=? AND status='scheduled'",
            (self.aid,)).fetchone()["c"]
        self.assertGreater(before, 0)
        services.exit_adoption(self.conn, self.aid, 1, "时间冲突", now=datetime(2026, 4, 1))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM adoptions WHERE id=?", (self.aid,)).fetchone()["status"],
            "exited")
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE adoption_id=? AND status='scheduled'",
            (self.aid,)).fetchone()["c"], 0)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM payments WHERE adoption_id=?", (self.aid,)).fetchone()["status"],
            "refunded")
        # 名额释放：同期限可再认养
        a2 = self.apply(2, "T100", "2026-03-01", "2026-08-01", now=datetime(2026, 4, 2))
        self.assertEqual(a2["status"], "pending")

    def test_overdue_task_can_be_completed_late(self):
        task = self.conn.execute(
            "INSERT INTO care_tasks(tree_id,adoption_id,kind,due_date,status,assignee,created_at)"
            " VALUES ('T100',?,'water','2026-03-05','scheduled','adopter','2026-03-01T00:00:00')",
            (self.aid,)).lastrowid
        services.sweep_overdue(self.conn, datetime(2026, 3, 10))
        t = services.complete_task(self.conn, task, 1, "补浇",
                                   done_date="2026-03-09", now=datetime(2026, 3, 10))
        self.assertEqual(t["status"], "done")
        self.assertIn("逾期补做", t["note"])


# --------------------------------------------------------------- 7. HTTP 层隐私 403

class TestHttpPrivacy(DBTestBase):
    def setUp(self):
        super().setUp()
        self.add_tree("T200")
        self.a = services.apply_adoption(
            self.conn, 1, "T200", "2026-03-01", "2026-06-01", "watering",
            now=datetime(2026, 3, 1, 9))
        self.httpd = build_server("127.0.0.1", 0, self.db_path)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd._stop_event.set()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def _req(self, method, path, uid, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
            headers={"Content-Type": "application/json", "X-User-Id": str(uid)})
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_other_user_gets_403_on_adoption(self):
        status, body = self._req("GET", f"/api/adoptions/{self.a['id']}", 2)
        self.assertEqual(status, 403)
        self.assertIn("error", body)

    def test_owner_can_view(self):
        status, body = self._req("GET", f"/api/adoptions/{self.a['id']}", 1)
        self.assertEqual(status, 200)
        self.assertEqual(body["id"], self.a["id"])

    def test_map_requires_identity(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/api/trees/map")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(cm.exception.code, 401)

    def test_other_cannot_pay(self):
        status, _ = self._req("POST", f"/api/adoptions/{self.a['id']}/pay", 2)
        self.assertEqual(status, 403)

    def test_review_forbidden_for_resident(self):
        status, _ = self._req("GET", "/api/reviews", 1)
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main(verbosity=2)
