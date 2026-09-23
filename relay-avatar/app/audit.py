"""app/audit.py —— append-only 审计（对标 relay-service/app/audit.py 语义）。

记录：授权/拒绝、门禁决定、监控指令、后端升级、漂移、限流命中、store 登记。
写 $STATE/audit/audit.log（一行一 JSON，只追加不改写）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path


class Audit:
    def __init__(self, state: Path):
        self.dir = state / "audit"
        self.file = self.dir / "audit.log"

    def log(self, action: str, actor: str = "", detail: str = "",
            backend: str = "", ingress: str = "", task_id: object = None):
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "action": action, "actor": str(actor),
                   "detail": str(detail)[:300]}
            if backend:
                rec["backend"] = backend
            if ingress:
                rec["ingress"] = ingress
            if task_id is not None:
                rec["task_id"] = task_id
            with open(self.file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
