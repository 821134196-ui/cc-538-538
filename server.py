"""HTTP 服务：JSON API + 静态前端。每请求一个 SQLite 连接（WAL 并发）。

演示鉴权：请求头 X-User-Id 标识当前用户（本地单机应用，无真实登录）。
"""
from __future__ import annotations

import base64
import json
import re
import threading
import time
from datetime import date
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import services
from db import DEFAULT_DB, PHOTO_DIR, connect, init_db
from imaging import png_brightness

WEB_DIR = Path(__file__).resolve().parent / "web"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]+\.png$")

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png",
        ".svg": "image/svg+xml", ".ico": "image/x-icon"}


class AppState:
    def __init__(self, db_path: Path = DEFAULT_DB):
        self.db_path = db_path
        self._local = threading.local()

    def conn(self):
        # SQLite 连接不跨线程共享；按线程缓存
        if not getattr(self._local, "conn", None):
            self._local.conn = connect(self.db_path)
        return self._local.conn


STATE = AppState()


def sweeper_loop(state: AppState, stop: threading.Event) -> None:
    """每 30 秒释放到期占位、标记逾期任务。"""
    while not stop.wait(30):
        try:
            conn = state.conn()
            services.sweep_expired_holds(conn)
            services.sweep_overdue(conn)
        except Exception as exc:  # 后台线程不能把进程带走
            print("[sweeper]", exc)


class Handler(BaseHTTPRequestHandler):
    server_version = "TreeAdopt/1.0"

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args))

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise services.ServiceError("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise services.ServiceError("请求体须为 JSON 对象")
        return data

    def _user_id(self, body: dict | None = None) -> int:
        raw = self.headers.get("X-User-Id") or (body or {}).get("as_user_id")
        if not raw:
            raise services.ServiceError("缺少当前用户（X-User-Id）", 401)
        try:
            return int(raw)
        except ValueError:
            raise services.ServiceError("用户标识无效", 401)

    # ------------------------------------------------------------ 路由
    def do_GET(self) -> None:
        try:
            url = urlparse(self.path)
            path, qs = url.path, parse_qs(url.query)
            if path.startswith("/api/"):
                self._api_get(path, qs)
            elif path.startswith("/photos/"):
                self._serve_photo(path.rsplit("/", 1)[-1])
            else:
                self._serve_static(path)
        except services.ServiceError as e:
            self._send_json({"error": str(e)}, e.status)
        except Exception as e:
            self._send_json({"error": f"服务器错误: {e}"}, 500)

    def do_POST(self) -> None:
        try:
            body = self._read_json()
            self._api_post(urlparse(self.path).path, body)
        except services.ServiceError as e:
            self._send_json({"error": str(e)}, e.status)
        except Exception as e:
            self._send_json({"error": f"服务器错误: {e}"}, 500)

    # ------------------------------------------------------------ GET API
    def _api_get(self, path: str, qs: dict) -> None:
        c = STATE.conn()
        if path == "/api/users":
            rows = c.execute("SELECT id,name,role FROM users ORDER BY id").fetchall()
            self._send_json([dict(r) for r in rows])
            return
        uid = self._user_id()
        if path == "/api/trees/map":
            self._send_json(services.get_tree_map(c, uid))
        elif path == "/api/adoptions/mine":
            self._send_json(services.list_my_adoptions(c, uid))
        elif path == "/api/calendar":
            self._send_json(services.get_calendar(c, uid, (qs.get("month") or [None])[0]))
        elif path == "/api/reviews":
            self._send_json(services.list_review_queue(c, uid))
        elif path == "/api/notifications":
            self._send_json(services.list_notifications(c, uid))
        elif (m := re.fullmatch(r"/api/trees/(T\d+)", path)):
            self._send_json(services.get_tree_detail(c, m.group(1), uid))
        elif (m := re.fullmatch(r"/api/adoptions/(\d+)", path)):
            self._send_json(services.get_adoption(c, int(m.group(1)), uid))
        else:
            self._send_json({"error": "未知接口"}, 404)

    # ------------------------------------------------------------ POST API
    def _api_post(self, path: str, body: dict) -> None:
        c = STATE.conn()
        uid = self._user_id(body)

        if (m := re.fullmatch(r"/api/trees/(T\d+)/adoptions", path)):
            self._send_json(services.apply_adoption(
                c, uid, m.group(1), body["start_date"], body["end_date"],
                body.get("care_mode", "watering")), 201)
        elif (m := re.fullmatch(r"/api/adoptions/(\d+)/pay", path)):
            self._send_json(services.pay_adoption(c, int(m.group(1)), uid))
        elif (m := re.fullmatch(r"/api/adoptions/(\d+)/exit", path)):
            self._send_json(services.exit_adoption(
                c, int(m.group(1)), uid, body.get("reason", "")))
        elif (m := re.fullmatch(r"/api/adoptions/(\d+)/annual-report", path)):
            year = int(body.get("year") or date.today().year)
            self._send_json(services.generate_annual_report(
                c, int(m.group(1)), year, uid))
        elif (m := re.fullmatch(r"/api/tasks/(\d+)/complete", path)):
            self._send_json(services.complete_task(
                c, int(m.group(1)), uid, body.get("note", ""),
                body.get("done_date")))
        elif (m := re.fullmatch(r"/api/trees/(T\d+)/photos", path)):
            self._upload_photo(m.group(1), uid, body)
        elif (m := re.fullmatch(r"/api/trees/(T\d+)/anomalies", path)):
            self._send_json(services.report_anomaly(
                c, m.group(1), uid, body.get("kind", "other"),
                body.get("detail", "")), 201)
        elif (m := re.fullmatch(r"/api/anomalies/(\d+)/diagnose", path)):
            self._send_json(services.submit_diagnosis(
                c, int(m.group(1)), uid, body.get("diagnosis", ""),
                bool(body.get("is_real")), body.get("proposed_health")))
        elif (m := re.fullmatch(r"/api/anomalies/(\d+)/review", path)):
            self._send_json(services.review_diagnosis(
                c, int(m.group(1)), uid, bool(body.get("approve")),
                body.get("review_note", "")))
        elif (m := re.fullmatch(r"/api/trees/(T\d+)/transplant", path)):
            self._send_json(services.transplant_tree(
                c, m.group(1), uid, float(body["new_x"]), float(body["new_y"]),
                body.get("reason", ""), body.get("happened_on")))
        elif (m := re.fullmatch(r"/api/trees/(T\d+)/reopen", path)):
            self._send_json(services.reopen_tree(
                c, m.group(1), uid, body.get("target_health", "recovering")))
        else:
            self._send_json({"error": "未知接口"}, 404)

    # ------------------------------------------------------------ 照片
    def _upload_photo(self, tree_id: str, uid: int, body: dict) -> None:
        data_url = body.get("data_url", "")
        if not data_url.startswith("data:image/png;base64,"):
            raise services.ServiceError("仅接受 PNG 照片（base64 data URL）")
        try:
            raw = base64.b64decode(data_url.split(",", 1)[1], validate=True)
        except Exception:
            raise services.ServiceError("照片数据解码失败")
        if not raw.startswith(b"\x89PNG"):
            raise services.ServiceError("照片不是有效的 PNG")

        # 先落临时文件，服务端独立复测亮度，不信任前端回传值
        tmp = PHOTO_DIR / "upload.tmp.png"
        tmp.write_bytes(raw)
        try:
            brightness = png_brightness(tmp)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise services.ServiceError("照片无法解析，请重新拍摄")
        if brightness < services.MIN_PHOTO_BRIGHTNESS:
            tmp.unlink(missing_ok=True)
            raise services.ServiceError(
                f"照片亮度过低（{brightness:.2f} < {services.MIN_PHOTO_BRIGHTNESS}），"
                "无法辨识，请补光后重拍")

        taken_on = body.get("taken_on") or date.today().isoformat()
        photo = services.add_photo(
            STATE.conn(), tree_id, uid, "pending.png",
            body.get("caption", ""), taken_on, round(brightness, 3))
        final = PHOTO_DIR / f"{photo['id']}.png"
        tmp.rename(final)
        c = STATE.conn()
        c.execute("UPDATE photos SET filename=? WHERE id=?", (final.name, photo["id"]))
        self._send_json(services._photo_dict(c, photo["id"],
                      c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()), 201)

    def _serve_photo(self, name: str) -> None:
        if not SAFE_NAME.fullmatch(name):
            self._send_json({"error": "文件名无效"}, 400)
            return
        path = (PHOTO_DIR / name).resolve()
        if PHOTO_DIR.resolve() not in path.parents or not path.exists():
            self._send_json({"error": "照片不存在"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=300")
        self.end_headers()
        self.wfile.write(data)

    # ------------------------------------------------------------ 静态
    def _serve_static(self, path: str) -> None:
        if path in ("/", ""):
            path = "/index.html"
        target = (WEB_DIR / path.lstrip("/")).resolve()
        if WEB_DIR.resolve() not in target.parents or not target.is_file():
            target = WEB_DIR / "index.html"   # 单页应用回退
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type",
                         MIME.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def build_server(host: str, port: int, db_path: Path = DEFAULT_DB) -> ThreadingHTTPServer:
    global STATE
    STATE = AppState(db_path)
    init_db(STATE.conn())
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    stop = threading.Event()
    httpd._stop_event = stop
    t = threading.Thread(target=sweeper_loop, args=(STATE, stop), daemon=True)
    t.start()
    return httpd
