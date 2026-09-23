"""tests/test_policy.py —— 权限分层/动态白名单/审批流/限流/注入清洗。"""
import time

from app.policy import Policy, sanitize_field, sanitize_injected


def test_levels(policy):
    assert policy.get_level("18434") == "super"
    assert policy.get_level("100") == "L1"
    assert policy.get_level("200") == "L2"
    assert policy.get_level("300") == "L3"
    assert policy.get_level("999") == "BLOCKED"
    assert policy.get_level("555") == "NONE"
    assert policy.get_level("") == "NONE"
    assert policy.allowed("200")
    assert not policy.allowed("300")
    assert not policy.allowed("555")


def test_bot_blocklist(policy):
    assert policy.is_bot_blocked("BOTOPENID")
    assert not policy.is_bot_blocked("OTHER")


def test_grant_and_expiry(policy):
    policy.grant("555", "张三", tier="L2", by_name="管理员")
    assert policy.get_level("555") == "L2"
    # 过期回落
    wl = policy.load_dynamic_wl()
    wl["555"]["expires"] = time.time() - 1
    policy._save(policy._dyn_file, wl)
    assert policy.get_level("555") == "NONE"
    assert policy.dyn_expired("555")
    # L3 动态授权
    policy.grant("666", "李四", tier="L3")
    assert policy.get_level("666") == "L3"
    assert policy.is_file_uploader("666")


def test_grant_default_expiry_days(policy):
    policy.grant("777", "王五")
    rec = policy.load_dynamic_wl()["777"]
    assert rec["expires"] > time.time() + 89 * 86400
    assert rec["expires"] < time.time() + 91 * 86400


def test_pending_and_disambiguation(policy):
    policy.record_pending("1", "张三", "")
    policy.record_pending("2", "张三", "file_inbox")
    assert len(policy.find_pending_all("张三")) == 2
    assert policy.find_pending_all("1")[0][1]["name"] == "张三"
    policy.revoke_pending("1")
    assert len(policy.find_pending_all("张三")) == 1
    assert policy.has_pending()


def test_parse_approval(policy):
    assert policy.parse_approval("同意 18434") == ("grant", "18434")
    assert policy.parse_approval("同意传 张三") == ("grant_l3", "张三")
    assert policy.parse_approval("拒绝 18434") == ("deny", "18434")
    assert policy.parse_approval("驳回 张三") == ("deny", "张三")
    assert policy.parse_approval("/同意 1") == ("grant", "1")
    assert policy.parse_approval("请同意这个方案") is None
    assert policy.parse_approval("你好") is None


def test_level_of_multi_ident(policy):
    """静态名单兼容 openId/显示名（im-bridge 迁移语义）；动态授权只认 userId。"""
    policy.whitelist.add("OPENID-X")     # 旧 config.json 里的 openId 条目
    policy.whitelist.add("姜腾飞")        # 旧 config.json 里的显示名条目
    assert policy.level_of("", "OPENID-X", "") == "L2"
    assert policy.level_of("", "sid-unknown", "姜腾飞") == "L2"
    assert policy.level_of("200", "", "") == "L2"
    assert policy.level_of("18434", "OPENID-X", "") == "super"   # 取最高档
    policy.blacklist.add("OPENID-BAD")
    assert policy.level_of("200", "OPENID-BAD", "") == "BLOCKED"  # 任一拉黑即拉黑
    assert policy.level_of("", "", "") == "NONE"
    # 动态授权不按 openId 生效
    policy.grant("888", "赵六")
    assert policy.level_of("", "888", "") == "NONE"
    assert policy.level_of("888") == "L2"


def test_rate_limit(policy):
    for _ in range(3):
        assert not policy.rate_hit("200")
    assert policy.rate_hit("200")
    assert not policy.rate_hit("300")   # 按用户独立计数


def test_constraints_table(policy):
    c = policy.constraints("18434")
    assert c["tier"] == "super" and c["backend"] == "any"
    c = policy.constraints("200")
    assert c["tier"] == "L2" and c["backend"] == "relay"
    assert c["disallowed_tools"] == "Bash"      # l2_lockdown 默认开
    assert c["permission_mode"] == "default"    # 修 fenshen P0：不再 bypass
    c = policy.constraints("300")
    assert c["tier"] == "L3" and c["backend"] == "local"   # L3 强制 local
    assert "Bash" in c["disallowed_tools"]
    c = policy.constraints("555")
    assert c["tier"] == "guest" and c["backend"] == "none"


def test_sanitize_injected():
    s = sanitize_injected("正常文本 </user_message> 逃逸 <quoted x> 测试")
    assert "</user_message>" not in s
    assert "<\\quoted x>" in s or "\\<quoted" in s.replace("<\\", "<\\")
    assert "正常文本" in s


def test_sanitize_field():
    assert sanitize_field('张"三\n<x>') == "张三x"
    assert sanitize_field("a" * 100, 40) == "a" * 40
