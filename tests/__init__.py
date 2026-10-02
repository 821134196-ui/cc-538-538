"""测试公共夹具：临时库 + FakeClock。"""
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.adapters import FakeClock
from app.database import connect, init_db
from app.services import Service

START = datetime(2026, 10, 2, 9, 0, 0)


class ServiceCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.conn = connect(root / "test.db")
        init_db(self.conn)
        self.clock = FakeClock(START)
        self.svc = Service(self.conn, clock=self.clock, photo_root=root / "photos",
                           hold_ttl_minutes=15)
        self._seed_base()

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _seed_base(self):
        # 用户：两个居民、园艺师、主管
        users = {"alice": ("林晓", "resident"), "bob": ("王然", "resident"),
                 "chen": ("陈墨", "gardener"), "zhou": ("周岚", "supervisor")}
        self.uid = {}
        for name, (disp, role) in users.items():
            cur = self.conn.execute(
                "INSERT INTO users(username,display_name,role) VALUES (?,?,?)",
                (name, disp, role))
            self.uid[name] = cur.lastrowid
        # 两棵可认养的树 + 一棵封控树
        self.tid = {}
        for code, species, health, adoptable in (
                ("T001", "银杏", "健康", 1),
                ("T002", "香樟", "健康", 1),
                ("T004", "雪松", "患病封控", 0)):
            cur = self.conn.execute(
                "INSERT INTO trees(code,species,location,x,y,health_status,adoptable) "
                "VALUES (?,?,?,?,?,?,?)",
                (code, species, "测试位置", 100, 100, health, adoptable))
            self.tid[code] = cur.lastrowid

    def u(self, name):
        return self.svc.get_user(self.uid[name])
