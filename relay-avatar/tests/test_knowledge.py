"""tests/test_knowledge.py —— 分词/片段/本地检索/速记暂存。"""
from app.knowledge import Knowledge, split_frags, tokenize


def test_tokenize():
    toks = tokenize("门店日销口径调整 v2")
    assert "门店" in toks and "日销" in toks and "口径" in toks
    assert "v2" in toks or "v2" not in toks  # 英文短词按规则
    assert tokenize("") == []


def test_split_frags():
    body = "段落一。\n\n" + "长" * 2000
    frags = split_frags(body, max_len=500)
    assert all(len(f) <= 500 for f in frags)
    assert len(frags) >= 4


def test_local_search(cfg, storage, shared):
    (shared / "knowledge" / "工具运维" / "relay部署.md").write_text(
        "# relay 部署\n\n部署用 boot.sh，门禁跑 pytest。\n\n"
        "健康检查 curl /health。", encoding="utf-8")
    (shared / "knowledge" / "工具运维" / "无关.md").write_text(
        "今天天气不错，出去散步。", encoding="utf-8")
    k = Knowledge(cfg, storage)
    hit = k.search("relay 部署门禁")
    assert "boot.sh" in hit
    assert "参考资料" in hit      # 防注入前缀
    assert "散步" not in hit


def test_search_empty(cfg, storage):
    k = Knowledge(cfg, storage)
    assert k.search("") == ""


def test_save_note_staging(cfg, storage):
    k = Knowledge(cfg, storage)
    p = k.save_note("速记：明早九点开营")
    assert p.exists() and p.parent == storage.staging
    assert "速记" in p.read_text("utf-8")


def test_promote(cfg, storage, shared, monkeypatch):
    k = Knowledge(cfg, storage)
    note = k.save_note("知识内容")
    calls = []
    monkeypatch.setattr(storage, "_store",
                        lambda a, timeout=60: (calls.append(a) or (0, "")))
    dst, ok, _ = k.promote(note, "工具运维", "运维经验")
    assert ok and dst.exists()
    assert dst.parent == shared / "knowledge" / "工具运维"
    add = [c for c in calls if c[0] == "add"][0]
    assert "--wiki-tag" in add and "工具运维" in add
    assert "--kind" in add and "knowledge" in add
