"""app/file_inbox.py —— 上传文件收件 + store 台账登记（P1）；L3 隔离问答（P2 预留）。

入站原件一律落 store 台账体系：
  run/staging 暂存下载 → shared/data/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<senderId>/
  命名 <YYYYMMDD>_<senderId>_T0_<topic>_v<N>.<ext>（接收时无任务号先 T0，
  建任务后由调用方 store link 关联）→ store add --kind data --sender <uid>。
docx/pdf/xlsx 等经钉钉转 markdown 存同目录，文件名 variant 标 dingdoc，
store link 指回原件（血缘可溯）。
store DB 不可达：留在 run/staging + pending_store.json，恢复后补登记（不静默绕过）。
L3 问答硬隔离（manual+prompts=none+add-dir 本人 sender 数据区+禁 Bash/Write/…）
依赖 exec_local，P2 开放；此前 L3 纯文字问答礼貌告知未开放。
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from .storage import sanitize_topic, safe_sender

CONVERTIBLE = {".docx", ".doc", ".pdf", ".xlsx", ".xls", ".pptx", ".ppt"}


def extract_file_from_event(ev: dict) -> dict:
    """从事件提取文件要素 {file_id, media_id, file_name}。
    实测(dws v1.0.62)：单聊文件消息 content 是纯文本
    "[文件] <名> fileId: <ID> 注意：如需下载使用dws drive download命令"，
    文件走 fileId（drive download --node）；保留 mediaId/JSON content 兜底。"""
    file_id = ""
    media_id = ""
    file_name = str(ev.get("file_name") or ev.get("fileName") or
                    ev.get("name") or "")
    c = ev.get("content")
    if isinstance(c, str) and c.strip():
        m = re.search(r"fileId[:：]\s*([A-Za-z0-9_\-]+)", c)
        if m:
            file_id = m.group(1)
        mn = re.search(r"\[文件\]\s*(.+?)\s*fileId[:：]", c)
        if mn and not file_name:
            file_name = mn.group(1).strip()
        if not file_id and c.strip().startswith("{"):
            try:
                cj = json.loads(c)
                file_id = cj.get("fileId") or cj.get("file_id") or ""
                media_id = (cj.get("mediaId") or cj.get("downloadCode")
                            or cj.get("media_id") or "")
                file_name = file_name or cj.get("fileName") or \
                    cj.get("name") or ""
            except Exception:
                pass
    elif isinstance(c, dict):
        file_id = c.get("fileId") or c.get("file_id") or ""
        media_id = (c.get("mediaId") or c.get("downloadCode")
                    or c.get("media_id") or "")
        file_name = file_name or c.get("fileName") or c.get("name") or ""
    file_id = file_id or ev.get("file_id") or ev.get("fileId") or ""
    media_id = media_id or (ev.get("media_id") or ev.get("mediaId")
                            or ev.get("downloadCode") or "")
    return {"file_id": file_id, "media_id": media_id,
            "file_name": file_name or "文件"}


def find_media_ids(obj, out: list):
    """递归找 mediaId/downloadCode 类字段（图片/语音等媒体消息）。"""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("mediaId", "downloadCode", "pictureDownloadCode") \
                    and isinstance(v, str) and v:
                out.append(v)
            else:
                find_media_ids(v, out)
    elif isinstance(obj, list):
        for i in obj:
            find_media_ids(i, out)
    elif isinstance(obj, str) and obj[:1] in "[{":
        try:
            find_media_ids(json.loads(obj), out)
        except Exception:
            pass


class FileInbox:
    def __init__(self, cfg, dws, storage, audit):
        self.cfg = cfg
        self.dws = dws
        self.st = storage
        self.audit = audit
        sec = cfg.section("security")
        self.max_bytes = int(sec.get("media_max_size_mb", 20)) * 1024 * 1024
        self.allowed_ext = {str(e).lower()
                            for e in sec.get("media_allowed_ext", [])}

    # ── 下载 ──
    def download(self, ev: dict, fi: dict) -> Path | None:
        """下载到 run/staging，返回本地路径（失败 None）。"""
        ts = time.strftime("%Y%m%d-%H%M%S")
        name = re.sub(r"[^\w一-鿿.\- ]", "_",
                      Path(fi["file_name"]).name).strip() or "file"
        dst = self.st.staging / f"{ts}__{name}"
        if fi["file_id"]:
            p = self.dws.download_file_by_id(fi["file_id"], dst)
            if p and Path(p).exists() and Path(p).stat().st_size:
                return Path(p)
        if fi["media_id"]:
            r = self.dws.download_media(
                fi["media_id"], ev.get("message_id") or "",
                ev.get("conversation_id") or "", self.st.staging)
            p = (r or {}).get("path") or (r or {}).get("output") or ""
            if p and Path(str(p)).exists() and Path(str(p)).stat().st_size:
                return Path(str(p))
        return None

    def check(self, path: Path) -> str:
        """大小/扩展名白名单校验，返回拒绝原因（空串=通过）。"""
        try:
            if path.stat().st_size > self.max_bytes:
                return (f"文件超过 {self.max_bytes // 1048576}MB 上限，"
                        "请压缩后再传。")
        except Exception:
            return "文件校验失败，请重发。"
        if self.allowed_ext and path.suffix.lower() not in self.allowed_ext:
            return f"不支持的文件类型 {path.suffix or '(无扩展名)'}。"
        return ""

    # ── 登记（store 台账） ──
    def register(self, path: Path, uid: str, sender: str,
                 task_id: object = 0) -> tuple[Path, bool]:
        """staging → shared/data 正式落盘 + store add + doc→md。
        返回 (最终路径, 是否已登记台账)。store 不可达时留在 staging（补登记队列）。"""
        uid = safe_sender(uid)
        topic = sanitize_topic(Path(path.name).stem.split("__")[-1])
        if not self.st.store_ping():
            staged = self.st.stage_file(path, uid, topic, kind="data",
                                        task=task_id)
            self.audit.log("store_unavailable", uid,
                           f"暂存待补登记: {(staged or path).name}")
            return (staged or path), False
        d = self.st.data_dir(uid)
        stem = self.st.data_stem(uid, topic, task_id=task_id)
        dst = self.st.next_version(d, stem, path.suffix or ".bin")
        try:
            shutil.move(str(path), str(dst))
        except Exception as e:
            self.audit.log("store_move_failed", uid, str(e)[:200])
            return path, False
        ok, out = self.st.store_add(dst, "data", sender=uid,
                                    task=task_id or 0, topic=topic,
                                    note=f"钉钉上传 by {sender}")
        if not ok:
            self.audit.log("store_add_failed", uid,
                           f"{dst.name}: {out[:150]}")
            return dst, False
        self.audit.log("file_registered", uid, str(dst), task_id=task_id)
        self._convert_doc(dst, uid, topic, task_id)
        return dst, True

    def _convert_doc(self, dst: Path, uid: str, topic: str,
                     task_id: object = 0):
        """可转格式 → 同目录 markdown（variant=dingdoc），refs 指回原件。"""
        if dst.suffix.lower() not in CONVERTIBLE:
            return
        md_stem = self.st.data_stem(uid, topic, task_id=task_id) + "-dingdoc"
        md = self.st.next_version(dst.parent, md_stem, ".md")
        tmp = self.st.staging / (md.name + ".tmp")
        try:
            p = self.dws.doc_to_markdown(str(dst), tmp)
            if p and Path(p).exists() and Path(p).stat().st_size:
                shutil.move(str(tmp), str(md))
                ok, _ = self.st.store_add(md, "data", sender=uid,
                                          task=task_id or 0, topic=topic,
                                          note="钉钉文档转markdown")
                if ok:
                    self.st.store_link(md, dst, rel="dingdoc")
        except Exception as e:
            self.audit.log("doc_convert_failed", uid, str(e)[:200])
        finally:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # ── 入口：L3/授权用户发文件 ──
    def receive(self, uid: str, sender: str, ev: dict) -> str:
        """文件消息 → 下载+校验+登记，返回回执文本（非文件消息返回空串）。"""
        fi = extract_file_from_event(ev)
        if not (fi["file_id"] or fi["media_id"]):
            return ""
        path = self.download(ev, fi)
        if not path:
            return "⚠️ 文件没接住，可能太大或已过期，麻烦重发一次～"
        bad = self.check(path)
        if bad:
            try:
                path.unlink()
            except Exception:
                pass
            return f"⚠️ {bad}"
        final, registered = self.register(path, uid, sender)
        name = Path(fi["file_name"]).name
        if registered:
            return (f"✅ 已收到《{name}》并登记台账（{final.parent.name}/ 下），"
                    "可直接就这份文件提问或交代任务。")
        return (f"✅ 已收到《{name}》，暂存待台账恢复后自动补登记"
                "（文件已安全落盘，不影响使用）。")

    # ── L3 隔离问答在 main._l3_qa（exec_local 硬隔离 + 流式卡片） ──
