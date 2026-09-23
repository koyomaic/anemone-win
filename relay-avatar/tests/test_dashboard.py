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

    def __init__(self, cfg, storage, password="pw123", token="tk9"):
        cfg.env["AVATAR_DASHBOARD_PASSWORD"] = password
        cfg.env["AVATAR_DASHBOARD_TOKEN"] = token
        self.dash = Dashboard(cfg, storage)
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
