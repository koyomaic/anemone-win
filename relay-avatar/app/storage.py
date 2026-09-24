"""app/storage.py —— 统一存储规则（两入口共用）+ store 台账集成。

规则（对齐 09-22 store 存储规范，见 workspace AGENTS.md「共享区约定」）：
- 业务文件一律进 store 台账体系（shared/ = 存储根）：
    用户上传原件 → shared/data/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<senderId>/
    命名 <YYYYMMDD>_<senderId>_T<taskId>_<topic>_v<N>.<ext>，无任务号先 T0，建任务后 store link
    附件转文本   → 同目录，variant 标 dingdoc，refs 指回原件
    出站报告     → shared/report/<域>/<YYYY-MM|YYYY-Www>/
    知识归档     → shared/knowledge/<tag域>/ + 同任务双写 jingboWiki + store add --wiki-tag
- avatar 私有目录（$STATE）只放运行态：state/rounds/audit/events/run，不进台账。
- 原子写（tmp+rename）；禁同名覆盖，改版 vN+1；正式目录禁留 .bak-*/.tmp。
- store 连接走最小权限：STORE_ENV 指向不存在路径屏蔽 relay.env，落到
    index/store-task.env 的每机私有角色（store_task_<machine-id 前 8 位>）；
    禁读 relay.env 与任何 /proc/<pid>/environ。
- DB 不可达：文件先落 run/staging 暂存并回执，恢复后补登记（不静默绕过）。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

BAD_CHAR = re.compile(r"[ #%&]")


def sanitize_topic(topic: str, limit: int = 40) -> str:
    """topic 可中文，禁空格（用-）与 # % &；路径段一律 ASCII 之外仅允许中文。"""
    t = re.sub(r"\s+", "-", str(topic or "").strip())
    t = BAD_CHAR.sub("", t)
    t = re.sub(r"[^\w一-鿿.\-]", "", t)
    return t[:limit].strip("-.") or "未命名"


def safe_sender(sender_id: str) -> str:
    """senderId 清洗（防路径穿越）：只留字母数字。"""
    return re.sub(r"\W", "", str(sender_id or "")) or "unknown"


def atomic_write_text(path: Path, text: str, chmod: int | None = None):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    if chmod is not None:
        os.chmod(tmp, chmod)
    os.replace(tmp, str(path))


def atomic_write_json(path: Path, obj, chmod: int | None = None):
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=1),
                      chmod=chmod)


class Storage:
    def __init__(self, cfg):
        self.cfg = cfg
        self.state: Path = cfg.state
        self.run: Path = self.state / "run"
        self.staging: Path = self.run / "staging"
        self.rounds: Path = self.state / "rounds"
        self.audit_dir: Path = self.state / "audit"
        for d in (self.run, self.staging, self.rounds, self.audit_dir):
            d.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.state, 0o700)
        except Exception:
            pass
        self.store_root: Path = cfg.store_root
        self._store_ok: bool | None = None   # 最近一次 ping 结果缓存

    # ── 运行态 ──
    def load_json(self, path: Path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default

    def save_json(self, path: Path, obj):
        atomic_write_json(path, obj)

    def events_file(self) -> Path:
        """原始入口事件按日轮转（保留 30 天）。"""
        return self.state / f"events-{time.strftime('%Y%m%d')}.ndjson"

    def append_event(self, line: str):
        try:
            with open(self.events_file(), "a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")
        except Exception:
            pass

    def gc_events(self, keep_days: int = 30):
        cutoff = time.time() - keep_days * 86400
        for p in self.state.glob("events-*.ndjson"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except Exception:
                pass
        legacy = self.state / "events.ndjson"
        if legacy.exists():
            try:
                if legacy.stat().st_mtime < cutoff:
                    legacy.unlink()
            except Exception:
                pass

    def archive_round(self, rid, kind: str, payload):
        """每轮归档 rounds/<id>.jsonl：入口/聚合消息/task_id/门禁/结果摘要。
        任务正文不重复落盘，relay PG 为真源。"""
        try:
            with open(self.rounds / f"{rid}.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps({"t": time.time(), "kind": kind,
                                    "data": payload}, ensure_ascii=False,
                                   default=str)[:8000] + "\n")
        except Exception:
            pass

    # ── store 台账（最小权限） ──
    def _store_env(self) -> dict:
        """store CLI 子进程环境：屏蔽 RELAY_DB/relay.env，强制落 index/store-task.env。"""
        e = {k: v for k, v in os.environ.items()
             if not k.startswith("RELAY_") and k != "STORE_DB"}
        if self.cfg.get("store", "least_privilege", True):
            e["STORE_ENV"] = str(self.run / ".no-relay-env")   # 不存在的路径
        e["STORE_ROOT"] = str(self.store_root)
        return e

    def _store(self, args: list, timeout: int = 60,
               extra_env: dict | None = None) -> tuple[int, str]:
        cli = str(self.cfg.get("store", "cli", "store"))
        env = self._store_env()
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})
        try:
            p = subprocess.run([cli, "--root", str(self.store_root)] + args,
                               capture_output=True, text=True, timeout=timeout,
                               env=env)
            return p.returncode, (p.stdout or p.stderr or "")[:400]
        except FileNotFoundError:
            return 127, "store CLI 不存在"
        except Exception as e:
            return 1, f"store 调用失败: {e}"

    def store_ping(self, use_cache: bool = True) -> bool:
        """0=就绪 1=缺表 2=DB不可达；缺表/不可达都视为暂不可登记。"""
        if use_cache and self._store_ok is not None and \
                time.time() - getattr(self, "_store_ping_ts", 0) < 300:
            return self._store_ok
        rc, _ = self._store(["ping"], timeout=20)
        self._store_ok = (rc == 0)
        self._store_ping_ts = time.time()
        return self._store_ok

    def data_dir(self, sender_id: str, when: dt.date | None = None) -> Path:
        d = when or dt.date.today()
        return (self.store_root / "data" / f"{d:%Y}" / f"{d:%Y-%m}" /
                f"{d:%Y-%m-%d}" / safe_sender(sender_id))

    def report_dir(self, domain: str, when: dt.date | None = None) -> Path:
        d = when or dt.date.today()
        return self.store_root / "report" / sanitize_topic(domain) / f"{d:%Y-%m}"

    def next_version(self, dirp: Path, stem: str, ext: str) -> Path:
        """禁同名覆盖：同 stem 已有 vN 则 vN+1。"""
        dirp.mkdir(parents=True, exist_ok=True)
        n = 1
        for p in dirp.glob(f"{stem}_v*{ext}"):
            m = re.search(r"_v(\d+)" + re.escape(ext) + "$", p.name)
            if m:
                n = max(n, int(m.group(1)) + 1)
        return dirp / f"{stem}_v{n}{ext}"

    def data_stem(self, sender_id: str, topic: str, task_id: object = 0,
                  when: dt.date | None = None) -> str:
        """data 文件名主干（不含 _vN.ext）：<YYYYMMDD>_<senderId>_T<taskId>_<topic>"""
        d = when or dt.date.today()
        tid = task_id if task_id else 0
        return (f"{d:%Y%m%d}_{safe_sender(sender_id)}_T{tid}_"
                f"{sanitize_topic(topic)}")

    def store_add(self, path: Path, kind: str, sender: str = "",
                  task: object = "", topic: str = "", wiki_tag: str = "",
                  note: str = "") -> tuple[bool, str]:
        args = ["add", str(path), "--kind", kind]
        if sender:
            args += ["--sender", str(sender)]
        if task not in ("", None):
            args += ["--task", str(task)]
        if topic:
            args += ["--topic", sanitize_topic(topic)]
        if wiki_tag:
            args += ["--wiki-tag", wiki_tag]
        if note:
            args += ["--note", note[:200]]
        rc, out = self._store(args)
        return rc == 0, out

    def store_link(self, src: Path, dst: Path, rel: str = "") -> bool:
        args = ["link", str(src), str(dst)]
        if rel:
            args += ["--rel", rel]
        rc, _ = self._store(args)
        return rc == 0

    # ── 暂存与补登记（DB 不可达兜底） ──
    def stage_file(self, src: Path, sender_id: str, topic: str,
                   kind: str = "data", task: object = 0) -> Path | None:
        """store 不可达时：文件移入 run/staging 并登记 pending，恢复后 retry_pending 补录。"""
        try:
            self.staging.mkdir(parents=True, exist_ok=True)
            ts = time.strftime("%Y%m%d-%H%M%S")
            dst = self.staging / f"{ts}_{safe_sender(sender_id)}_{src.name}"
            shutil.move(str(src), str(dst))
            pend = self.load_json(self.run / "pending_store.json", [])
            pend.append({"path": str(dst), "kind": kind,
                         "sender": safe_sender(sender_id),
                         "topic": sanitize_topic(topic), "task": str(task or 0),
                         "ts": time.time()})
            self.save_json(self.run / "pending_store.json", pend)
            return dst
        except Exception:
            return None

    def retry_pending(self) -> int:
        """store 恢复后补登记暂存文件：正式落盘 shared/data + store add。返回补录数。"""
        pend = self.load_json(self.run / "pending_store.json", [])
        if not pend or not self.store_ping(use_cache=False):
            return 0
        done = 0
        rest = []
        for rec in pend:
            src = Path(rec["path"])
            if not src.exists():
                done += 1
                continue
            try:
                when = dt.date.fromtimestamp(rec.get("ts", time.time()))
            except Exception:
                when = dt.date.today()
            d = self.data_dir(rec["sender"], when)
            stem = self.data_stem(rec["sender"], rec["topic"],
                                  task_id=0, when=when)
            dst = self.next_version(d, stem, src.suffix or ".bin")
            try:
                shutil.move(str(src), str(dst))
                ok, _ = self.store_add(dst, rec["kind"], sender=rec["sender"],
                                       task=rec.get("task") or 0,
                                       topic=rec["topic"],
                                       note="avatar 暂存补登记")
                if ok:
                    done += 1
                    continue
            except Exception:
                pass
            rest.append(rec)
        self.save_json(self.run / "pending_store.json", rest)
        return done
