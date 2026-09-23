"""app/monitor.py —— 心跳 + probe 快照 + 告警（吸收 fenshen 监控体系）。

- 进程级：heartbeat 30s 独立线程写 run/heartbeat；判活=进程在+心跳新鲜。
- 入站级：inbound_health.json（ingress 写，这里读入快照），测「连着但收不到」假死。
- 采集级：probe 快照（主服务/心跳/入站/配套 timer/磁盘内存/relay /health 版本）
  → run/monitor/snapshot-<host>.json；看板与告警共用。
- 告警：状态跳变才推（新异常报警/恢复报绿），同一持续异常最多每 6h 重提；
  critical → dws 直聊 markdown 发 alert_to（复用 relay-boot 预警通道样式）。
"""
from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

ICON = {"ok": "🟢", "warn": "🟠", "down": "🔴", "critical": "🔴",
        "stale": "🟠", "bad_credentials": "🔴", "stalled": "🔴"}


def _run(cmd: list, timeout: int = 10) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except Exception as e:
        return 1, str(e)[:120]


def _http_version(url: str, timeout: int = 5) -> tuple[bool, str]:
    try:
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urllib.request.urlopen(url, timeout=timeout, context=ctx) as r:
            d = json.loads(r.read())
        return bool(d.get("ok")), str(d.get("version", ""))
    except Exception:
        return False, ""


def _mem_pct() -> int:
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.strip().split()[0])
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", 0)
        return int((total - avail) * 100 / total) if total else -1
    except Exception:
        return -1


class Monitor:
    def __init__(self, cfg, storage, dws, audit):
        self.cfg = cfg
        self.st = storage
        self.dws = dws
        self.audit = audit
        self.mon_dir = storage.run / "monitor"
        self.mon_dir.mkdir(parents=True, exist_ok=True)
        self.host = socket.gethostname()
        self.stop = threading.Event()

    # ── 心跳 ──
    def heartbeat_loop(self):
        hb = self.st.run / "heartbeat"
        sec = int(self.cfg.get("monitor", "heartbeat_sec", 30))
        while not self.stop.is_set():
            try:
                hb.write_text(str(int(time.time())), encoding="utf-8")
            except Exception:
                pass
            self.stop.wait(sec)

    # ── probe 快照 ──
    def build_snapshot(self) -> dict:
        svcs, issues = [], []

        def add(name, label, status, detail):
            svcs.append({"name": name, "label": label, "status": status,
                         "detail": str(detail)[:120]})

        # relay 主服务 + /health 版本
        rc, _ = _run(["systemctl", "is-active", "--quiet", "relay"])
        ok, ver = False, ""
        for u in ("https://127.0.0.1:8788/health",
                  "http://127.0.0.1:8787/health"):
            ok, ver = _http_version(u)
            if ok:
                break
        if rc == 0 and ok:
            add("relay", "relay 主服务", "ok", f"healthy {ver[:12]}")
        else:
            add("relay", "relay 主服务", "down",
                f"active={rc == 0} health={ok}")
            issues.append({"target": "relay", "label": "relay 主服务",
                           "level": "critical",
                           "msg": f"relay 不健康（active={rc == 0} health={ok}）"})

        # avatar 心跳新鲜度
        try:
            age = time.time() - float(
                (self.st.run / "heartbeat").read_text().strip())
        except Exception:
            age = -1
        if 0 <= age < 120:
            add("avatar-heartbeat", "avatar 心跳", "ok", f"{int(age)}s 前")
        else:
            add("avatar-heartbeat", "avatar 心跳", "down", f"age={int(age)}")
            issues.append({"target": "avatar-heartbeat",
                           "label": "avatar 心跳", "level": "critical",
                           "msg": f"心跳不新鲜（age={int(age)}s）"})

        # 入站健康
        try:
            ih = json.loads(
                (self.st.run / "inbound_health.json").read_text("utf-8"))
        except Exception:
            ih = {}
        stt = ih.get("state", "unknown")
        iage = int(time.time() - ih.get("ts", 0)) if ih else -1
        if stt == "ok":
            add("inbound", "钉钉入站", "ok", f"{ih.get('detail', '')[:40]}")
        elif stt in ("bad_credentials", "stalled"):
            add("inbound", "钉钉入站", stt, ih.get("detail", "")[:60])
            issues.append({"target": "inbound", "label": "钉钉入站",
                           "level": "critical",
                           "msg": f"{stt}: {ih.get('detail', '')[:100]}"})
        else:
            add("inbound", "钉钉入站", "warn", "无健康数据（未启用？）")

        # 配套 timer
        for t in ("relay-boot.timer", "relay-backup.timer",
                  "dws-refresh.timer"):
            rc, _ = _run(["systemctl", "is-active", "--quiet", t])
            add(t, t, "ok" if rc == 0 else "down",
                "active" if rc == 0 else "inactive")
            if rc != 0:
                issues.append({"target": t, "label": t, "level": "warn",
                               "msg": f"{t} 未激活"})

        # 资源
        try:
            du = shutil.disk_usage(str(self.cfg.store_root))
            disk = int(du.used * 100 / du.total)
        except Exception:
            disk = -1
        mem = _mem_pct()
        if disk >= 90:
            issues.append({"target": "disk", "label": "磁盘",
                           "level": "critical", "msg": f"磁盘 {disk}%"})
        elif disk >= 80:
            issues.append({"target": "disk", "label": "磁盘",
                           "level": "warn", "msg": f"磁盘 {disk}%"})
        if mem >= 90:
            issues.append({"target": "mem", "label": "内存",
                           "level": "critical", "msg": f"内存 {mem}%"})
        elif mem >= 85:
            issues.append({"target": "mem", "label": "内存",
                           "level": "warn", "msg": f"内存 {mem}%"})

        overall = ("critical" if any(i["level"] == "critical" for i in issues)
                   else "warn" if issues else "ok")
        return {"host": self.host, "host_cn": self.host,
                "hostname": self.host, "ts": int(time.time()),
                "ts_human": time.strftime("%F %T"),
                "services": svcs, "issues": issues, "overall": overall,
                "resources": {"disk_used_pct": disk, "mem_used_pct": mem}}

    def persist(self, snap: dict) -> Path:
        p = self.mon_dir / f"snapshot-{self.host}.json"
        try:
            p.write_text(json.dumps(snap, ensure_ascii=False, indent=1),
                         encoding="utf-8")
        except Exception:
            pass
        return p

    # ── 告警（状态跳变才推） ──
    def alarm(self):
        snap = self.build_snapshot()
        self.persist(snap)
        sf = self.mon_dir / "notify_state.json"
        try:
            st = json.loads(sf.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        prev = st.get("active_issues", {})
        now = time.time()
        cur = {f'{i["target"]}::{i["msg"][:20]}': i for i in snap["issues"]}
        new = set(cur) - set(prev)
        gone = set(prev) - set(cur)
        repeat_h = int(self.cfg.get("monitor", "alert_repeat_hours", 6))
        repeat = [k for k in (set(cur) & set(prev))
                  if now - prev.get(k, 0) > repeat_h * 3600]
        to = str(self.cfg.get("monitor", "alert_to", "") or "")
        if to and (new or gone or repeat):
            target = {"user_id": to}
            for k in list(new) + repeat:
                i = cur[k]
                if i["level"] != "critical":
                    continue
                self.dws.send_text(
                    target,
                    f"# 🔴 relay-avatar 告警（{self.host}）\n"
                    f"**{i.get('label', i['target'])}**：{i['msg']}\n\n"
                    f"> {snap['ts_human']}{'（持续未恢复）' if k in repeat else ''}")
                self.audit.log("alert_sent", to, i["msg"][:120])
            for k in gone:
                lbl = st.get("labels", {}).get(k, k.split("::")[0])
                self.dws.send_text(
                    target,
                    f"# 🟢 已恢复（{self.host}）\n✅ {lbl}\n\n> {snap['ts_human']}")
        active = {}
        labels = st.get("labels", {})
        for k in cur:
            active[k] = now if (k in new or k in repeat) else prev.get(k, now)
            labels[k] = cur[k].get("label", cur[k]["target"])
        try:
            sf.write_text(json.dumps({"active_issues": active,
                                      "labels": labels},
                                     ensure_ascii=False, indent=1),
                          encoding="utf-8")
        except Exception:
            pass
        return snap

    def probe_loop(self):
        interval = int(self.cfg.get("monitor", "probe_interval_sec", 300))
        last_gc = 0.0
        while not self.stop.is_set():
            try:
                self.alarm()
                if time.time() - last_gc > 3600:
                    last_gc = time.time()
                    self.st.gc_events(30)
                    self.st.retry_pending()
            except Exception as e:
                print("probe error:", e, flush=True)
            self.stop.wait(interval)
