"""期限互斥：重叠认养拒绝、待支付占位也占名额、并发申请按提交时间先到先得。"""
import threading
import unittest
from datetime import date

from app.database import connect
from app.services import Service, ServiceError

from tests import START, ServiceCase


class MutexTest(ServiceCase):
    def test_adjacent_ranges_allowed_but_overlapping_rejected(self):
        t1 = self.tid["T001"]
        # 第一份申请（alice，待支付占位）
        a1 = self.svc.apply_adoption(t1, self.uid["alice"],
                                     "2026-10-10", "2026-12-10", "自助养护")
        self.assertTrue(a1 > 0)

        cases = [
            ("2026-10-09", "2026-10-11"),  # 头重叠
            ("2026-12-09", "2026-12-20"),  # 尾重叠
            ("2026-11-01", "2026-11-10"),  # 中间包含
            ("2026-10-10", "2026-12-10"),  # 完全相同
            ("2026-10-02", "2027-01-01"),  # 完全包住
        ]
        for s, e in cases:
            with self.subTest(r=(s, e)):
                with self.assertRaises(ServiceError) as cm:
                    self.svc.apply_adoption(t1, self.uid["bob"], s, e, "自助养护")
                self.assertIn("冲突", str(cm.exception))

        # 相邻区间 [a,b) 半开区间不视为重叠：结束日当天可开始新认养
        a2 = self.svc.apply_adoption(t1, self.uid["bob"],
                                     "2026-12-10", "2027-02-01", "委托园艺师")
        self.assertTrue(a2 > 0)
        # 占位同样占名额：第三人连相邻之后的区间里再挤仍被 bob 的占位挡住
        with self.assertRaises(ServiceError):
            self.svc.apply_adoption(t1, self.uid["alice"],
                                    "2027-01-01", "2027-03-01", "自助养护")

    def test_pending_hold_blocks_until_released(self):
        t2 = self.tid["T002"]
        self.svc.apply_adoption(t2, self.uid["alice"],
                                "2026-11-01", "2026-11-30", "自助养护")
        with self.assertRaises(ServiceError):
            self.svc.apply_adoption(t2, self.uid["bob"],
                                    "2026-11-10", "2026-12-10", "自助养护")
        # 15 分钟到期释放后，bob 可立即申请同一区间
        self.clock.advance(minutes=16)
        self.assertEqual(self.svc.release_expired_holds(), 1)
        aid = self.svc.apply_adoption(t2, self.uid["bob"],
                                      "2026-11-01", "2026-11-30", "自助养护")
        self.assertTrue(aid > 0)

    def test_locked_tree_not_appliable(self):
        with self.assertRaises(ServiceError):
            self.svc.apply_adoption(self.tid["T004"], self.uid["alice"],
                                    "2026-10-10", "2027-10-10", "自助养护")


class ConcurrentApplyTest(ServiceCase):
    """并发申请同一期限：只有第一个提交者成功，其余得到冲突错误。"""

    def test_concurrent_exactly_one_wins(self):
        db_path = self.conn.execute("PRAGMA database_list").fetchone()[2]
        barrier = threading.Barrier(5)
        results = []
        lock = threading.Lock()

        def worker(username):
            own = connect(db_path)
            svc = Service(own, clock=self.clock,
                          photo_root=self.svc.photo_root, hold_ttl_minutes=15)
            uid = svc.get_user_by_name(username)["id"]
            barrier.wait()
            try:
                aid = svc.apply_adoption(self.tid["T002"], uid,
                                         "2026-12-01", "2027-03-01", "自助养护")
                with lock:
                    results.append(("ok", aid))
            except ServiceError as e:
                with lock:
                    results.append(("conflict", str(e)))
            except Exception as e:  # noqa
                with lock:
                    results.append(("error", f"{type(e).__name__}: {e}"))
            own.close()

        threads = [threading.Thread(target=worker, args=(n,))
                   for n in ("alice", "bob", "alice", "bob", "alice")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        oks = [r for r in results if r[0] == "ok"]
        conflicts = [r for r in results if r[0] == "conflict"]
        errors = [r for r in results if r[0] == "error"]
        self.assertEqual(len(oks), 1, f"应恰好 1 个成功，实际：{results}")
        self.assertEqual(len(conflicts), 4, f"其余 4 个应冲突，实际：{results}")
        self.assertEqual(errors, [])
        # 赢家支付后状态 active，且与后来者仍互斥
        own = connect(db_path)
        svc = Service(own, clock=self.clock, photo_root=self.svc.photo_root)
        winner_uid = own.execute(
            "SELECT user_id FROM adoptions WHERE id=?", (oks[0][1],)).fetchone()["user_id"]
        svc.pay_adoption(oks[0][1], winner_uid)
        row = own.execute("SELECT status FROM adoptions WHERE id=?", (oks[0][1],)).fetchone()
        self.assertEqual(row["status"], "active")
        own.close()


if __name__ == "__main__":
    unittest.main()
