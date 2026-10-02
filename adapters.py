"""模拟适配器：支付与短信通知。接口与真实服务一致，只在本地落库/打印。"""
from __future__ import annotations

import sqlite3
import uuid


class PaymentAdapter:
    """模拟支付网关：charge 同步成功并返回流水号；refund 原路退回。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def charge(self, adoption_id: int, amount: int, now: str) -> str:
        pid = "PAY-" + uuid.uuid4().hex[:12].upper()
        self.conn.execute(
            "INSERT INTO payments(id, adoption_id, amount, status, created_at) "
            "VALUES (?,?,?,'paid',?)",
            (pid, adoption_id, amount, now),
        )
        return pid

    def refund(self, payment_id: str) -> None:
        self.conn.execute("UPDATE payments SET status='refunded' WHERE id=?", (payment_id,))


class NotificationAdapter:
    """模拟短信：写入 notifications 表（前端可查），不真正发送。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def send(self, user_id: int, title: str, body: str, now: str,
             channel: str = "sms") -> int:
        cur = self.conn.execute(
            "INSERT INTO notifications(user_id, title, body, channel, status, created_at) "
            "VALUES (?,?,?,?, 'queued', ?)",
            (user_id, title, body, channel, now),
        )
        return cur.lastrowid
