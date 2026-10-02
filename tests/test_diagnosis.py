"""异常审批：园艺师诊断须主管复核、居民报告园艺师核查、健康结论只在确认后变更、
枯萎误报更新计划、移栽/封控联动计划与认养人通知。"""
import unittest

from app.services import PermissionDenied, ServiceError

from tests import ServiceCase


class DiagnosisReviewTest(ServiceCase):
    def test_gardener_cannot_self_approve_and_resident_cannot_set_health(self):
        t1 = self.tid["T001"]
        # 居民试图通过服务层直接改健康结论——没有任何公开方法；诊断入口也限园艺师
        with self.assertRaises(ServiceError):
            self.svc.submit_diagnosis(self.u("alice"), t1, "病害", "我觉得树病了")
        did = self.svc.submit_diagnosis(self.u("chen"), t1, "病害",
                                        "叶斑病早期，建议封控", "患病封控")
        d = self.conn.execute("SELECT * FROM diagnoses WHERE id=?", (did,)).fetchone()
        self.assertEqual(d["status"], "待复核")
        # 提交诊断不改变健康状态
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id=?", (t1,)).fetchone()["health_status"],
            "健康")
        # 园艺师自己不能复核；另一个园艺师也不能；居民更不能
        for who in ("chen", "alice", "bob"):
            with self.subTest(who=who):
                with self.assertRaises(ServiceError):
                    self.svc.review_diagnosis(self.u(who), did, True, "同意")
        # 健康状态在复核前仍不变
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id=?", (t1,)).fetchone()["health_status"],
            "健康")
        # 主管驳回：健康状态依旧不变
        self.svc.review_diagnosis(self.u("zhou"), did, False, "证据不足")
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id=?", (t1,)).fetchone()["health_status"],
            "健康")
        # 已复核的记录不能二次复核
        with self.assertRaises(ServiceError):
            self.svc.review_diagnosis(self.u("zhou"), did, True, "再同意一次")

    def test_supervisor_approval_updates_health_and_plan(self):
        t1 = self.tid["T001"]
        did = self.svc.submit_diagnosis(self.u("chen"), t1, "病害",
                                        "松材线虫，封控", "患病封控")
        self.svc.review_diagnosis(self.u("zhou"), did, True, "同意封控")
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id=?", (t1,)).fetchone()["health_status"],
            "患病封控")
        d = self.conn.execute("SELECT * FROM diagnoses WHERE id=?", (did,)).fetchone()
        self.assertEqual(d["status"], "已确认")
        self.assertEqual(d["reviewer_id"], self.uid["zhou"])
        # 封控联动：生成 4 条封控期病虫观察任务
        obs = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE tree_id=? AND kind='病虫观察' "
            "AND note LIKE '%封控期%'", (t1,)).fetchone()["c"]
        self.assertEqual(obs, 4)
        events = [r["event_type"] for r in self.svc.tree_events(t1)]
        self.assertIn("异常诊断已确认", events)

    def test_transplant_case_plan_and_notify(self):
        t1 = self.tid["T001"]
        # 认养人有生效中的认养 + 计划
        aid = self.svc.apply_adoption(t1, self.uid["alice"],
                                      "2026-10-02", "2027-10-01", "委托园艺师")
        self.svc.pay_adoption(aid, self.uid["alice"])
        n = self.svc.plan_for_adoption(self.u("chen"), aid)
        self.assertGreater(n, 0)
        pending_before = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE tree_id=? AND status='待执行'",
            (t1,)).fetchone()["c"]

        did = self.svc.submit_diagnosis(self.u("chen"), t1, "移栽",
                                        "步道改造冲突，带土球移栽南坡", "移栽中")
        self.svc.review_diagnosis(self.u("zhou"), did, True, "同意移栽")
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id=?", (t1,)).fetchone()["health_status"],
            "移栽中")
        # 原计划全部中止
        pending_after = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE tree_id=? AND status='待执行'",
            (t1,)).fetchone()["c"]
        # 剩余待执行只可能是移栽后第 7/14 天复壮观察（2 条），原计划全部取消
        new_obs = self.conn.execute(
            "SELECT planned_date, note FROM care_tasks WHERE tree_id=? AND note LIKE '%复壮%' "
            "ORDER BY planned_date", (t1,)).fetchall()
        self.assertEqual(len(new_obs), 2)
        self.assertEqual(pending_after, 2)
        self.assertLess(pending_after, pending_before)
        # 认养人收到移栽通知
        titles = [n["title"] for n in self.svc.notify.inbox(self.uid["alice"])]
        self.assertIn("认养树木移栽通知", titles)

    def test_resident_false_alarm_updates_care_plan(self):
        t1 = self.tid["T001"]
        rid = self.svc.report_withering(self.u("alice"), t1, "叶子发蔫担心枯死")
        # 居民报告不能由居民/主管直接定案，须园艺师核查
        with self.assertRaises(ServiceError):
            self.svc.review_diagnosis(self.u("zhou"), rid, True, "我是主管但这里不对口")
        # 园艺师现场核查：系误报
        self.svc.review_diagnosis(self.u("chen"), rid, False,
                                  "现场核查正常，季节性换叶，浇一次水即可")
        d = self.conn.execute("SELECT * FROM diagnoses WHERE id=?", (rid,)).fetchone()
        self.assertEqual(d["status"], "已驳回")
        # 误报闭环：新增补水 + 复查两条任务
        rows = self.conn.execute(
            "SELECT kind, note FROM care_tasks WHERE tree_id=? AND note LIKE '%误报%' "
            "ORDER BY id", (t1,)).fetchall()
        kinds = sorted(r["kind"] for r in rows)
        self.assertEqual(kinds, ["浇水", "病虫观察"])
        events = [r["event_type"] for r in self.svc.tree_events(t1)]
        self.assertIn("居民误报枯萎", events)
        # 健康结论从未改变
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id=?", (t1,)).fetchone()["health_status"],
            "健康")

    def test_resident_report_confirmed_then_escalates_to_supervisor(self):
        t1 = self.tid["T001"]
        rid = self.svc.report_withering(self.u("alice"), t1, "大面积枯枝")
        # 园艺师核查属实 → 不能直接改健康，自动转主管
        self.svc.review_diagnosis(self.u("chen"), rid, True, "确实存在枯萎症状")
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id=?", (t1,)).fetchone()["health_status"],
            "健康")  # 仍是健康，等待主管
        esc = self.conn.execute(
            "SELECT * FROM diagnoses WHERE reporter_kind='gardener' AND status='待复核' "
            "AND detail LIKE '%居民枯萎报告经现场核查属实%'").fetchone()
        self.assertIsNotNone(esc)
        self.svc.review_diagnosis(self.u("zhou"), esc["id"], True, "同意列为观察")
        # 转单 proposed_health='观察'
        self.assertEqual(self.conn.execute(
            "SELECT health_status FROM trees WHERE id=?", (t1,)).fetchone()["health_status"],
            "观察")


if __name__ == "__main__":
    unittest.main()
