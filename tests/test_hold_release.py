"""占位释放：未支付 15 分钟到期、支付成功生效、临界时刻支付、释放后可再申请、支付他人占位被拒。"""
import unittest

from app.services import PermissionDenied, ServiceError

from tests import ServiceCase


class HoldReleaseTest(ServiceCase):
    def _apply(self, who="alice", tree="T001", s="2026-10-10", e="2026-12-10"):
        return self.svc.apply_adoption(self.tid[tree], self.uid[who], s, e, "自助养护")

    def test_hold_expires_after_ttl(self):
        aid = self._apply()
        row = self.conn.execute("SELECT status FROM adoptions WHERE id=?", (aid,)).fetchone()
        self.assertEqual(row["status"], "pending_payment")

        # TTL 内巡检不释放
        self.clock.advance(minutes=14)
        self.assertEqual(self.svc.release_expired_holds(), 0)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM adoptions WHERE id=?", (aid,)).fetchone()["status"],
            "pending_payment")

        # 到期后巡检释放
        self.clock.advance(minutes=2)
        self.assertEqual(self.svc.release_expired_holds(), 1)
        self.assertEqual(self.conn.execute(
            "SELECT status, released_at FROM adoptions WHERE id=?", (aid,)).fetchone()["status"],
            "expired")

    def test_payment_activates_hold(self):
        aid = self._apply()
        self.clock.advance(minutes=5)
        res = self.svc.pay_adoption(aid, self.uid["alice"])
        self.assertTrue(res["ok"])
        a = self.conn.execute("SELECT * FROM adoptions WHERE id=?", (aid,)).fetchone()
        self.assertEqual(a["status"], "active")
        self.assertIsNotNone(a["paid_at"])
        # 支付流水存在
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) c FROM payments WHERE adoption_id=?", (aid,)).fetchone()["c"], 1)
        # 后续巡检不应再释放
        self.clock.advance(hours=2)
        self.assertEqual(self.svc.release_expired_holds(), 0)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM adoptions WHERE id=?", (aid,)).fetchone()["status"], "active")
        # 申请人收到成功通知
        notifs = self.svc.notify.inbox(self.uid["alice"])
        self.assertTrue(any("支付成功" in n["title"] for n in notifs))

    def test_pay_at_last_minute_ok_but_late_fails(self):
        aid = self._apply()
        self.clock.advance(minutes=15)
        # 恰好 15 分钟：TTL 是 > 比较，仍可支付
        self.svc.pay_adoption(aid, self.uid["alice"])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM adoptions WHERE id=?", (aid,)).fetchone()["status"], "active")

        aid2 = self._apply(who="bob", tree="T002")
        self.clock.advance(minutes=16)
        with self.assertRaises(ServiceError) as cm:
            self.svc.pay_adoption(aid2, self.uid["bob"])
        self.assertIn("到期", str(cm.exception))

    def test_released_slot_reopenable_and_notifies(self):
        aid = self._apply()
        self.clock.advance(minutes=16)
        self.svc.release_expired_holds()
        # 释放后同一区间可被另一人申请并支付
        aid2 = self._apply(who="bob")
        self.svc.pay_adoption(aid2, self.uid["bob"])
        self.assertEqual(self.conn.execute(
            "SELECT status FROM adoptions WHERE id=?", (aid2,)).fetchone()["status"], "active")
        notifs = self.svc.notify.inbox(self.uid["alice"])
        self.assertTrue(any("占位已释放" in n["title"] for n in notifs))

    def test_cannot_pay_others_hold(self):
        aid = self._apply(who="alice")
        with self.assertRaises(PermissionDenied):
            self.svc.pay_adoption(aid, self.uid["bob"])

    def test_exit_cancels_future_tasks(self):
        aid = self._apply(s="2026-10-02", e="2027-04-01")
        self.svc.pay_adoption(aid, self.uid["alice"])
        self.svc.plan_for_adoption(self.u("chen"), aid)
        before = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE adoption_id=? AND status='待执行'",
            (aid,)).fetchone()["c"]
        self.assertGreater(before, 0)
        self.svc.exit_adoption(aid, self.uid["alice"], "工作调动")
        left = self.conn.execute(
            "SELECT COUNT(*) c FROM care_tasks WHERE adoption_id=? AND status='待执行'",
            (aid,)).fetchone()["c"]
        self.assertEqual(left, 0)
        self.assertEqual(self.conn.execute(
            "SELECT status FROM adoptions WHERE id=?", (aid,)).fetchone()["status"], "exited")
        # 退出后区间可再被申请
        aid2 = self.svc.apply_adoption(self.tid["T001"], self.uid["bob"],
                                       "2026-12-01", "2027-02-01", "委托园艺师")
        self.assertTrue(aid2 > 0)


if __name__ == "__main__":
    unittest.main()
