"""tests/test_dashboard.py —— 口令登录 + 防爆破 + 会话 cookie（起真实 HTTP 服务测）。"""
import http.client
import json
import threading
import urllib.parse
from http.server import ThreadingHTTPServer

from app.dashboard import COOKIE, LOGIN_MAX_FAILS, Dashboard, LoginGuard


def test_guard_lock_window():
    g = LoginGuard("pw123")
    for i in range(LOGIN_MAX_FAILS - 1):
        st, _ = g.try_login("1.2.3.4", "wrong")
        assert st == "fail"
    st, left = g.try_login("1.2.3.4", "wrong")
    assert st == "fail"
    # 第 5+1 次起锁定
    st, left = g.try_login("1.2.3.4", "pw123")   # 正确口令也被锁拒
    assert st == "locked" and left > 0
    # 其他 IP 不受影响
    assert g.try_login("9.9.9.9", "pw123")[0] == "ok"


def test_guard_session_lifecycle():
    g = LoginGuard("pw")
    st, tok = g.try_login("1.1.1.1", "pw")
    assert st == "ok" and g.check(tok)
    assert not g.check("bogus") and not g.check("")
    g.logout(tok)
    assert not g.check(tok)
    # 过期会话
    st, tok2 = g.try_login("1.1.1.1", "pw")
    g.sessions[tok2] = 1   # 拨到过期
    assert not g.check(tok2)


def test_guard_disabled():
    assert not LoginGuard("").enabled


class LiveServer:
    """真实起服务测 HTTP 交互（127.0.0.1 随机端口）。"""

    def __init__(self, cfg, storage, password="pw123", token="tk9",
                 policy=None, audit=None):
        cfg.env["AVATAR_DASHBOARD_PASSWORD"] = password
        cfg.env["AVATAR_DASHBOARD_TOKEN"] = token
        self.dash = Dashboard(cfg, storage, audit, policy=policy)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), self.dash._handler())
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def req(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        out = (r.status, dict(r.getheaders()), data)
        c.close()
        return out

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def test_login_flow_live(cfg, storage):
    srv = LiveServer(cfg, storage)
    try:
        # 未登录 → 登录页
        st, _, body = srv.req("GET", "/")
        assert st == 200 and "访问口令".encode() in body
        # 错误口令 → 303 /?e=1 → 错误提示页
        form = urllib.parse.urlencode({"password": "bad"}).encode()
        st, hd, _ = srv.req("POST", "/login", body=form,
                            headers={"Content-Type":
                                     "application/x-www-form-urlencoded"})
        assert st == 303 and hd["Location"] == "/?e=1"
        st, _, body = srv.req("GET", "/?e=1")
        assert "口令错误".encode() in body
        # 正确口令 → 302 + 会话 cookie → 首页可看
        form = urllib.parse.urlencode({"password": "pw123"}).encode()
        st, hd, _ = srv.req("POST", "/login", body=form,
                            headers={"Content-Type":
                                     "application/x-www-form-urlencoded"})
        assert st == 302 and hd["Location"] == "/"
        cookie = hd["Set-Cookie"]
        assert cookie.startswith(COOKIE + "=") and "HttpOnly" in cookie \
            and "SameSite=Strict" in cookie
        st, _, body = srv.req("GET", "/", headers={"Cookie": cookie})
        assert st == 200 and "relay-avatar 看板".encode() in body
        st, _, body = srv.req("GET", "/api/snapshots",
                              headers={"Cookie": cookie})
        assert st == 200 and json.loads(body) == [] or st == 200
        # 退出 → 回登录页
        st, _, _ = srv.req("GET", "/logout", headers={"Cookie": cookie})
        assert st == 303
        st, _, body = srv.req("GET", "/", headers={"Cookie": cookie})
        assert "访问口令".encode() in body
    finally:
        srv.close()


def test_bruteforce_lock_live(cfg, storage):
    srv = LiveServer(cfg, storage)
    try:
        form = urllib.parse.urlencode({"password": "bad"}).encode()
        for _ in range(LOGIN_MAX_FAILS):
            srv.req("POST", "/login", body=form,
                    headers={"Content-Type":
                             "application/x-www-form-urlencoded"})
        # 锁定后即使口令正确也 429 + Retry-After
        form = urllib.parse.urlencode({"password": "pw123"}).encode()
        st, hd, body = srv.req("POST", "/login", body=form,
                               headers={"Content-Type":
                                        "application/x-www-form-urlencoded"})
        assert st == 429 and int(hd["Retry-After"]) > 0
        assert "锁定".encode() in body
    finally:
        srv.close()


def test_token_and_health_still_work(cfg, storage):
    srv = LiveServer(cfg, storage)
    try:
        # /health 开放（provision 探活）
        st, _, body = srv.req("GET", "/health")
        assert st == 200 and json.loads(body)["ok"] is True
        # X-Token 机访通道仍有效（probe 上报）
        import time as _t
        st, _, _ = srv.req("POST", "/report",
                           body=json.dumps({"host": "test机", "ts": _t.time(),
                                            "services": []}).encode(),
                           headers={"X-Token": "tk9"})
        assert st == 200
        # 错 token 拒绝
        st, _, _ = srv.req("POST", "/report", body=b"{}",
                           headers={"X-Token": "wrong"})
        assert st == 403
        # token 可看 API
        st, _, body = srv.req("GET", "/api/snapshots",
                              headers={"X-Token": "tk9"})
        assert st == 200 and "test机" in body.decode()
    finally:
        srv.close()


# ---- 「用户与权限」页签：/api/users 读花名册 + 写操作（仅口令会话）----

def _login_cookie(srv) -> str:
    form = urllib.parse.urlencode({"password": "pw123"}).encode()
    st, hd, _ = srv.req("POST", "/login", body=form,
                        headers={"Content-Type":
                                 "application/x-www-form-urlencoded"})
    assert st == 302
    return hd["Set-Cookie"]


def test_users_api_roster(cfg, storage, policy):
    srv = LiveServer(cfg, storage, policy=policy)
    try:
        ck = _login_cookie(srv)
        st, _, body = srv.req("GET", "/api/users", headers={"Cookie": ck})
        assert st == 200
        d = json.loads(body)
        # 静态名单来自 conftest POLICY_TMPL
        assert d["superadmin"] == ["18434"]
        assert d["whitelist"] == ["200"] and d["admins"] == ["100"]
        assert d["file_uploaders"] == ["300"] and d["blacklist"] == ["999"]
        assert d["bot_blocklist"] == ["BOTOPENID"]
        assert d["rate_limit"] == 3 and d["grant_valid_days"] == 90
        assert d["guest_mode"] == "silent"
        assert d["dynamic"] == {} and d["pending"] == {}
        # 机访 token 可读
        st, _, body = srv.req("GET", "/api/users", headers={"X-Token": "tk9"})
        assert st == 200 and json.loads(body)["superadmin"] == ["18434"]
        # 未登录不可读（走登录页）
        st, _, body = srv.req("GET", "/api/users")
        assert "访问口令".encode() in body
    finally:
        srv.close()


def test_users_api_write_requires_session(cfg, storage, policy):
    srv = LiveServer(cfg, storage, policy=policy)
    try:
        # 机访 token 只读：写操作 403（防脚本误改名单）
        st, _, _ = srv.req("POST", "/api/users",
                           body=json.dumps({"action": "grant", "uid": "1"}).encode(),
                           headers={"X-Token": "tk9",
                                    "Content-Type": "application/json"})
        assert st == 403
    finally:
        srv.close()


def test_users_api_approve_deny_grant_revoke(cfg, storage, policy, audit):
    srv = LiveServer(cfg, storage, policy=policy, audit=audit)
    try:
        ck = _login_cookie(srv)
        H = {"Cookie": ck, "Content-Type": "application/json"}

        def post(payload):
            return srv.req("POST", "/api/users",
                           body=json.dumps(payload).encode(), headers=H)

        # 待审申请 → 花名册可见
        policy.record_pending("12345", "张三", title="工程师")
        d = json.loads(srv.req("GET", "/api/users", headers={"Cookie": ck})[2])
        assert d["pending"]["12345"]["name"] == "张三"

        # 批准 → 进动态白名单 L2，待审清空
        st, _, _ = post({"action": "approve", "uid": "12345"})
        assert st == 200
        wl = policy.load_dynamic_wl()
        assert wl["12345"]["tier"] == "L2" and wl["12345"]["name"] == "张三"
        assert wl["12345"]["by"] == "dashboard"
        assert policy.load_pending() == {}

        # 手动授权 L3 + 自定义天数
        st, _, _ = post({"action": "grant", "uid": "777", "name": "李四",
                         "tier": "L3", "days": 7})
        assert st == 200
        import time as _t
        rec = policy.load_dynamic_wl()["777"]
        assert rec["tier"] == "L3"
        assert abs(rec["expires"] - (_t.time() + 7 * 86400)) < 60

        # 非法 tier / 未知 action / 缺 uid → 400
        assert post({"action": "grant", "uid": "8", "tier": "L9"})[0] == 400
        assert post({"action": "hack", "uid": "8"})[0] == 400
        assert post({"action": "grant", "uid": ""})[0] == 400

        # 撤销动态白名单：一次成功，二次 404
        assert post({"action": "revoke", "uid": "777"})[0] == 200
        assert "777" not in policy.load_dynamic_wl()
        assert post({"action": "revoke", "uid": "777"})[0] == 404

        # 拒绝待审：只清 pending，不进白名单
        policy.record_pending("555", "王五")
        assert post({"action": "deny", "uid": "555"})[0] == 200
        assert policy.load_pending() == {}
        assert "555" not in policy.load_dynamic_wl()

        # 审计留痕：4 次成功写操作各一条（400/404 失败不留痕）
        acts = [ln for ln in (storage.state / "audit" / "audit.log").read_text(
            "utf-8").splitlines() if "dash_user_" in ln]
        assert len(acts) == 4
        assert {json.loads(a)["action"] for a in acts} == {
            "dash_user_approve", "dash_user_grant",
            "dash_user_revoke", "dash_user_deny"}
    finally:
        srv.close()


def test_users_page_has_tabs(cfg, storage, policy):
    srv = LiveServer(cfg, storage, policy=policy)
    try:
        ck = _login_cookie(srv)
        st, _, body = srv.req("GET", "/", headers={"Cookie": ck})
        assert st == 200
        for marker in ("用户与权限", 'id="view-users"', 'id="tb-users"',
                       'id="btn-grant"', "/api/users"):
            assert marker.encode() in body, marker
    finally:
        srv.close()
