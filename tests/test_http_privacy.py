"""HTTP 端到端：登录态、跨用户 403、照片访问控制、申请冲突页面、复核按钮权限。"""
import http.client
import tempfile
import threading
import unittest
import urllib.parse
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path

from app.adapters import FakeClock
from app.database import connect, init_db
from app.web import build_server


class HttpCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.db = root / "http.db"
        cls.photos = root / "photos"
        conn = connect(cls.db)
        init_db(conn)
        cls.clock = FakeClock(datetime(2026, 10, 2, 9, 0))
        for uname, disp, role in (
                ("alice", "林晓", "resident"), ("bob", "王然", "resident"),
                ("chen", "陈墨", "gardener"), ("zhou", "周岚", "supervisor")):
            conn.execute("INSERT INTO users(username,display_name,role) VALUES (?,?,?)",
                         (uname, disp, role))
        for i, code in enumerate(("T001", "T002", "T003"), start=1):
            conn.execute(
                "INSERT INTO trees(code,species,location,x,y,health_status,adoptable,photo_path) "
                f"VALUES ('{code}','测试树','测试',100,100,'健康',1,NULL)")
        conn.commit()
        conn.close()

        cls.server = build_server(cls.db, photo_root=cls.photos, host="127.0.0.1",
                                  port=0, clock=cls.clock)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)
        cls.tmp.cleanup()

    def req(self, method, path, cookie=None, data=None, ctype="application/x-www-form-urlencoded"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {}
        if cookie:
            headers["Cookie"] = cookie
        body = None
        if data is not None:
            if ctype == "application/x-www-form-urlencoded":
                body = urllib.parse.urlencode(data).encode()
            else:
                import json
                body = json.dumps(data).encode()
            headers["Content-Type"] = ctype
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        payload = resp.read().decode("utf-8")
        set_cookie = resp.getheader("Set-Cookie")
        conn.close()
        return resp.status, resp.getheader("Location"), set_cookie, payload

    def login(self, username):
        status, loc, sc, _ = self.req("POST", "/login",
                                      data={"username": username, "next": "/map"})
        self.assertEqual(status, 303)
        return sc.split(";")[0]

    def uid_by_name(self, name):
        conn = connect(self.db)
        r = conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone()
        conn.close()
        return r["id"]

    def test_anonymous_redirect_to_login(self):
        for path in ("/map", "/calendar", "/adoptions", "/reports", "/trees/1"):
            status, loc, _, _ = self.req("GET", path)
            self.assertEqual(status, 303, path)
            self.assertIn("/login", loc, path)

    def test_apply_pay_and_cross_user_403(self):
        alice = self.login("alice")
        bob = self.login("bob")
        # alice 申请 T001
        status, loc, _, _ = self.req("POST", "/apply", cookie=alice, data={
            "tree_id": 1, "start_date": "2026-10-10", "end_date": "2026-12-10",
            "care_mode": "自助养护"})
        self.assertEqual(status, 303)
        aid = int(loc.rstrip("/").split("/")[-1])
        # bob 同期限申请 → 400 冲突
        status, _, _, body = self.req("POST", "/apply", cookie=bob, data={
            "tree_id": 1, "start_date": "2026-10-10", "end_date": "2026-12-10",
            "care_mode": "自助养护"})
        self.assertEqual(status, 400)
        self.assertIn("冲突", body)
        # bob 偷看 alice 的认养单 → 403
        status, _, _, body = self.req("GET", f"/adoptions/{aid}", cookie=bob)
        self.assertEqual(status, 403)
        # bob 试图替 alice 支付 → 403
        status, _, _, _ = self.req("POST", f"/adoptions/{aid}/pay", cookie=bob)
        self.assertEqual(status, 403)
        # 占位过期后 bob 可申请
        self.clock.advance(minutes=16)
        status, loc2, _, body = self.req("POST", "/apply", cookie=bob, data={
            "tree_id": 1, "start_date": "2026-10-10", "end_date": "2026-12-10",
            "care_mode": "自助养护"})
        # 申请前页面动作会自动巡检（/apply GET），POST 服务层事务内也会过期占位
        self.assertEqual(status, 303, body)
        aid2 = int(loc2.rstrip("/").split("/")[-1])
        status, _, _, _ = self.req("POST", f"/adoptions/{aid2}/pay", cookie=bob)
        self.assertEqual(status, 303)

    def test_review_permissions_over_http(self):
        chen = self.login("chen")
        zhou = self.login("zhou")
        alice = self.login("alice")
        # 园艺师提交诊断
        status, loc, _, _ = self.req("POST", "/trees/1/diagnosis", cookie=chen, data={
            "kind": "虫害", "detail": "HTTP 测试虫害", "proposed_health": "观察"})
        self.assertEqual(status, 303)
        # 种子诊断 id：直接查库
        conn = connect(self.db)
        did = conn.execute("SELECT MAX(id) id FROM diagnoses").fetchone()["id"]
        conn.close()
        # 居民复核 → 400（路由允许登录，但服务层拒绝：角色不符）
        status, _, _, _ = self.req("POST", f"/diagnoses/{did}/review", cookie=alice,
                                   data={"decision": "approve", "note": "x"})
        self.assertIn(status, (400, 403))
        # 园艺师自己复核 → 同样拒绝
        status, _, _, _ = self.req("POST", f"/diagnoses/{did}/review", cookie=chen,
                                   data={"decision": "approve", "note": "x"})
        self.assertIn(status, (400, 403))
        # 主管复核成功
        status, _, _, _ = self.req("POST", f"/diagnoses/{did}/review", cookie=zhou,
                                   data={"decision": "approve", "note": "同意观察"})
        self.assertEqual(status, 303)
        conn = connect(self.db)
        self.assertEqual(conn.execute(
            "SELECT health_status FROM trees WHERE id=1").fetchone()["health_status"], "观察")
        self.assertEqual(conn.execute(
            "SELECT status FROM diagnoses WHERE id=?", (did,)).fetchone()["status"], "已确认")
        conn.close()

    def test_tree_detail_masks_private_for_non_owner(self):
        alice = self.login("alice")
        bob = self.login("bob")
        zhou = self.login("zhou")
        # alice 在独立的 T003 上认养并支付
        status, loc, _, _ = self.req("POST", "/apply", cookie=alice, data={
            "tree_id": 3, "start_date": "2026-11-01", "end_date": "2026-12-01",
            "care_mode": "自助养护"})
        self.assertEqual(status, 303, loc)
        aid = int(loc.rstrip("/").split("/")[-1])
        self.req("POST", f"/adoptions/{aid}/pay", cookie=alice)
        self.req("POST", f"/adoptions/{aid}/plan", cookie=zhou)
        # bob 看树详情：养护记录区应显示隐私提示，看不到认养人姓名
        status, _, _, body = self.req("GET", "/trees/3", cookie=bob)
        self.assertEqual(status, 200)
        self.assertIn("仅对本树认养人", body)

    def test_photo_requires_login(self):
        status, loc, _, _ = self.req("GET", "/photos/202610/x.svg")
        self.assertEqual(status, 303)
        self.assertIn("/login", loc)


if __name__ == "__main__":
    unittest.main()
