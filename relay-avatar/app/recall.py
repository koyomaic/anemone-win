"""app/recall.py —— 出站消息回执落档与撤回。

发送时把 openTaskId 记入 run/outbox.jsonl（零额外 dws 调用）；撤回时再
query-send-status 解析 openConversationId/openMessageId（该格式仅发送回执链
可得，messages-list 的 messageId 是另一种格式，recall/mget 均拒收——
2026-09-23 实测教训），随后 chat message recall。
供 avatar-cli recall 与守护进程内部复用。
"""
from __future__ import annotations

import json
import time


def open_task_id(resp: dict) -> str:
    """从 dws 发送响应提取 openTaskId（多层兜底）。"""
    if not isinstance(resp, dict):
        return ""
    return str(((resp.get("data") or {}).get("result") or {}).get("openTaskId")
               or (resp.get("result") or {}).get("openTaskId")
               or resp.get("openTaskId") or "")


def record(storage, kind: str, to: str, resp: dict, task_id=None,
           snippet: str = ""):
    """回执落档 run/outbox.jsonl（append-only；无 openTaskId 不落，如 dry-run）。"""
    otid = open_task_id(resp)
    if not otid:
        return
    rec = {"ts": time.time(), "type": kind, "to": str(to or "")[:64],
           "open_task_id": otid, "task_id": task_id,
           "snippet": str(snippet)[:60]}
    try:
        with open(storage.run / "outbox.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def load_outbox(storage, task_id=None, n: int = 1) -> list:
    """取最近 n 条回执（可按 relay task_id 过滤），新→旧。"""
    try:
        lines = (storage.run / "outbox.jsonl").read_text(
            encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for line in reversed(lines):
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if task_id is not None and str(rec.get("task_id")) != str(task_id):
            continue
        out.append(rec)
        if len(out) >= n:
            break
    return out


def resolve(dws, otid: str) -> tuple:
    """openTaskId → (openConversationId, openMessageId)；失败 ("","")。"""
    r = dws.run(["chat", "message", "query-send-status",
                 "--open-task-id", otid, "--format", "json"], timeout=20)
    res = ((r.get("data") or {}).get("result") or r.get("result") or r
           if isinstance(r, dict) else {})
    if not isinstance(res, dict):
        return "", ""
    return (str(res.get("openConversationId") or ""),
            str(res.get("openMessageId") or ""))


def recall_message(dws, cid: str, mid: str) -> dict:
    r = dws.run(["chat", "message", "recall",
                 "--conversation-id", cid, "--message-id", mid,
                 "--yes", "--format", "json"], timeout=20)
    ok = bool(r.get("success") or (r.get("data") or {}).get("success")
              or r.get("ok"))
    return {"ok": ok, "resp": str(r)[:200]}


def recall_recent(storage, dws, n: int = 1, task_id=None) -> list:
    """撤回最近 n 条出站消息（或指定 task 的出站消息）。逐条返回结果。"""
    results = []
    for rec in load_outbox(storage, task_id=task_id, n=n):
        cid, mid = resolve(dws, rec["open_task_id"])
        if not (cid and mid):
            results.append({**rec, "recalled": False,
                            "reason": "query-send-status 未解析出消息ID"})
            continue
        r = recall_message(dws, cid, mid)
        results.append({**rec, "recalled": r["ok"],
                        "reason": "" if r["ok"] else r["resp"]})
    return results


def gc_outbox(storage, keep: int = 500):
    """outbox 只留最近 keep 条（monitor 小时级 gc 调用）。"""
    p = storage.run / "outbox.jsonl"
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > keep:
            from .storage import atomic_write_text
            atomic_write_text(p, "\n".join(lines[-keep:]) + "\n")
    except Exception:
        pass
