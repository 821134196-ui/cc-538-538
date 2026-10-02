"""Web 界面（Python 标准库 http.server，零依赖）。

功能页：树木地图 / 树木详情（季节养护记录）/ 认养申请 / 我的认养（支付占位、退出）/
养护日历 / 巡查照片 / 异常复核 / 年度报告 / 通知收件箱。
"""
from __future__ import annotations

import base64
import http.cookies
import json
import re
import secrets
import sqlite3
import threading
import urllib.parse
from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .adapters import Clock
from .database import connect, init_db
from .services import STAFF_ROLES, PermissionDenied, Service, ServiceError, season_of

# ---------- 配色：白底、森林绿、少量花色点缀 ----------
CSS = """
:root{--green:#2e6b4f;--green-d:#1f5132;--green-l:#eaf3ec;--ink:#23332a;--muted:#64756b;
--line:#d7e3d8;--pink:#e58fb1;--gold:#e9b44c;--bg:#f7faf6;}
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
color:var(--ink);background:var(--bg);line-height:1.6}
a{color:var(--green-d);text-decoration:none}
a:hover{text-decoration:underline}
header{background:#fff;border-bottom:1px solid var(--line);padding:10px 22px;display:flex;
align-items:center;gap:18px;flex-wrap:wrap}
header .brand{font-weight:700;color:var(--green-d);font-size:18px}
header nav{display:flex;gap:14px;flex-wrap:wrap}
header .sp{margin-left:auto;display:flex;gap:12px;align-items:center;font-size:14px}
main{max-width:1080px;margin:22px auto;padding:0 18px}
.card{background:#fff;border:1px solid var(--line);border-radius:12px;padding:18px 20px;margin-bottom:18px}
h1{font-size:22px;color:var(--green-d);margin:.2em 0 .6em}
h2{font-size:17px;margin:.2em 0 .8em;color:var(--green-d)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:13px;font-weight:600}
.b-adopt{background:#eef4ff;color:#2f5597}
.b-free{background:var(--green-l);color:var(--green-d)}
.b-lock{background:#fdf3e0;color:#9a6a12}
.b-h-health{background:var(--green-l);color:var(--green-d)}
.b-h-watch{background:#fdf3e0;color:#9a6a12}
.b-h-lock{background:#fbe7ee;color:#a2335c}
.b-h-move{background:#e9eef8;color:#384b78}
.b-status{background:#eef0ee;color:#55625a}
table{width:100%;border-collapse:collapse;font-size:14px}
th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{color:var(--muted);font-weight:600;background:#fbfdfb}
.btn{display:inline-block;border:0;border-radius:8px;padding:7px 16px;background:var(--green);
color:#fff;cursor:pointer;font-size:14px}
.btn:hover{background:var(--green-d);text-decoration:none}
.btn.secondary{background:#fff;color:var(--green-d);border:1px solid var(--green)}
.btn.danger{background:#fff;color:#a2335c;border:1px solid #d98ba3}
.btn.small{padding:3px 10px;font-size:13px}
input,select,textarea{padding:7px 10px;border:1px solid var(--line);border-radius:8px;font-size:14px;
font-family:inherit;background:#fff}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 3px}
.row{display:flex;gap:14px;flex-wrap:wrap;align-items:flex-end}
.flash{background:#fbe7ee;border:1px solid #e7b3c5;color:#8c2a4e;padding:10px 14px;border-radius:10px;margin-bottom:14px}
.ok{background:var(--green-l);border-color:#b7d2bf;color:var(--green-d)}
.muted{color:var(--muted);font-size:13px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.tag{font-size:12px;padding:1px 8px;border-radius:6px;background:#f0f4f0;color:#486050}
.season-title{display:flex;align-items:center;gap:10px;margin:16px 0 6px;color:var(--green-d);font-weight:700}
.season-title::after{content:"";flex:1;height:1px;background:var(--line)}
.photos{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px}
.photos img{width:100%;height:150px;object-fit:cover;border-radius:10px;border:1px solid var(--line);
filter:brightness(1.04)}
.cal{display:grid;grid-template-columns:repeat(7,1fr);gap:6px}
.cal .dow{font-size:12px;color:var(--muted);text-align:center}
.cal .day{min-height:84px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:5px 7px;font-size:12px}
.cal .day.other{background:#f6f8f5;color:#aab7ad}
.cal .day.today{border-color:var(--green);box-shadow:0 0 0 1px var(--green) inset}
.cal .ev{margin-top:3px;padding:1px 6px;border-radius:5px;display:block}
.ev-浇水{background:#e6f0fa;color:#2f5f8f}
.ev-修剪{background:#f3ecfa;color:#6b3f96}
.ev-病虫观察{background:#fdf0dc;color:#94601a}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;margin:8px 0}
.dot{display:inline-block;width:12px;height:12px;border-radius:50%;margin-right:4px;vertical-align:-1px}
.timeline{border-left:2px solid var(--line);margin-left:6px;padding-left:16px}
.timeline li{list-style:none;position:relative;margin-bottom:10px}
.timeline li::before{content:"";position:absolute;left:-23px;top:7px;width:8px;height:8px;border-radius:50%;background:var(--green)}
.notif{padding:10px 14px;border-bottom:1px solid var(--line)}
.notif.unread{background:#f2f8f3}
.pill-flower{background:var(--pink);color:#fff;border-radius:999px;padding:1px 8px;font-size:12px}
"""

HEALTH_BADGE = {"健康": "b-h-health", "观察": "b-h-watch",
                "患病封控": "b-h-lock", "移栽中": "b-h-move"}
HEALTH_COLOR = {"健康": "#3d7d5c", "观察": "#e9b44c",
                "患病封控": "#d1668c", "移栽中": "#7b8fc0"}
ADOPT_BADGE = {"可认养": "b-free", "已认养": "b-adopt",
               "名额锁定中": "b-lock", "已被预约": "b-lock"}


def esc(s) -> str:
    return (str(s if s is not None else "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


def qs(url: str, **params) -> str:
    return url + "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})


def badge(cls, text):
    return f'<span class="badge {cls}">{esc(text)}</span>'


class AppContext:
    def __init__(self, conn: sqlite3.Connection, clock: Clock, photo_root: Path):
        self.svc = Service(conn, clock=clock, photo_root=photo_root)
        self.clock = clock
        self.photo_root = photo_root
        self.sessions: dict[str, int] = {}
        self.lock = threading.Lock()


def layout(ctx: AppContext, user, title: str, body: str, flash: str | None = None) -> bytes:
    nav = [("/map", "树木地图"), ("/calendar", "养护日历")]
    if user:
        if user["role"] == "resident":
            nav += [("/apply", "认养申请"), ("/adoptions", "我的认养"), ("/reports", "年度反馈")]
        else:
            nav += [("/adoptions", "认养记录"), ("/reports", "年度报告"),
                    ("/diagnoses", "异常复核")]
        nav.append(("/notifications", "通知"))
    role_name = {"resident": "居民", "gardener": "园艺师",
                 "supervisor": "主管", "admin": "管理员"}.get(user["role"], "") if user else ""
    unread = ""
    if user:
        n = len([1 for _ in ctx.svc.conn.execute(
            "SELECT 1 FROM notifications WHERE user_id=? AND read=0", (user["id"],))])
        unread = f'<span class="pill-flower">{n}</span>' if n else ""
    nav_html = "".join(f'<a href="{u}">{esc(t)}</a>' for u, t in nav)
    user_html = (
        f'<span>{esc(user["display_name"])}（{role_name}）</span> {unread}'
        f'<form method="post" action="/logout" style="margin:0"><button class="btn small secondary">退出登录</button></form>'
        if user else '<a href="/login" class="btn small">登录</a>')
    flash_html = f'<div class="flash">{esc(flash)}</div>' if flash else ""
    html = f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light">
<title>{esc(title)} · 公园树木认养</title><style>{CSS}</style></head>
<body><header><span class="brand">🌳 公园树木认养</span><nav>{nav_html}</nav>
<span class="sp">{user_html}</span></header>
<main>{flash_html}{body}</main></body></html>"""
    return html.encode("utf-8")


def make_handler(ctx: AppContext):
    svc = ctx.svc

    class Handler(BaseHTTPRequestHandler):
        server_version = "ParkAdoption/1.0"

        def log_message(self, *a):
            pass

        # ---- 基础设施 ----
        @property
        def user(self) -> sqlite3.Row | None:
            cookie = http.cookies.SimpleCookie(self.headers.get("Cookie", ""))
            sid = cookie.get("sid")
            if not sid:
                return None
            uid = ctx.sessions.get(sid.value)
            return svc.get_user(uid) if uid else None

        def require_login(self) -> sqlite3.Row | None:
            u = self.user
            if not u:
                self.redirect(qs("/login", next=self.path))
            return u

        def require_role(self, roles) -> sqlite3.Row | None:
            u = self.require_login()
            if u and u["role"] not in roles:
                self.error(403, "权限不足：该操作需要园方身份。")
                return None
            return u

        def send_html(self, html: bytes, status=200, extra_headers=None):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy",
                             "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; "
                             "script-src 'unsafe-inline'; base-uri 'none'")
            for k, v in (extra_headers or []):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(html)

        def error(self, status: int, msg: str):
            u = self.user
            self.send_html(layout(ctx, u, "操作未成功",
                                  f'<div class="card"><h1>{status}</h1><p>{esc(msg)}</p>'
                                  f'<p><a class="btn" href="javascript:history.back()">返回</a></p></div>'),
                          status)

        def redirect(self, url: str, status=303, cookie=None):
            self.send_response(status)
            self.send_header("Location", url)
            if cookie:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def form(self) -> dict[str, str]:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

        def json_body(self) -> dict:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            return json.loads(raw.decode("utf-8"))

        def render(self, title, body, flash=None, status=200):
            self.send_html(layout(ctx, self.user, title, body, flash), status)

        def do_GET(self):
            try:
                self.route_get()
            except PermissionDenied as e:
                self.error(403, str(e))
            except ServiceError as e:
                self.error(400, str(e))
            except Exception as e:  # noqa
                self.error(500, f"服务器错误：{type(e).__name__}: {e}")

        def do_POST(self):
            try:
                self.route_post()
            except PermissionDenied as e:
                self.error(403, str(e))
            except ServiceError as e:
                self.error(400, str(e))
            except Exception as e:  # noqa
                self.error(500, f"服务器错误：{type(e).__name__}: {e}")

        def can_complete_task(self, user, tk) -> bool:
            if tk["status"] != "待执行":
                return False
            if user["role"] in STAFF_ROLES:
                return True
            return (tk["adoption_user_id"] == user["id"]
                    and tk["care_mode"] == "自助养护")

        # ================= GET 路由 =================
        def route_get(self):
            p = urllib.parse.urlparse(self.path)
            path, query = p.path, urllib.parse.parse_qs(p.query)
            if path == "/":
                return self.redirect("/map")
            if path == "/login":
                return self.page_login(query)
            if path == "/map":
                return self.page_map(query)
            if path == "/apply":
                return self.page_apply(query)
            if path == "/adoptions":
                return self.page_adoptions()
            if path == "/calendar":
                return self.page_calendar(query)
            if path == "/diagnoses":
                return self.page_diagnoses()
            if path == "/reports":
                return self.page_reports()
            if path == "/notifications":
                return self.page_notifications()
            m = re.fullmatch(r"/trees/(\d+)", path)
            if m:
                return self.page_tree(int(m.group(1)))
            m = re.fullmatch(r"/adoptions/(\d+)", path)
            if m:
                return self.page_adoption(int(m.group(1)))
            m = re.fullmatch(r"/diagnoses/(\d+)", path)
            if m:
                return self.page_diagnosis(int(m.group(1)))
            m = re.fullmatch(r"/trees/(\d+)/reports/(\d+)", path)
            if m:
                return self.page_report(int(m.group(1)), int(m.group(2)))
            if path.startswith("/photos/"):
                return self.serve_photo(path[len("/photos/"):])
            self.error(404, "页面不存在")

        # ================= POST 路由 =================
        def route_post(self):
            path = urllib.parse.urlparse(self.path).path
            if path == "/login":
                return self.do_login()
            if path == "/logout":
                return self.do_logout()
            if path == "/apply":
                u = self.require_role(("resident",))
                if not u:
                    return
                f = self.form()
                aid = svc.apply_adoption(int(f["tree_id"]), u["id"],
                                         f["start_date"], f["end_date"], f["care_mode"])
                return self.redirect(f"/adoptions/{aid}")
            if path == "/login":
                return self.do_login()
            m = re.fullmatch(r"/adoptions/(\d+)/pay", path)
            if m:
                u = self.require_login()
                if u:
                    svc.pay_adoption(int(m.group(1)), u["id"])
                    self.redirect(f"/adoptions/{m.group(1)}")
                return
            m = re.fullmatch(r"/adoptions/(\d+)/exit", path)
            if m:
                u = self.require_login()
                if u:
                    f = self.form()
                    svc.exit_adoption(int(m.group(1)), u["id"], f.get("reason", "认养人主动退出"))
                    self.redirect(f"/adoptions/{m.group(1)}")
                return
            m = re.fullmatch(r"/adoptions/(\d+)/plan", path)
            if m:
                u = self.require_role(STAFF_ROLES)
                if u:
                    n = svc.plan_for_adoption(u, int(m.group(1)))
                    self.redirect(f"/adoptions/{m.group(1)}")
                return
            m = re.fullmatch(r"/tasks/(\d+)/complete", path)
            if m:
                u = self.require_login()
                if u:
                    f = self.form()
                    svc.complete_task(u, int(m.group(1)), f.get("summary", "已按计划完成"))
                    self.redirect("/calendar")
                return
            m = re.fullmatch(r"/trees/(\d+)/tasks", path)
            if m:
                u = self.require_role(STAFF_ROLES)
                if u:
                    f = self.form()
                    svc.add_task(u, int(m.group(1)), f["kind"], f["planned_date"],
                                 f.get("note", ""))
                    self.redirect(f"/trees/{m.group(1)}")
                return
            m = re.fullmatch(r"/trees/(\d+)/report", path)
            if m:
                u = self.require_role(("resident",))
                if u:
                    f = self.form()
                    did = svc.report_withering(u, int(m.group(1)), f.get("detail", ""))
                    self.redirect(f"/diagnoses/{did}")
                return
            m = re.fullmatch(r"/trees/(\d+)/diagnosis", path)
            if m:
                u = self.require_role(("gardener",))
                if u:
                    f = self.form()
                    svc.submit_diagnosis(u, int(m.group(1)), f["kind"], f.get("detail", ""),
                                         f.get("proposed_health") or None)
                    self.redirect(f"/trees/{m.group(1)}")
                return
            m = re.fullmatch(r"/trees/(\d+)/photos", path)
            if m:
                return self.do_upload_photo(int(m.group(1)))
            m = re.fullmatch(r"/diagnoses/(\d+)/review", path)
            if m:
                u = self.require_login()
                if u:
                    f = self.form()
                    svc.review_diagnosis(u, int(m.group(1)),
                                         approve=f["decision"] == "approve",
                                         note=f.get("note", ""))
                    self.redirect(f"/diagnoses/{m.group(1)}")
                return
            m = re.fullmatch(r"/trees/(\d+)/reports/generate", path)
            if m:
                u = self.require_role(STAFF_ROLES)
                if u:
                    year = int(self.form().get("year", ctx.clock.today()[:4]))
                    svc.generate_annual_report(u, int(m.group(1)), year)
                    self.redirect(f"/trees/{m.group(1)}")
                return
            if path == "/admin/sweep":
                u = self.require_role(STAFF_ROLES)
                if u:
                    n1 = svc.release_expired_holds()
                    n2 = svc.sweep_overdue()
                    self.redirect("/calendar" if False else
                                  qs("/map", msg=f"每日巡检完成：释放过期占位 {n1} 个，标记逾期任务 {n2} 条"))
                return
            self.error(404, "接口不存在")

        # ---------- 登录 ----------
        def page_login(self, query):
            users = svc.list_users()
            cards = "".join(
                f'<button class="btn secondary" style="margin:4px" name="username" value="{u["username"]}">'
                f'{esc(u["display_name"])} · { {"resident":"居民","gardener":"园艺师","supervisor":"主管","admin":"管理员"}[u["role"]] }'
                f'（{esc(u["username"])}）</button>'
                for u in users)
            nxt = query.get("next", ["/map"])[0]
            body = f"""<div class="card" style="max-width:560px;margin:40px auto">
<h1>登录公园树木认养平台</h1>
<p class="muted">演示环境：选择一个身份一键登录（无需密码）。</p>
<form method="post" action="/login">
<input type="hidden" name="next" value="{esc(nxt)}">
{cards}
</form></div>"""
            self.send_html(layout(ctx, None, "登录", body))

        def do_login(self):
            f = self.form()
            u = svc.get_user_by_name(f.get("username", ""))
            if not u:
                return self.error(400, "用户不存在")
            token = secrets.token_hex(16)
            ctx.sessions[token] = u["id"]
            nxt = f.get("next") or "/map"
            if not nxt.startswith("/"):
                nxt = "/map"
            cookie = (f"sid={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=86400")
            self.redirect(nxt, cookie=cookie)

        def do_logout(self):
            self.redirect("/login", cookie="sid=; Path=/; Max-Age=0")

        # ---------- 树木地图 ----------
        def page_map(self, query):
            u = self.require_login()
            if not u:
                return
            trees = svc.list_trees()
            # 步道（示意路径）
            paths = '<path d="M-20 470 C 220 430, 380 520, 620 440 S 980 470, 1020 420" '\
                    'fill="none" stroke="#e7dcc6" stroke-width="26" stroke-linecap="round"/>'
            paths += '<path d="M120 300 C 300 250, 480 180, 720 210" fill="none" '\
                     'stroke="#e7dcc6" stroke-width="18" stroke-linecap="round"/>'
            paths += '<path d="M560 392 C 600 300, 620 240, 640 178" fill="none" '\
                     'stroke="#e7dcc6" stroke-width="14" stroke-linecap="round"/>'
            markers = []
            for t in trees:
                st = t["adoption_state"]
                ring = {"可认养": "#ffffff", "已认养": "#2f5597",
                        "名额锁定中": "#e9b44c", "已被预约": "#e9b44c"}[st["state"]]
                dash = ' stroke-dasharray="4 3"' if st["state"] in ("名额锁定中", "已被预约") else ""
                markers.append(f"""
<a href="/trees/{t['id']}">
  <circle cx="{t['x']}" cy="{t['y']}" r="27" fill="{HEALTH_COLOR[t['health_status']]}"
    fill-opacity="0.92" stroke="{ring}" stroke-width="3"{dash}/>
  <text x="{t['x']}" y="{t['y']+4}" text-anchor="middle" font-size="12"
    font-weight="700" fill="#fff" pointer-events="none">{esc(t['code'])}</text>
  <title>{esc(t['code'])} {esc(t['species'])}｜健康：{t['health_status']}｜认养：{st['state']}</title>
</a>""")
            legend = "".join(
                f'<span><span class="dot" style="background:{c}"></span>{k}</span>'
                for k, c in HEALTH_COLOR.items())
            msg = query.get("msg", [None])[0]
            staff_btn = ""
            if u["role"] in STAFF_ROLES:
                staff_btn = ('<form method="post" action="/admin/sweep" style="display:inline">'
                             '<button class="btn small secondary">▶ 运行每日巡检'
                             '（释放过期占位 / 标记并重排逾期任务）</button></form>')
            body = f"""
<h1>公园树木地图 <span class="muted" style="font-weight:400">树冠位置与步道</span></h1>
<div class="card">
  <div class="legend">
    <b>树冠颜色＝健康状态：</b>{legend}
    <span><span class="dot" style="background:#fff;border:2px solid #2f5597"></span>蓝环＝已认养</span>
    <span><span class="dot" style="background:#fff;border:2px dashed #e9b44c"></span>黄虚环＝名额锁定/已预约</span>
    {staff_btn}
  </div>
  <svg viewBox="0 0 1000 560" style="width:100%;height:auto;background:#f0f6ee;border-radius:12px"
       role="img" aria-label="公园树木地图">
    <rect width="1000" height="560" fill="#eef5ea"/>
    <ellipse cx="470" cy="140" rx="120" ry="52" fill="#dcebd2" opacity="0.7"/>
    <text x="470" y="92" text-anchor="middle" font-size="14" fill="#5b7a63">湿地木栈道</text>
    <ellipse cx="760" cy="250" rx="110" ry="60" fill="#dcebd2" opacity="0.7"/>
    <text x="760" y="318" text-anchor="middle" font-size="14" fill="#5b7a63">北坡草坪</text>
    {paths}
    <text x="610" y="475" font-size="13" fill="#9a8f74" transform="rotate(-7 610 475)">环湖步道</text>
    <text x="300" y="246" font-size="13" fill="#9a8f74" transform="rotate(-12 300 246)">林荫小径</text>
    {''.join(markers)}
  </svg>
  <p class="muted">点击树冠查看树木档案。认养状态与健康状态在详情页分开展示。</p>
</div>"""
            self.render("树木地图", body, flash=msg, status=200 if not msg else 200)

        # ---------- 树木详情 ----------
        def page_tree(self, tree_id: int):
            u = self.require_login()
            if not u:
                return
            t = svc.get_tree(tree_id)
            st = svc._adoption_state(tree_id, ctx.clock.today())
            is_staff = u["role"] in STAFF_ROLES
            is_owner = svc.conn.execute(
                "SELECT 1 FROM adoptions WHERE tree_id=? AND user_id=? "
                "AND status IN ('active','exited') LIMIT 1",
                (tree_id, u["id"])).fetchone() is not None
            can_see_private = is_staff or is_owner

            adopt_detail = f'当前 {esc(st["state"])}'
            if st.get("until"):
                if st["state"] == "已认养":
                    adopt_detail += f'，认养期至 {st["until"]}'
                else:
                    adopt_detail += f'（{st.get("from","")} ~ {st["until"]}）'

            # 认养列表（仅园方/认养人可见）
            adopt_rows = ""
            if can_see_private:
                rows = svc.conn.execute(
                    """SELECT a.*, us.display_name FROM adoptions a
                       JOIN users us ON us.id=a.user_id
                       WHERE a.tree_id=? ORDER BY a.start_date DESC""", (tree_id,)).fetchall()
                status_cn = {"pending_payment": "待支付（占位）", "active": "生效中",
                             "exited": "已退出", "rejected": "已驳回", "expired": "占位过期"}
                adopt_rows = "".join(
                    f"<tr><td>{esc(r['display_name'])}</td><td>{r['start_date']} ~ {r['end_date']}</td>"
                    f"<td>{esc(r['care_mode'])}</td><td><span class='tag'>{status_cn[r['status']]}</span></td></tr>"
                    for r in rows)

            apply_hint = ""
            report_form = ""
            if u["role"] == "resident":
                report_form = f"""
                <form method="post" action="/trees/{tree_id}/report" class="card">
                  <h2>发现枯萎/异常？向园方报告</h2>
                  <p class="muted">居民报告会由园艺师现场核查；居民不能直接出具或修改专业健康结论。</p>
                  <textarea name="detail" rows="2" style="width:100%" placeholder="描述看到的现象，如叶片发蔫、位置等"></textarea>
                  <div style="margin-top:8px"><button class="btn secondary">提交报告</button></div>
                </form>"""

            gardener_form = ""
            if u["role"] == "gardener":
                gardener_form = f"""
                <form method="post" action="/trees/{tree_id}/diagnosis" class="card">
                  <h2>提交异常诊断（提交后由主管复核）</h2>
                  <div class="row">
                    <div><label>异常类型</label>
                      <select name="kind"><option>病害</option><option>虫害</option>
                      <option>枯萎误报</option><option>移栽</option><option>其他</option></select></div>
                    <div><label>拟更新健康结论（主管确认后生效）</label>
                      <select name="proposed_health"><option value="">不改变</option>
                      <option>健康</option><option>观察</option>
                      <option>患病封控</option><option>移栽中</option></select></div>
                  </div>
                  <label>诊断说明</label>
                  <textarea name="detail" rows="2" style="width:100%"></textarea>
                  <div style="margin-top:8px"><button class="btn">提交主管复核</button></div>
                </form>"""

            # 养护记录（季节分组，隐私受限）
            records_html = ""
            if can_see_private:
                grouped = svc.tree_care_records(tree_id)
                blocks = []
                for sname in ("春", "夏", "秋", "冬"):
                    items = grouped[sname]
                    if not items:
                        continue
                    lis = "".join(
                        f"<li><b>{esc(l['executed_on'])}</b> {esc(l['kind'])}"
                        f"（计划 {esc(l['planned_date'])}）— {esc(l['summary'])}"
                        f" <span class='muted'>执行人：{esc(l['actor_name'])}</span></li>"
                        for l in items)
                    blocks.append(f'<div class="season-title">{sname}季 · {len(items)} 条执行记录</div>'
                                  f"<ul style='margin:4px 0'>{lis}</ul>")
                records_html = "".join(blocks) or '<p class="muted">暂无真实执行记录。</p>'
            else:
                records_html = ('<p class="muted">🔒 巡查照片与真实养护记录仅对本树认养人及园方开放，'
                                '认养生效后可查看。</p>')

            # 待办任务
            tasks_html = ""
            if can_see_private:
                tasks = svc.list_tasks(svc.get_user(u["id"]), tree_id=tree_id)
                rows = []
                for tk in tasks:
                    action = ""
                    if self.can_complete_task(u, tk):
                        action = (f'<form method="post" action="/tasks/{tk["id"]}/complete" style="margin:0">'
                                  f'<button class="btn small">登记执行</button></form>')
                    rows.append(f"<tr><td>{esc(tk['planned_date'])}</td><td>{esc(tk['season'])}季</td>"
                                f"<td>{esc(tk['kind'])}</td><td>{esc(tk['status'])}</td>"
                                f"<td>{esc(tk['note'])}</td><td>{action}</td></tr>")
                add_form = ""
                if is_staff:
                    add_form = f"""
                    <form method="post" action="/trees/{tree_id}/tasks" class="row" style="margin-top:10px">
                      <div><label>类型</label><select name="kind"><option>浇水</option>
                      <option>修剪</option><option>病虫观察</option></select></div>
                      <div><label>计划日期</label><input type="date" name="planned_date" required></div>
                      <div style="flex:1"><label>说明</label><input name="note" style="width:100%"></div>
                      <div><button class="btn small">新增任务</button></div>
                    </form>"""
                tasks_html = (f'<table><tr><th>计划日期</th><th>季节</th><th>类型</th><th>状态</th>'
                              f'<th>说明</th><th></th></tr>{"".join(rows)}</table>{add_form}')

            # 事件时间线
            events = svc.tree_events(tree_id)
            events_html = "".join(
                f"<li><b>{esc(e['created_at'][:10])}</b> {esc(e['event_type'])}"
                f" <span class='muted'>{esc(e['actor_name'] or '系统')}</span></li>"
                for e in events) or '<li class="muted">暂无事件</li>'

            # 照片
            photos_html = ""
            if can_see_private:
                photos = svc.list_photos(tree_id)
                cards = "".join(
                    f'<figure style="margin:0"><img src="/{esc(p["path"])}" alt="{esc(p["caption"])}" '
                    f'loading="lazy"><figcaption class="muted" style="font-size:12px">'
                    f'{esc(p["taken_on"])} · {esc(p["uploader_name"])}<br>{esc(p["caption"])}</figcaption></figure>'
                    for p in photos)
                photos_html = f'<div class="photos">{cards}</div>' if cards else '<p class="muted">暂无照片。</p>'
            upload_form = ""
            if can_see_private:
                upload_form = f"""
                <form id="pf" class="row" style="margin-top:12px">
                  <div><label>巡查照片</label><input type="file" id="file" accept="image/*" required></div>
                  <div><label>拍摄日期</label><input type="date" id="taken_on"></div>
                  <div style="flex:1"><label>说明</label><input id="caption" style="width:100%"
                    placeholder="如：花后修剪现场"></div>
                  <div><button type="button" class="btn small" onclick="up()">上传照片</button></div>
                </form>
                <script>
                function up(){{
                  const f=document.getElementById('file').files[0];
                  if(!f){{alert('请选择照片');return;}}
                  if(f.size>4*1024*1024){{alert('照片需小于 4MB');return;}}
                  const r=new FileReader();
                  r.onload=()=>fetch('/trees/{tree_id}/photos',{{method:'POST',
                    headers:{{'Content-Type':'application/json'}},
                    body:JSON.stringify({{filename:f.name,data:r.result,
                      caption:document.getElementById('caption').value,
                      taken_on:document.getElementById('taken_on').value}})
                  }}).then(x=>x.ok?location.reload():x.text().then(t=>alert(t)));
                  r.readAsDataURL(f);
                }}
                </script>"""

            # 年报
            report_block = ""
            if can_see_private:
                reps = svc.conn.execute(
                    "SELECT * FROM annual_reports WHERE tree_id=? ORDER BY year DESC",
                    (tree_id,)).fetchall()
                links = " ".join(
                    f'<a class="tag" href="/trees/{tree_id}/reports/{r["year"]}">{r["year"]} 年度反馈</a>'
                    for r in reps)
                gen = ""
                if is_staff:
                    yr = int(ctx.clock.today()[:4])
                    gen = (f'<form method="post" action="/trees/{tree_id}/reports/generate" '
                           f'style="display:inline;margin-left:10px">'
                           f'<input type="number" name="year" value="{yr}" style="width:90px"> '
                           f'<button class="btn small">编制/刷新年度反馈</button></form>')
                report_block = f"<div>{links or '<span class=muted>暂无年度反馈</span>'}{gen}</div>"

            body = f"""
<p><a href="/map">← 返回地图</a></p>
<div class="grid2">
  <div class="card">
    <h1>{esc(t['code'])} · {esc(t['species'])}</h1>
    <p style="font-size:15px">📍 {esc(t['location'])}</p>
    <p class="muted">{esc(t['note'])}</p>
    <p style="margin:14px 0">
      <b>认养状态：</b>{badge(ADOPT_BADGE.get(st['state'], 'b-status'), adopt_detail)}
    </p>
    <p style="margin:14px 0">
      <b>健康状态：</b>{badge(HEALTH_BADGE[t['health_status']], t['health_status'])}
      <span class="muted">（专业结论，由园艺师诊断、主管复核后更新）</span>
    </p>
    <p>{'<a class="btn" href="' + qs('/apply', tree=tree_id) + '">申请认养</a>'
        if u['role'] == 'resident' and t['adoptable'] and st['state'] == '可认养'
        else ('<span class="tag">该树暂未开放认养</span>' if not t['adoptable'] else '')}</p>
  </div>
  <div class="card">
    <h2>事件时间线</h2>
    <ul class="timeline">{events_html}</ul>
  </div>
</div>

<div class="card">
  <h2>认养记录 {"" if can_see_private else "（仅本人/园方可见）"}</h2>
  {('<table><tr><th>认养人</th><th>期限</th><th>养护方式</th><th>状态</th></tr>' + adopt_rows + "</table>")
   if can_see_private else '<p class="muted">🔒 为保护跨用户隐私，他人认养记录不公开。</p>'}
</div>

<div class="card"><h2>养护计划（浇水 / 修剪 / 病虫观察）</h2>{tasks_html or '<p class="muted">🔒 仅认养人与园方可见。</p>'}</div>
<div class="card"><h2>真实执行记录（按季节排列）</h2>{records_html}</div>
<div class="card"><h2>年度反馈</h2>{report_block or '<p class="muted">🔒 仅认养人与园方可见。</p>'}</div>
<div class="card"><h2>巡查照片</h2>{photos_html}{upload_form}</div>
{gardener_form}{report_form}
"""
            self.render(f"树木 {t['code']}", body)

        # ---------- 认养申请 ----------
        def page_apply(self, query):
            u = self.require_role(("resident",))
            if not u:
                return
            svc.release_expired_holds()
            trees = svc.list_trees()
            preselect = query.get("tree", [""])[0]
            opts = ""
            for t in trees:
                st = t["adoption_state"]
                disabled = ""
                label = f"{t['code']} {t['species']}（{st['state']}）"
                if not t["adoptable"] or st["state"] != "可认养":
                    disabled = "disabled"
                    label += " — 不可申请"
                sel = "selected" if str(t["id"]) == preselect else ""
                opts += f'<option value="{t["id"]}" {sel} {disabled}>{esc(label)}</option>'
            today = ctx.clock.today()
            body = f"""
<div class="card" style="max-width:680px;margin:20px auto">
<h1>树木认养申请</h1>
<p class="muted">规则：同一棵树的认养期限不可重叠；提交后按提交时间锁定名额，
请在 <b>15 分钟</b>内完成模拟支付，逾期占位自动释放。</p>
<form method="post" action="/apply">
  <label>选择树木</label><select name="tree_id" required>{opts}</select>
  <div class="row">
    <div><label>认养开始</label><input type="date" name="start_date" min="{today}" value="{today}" required></div>
    <div><label>认养结束</label><input type="date" name="end_date" required></div>
    <div><label>养护方式</label><select name="care_mode">
      <option>自助养护</option><option>委托园艺师</option></select></div>
  </div>
  <p class="muted" style="margin-top:12px">费用由模拟支付适配器计算：0.10 元/天。</p>
  <button class="btn">提交申请并锁定名额</button>
</form></div>"""
            self.render("认养申请", body)

        # ---------- 我的认养 ----------
        def page_adoptions(self):
            u = self.require_login()
            if not u:
                return
            if u["role"] in STAFF_ROLES:
                rows = svc.conn.execute(
                    """SELECT a.*, t.code, t.species, us.display_name FROM adoptions a
                       JOIN trees t ON t.id=a.tree_id JOIN users us ON us.id=a.user_id
                       ORDER BY a.created_at DESC LIMIT 100""").fetchall()
            else:
                rows = svc.my_adoptions(u["id"])
            status_cn = {"pending_payment": "待支付（名额锁定中）", "active": "生效中",
                         "exited": "已退出", "rejected": "已驳回", "expired": "占位已过期"}
            trs = []
            for r in rows:
                action = f'<a class="btn small" href="/adoptions/{r["id"]}">查看</a> '
                if r["status"] == "pending_payment" and r["user_id"] == u["id"]:
                    action += (f'<form method="post" action="/adoptions/{r["id"]}/pay" style="display:inline">'
                               '<button class="btn small">模拟支付</button></form>')
                if r["status"] == "active" and r["user_id"] == u["id"]:
                    action += (f'<form method="post" action="/adoptions/{r["id"]}/exit" style="display:inline">'
                               '<button class="btn small danger">退出认养</button></form>')
                who = esc(r["display_name"]) if "display_name" in r.keys() else esc(u["display_name"])
                trs.append(f"<tr><td>{esc(r['code'])} {esc(r['species'])}</td><td>{who}</td>"
                           f"<td>{r['start_date']} ~ {r['end_date']}</td>"
                           f"<td>{esc(r['care_mode'])}</td><td>{status_cn[r['status']]}</td>"
                           f"<td>¥{r['fee_cents']/100:.2f}</td><td>{action}</td></tr>")
            body = f"""<h1>{'全部认养记录（园方视图）' if u['role'] in STAFF_ROLES else '我的认养'}</h1>
<div class="card"><table><tr><th>树木</th><th>认养人</th><th>期限</th><th>养护方式</th>
<th>状态</th><th>费用</th><th>操作</th></tr>{''.join(trs)}</table>
<p class="muted" style="margin-top:10px">「待支付」为名额锁定占位，15 分钟未支付自动释放。</p></div>"""
            self.render("认养记录", body)

        def page_adoption(self, aid: int):
            u = self.require_login()
            if not u:
                return
            a = svc.require_owner_or_staff(aid, u)
            owner = svc.get_user(a["user_id"])
            status_cn = {"pending_payment": "待支付（名额锁定中，15 分钟内有效）", "active": "生效中",
                         "exited": "已退出", "rejected": "已驳回", "expired": "占位已过期"}
            actions = ""
            if a["status"] == "pending_payment" and a["user_id"] == u["id"]:
                actions = f"""<form method="post" action="/adoptions/{aid}/pay">
                  <button class="btn">模拟支付 ¥{a['fee_cents']/100:.2f}（支付适配器）</button></form>"""
            if a["status"] == "active" and a["user_id"] == u["id"]:
                actions = f"""<form method="post" action="/adoptions/{aid}/exit">
                  <label>退出原因（选填）</label><input name="reason" style="width:100%">
                  <button class="btn danger" style="margin-top:8px">确认退出认养</button></form>"""
            plan_btn = ""
            if u["role"] in STAFF_ROLES and a["status"] == "active":
                plan_btn = (f'<form method="post" action="/adoptions/{aid}/plan" style="margin-top:10px">'
                            '<button class="btn secondary">按认养期限生成常规养护计划</button></form>')
            tasks = svc.conn.execute(
                "SELECT * FROM care_tasks WHERE adoption_id=? ORDER BY planned_date", (aid,)).fetchall()
            task_rows = "".join(
                f"<tr><td>{esc(t['planned_date'])}</td><td>{esc(t['season'])}季</td>"
                f"<td>{esc(t['kind'])}</td><td>{esc(t['status'])}</td><td>{esc(t['note'])}</td></tr>"
                for t in tasks) or '<tr><td colspan="5" class="muted">暂无计划</td></tr>'
            body = f"""
<p><a href="/adoptions">← 返回认养列表</a></p>
<div class="card"><h1>认养单 #{aid}</h1>
<table>
<tr><th>树木</th><td><a href="/trees/{a['tree_id']}">{esc(a['code'])} {esc(a['species'])}</a></td></tr>
<tr><th>认养人</th><td>{esc(owner['display_name'])}（{esc(owner['username'])}）</td></tr>
<tr><th>期限</th><td>{a['start_date']} ~ {a['end_date']}</td>
<tr><th>养护方式</th><td>{esc(a['care_mode'])}</td></tr>
<tr><th>状态</th><td>{status_cn[a['status']]}</td></tr>
<tr><th>提交/支付时间</th><td>{esc(a['created_at'])} / {esc(a['paid_at'] or '—')}</td></tr>
</table>{actions}{plan_btn}</div>
<div class="card"><h2>关联养护计划</h2>
<table><tr><th>计划日期</th><th>季节</th><th>类型</th><th>状态</th><th>说明</th></tr>{task_rows}</table></div>"""
            self.render(f"认养单 #{aid}", body)

        # ---------- 养护日历 ----------
        def page_calendar(self, query):
            u = self.require_login()
            if not u:
                return
            today = date.fromisoformat(ctx.clock.today())
            year = int(query.get("year", [today.year])[0])
            month = int(query.get("month", [today.month])[0])
            if month == 0:
                year, month = year - 1, 12
            if month == 13:
                year, month = year + 1, 1
            tasks = [t for t in svc.list_tasks(u)
                     if int(t["planned_date"][:4]) == year and int(t["planned_date"][5:7]) == month]
            by_day: dict[int, list] = {}
            for t in tasks:
                by_day.setdefault(int(t["planned_date"][8:10]), []).append(t)

            first = date(year, month, 1)
            start = first - __import__("datetime").timedelta(days=first.weekday())
            dows = "一二三四五六日"
            head = "".join(f'<div class="dow">周{c}</div>' for c in dows)
            cells = []
            cur = start
            for _ in range(42):
                classes = "day"
                if cur.month != month:
                    classes += " other"
                if cur == today:
                    classes += " today"
                evs = ""
                for t in by_day.get(cur.day, []):
                    color = {"已完成": "#2e6b4f", "已逾期": "#c0563f",
                             "已取消": "#9aa59d"}.get(t["status"], "#23332a")
                    evs += (f'<span class="ev ev-{esc(t["kind"])}" style="font-size:11px;color:{color}">'
                            f'{esc(t["kind"][:2])}{("✓" if t["status"]=="已完成" else "×" if t["status"]=="已取消" else "")}'
                            f' {esc(t["code"])}</span>')
                cells.append(f'<div class="{classes}"><b>{cur.day}</b>{evs}</div>')
                cur += __import__("datetime").timedelta(days=1)

            rows = []
            for t in tasks:
                act = ""
                if self.can_complete_task(u, t):
                    act = (f'<form method="post" action="/tasks/{t["id"]}/complete" style="margin:0;display:inline">'
                           f'<button class="btn small">登记执行</button></form>')
                rows.append(f"<tr><td>{esc(t['planned_date'])}</td><td>{esc(t['code'])} {esc(t['species'])}</td>"
                            f"<td>{esc(t['kind'])}</td><td>{esc(t['status'])}</td>"
                            f"<td>{esc(t['note'])}</td><td>{act}</td></tr>")
            body = f"""
<h1>养护日历 <span class="muted">{"" if u["role"] in STAFF_ROLES else "（仅显示我认养树木的任务）"}</span></h1>
<div class="card">
<div class="row" style="justify-content:space-between">
  <a class="btn small secondary" href="/calendar?year={year if month>1 else year-1}&month={month-1 if month>1 else 12}">← 上月</a>
  <b>{year} 年 {month} 月</b>
  <a class="btn small secondary" href="/calendar?year={year if month<12 else year+1}&month={month+1 if month<12 else 1}">下月 →</a>
</div>
<div class="cal" style="margin-top:12px">{head}{''.join(cells)}</div>
<p class="muted">蓝色浇水 / 紫色修剪 / 橙色病虫观察；每日巡检会把过期任务标记为「已逾期」并自动重排。</p>
</div>
<div class="card"><h2>本月任务</h2>
<table><tr><th>日期</th><th>树木</th><th>类型</th><th>状态</th><th>说明</th><th></th></tr>
{''.join(rows) or '<tr><td colspan=6 class=muted>本月无任务</td></tr>'}</table></div>"""
            self.render("养护日历", body)

        # ---------- 异常复核 ----------
        def page_diagnoses(self):
            u = self.require_login()
            if not u:
                return
            rows = svc.list_diagnoses(u)
            status_cn = {"待复核": "待复核", "已确认": "已确认", "已驳回": "已驳回（误报/不成立）"}
            trs = "".join(
                f"<tr><td><a href='/diagnoses/{r['id']}'>#{r['id']}</a></td>"
                f"<td>{esc(r['code'])}</td>"
                f"<td>{'🧑‍🌾 园艺师' if r['reporter_kind']=='gardener' else '🏠 居民'}"
                f" {esc(r['reporter_name'])}</td>"
                f"<td>{esc(r['kind'])}</td><td>{esc(r['detail'][:60])}…</td>"
                f"<td>{badge('b-lock' if r['status']=='待复核' else 'b-free', status_cn[r['status']])}</td></tr>"
                for r in rows)
            tip = {
                "gardener": "您提交的诊断须由主管复核后生效；居民的枯萎报告由您现场核查。",
                "supervisor": "园艺师提交的异常诊断由您复核；确认后专业健康结论方可更新。",
                "resident": "这里列出您提交的枯萎报告及核查结果。",
            }.get(u["role"], "")
            body = f"""<h1>异常诊断与复核</h1>
<div class="card"><p class="muted">{tip}</p>
<table><tr><th>#</th><th>树木</th><th>提交人</th><th>类型</th><th>内容</th><th>状态</th></tr>
{trs or '<tr><td colspan=6 class=muted>暂无记录</td></tr>'}</table></div>"""
            self.render("异常复核", body)

        def page_diagnosis(self, did: int):
            u = self.require_login()
            if not u:
                return
            d = svc.get_diagnosis(did)
            if u["role"] == "resident" and d["reporter_id"] != u["id"]:
                return self.error(403, "无权查看他人提交的报告")
            can_review = (
                d["status"] == "待复核" and
                ((d["reporter_kind"] == "gardener" and u["role"] == "supervisor") or
                 (d["reporter_kind"] == "resident" and u["role"] == "gardener")))
            source = "🧑‍🌾 园艺师异常诊断（主管复核）" if d["reporter_kind"] == "gardener" \
                else "🏠 居民疑似枯萎报告（园艺师现场核查）"
            review_form = ""
            if can_review:
                if d["reporter_kind"] == "gardener":
                    approve_label = f"确认诊断{'并更新健康结论为「' + d['proposed_health'] + '」' if d['proposed_health'] else ''}"
                    reject_label = "驳回诊断"
                else:
                    approve_label = "现场核查：确有异常（转主管复核健康结论）"
                    reject_label = "现场核查：系误报（树木正常）"
                review_form = f"""
                <form method="post" action="/diagnoses/{did}/review" class="card">
                  <h2>复核意见</h2>
                  <textarea name="note" rows="3" style="width:100%" placeholder="复核说明，将反馈给提交人"></textarea>
                  <div style="margin-top:10px">
                    <button name="decision" value="approve" class="btn">{esc(approve_label)}</button>
                    <button name="decision" value="reject" class="btn danger">{esc(reject_label)}</button>
                  </div>
                </form>"""
            result = ""
            if d["status"] != "待复核":
                result = f"""<p><b>复核结果：</b>{d['status']}；<b>复核人：</b>{esc(d['reviewer_name'] or '—')}
                <br><b>复核意见：</b>{esc(d['review_note'] or '—')}
                <br><span class="muted">{esc(d['reviewed_at'] or '')}</span></p>"""
            body = f"""
<p><a href="/diagnoses">← 返回复核列表</a></p>
<div class="card"><h1>异常记录 #{did}</h1>
<table>
<tr><th>来源</th><td>{source}</td></tr>
<tr><th>树木</th><td><a href="/trees/{d['tree_id']}">{esc(d['code'])} {esc(d['species'])}</a></td></tr>
<tr><th>类型</th><td>{esc(d['kind'])}</td></tr>
<tr><th>提交人</th><td>{esc(d['reporter_name'])}（{esc(d['created_at'])}）</td></tr>
<tr><th>内容</th><td>{esc(d['detail'])}</td></tr>
<tr><th>拟更新健康结论</th><td>{esc(d['proposed_health'] or '不改变')}（确认后生效）</td></tr>
</table>{result}</div>{review_form}"""
            self.render(f"异常 #{did}", body)

        # ---------- 年度报告 ----------
        def page_reports(self):
            u = self.require_login()
            if not u:
                return
            rows = svc.list_reports(u)
            trs = "".join(
                f"<tr><td><a href='/trees/{r['tree_id']}/reports/{r['year']}'>"
                f"{esc(r['code'])} {esc(r['species'])} · {r['year']}</a></td>"
                f"<td>{r['watering']}</td><td>{r['pruning']}</td><td>{r['pest']}</td>"
                f"<td>{r['alerts']}</td><td>{esc(r['summary'][:70])}…</td></tr>"
                for r in rows)
            body = f"""<h1>{'年度报告管理' if u['role'] in STAFF_ROLES else '我的年度反馈'}</h1>
<div class="card"><table><tr><th>树木与年度</th><th>浇水</th><th>修剪</th><th>病虫观察</th>
<th>确认异常</th><th>摘要</th></tr>{trs or '<tr><td colspan=6 class=muted>暂无已发布的年度反馈</td></tr>'}</table>
<p class="muted">年度反馈仅该树的认养人与园方可查看。</p></div>"""
            self.render("年度反馈", body)

        def page_report(self, tree_id: int, year: int):
            u = self.require_login()
            if not u:
                return
            r = svc.get_report(u, tree_id, year)
            t = svc.get_tree(tree_id)
            body = f"""
<p><a href="/reports">← 返回年度反馈列表</a></p>
<div class="card"><h1>{esc(t['code'])} {esc(t['species'])} · {year} 年度反馈</h1>
<div class="row" style="margin:14px 0">
  <span class="tag">浇水 {r['watering']} 次</span>
  <span class="tag">修剪 {r['pruning']} 次</span>
  <span class="tag">病虫观察 {r['pest']} 次</span>
  <span class="tag">确认异常 {r['alerts']} 起</span>
</div>
<p style="font-size:15px">{esc(r['summary'])}</p>
<p class="muted">发布时间：{esc(r['created_at'])}</p></div>"""
            self.render(f"{year} 年度反馈", body)

        # ---------- 通知 ----------
        def page_notifications(self):
            u = self.require_login()
            if not u:
                return
            items = svc.notify.inbox(u["id"])
            svc.conn.execute("UPDATE notifications SET read=1 WHERE user_id=?", (u["id"],))
            svc.conn.commit()
            html = "".join(
                f'<div class="notif {"unread" if not n["read"] else ""}"><b>{esc(n["title"])}</b>'
                f'<div>{esc(n["body"])}</div><div class="muted">{esc(n["created_at"])}</div></div>'
                for n in items) or '<p class="muted">暂无通知</p>'
            self.render("通知", f'<div class="card"><h1>通知（模拟通知适配器）</h1>{html}</div>')

        # ---------- 照片上传 / 静态文件 ----------
        def do_upload_photo(self, tree_id: int):
            u = self.require_login()
            if not u:
                return
            if svc.conn.execute(
                    "SELECT 1 FROM adoptions WHERE tree_id=? AND user_id=? "
                    "AND status IN ('active','exited') LIMIT 1",
                    (tree_id, u["id"])).fetchone() is None and u["role"] not in STAFF_ROLES:
                return self.error(403, "仅本树认养人或园方可上传巡查照片")
            body = self.json_body()
            data_url = body.get("data", "")
            m = re.match(r"data:image/([\w+]+);base64,(.*)", data_url, re.S)
            if not m:
                return self.error(400, "照片格式不正确")
            subtype = m.group(1)
            ext = "." + {"jpeg": "jpg", "svg+xml": "svg"}.get(subtype, subtype)
            if ext not in (".jpg", ".png", ".gif", ".webp", ".svg"):
                return self.error(400, "仅支持 jpg/png/gif/webp/svg")
            data = base64.b64decode(m.group(2))
            if len(data) > 4 * 1024 * 1024:
                return self.error(400, "照片需小于 4MB")
            name = body.get("filename", "photo" + ext)
            if not name.lower().endswith(ext):
                name += ext
            # 复用 service 的落盘逻辑（写入临时再登记）
            pid = svc.save_photo(tree_id, u["id"], name, data,
                                 body.get("caption", ""), body.get("taken_on") or None)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "id": pid}).encode())

        def serve_photo(self, rel: str):
            u = self.require_login()
            if not u:
                return
            # 防目录穿越；URL 为 /photos/<月目录>/<文件>，落盘根是配置的 photo_root
            root = ctx.photo_root.resolve()
            target = (root / rel).resolve()
            if root not in target.parents or not target.is_file():
                return self.error(404, "照片不存在")
            # 档案照片：所有登录用户可看；巡查/日志照片：仅该树认养人或园方
            is_profile = svc.conn.execute(
                "SELECT 1 FROM trees WHERE photo_path=? LIMIT 1", (f"photos/{rel}",)).fetchone()
            if not is_profile:
                row = svc.conn.execute(
                    "SELECT tree_id FROM photos WHERE path=? "
                    "UNION SELECT tree_id FROM care_logs WHERE photo_path=? LIMIT 1",
                    (f"photos/{rel}", f"photos/{rel}")).fetchone()
                if row:
                    tree_id = row["tree_id"]
                    owner = svc.conn.execute(
                        "SELECT 1 FROM adoptions WHERE tree_id=? AND user_id=? "
                        "AND status IN ('active','exited') LIMIT 1",
                        (tree_id, u["id"])).fetchone()
                    if not owner and u["role"] not in STAFF_ROLES:
                        return self.error(403, "无权查看该树的巡查照片：仅认养人可查看")
            ctype = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                     ".gif": "image/gif", ".webp": "image/webp",
                     ".svg": "image/svg+xml"}.get(target.suffix.lower(), "application/octet-stream")
            data = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "private, max-age=300")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def build_server(db_path: str | Path, photo_root: str | Path = "data/photos",
                 host: str = "127.0.0.1", port: int = 8000, clock: Clock | None = None,
                 reuse_db: bool = False) -> ThreadingHTTPServer:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    init_db(conn)
    ctx = AppContext(conn, clock or Clock(), Path(photo_root))
    server = ThreadingHTTPServer((host, port), make_handler(ctx))
    server.ctx = ctx  # type: ignore[attr-defined]
    return server
