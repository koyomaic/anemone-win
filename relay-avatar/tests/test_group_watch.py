"""tests/test_group_watch.py —— 群监控指令解析 + 登记表 + 默认关行为。"""
from app.group_watch import GroupWatch, WatchList, parse_watch_cmd


def test_parse_watch_cmd():
    assert parse_watch_cmd("监控本群") == ("watch", "")
    assert parse_watch_cmd("/监控 财务群") == ("watch", "财务群")
    assert parse_watch_cmd("取消监控") == ("unwatch", "")
    assert parse_watch_cmd("停止监控 本群") == ("unwatch", "")
    assert parse_watch_cmd("帮我盯着 项目群") == ("watch", "项目群")
    assert parse_watch_cmd("你好") is None
    assert parse_watch_cmd("关注 项目群") == ("watch", "项目群")


def test_disabled_by_default(cfg, storage, audit):
    gw = GroupWatch(cfg, storage, audit)
    assert not gw.enabled
    fb = gw.handle_cmd(("watch", ""), "200", "cid1", True)
    assert "未启用" in fb
    # 未启用时采集 no-op
    gw.collect({"conversation_id": "cid1", "content": "x", "sender": "s"})
    assert not (storage.run / "watch").exists()


def test_watchlist_add_remove(cfg, storage):
    wl = WatchList(storage)
    assert wl.add("cid1", "财务群", "200") == "added"
    assert wl.add("cid1", "财务群", "200") == "exists"
    wl.add("cid1", "财务群", "300")
    assert wl.remove("cid1", "200") == "removed"
    assert "cid1" in wl.watched()      # 还有 300 在
    assert wl.remove("cid1", "300") == "removed"
    assert "cid1" not in wl.watched()  # 无人指派即摘除
    assert wl.remove("cid1", "200") == "absent"


def test_enabled_flow(cfg, storage, audit, monkeypatch):
    monkeypatch.setitem(cfg._p.setdefault("group_watch", {}), "enabled", True)
    gw = GroupWatch(cfg, storage, audit)
    fb = gw.handle_cmd(("watch", ""), "200", "cid1", True)
    assert "已开始监控" in fb
    gw.collect({"conversation_id": "cid1", "content": "要点内容",
                "sender": "张三"})
    gw.collect({"conversation_id": "cidX", "content": "未登记群",
                "sender": "李四"})
    files = list((storage.run / "watch").glob("*.ndjson"))
    assert len(files) == 1
    assert "要点内容" in files[0].read_text("utf-8")
    assert "未登记群" not in files[0].read_text("utf-8")
    fb = gw.handle_cmd(("unwatch", ""), "200", "cid1", True)
    assert "已取消" in fb
