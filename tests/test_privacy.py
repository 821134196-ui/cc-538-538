"""跨用户隐私：认养记录、养护计划/执行、照片、年报、诊断报告均隔离。"""
import unittest

from app.services import PermissionDenied, ServiceError

from tests import ServiceCase


class ServicePrivacyTest(ServiceCase):
    def setUp(self):
        super().setUp()
        t1 = self.tid["T001"]
        self.aid = self.svc.apply_adoption(t1, self.uid["alice"],
                                           "2026-10-02", "2027-04-01", "自助养护")
        self.svc.pay_adoption(self.aid, self.uid["alice"])
        self.svc.plan_for_adoption(self.u("chen"), self.aid)
        task = self.conn.execute(
            "SELECT id FROM care_tasks WHERE adoption_id=? ORDER BY planned_date LIMIT 1",
            (self.aid,)).fetchone()
        self.task_id = task["id"]
        self.svc.complete_task(self.u("alice"), self.task_id, "alice 自助浇水记录")
        self.svc.generate_annual_report(self.u("chen"), t1, 2026)

    def test_other_resident_cannot_access_adoption(self):
        with self.assertRaises(PermissionDenied):
            self.svc.require_owner_or_staff(self.aid, self.u("bob"))

    def test_tasks_scoped_per_resident(self):
        alice_tasks = self.svc.list_tasks(self.u("alice"))
        bob_tasks = self.svc.list_tasks(self.u("bob"))
        self.assertTrue(any(t["tree_id"] == self.tid["T001"] for t in alice_tasks))
        self.assertEqual(bob_tasks, [])

    def test_care_records_visibility_on_tree_page_data(self):
        # 用年报权限校验函数代表详情页隐私闸门
        self.assertTrue(self.svc.can_view_report(self.u("alice"), self.tid["T001"], 2026))
        self.assertFalse(self.svc.can_view_report(self.u("bob"), self.tid["T001"], 2026))
        with self.assertRaises(PermissionDenied):
            self.svc.get_report(self.u("bob"), self.tid["T001"], 2026)

    def test_bob_cannot_execute_alice_task_in_staff_mode(self):
        # bob 非认养人，且该任务是自助养护：不能登记
        other = self.conn.execute(
            "SELECT id FROM care_tasks WHERE adoption_id=? AND status='待执行' "
            "AND kind='病虫观察' LIMIT 1", (self.aid,)).fetchone()
        if other:
            with self.assertRaises(ServiceError):
                self.svc.complete_task(self.u("bob"), other["id"], "bob 冒名执行")

    def test_resident_diagnosis_list_only_own(self):
        self.svc.report_withering(self.u("alice"), self.tid["T001"], "alice 的报告")
        self.svc.report_withering(self.u("bob"), self.tid["T002"], "bob 的报告")
        alice_rows = self.svc.list_diagnoses(self.u("alice"))
        bob_rows = self.svc.list_diagnoses(self.u("bob"))
        self.assertTrue(all(r["reporter_id"] == self.uid["alice"] for r in alice_rows))
        self.assertTrue(all(r["reporter_id"] == self.uid["bob"] for r in bob_rows))
        # 园艺师/主管可见全部
        self.assertGreaterEqual(len(self.svc.list_diagnoses(self.u("chen"))), 2)

    def test_staff_can_access(self):
        a = self.svc.require_owner_or_staff(self.aid, self.u("zhou"))
        self.assertEqual(a["id"], self.aid)
        # 园方可以看所有任务
        self.assertTrue(len(self.svc.list_tasks(self.u("chen"))) >= 1)

    def test_owner_sees_staff_special_tasks_but_cannot_execute_entrusted(self):
        # 在 alice 的树上加一条无 adoption_id 的专项任务（模拟移栽复壮观察）
        special = self.svc.add_task(self.u("chen"), self.tid["T001"], "病虫观察",
                                    "2026-10-20", "移栽后复壮观察")
        alice_tasks = self.svc.list_tasks(self.u("alice"))
        self.assertIn(special, [t["id"] for t in alice_tasks])
        # 非认养人仍不可见
        self.assertNotIn(special, [t["id"] for t in self.svc.list_tasks(self.u("bob"))])
        # 委托园艺师模式：认养人不能自行登记执行
        aid2 = self.svc.apply_adoption(self.tid["T002"], self.uid["bob"],
                                       "2026-10-02", "2027-04-01", "委托园艺师")
        self.svc.pay_adoption(aid2, self.uid["bob"])
        self.svc.plan_for_adoption(self.u("chen"), aid2)
        task2 = self.conn.execute(
            "SELECT id FROM care_tasks WHERE adoption_id=? ORDER BY planned_date LIMIT 1",
            (aid2,)).fetchone()["id"]
        with self.assertRaises(ServiceError):
            self.svc.complete_task(self.u("bob"), task2, "bob 自行登记委托任务")
