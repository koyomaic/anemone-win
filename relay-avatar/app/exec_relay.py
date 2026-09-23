"""app/exec_relay.py —— relay 后端适配器（默认执行路径）。

约束表映射（policy.constraints → relay 语义）：
  super/L2/L1 → 正常任务 + 门禁交互（pending_approval/review 透传钉钉）
  L3          → 不适用（强制 local；local 未启用时礼貌拒绝，绝不降级放行）
  guest       → 不执行（引导申请授权）
升级不降级：任何后端遇到约束表不允许的操作，拒绝或升级为 relay 任务走门禁。
"""
from __future__ import annotations

import time


class ExecRelay:
    name = "relay"

    def __init__(self, cfg, relay, storage, audit):
        self.cfg = cfg
        self.relay = relay
        self.st = storage
        self.audit = audit

    def available(self) -> bool:
        return self.relay.configured

    def submit(self, desc: str, resume_from=None, uid: str = "",
               ingress: str = "dingtalk") -> dict:
        """提交任务。返回 relay 响应（含 task_id 或 _error）。"""
        r = self.relay.create_task(desc, resume_from=resume_from)
        if "_error" not in r:
            self.audit.log("task_submit", uid, f"#{r.get('task_id')}",
                           backend=self.name, ingress=ingress,
                           task_id=r.get("task_id"))
        return r

    def poll(self, tid) -> dict:
        return self.relay.get_task(tid)

    def approve(self, tid, decision: str, uid: str = "") -> dict:
        r = self.relay.approve(tid, decision)
        self.audit.log("gate_approve", uid, f"#{tid} {decision}",
                       backend=self.name, task_id=tid)
        return r

    def confirm(self, tid, outcome: str, uid: str = "") -> dict:
        r = self.relay.confirm(tid, outcome)
        self.audit.log("gate_confirm", uid, f"#{tid} {outcome}",
                       backend=self.name, task_id=tid)
        return r

    def cancel(self, tid, uid: str = "") -> dict:
        r = self.relay.cancel(tid)
        self.audit.log("task_cancel", uid, f"#{tid}",
                       backend=self.name, task_id=tid)
        return r

    def wait_terminal(self, tid, timeout_sec: int = 0,
                      interval: float = 3.0) -> dict:
        """轮询到终态（avatar-cli 用）。timeout_sec=0 不限时。"""
        deadline = time.time() + timeout_sec if timeout_sec else None
        while True:
            row = self.poll(tid)
            st = row.get("status")
            if st in ("done", "conflict", "failed", "cancelled",
                      "pending_approval", "review") or "_error" in row:
                return row
            if deadline and time.time() > deadline:
                return row
            time.sleep(interval)
