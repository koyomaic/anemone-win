"""tests/test_file_inbox.py —— 文件收件 + store 登记 + doc→md + 校验。"""
from pathlib import Path

from app.file_inbox import FileInbox, extract_file_from_event, find_media_ids


class FakeDws:
    def __init__(self, payload: bytes = b"data", md: bool = False):
        self.payload = payload
        self.md = md
        self.downloaded = []

    def download_file_by_id(self, file_id, out_abs):
        self.downloaded.append(("fileId", file_id))
        Path(out_abs).write_bytes(self.payload)
        return str(out_abs)

    def download_media(self, media_id, message_id, conv_id, outdir):
        self.downloaded.append(("mediaId", media_id))
        p = Path(outdir) / f"{media_id[:8]}.png"
        p.write_bytes(self.payload)
        return {"path": str(p)}

    def doc_to_markdown(self, local_path, out_md):
        if not self.md:
            return ""
        Path(out_md).write_text("# 转出的正文")
        return str(out_md)


def make_inbox(cfg, dws, storage, audit):
    return FileInbox(cfg, dws, storage, audit)


def test_extract_file_from_event_text():
    ev = {"content": "[文件] 京博选型-深挖.docx fileId: QPGYqjpJYdr14g 注意：如需下载使用dws drive download命令"}
    fi = extract_file_from_event(ev)
    assert fi["file_id"] == "QPGYqjpJYdr14g"
    assert fi["file_name"] == "京博选型-深挖.docx"


def test_extract_file_from_event_json():
    ev = {"content": '{"fileId":"ABC","fileName":"x.pdf"}'}
    fi = extract_file_from_event(ev)
    assert fi["file_id"] == "ABC" and fi["file_name"] == "x.pdf"


def test_extract_media_ids_nested():
    out = []
    find_media_ids({"a": [{"mediaId": "M1"}, {"x": '{"downloadCode":"D1"}'}]},
                   out)
    assert "M1" in out and "D1" in out


def test_receive_registers_when_store_ok(cfg, storage, audit, monkeypatch):
    dws = FakeDws(md=True)
    inbox = make_inbox(cfg, dws, storage, audit)
    monkeypatch.setattr(storage, "_store", lambda a, timeout=60: (0, ""))
    storage._store_ok = True
    ev = {"content": "[文件] 报告.docx fileId: FID123",
          "message_id": "m1", "conversation_id": "c1"}
    receipt = inbox.receive("20102", "张三", ev)
    assert "已收到" in receipt and "登记台账" in receipt
    # 原件落 shared/data 合规命名
    files = list((cfg.store_root / "data").rglob("*_20102_T0_报告_v1.docx"))
    assert len(files) == 1
    # doc→md variant=dingdoc + 血缘
    mds = list(files[0].parent.glob("*-dingdoc_v1.md"))
    assert len(mds) == 1 and mds[0].read_text("utf-8") == "# 转出的正文"


def test_receive_staging_when_store_down(cfg, storage, audit):
    dws = FakeDws()
    inbox = FileInbox(cfg, dws, storage, audit)
    ev = {"content": "[文件] a.md fileId: FID1", "message_id": "m",
          "conversation_id": "c"}
    receipt = inbox.receive("20102", "张三", ev)
    assert "暂存" in receipt
    pend = storage.load_json(storage.run / "pending_store.json", [])
    assert len(pend) == 1 and pend[0]["sender"] == "20102"


def test_receive_rejects_bad_ext(cfg, storage, audit):
    dws = FakeDws()
    inbox = FileInbox(cfg, dws, storage, audit)
    ev = {"content": "[文件] virus.exe fileId: F1", "message_id": "m",
          "conversation_id": "c"}
    receipt = inbox.receive("20102", "张三", ev)
    assert "不支持的文件类型" in receipt


def test_receive_rejects_oversize(cfg, storage, audit):
    dws = FakeDws(payload=b"x" * (21 * 1024 * 1024))
    inbox = FileInbox(cfg, dws, storage, audit)
    ev = {"content": "[文件] big.md fileId: F1", "message_id": "m",
          "conversation_id": "c"}
    assert "超过" in inbox.receive("20102", "张三", ev)


def test_receive_not_file(cfg, storage, audit):
    inbox = FileInbox(cfg, FakeDws(), storage, audit)
    assert inbox.receive("20102", "张三", {"content": "普通消息"}) == ""
