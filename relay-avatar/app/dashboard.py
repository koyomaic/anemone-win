"""app/dashboard.py —— 监控看板（零依赖 http.server，:8791，X-Token 鉴权）。

延续 87xx 段：8787 relay 本地 / 8788 TLS / 8790 claude-proxy / 8791 avatar。
比 fenshen（内网无鉴权）收紧：GET/POST 均需 token（env AVATAR_DASHBOARD_TOKEN，
X-Token 头或 ?token= 查询参数）。
- GET  /            单机卡片 HTML（30s 自刷新）+ 合并他机上报快照
- GET  /api/snapshots  JSON
- POST /report      他机 probe 快照汇入（snapshot-<host>.json）
主进程内 daemon 线程运行（marker 关=服务停=看板停，provision 只在启用时探活）。
"""
from __future__ import annotations

import html
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ICON = {"ok": "🟢", "warn": "🟠", "down": "🔴", "critical": "🔴",
        "stale": "🟠", "bad_credentials": "🔴", "stalled": "🔴"}
ZOMBIE_H = 6


class Dashboard:
    def __init__(self, cfg, storage):
        self.cfg = cfg
        self.mon = storage.run / "monitor"
        self.port = int(cfg.get("monitor", "dashboard_port", 8791))
        self.token = str(cfg.env.get("AVATAR_DASHBOARD_TOKEN", "") or "")

    def load_snaps(self) -> list:
        out = []
        for f in sorted(self.mon.glob("snapshot-*.json")):
            try:
                out.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        now = time.time()
        return [s for s in out if now - s.get("ts", 0) < ZOMBIE_H * 3600]

    def render(self) -> str:
        snaps = self.load_snaps()
        cards = ""
        for s in sorted(snaps, key=lambda x: x.get("host", "")):
            st = s.get("overall", "ok")
            rows = ""
            for svc in s.get("services", []):
                rows += (f'<div class="r"><span>{ICON.get(svc["status"], "⚪")}'
                         f'</span><b>{html.escape(str(svc.get("label", svc["name"])))}'
                         f'</b><i>{html.escape(str(svc.get("detail", "")))}</i></div>')
            iss = "".join(f'<div class="i">{"🔴" if i["level"] == "critical" else "🟠"} '
                          f'{html.escape(str(i.get("label", i["target"])))}：'
                          f'{html.escape(str(i["msg"]))}</div>'
                          for i in s.get("issues", []))
            r = s.get("resources", {})
            age = int((time.time() - s.get("ts", time.time())) / 60)
            cards += (f'<div class="c {st}"><h2>{html.escape(str(s.get("host", "?")))} '
                      f'{ICON.get(st, "🟢")} <small>{age}min 前</small></h2>'
                      f'<div class="res">磁盘 {r.get("disk_used_pct", "?")}% · '
                      f'内存 {r.get("mem_used_pct", "?")}%</div>'
                      f'{iss}{rows}</div>')
        return ("<!doctype html><meta charset=utf-8>"
                "<meta http-equiv=refresh content=30>"
                "<title>relay-avatar 看板</title><style>"
                "body{font-family:sans-serif;background:#f4f6f9;margin:0;padding:20px}"
                ".c{background:#fff;border-radius:12px;padding:14px 18px;margin:0 auto 14px;"
                "max-width:860px;border-left:5px solid #30a46c;box-shadow:0 1px 3px rgba(0,0,0,.06)}"
                ".c.warn{border-left-color:#f5a623}.c.critical{border-left-color:#e5484d}"
                "h2{margin:0 0 6px;font-size:17px}small{color:#9aa4b2;font-weight:400}"
                ".res{color:#6b7280;font-size:13px;margin-bottom:8px}"
                ".r{display:flex;gap:8px;padding:4px 0;border-top:1px solid #f0f1f4;font-size:14px}"
                ".r i{margin-left:auto;color:#9aa4b2;font-style:normal;font-family:monospace;font-size:12px}"
                ".i{padding:4px 0;font-size:14px}</style>"
                f"<h1 style='max-width:860px;margin:0 auto 16px'>🛰️ relay-avatar 看板 "
                f"<small style='font-size:13px;color:#9aa4b2'>:{self.port}</small></h1>"
                + (cards or "<p style='text-align:center;color:#9aa4b2'>暂无快照，等待采集…</p>"))

    def _handler(self):
        dash = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self) -> bool:
                if not dash.token:
                    return True
                got = self.headers.get("X-Token", "")
                if not got and "token=" in self.path:
                    got = self.path.split("token=", 1)[1].split("&")[0]
                return got == dash.token

            def _send(self, body: bytes, ctype: str, code: int = 200):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/health"):
                    return self._send(b'{"ok":true}', "application/json")
                if not self._auth():
                    return self._send(b'{"error":"bad token"}',
                                      "application/json", 403)
                if self.path.startswith("/api/snapshots"):
                    return self._send(
                        json.dumps(dash.load_snaps(),
                                   ensure_ascii=False).encode(),
                        "application/json; charset=utf-8")
                self._send(dash.render().encode(), "text/html; charset=utf-8")

            def do_POST(self):
                if not self._auth():
                    return self._send(b'{"error":"bad token"}',
                                      "application/json", 403)
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    body = json.loads(self.rfile.read(n).decode())
                except Exception:
                    return self._send(b'{"error":"bad body"}',
                                      "application/json", 400)
                if self.path.startswith("/report"):
                    host = re.sub(r"[^\w一-龥-]", "",
                                  str(body.get("host") or "unknown"))[:40]
                    dash.mon.mkdir(parents=True, exist_ok=True)
                    (dash.mon / f"snapshot-{host or 'unknown'}.json").write_text(
                        json.dumps(body, ensure_ascii=False, indent=1),
                        encoding="utf-8")
                    return self._send(b'{"ok":true}', "application/json")
                self._send(b'{"error":"not found"}', "application/json", 404)

        return H

    def serve_thread(self) -> threading.Thread:
        t = threading.Thread(target=self._serve, daemon=True)
        t.start()
        return t

    def _serve(self):
        try:
            srv = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler())
            print(f"[dashboard] :{self.port}", flush=True)
            srv.serve_forever()
        except Exception as e:
            print("[dashboard] 启动失败:", e, flush=True)
