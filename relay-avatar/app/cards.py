from __future__ import annotations

import json
import time


class CardRegistry:
    def __init__(self, storage, audit, cfg):
        self.st = storage
        self.audit = audit
        self.cfg = cfg
        self.file = storage.run / "card_active.json"
        self.orphan_file = storage.run / "card_orphans.jsonl"
        self._active = storage.load_json(self.file, {}) or {}
        self._dirty = False

    def save(self):
        if self._dirty:
            try:
                self.st.save_json(self.file, self._active)
                self._dirty = False
            except Exception:
                pass

    def _rec(self, biz: str, task_id=None, backend: str = "",
             target_sig: str = "", hint: str = "") -> dict:
        now = time.time()
        ids = []
        if task_id:
            ids.append(task_id)
        return {"biz": str(biz), "target_sig": str(target_sig or "")[:64],
                "task_ids": ids, "backend": str(backend or ""),
                "hint": str(hint or "")[:40], "state": "open",
                "created": now, "last_update": now, "last_flow": 1,
                "attempts": 0}

    def open(self, biz: str, target_sig: str = "", task_id=None,
             backend: str = "", hint: str = ""):
        if not biz:
            return
        rec = self._active.get(str(biz))
        if rec:
            rec["last_update"] = time.time()
            rec["state"] = "open"
            rec["backend"] = backend or rec.get("backend", "")
            rec["hint"] = hint or rec.get("hint", "")
            if task_id and task_id not in rec.get("task_ids", []):
                rec["task_ids"] = (rec.get("task_ids") or [])[-4:] + [task_id]
        else:
            self._active[str(biz)] = self._rec(biz, task_id, backend,
                                               target_sig, hint)
        self._dirty = True
        self.save()

    def touch(self, biz: str, flow_status=None, task_id=None):
        rec = self._active.get(str(biz or ""))
        if not rec:
            return
        rec["last_update"] = time.time()
        if flow_status is not None:
            rec["last_flow"] = int(flow_status)
        if task_id and task_id not in rec.get("task_ids", []):
            rec["task_ids"] = (rec.get("task_ids") or [])[-4:] + [task_id]
        self._dirty = True

    def close(self, biz: str, status: int = 3):
        rec = self._active.pop(str(biz or ""), None)
        if rec:
            rec["state"] = "closed"
            rec["closed"] = time.time()
            rec["last_flow"] = int(status)
            self._dirty = True
            self.save()
        return bool(rec)

    def orphan(self, biz: str, reason: str = "", task_id=None):
        if not biz:
            return
        rec = self._active.pop(str(biz), None) or self._rec(biz, task_id)
        rec.update({"state": "orphan", "reason": str(reason or "")[:180],
                    "orphaned": time.time()})
        if task_id and task_id not in rec.get("task_ids", []):
            rec["task_ids"] = (rec.get("task_ids") or [])[-4:] + [task_id]
        self._dirty = True
        self.save()
        try:
            with open(self.orphan_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        self.gc_orphans()

    def gc_orphans(self, keep: int = 500):
        try:
            lines = self.orphan_file.read_text(encoding="utf-8").splitlines()
            if len(lines) > keep:
                from .storage import atomic_write_text
                atomic_write_text(self.orphan_file,
                                  "\n".join(lines[-keep:]) + "\n")
        except Exception:
            pass

    def active(self) -> dict:
        return dict(self._active)

    def stale(self, now: float, ttl: float, skip=None, batch: int = 5) -> list:
        skip = set(skip or set())
        out = []
        for biz, rec in list(self._active.items()):
            if biz in skip:
                continue
            ts = float(rec.get("last_update") or rec.get("created") or 0)
            if now - ts < ttl:
                continue
            out.append((biz, dict(rec)))
            if len(out) >= batch:
                break
        return out
