"""本地启动入口：python -m app.main  →  初始化库、（可选）预置数据并启动。"""
from __future__ import annotations

import argparse
from pathlib import Path

from .database import connect, init_db
from .seed import seed
from .web import build_server


def main():
    ap = argparse.ArgumentParser(description="公园树木认养系统（本地版）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--db", default="data/park.db")
    ap.add_argument("--photos", default="data/photos")
    ap.add_argument("--no-seed", action="store_true", help="不重置/预置演示数据")
    args = ap.parse_args()

    db_path = Path(args.db)
    photos = Path(args.photos)
    if not args.no_seed:
        seed(db_path, photos)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = connect(db_path)
        init_db(conn)
        conn.close()
        photos.mkdir(parents=True, exist_ok=True)

    server = build_server(db_path, photo_root=photos, host=args.host, port=args.port)
    url = f"http://{args.host}:{args.port}/"
    print("=" * 56)
    print(" 🌳 公园树木认养系统已启动")
    print(f"   地址：{url}")
    print("   演示账号：alice/bob（居民） chen（园艺师） zhou（主管）")
    print("   停止：Ctrl+C")
    print("=" * 56)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")


if __name__ == "__main__":
    main()
