"""模拟适配器：支付与通知。真实环境应替换为对应网关/推送 SDK。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path


class Clock:
    """可注入时钟，测试时用 FakeClock 替换。"""

    def now(self) -> datetime:
        return datetime.now().replace(microsecond=0)

    def today(self) -> str:
        return self.now().date().isoformat()


class FakeClock(Clock):
    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, **kwargs) -> None:
        from datetime import timedelta

        self._now += timedelta(**kwargs)


class MockPaymentAdapter:
    """模拟支付：永远支付成功，写流水。"""

    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    def charge(self, adoption_id: int, amount_cents: int) -> dict:
        ts = self.clock.now().isoformat(timespec="seconds")
        cur = self.conn.execute(
            "INSERT INTO payments(adoption_id, amount_cents, status, created_at) VALUES (?,?,?,?)",
            (adoption_id, amount_cents, "paid", ts),
        )
        return {"ok": True, "payment_id": cur.lastrowid, "paid_at": ts}


class MockNotificationAdapter:
    """模拟通知：写站内收件箱（真实环境可接短信/公众号/邮件）。"""

    def __init__(self, conn: sqlite3.Connection, clock: Clock):
        self.conn = conn
        self.clock = clock

    def send(self, user_id: int, title: str, body: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO notifications(user_id, title, body, created_at) VALUES (?,?,?,?)",
            (user_id, title, body, self.clock.now().isoformat(timespec="seconds")),
        )
        return cur.lastrowid

    def inbox(self, user_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50",
            (user_id,),
        ).fetchall()

    def mark_read(self, notif_id: int, user_id: int) -> None:
        self.conn.execute(
            "UPDATE notifications SET read=1 WHERE id=? AND user_id=?", (notif_id, user_id)
        )
        self.conn.commit()
