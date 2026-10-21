from app.ingress_dingtalk import Dws, parse_card_response


class Cfg:
    avatar_name = "测试"
    dws_profile = ""

    def get(self, section, key, default=None):
        return default

    def section(self, name):
        return {}


def make_dws(monkeypatch, resp):
    d = Dws(Cfg(), audit=None, dry=False)
    d.run = lambda args, timeout=60: resp
    return d


def test_parse_accepted_unverified_is_ok():
    r = parse_card_response({"success": True, "accepted": True,
                             "verified": False,
                             "warning": "服务端已接受"})
    assert r["ok"] is True
    assert r["verified"] is False


def test_parse_error_retryable():
    r = parse_card_response({"error": {"message": "timeout",
                                       "retryable": True}})
    assert r["ok"] is False and r["retryable"] is True


def test_parse_success_false():
    r = parse_card_response({"success": False, "warning": "未应用"})
    assert r["ok"] is False


def test_card_update_ex_uses_structured_response(monkeypatch):
    d = make_dws(monkeypatch, {"success": True, "accepted": True,
                               "verified": False})
    res = d.card_update_ex("BIZ", "内容", 3)
    assert res["ok"] is True and res["verified"] is False
    assert d.card_update("BIZ", "内容", 3) is True


def test_card_update_ex_empty_biz():
    d = Dws(Cfg(), audit=None, dry=False)
    res = d.card_update_ex("", "内容", 3)
    assert res["ok"] is False and res["error"] == "empty_biz"
