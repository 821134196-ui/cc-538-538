"""本地启动入口：初始化 SQLite、预置演示数据（含移栽案例）、启动 Web 服务。

用法：
    python3 main.py [--reset] [--port 8000] [--host 127.0.0.1]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime

from db import DEFAULT_DB, connect, init_db
import seed as seed_mod
import services
from server import build_server


def main() -> int:
    ap = argparse.ArgumentParser(description="公园树木认养本地应用")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="清空并重新预置数据")
    args = ap.parse_args()

    if args.reset:
        print("重置数据并预置演示场景……")
        seed_mod.reset_and_seed(DEFAULT_DB)
    else:
        conn = connect(DEFAULT_DB)
        init_db(conn)
        if not seed_mod.is_seeded(conn):
            print("首次启动，预置演示数据（含 T004 移栽案例）……")
            seed_mod.seed(conn)
        conn.close()

    httpd = build_server(args.host, args.port, DEFAULT_DB)
    # 启动即清扫一次：释放已过期占位、把逾期任务转状态
    conn = connect(DEFAULT_DB)
    n1 = services.sweep_expired_holds(conn)
    n2 = services.sweep_overdue(conn)
    conn.close()
    if n1 or n2:
        print(f"启动清扫：释放占位 {n1} 个，逾期任务 {n2} 项")

    url = f"http://{args.host}:{args.port}/"
    print("=" * 56)
    print("  公园树木认养系统已启动")
    print(f"  打开浏览器访问：{url}")
    print("  演示账号：李阿姨(1/居民) 王居民(2) 周同学(3)")
    print("            陈园艺(10)  赵主管(20)")
    print(f"  启动时间：{datetime.now():%Y-%m-%d %H:%M:%S}（Ctrl+C 停止）")
    print("=" * 56)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        httpd._stop_event.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
