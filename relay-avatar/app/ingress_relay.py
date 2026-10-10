"""app/ingress_relay.py —— relay API 客户端（设备鉴权，avatar-cli 同源）。

双入口之 B：web/CLI/他机/avatar-cli 直接走 relay API 提任务，relay 服务端
执行门禁/read_only/审计（策略核两侧引用同一份 policy.toml 的 tier 约束表）。
本模块是 avatar 侧与 avatar-cli 共用的签名客户端。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
import urllib.error
import urllib.request


class RelayClient:
    def __init__(self, env: dict):
        self.base = str(env.get("RELAY_BASE", "http://127.0.0.1:8787")).rstrip("/")
        self.dev = str(env.get("RELAY_DEVICE_ID", ""))
        self.secret = str(env.get("RELAY_DEVICE_SECRET", "")).encode()

    @property
    def configured(self) -> bool:
        return bool(self.dev and self.secret)

    def req(self, method: str, path: str, body=None, timeout: int = 15) -> dict:
        data = json.dumps(body).encode() if body is not None else b""
        ts = str(int(time.time()))
        nonce = secrets.token_hex(8)
        digest = hashlib.sha256(data).hexdigest()
        msg = f"{ts}\n{method}\n{path}\n{digest}\n{nonce}"
        sig = hmac.new(self.secret, msg.encode(), hashlib.sha256).hexdigest()
        r = urllib.request.Request(self.base + path, data=data or None,
                                   method=method, headers={
                                       "X-Device-Id": self.dev,
                                       "X-Timestamp": ts,
                                       "X-Nonce": nonce,
                                       "X-Signature": sig,
                                       "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return {"_error": e.code, "_detail": e.read().decode()[:300]}
        except Exception as e:
            return {"_error": "conn", "_detail": str(e)[:200]}

    def create_task(self, description: str, resume_from=None,
                    project: str = "relay-avatar", requester: str = "") -> dict:
        body = {"description": description, "priority": "normal",
                "project": project}
        if resume_from:
            body["resume_from"] = resume_from
        if requester:
            # 真实发起人。relay 侧 user 恒为机器人（全机队共用一个 token），
            # 不带这个字段就看不出任务是谁提的，冲突归因也会指向机器人自己。
            body["requester"] = requester
        return self.req("POST", "/tasks", body)

    def get_task(self, tid) -> dict:
        return self.req("GET", f"/tasks/{tid}")

    def approve(self, tid, decision: str) -> dict:
        return self.req("POST", f"/tasks/{tid}/approve", {"decision": decision})

    def confirm(self, tid, outcome: str) -> dict:
        return self.req("POST", f"/tasks/{tid}/confirm", {"outcome": outcome})

    def cancel(self, tid) -> dict:
        return self.req("POST", f"/tasks/{tid}/cancel")

    # ---- local 影子记录（只登记不派发，让 local 任务在任务列表可见）----

    def create_local_record(self, description: str, requester: str = "") -> dict:
        """建 local 影子记录。relay 不执行它，终态由 report_local_status 回报。"""
        body = {"description": description, "priority": "normal",
                "project": "avatar-local", "backend": "local"}
        if requester:
            body["requester"] = requester
        return self.req("POST", "/tasks", body)

    def report_local_status(self, tid, status: str, result: str = "",
                            error: str = "", tokens: int = 0,
                            cost: float = 0.0,
                            changed_files: list | None = None) -> dict:
        """回报 local 影子记录终态（幂等；relay 侧拒绝从终态再转）。"""
        return self.req("POST", f"/tasks/{tid}/local_status",
                        {"status": status, "result": result, "error": error,
                         "tokens": tokens, "cost": cost,
                         "changed_files": changed_files or []})

    # ---- 计划（定时任务）----

    def create_plan(self, name: str, on_calendar: str, action: dict) -> dict:
        return self.req("POST", "/plans",
                        {"name": name, "on_calendar": on_calendar,
                         "action": action, "enabled": True})

    def list_plans(self) -> dict:
        return self.req("GET", "/plans")

    def patch_plan(self, pid, fields: dict) -> dict:
        return self.req("PATCH", f"/plans/{pid}", fields)

    def delete_plan(self, pid) -> dict:
        return self.req("DELETE", f"/plans/{pid}")
