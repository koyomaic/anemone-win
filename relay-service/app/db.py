from __future__ import annotations

import json
import sqlite3
import threading
import time


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  token TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices(
  device_id TEXT PRIMARY KEY,
  user_name TEXT NOT NULL,
  device_name TEXT NOT NULL DEFAULT '',
  secret TEXT NOT NULL,
  created_at REAL NOT NULL,
  last_seen REAL,
  last_ip TEXT NOT NULL DEFAULT '',
  revoked_at REAL
);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  "user" TEXT NOT NULL,
  description TEXT NOT NULL,
  project TEXT NOT NULL DEFAULT '',
  targets TEXT NOT NULL DEFAULT '[]',
  priority TEXT NOT NULL DEFAULT 'normal',
  status TEXT NOT NULL DEFAULT 'queued',
  session_id TEXT NOT NULL DEFAULT '',
  engine TEXT NOT NULL DEFAULT '',
  workdir TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  changed_files TEXT NOT NULL DEFAULT '[]',
  conflicts TEXT NOT NULL DEFAULT '[]',
  tokens INTEGER NOT NULL DEFAULT 0,
  cost REAL NOT NULL DEFAULT 0,
  resume_from INTEGER,
  blocked_cmd TEXT NOT NULL DEFAULT '',
  resume_hint TEXT NOT NULL DEFAULT '',
  read_only INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL,
  started_at REAL,
  finished_at REAL
);
CREATE TABLE IF NOT EXISTS plans(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  owner_name TEXT NOT NULL,
  on_calendar TEXT NOT NULL,
  action TEXT NOT NULL DEFAULT '{}',
  enabled INTEGER NOT NULL DEFAULT 1,
  next_run REAL,
  last_run REAL,
  last_task_id INTEGER,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_plans_due ON plans(enabled, next_run);
"""

# PostgreSQL 方言（RELAY_DB 为 postgresql:// URL 时使用）；"user" 是 PG 保留字必须加引号
PG_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users(
      token TEXT PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      created_at DOUBLE PRECISION NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS devices(
      device_id TEXT PRIMARY KEY,
      user_name TEXT NOT NULL,
      device_name TEXT NOT NULL DEFAULT '',
      secret TEXT NOT NULL,
      created_at DOUBLE PRECISION NOT NULL,
      last_seen DOUBLE PRECISION,
      last_ip TEXT NOT NULL DEFAULT '',
      revoked_at DOUBLE PRECISION
    )""",
    """CREATE TABLE IF NOT EXISTS tasks(
      id BIGSERIAL PRIMARY KEY,
      "user" TEXT NOT NULL,
      description TEXT NOT NULL,
      project TEXT NOT NULL DEFAULT '',
      targets TEXT NOT NULL DEFAULT '[]',
      priority TEXT NOT NULL DEFAULT 'normal',
      status TEXT NOT NULL DEFAULT 'queued',
      session_id TEXT NOT NULL DEFAULT '',
      engine TEXT NOT NULL DEFAULT '',
      workdir TEXT NOT NULL DEFAULT '',
      result TEXT NOT NULL DEFAULT '',
      error TEXT NOT NULL DEFAULT '',
      changed_files TEXT NOT NULL DEFAULT '[]',
      conflicts TEXT NOT NULL DEFAULT '[]',
      tokens BIGINT NOT NULL DEFAULT 0,
      cost DOUBLE PRECISION NOT NULL DEFAULT 0,
      resume_from BIGINT,
      blocked_cmd TEXT NOT NULL DEFAULT '',
      resume_hint TEXT NOT NULL DEFAULT '',
      read_only BOOLEAN NOT NULL DEFAULT FALSE,
      created_at DOUBLE PRECISION NOT NULL,
      started_at DOUBLE PRECISION,
      finished_at DOUBLE PRECISION
    )""",
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at)",
    """CREATE TABLE IF NOT EXISTS plans(
      id BIGSERIAL PRIMARY KEY,
      name TEXT NOT NULL UNIQUE,
      owner_name TEXT NOT NULL,
      on_calendar TEXT NOT NULL,
      action TEXT NOT NULL DEFAULT '{}',
      enabled BOOLEAN NOT NULL DEFAULT TRUE,
      next_run DOUBLE PRECISION,
      last_run DOUBLE PRECISION,
      last_task_id BIGINT,
      created_at DOUBLE PRECISION NOT NULL,
      updated_at DOUBLE PRECISION NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_plans_due ON plans(enabled, next_run)",
]

TASK_FIELDS = {
    "user", "description", "project", "targets", "priority", "status",
    "session_id", "engine", "workdir", "result", "error", "changed_files",
    "conflicts", "tokens", "cost", "resume_from", "blocked_cmd", "resume_hint", "read_only",
    "started_at", "finished_at",
}

# plans 可更新字段白名单（update_plan 用）；owner_name/created_at 不可改。
PLAN_FIELDS = {
    "name", "on_calendar", "action", "enabled",
    "next_run", "last_run", "last_task_id",
}


def is_pg_url(target: str) -> bool:
    return target.startswith(("postgresql://", "postgres://"))


def _row_to_task(row) -> dict:
    t = dict(row)
    for k in ("targets", "changed_files", "conflicts"):
        try:
            t[k] = json.loads(t[k])
        except (TypeError, ValueError):
            t[k] = []
    t["read_only"] = bool(t.get("read_only"))
    return t


def _row_to_plan(row) -> dict:
    p = dict(row)
    try:
        p["action"] = json.loads(p["action"])
    except (TypeError, ValueError):
        p["action"] = {}
    p["enabled"] = bool(p.get("enabled"))
    return p


class DB:
    """存储后端。构造参数既可是 SQLite 文件路径，也可是 postgresql:// URL（自动识别）。

    PG 后端：psycopg2 + RealDictCursor，单连接 + 线程锁（与 SQLite 后端同并发模型），
    断线（OperationalError/InterfaceError）自动重连并重试一次。
    """

    def __init__(self, path: str):
        self._lock = threading.Lock()
        self._pg = is_pg_url(path)
        self._conn = None
        if self._pg:
            import psycopg2
            import psycopg2.extras
            self._psycopg2 = psycopg2
            self._pg_extras = psycopg2.extras
            self._pg_url = path
            self._conn = self._pg_connect()
            self._pg_init_schema()
        else:
            self._conn = sqlite3.connect(path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            with self._lock:
                self._conn.executescript(SCHEMA)
                cols = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
                for name, decl in (("blocked_cmd", "TEXT NOT NULL DEFAULT ''"),
                                   ("resume_hint", "TEXT NOT NULL DEFAULT ''"),
                                   ("read_only", "INTEGER NOT NULL DEFAULT 0"),
                                   ("engine", "TEXT NOT NULL DEFAULT ''")):
                    if name not in cols:
                        self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
                ucols = {r[1] for r in self._conn.execute("PRAGMA table_info(users)")}
                if "consumed_at" not in ucols:  # token 核销时刻（一次性激活码语义）
                    self._conn.execute("ALTER TABLE users ADD COLUMN consumed_at REAL")
                self._conn.commit()

    # ---- 后端内部 ----

    @property
    def backend(self) -> str:
        """当前存储后端类型：postgresql / sqlite（/health 暴露，供人与 agent 查证）。"""
        return "postgresql" if self._pg else "sqlite"

    def _pg_connect(self):
        return self._psycopg2.connect(self._pg_url, connect_timeout=10,
                                      cursor_factory=self._pg_extras.RealDictCursor)

    def _pg_init_schema(self) -> None:
        for ddl in PG_SCHEMA:
            self._run(ddl)
        rows, _ = self._run("SELECT column_name FROM information_schema.columns"
                            " WHERE table_schema='public' AND table_name='tasks'")
        cols = {r["column_name"] for r in rows}
        for name, decl in (("blocked_cmd", "TEXT NOT NULL DEFAULT ''"),
                           ("resume_hint", "TEXT NOT NULL DEFAULT ''"),
                           ("read_only", "BOOLEAN NOT NULL DEFAULT FALSE"),
                           ("engine", "TEXT NOT NULL DEFAULT ''")):
            if name not in cols:
                self._run(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
        rows, _ = self._run("SELECT column_name FROM information_schema.columns"
                            " WHERE table_schema='public' AND table_name='users'")
        ucols = {r["column_name"] for r in rows}
        if "consumed_at" not in ucols:  # token 核销时刻（一次性激活码语义）
            self._run("ALTER TABLE users ADD COLUMN consumed_at DOUBLE PRECISION")

    def _pg_retriable(self, exc: Exception) -> bool:
        return isinstance(exc, (self._psycopg2.OperationalError,
                                self._psycopg2.InterfaceError))

    def _pg_reset(self) -> None:  # 调用方须持锁
        try:
            if self._conn is not None and not self._conn.closed:
                self._conn.close()
        except Exception:
            pass
        self._conn = None

    def _run(self, sql: str, args: tuple = ()):
        """执行 SQL，返回 (rows, rowcount)。PG 后端 ? 占位符自动转 %s，断线重连重试一次。"""
        if self._pg:
            sql = sql.replace("?", "%s")
            fetch = sql.lstrip().upper().startswith("SELECT") or "RETURNING" in sql.upper()
        else:
            fetch = True
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._pg:
                        if self._conn is None or self._conn.closed:
                            self._conn = self._pg_connect()
                        cur = self._conn.cursor()
                        cur.execute(sql, args)
                    else:
                        cur = self._conn.execute(sql, args)
                    rows = cur.fetchall() if fetch else []
                    self._conn.commit()
                    return rows, cur.rowcount
                except Exception as e:
                    if not (self._pg and attempt == 1 and self._pg_retriable(e)):
                        raise
                    self._pg_reset()
        raise RuntimeError("unreachable")

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def _q(self, sql: str, args: tuple = (), one: bool = False):
        rows, _ = self._run(sql, args)
        return (rows[0] if rows else None) if one else rows

    # ---- users ----

    def create_user(self, name: str, token: str) -> None:
        self._q("INSERT INTO users(token, name, created_at) VALUES(?, ?, ?)",
                (token, name, time.time()))

    def user_by_token(self, token: str) -> dict:
        row = self._q("SELECT * FROM users WHERE token = ?", (token,), one=True)
        return dict(row) if row else {}

    def user_by_name(self, name: str) -> dict:
        row = self._q("SELECT * FROM users WHERE name = ?", (name,), one=True)
        return dict(row) if row else {}

    def delete_user(self, name: str) -> None:
        self._q("DELETE FROM devices WHERE user_name = ?", (name,))
        self._q("DELETE FROM users WHERE name = ?", (name,))

    def list_users(self) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT token, name, created_at, consumed_at FROM users ORDER BY name")]

    def consume_token(self, token: str) -> bool:
        """原子核销 token（一次性激活码语义）。返回是否由本次调用核销。"""
        _, rowcount = self._run(
            "UPDATE users SET consumed_at=? WHERE token=? AND consumed_at IS NULL",
            (time.time(), token))
        return rowcount > 0

    def reissue_token(self, name: str, token: str) -> None:
        """给既有用户换发新 token（旧 token 即刻失效；已激活设备不受影响）。"""
        self._q("UPDATE users SET token=?, consumed_at=NULL, created_at=? WHERE name=?",
                (token, time.time(), name))

    # ---- devices（token 激活后绑定的设备凭证） ----

    def device_upsert(self, device_id: str, user_name: str, device_name: str,
                      secret: str, ip: str) -> None:
        now = time.time()
        _, rowcount = self._run(
            "UPDATE devices SET user_name=?, device_name=?, secret=?, last_ip=?,"
            " last_seen=?, revoked_at=NULL WHERE device_id=?",
            (user_name, device_name, secret, ip, now, device_id))
        if rowcount == 0:
            self._q("INSERT INTO devices(device_id, user_name, device_name, secret,"
                    " created_at, last_seen, last_ip) VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (device_id, user_name, device_name, secret, now, now, ip))

    def device_get(self, device_id: str) -> dict:
        row = self._q("SELECT * FROM devices WHERE device_id = ?", (device_id,), one=True)
        return dict(row) if row else {}

    def devices_by_user(self, name: str) -> list[dict]:
        return [dict(r) for r in self._q(
            "SELECT device_id, user_name, device_name, created_at, last_seen, last_ip,"
            " revoked_at FROM devices WHERE user_name = ? ORDER BY created_at", (name,))]

    def device_counts(self) -> dict[str, int]:
        """各用户活跃（未吊销）设备数，监控页展示用。"""
        return {r["user_name"]: r["n"] for r in self._q(
            "SELECT user_name, COUNT(*) AS n FROM devices"
            " WHERE revoked_at IS NULL GROUP BY user_name")}

    def revoke_device(self, name: str, device_id: str) -> bool:
        _, rowcount = self._run(
            "UPDATE devices SET revoked_at=? WHERE device_id=? AND user_name=?"
            " AND revoked_at IS NULL", (time.time(), device_id, name))
        return rowcount > 0

    def touch_device(self, device_id: str, ip: str) -> None:
        self._q("UPDATE devices SET last_seen=?, last_ip=? WHERE device_id=?",
                (time.time(), ip, device_id))

    # ---- tasks ----

    def create_task(self, *, user: str, description: str, project: str = "",
                    targets: list[str] | None = None, priority: str = "normal",
                    resume_from: int | None = None, read_only: bool = False,
                    status: str = "queued", engine: str = "",
                    started_at: float | None = None) -> int:
        """建任务行。

        status/engine/started_at 供 local 影子记录用：avatar 的 local 通路在本机
        跑 claude，relay 不执行它，但要让它在任务列表里**可见**（否则 local 任务
        无 task_id、看板与 /tasks 全查不到，出事故只能翻 journal+audit+state.json
        三处对账）。影子记录直接以 status='running'、engine='local' 落库，
        **绝不进调度器**，故不占 max_concurrent 额度（/health 的 active 取
        sched.active，是调度器进程内计数，非 DB 查询）。
        """
        args = (user, description, project, json.dumps(targets or [], ensure_ascii=False),
                priority, resume_from, read_only, status, engine, started_at,
                time.time())
        sql = ('INSERT INTO tasks("user", description, project, targets, priority,'
               ' resume_from, read_only, status, engine, started_at, created_at)'
               " VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
        if self._pg:
            row = self._q(sql + " RETURNING id", args, one=True)
            return row["id"]
        self._q(sql, args)
        rows, _ = self._run("SELECT last_insert_rowid() AS id")
        return rows[0]["id"]

    def task(self, task_id: int) -> dict | None:
        row = self._q("SELECT * FROM tasks WHERE id = ?", (task_id,), one=True)
        return _row_to_task(row) if row else None

    def set(self, task_id: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in TASK_FIELDS}
        if not fields:
            return
        if "targets" in fields:
            fields["targets"] = json.dumps(fields["targets"], ensure_ascii=False)
        if "changed_files" in fields and isinstance(fields["changed_files"], list):
            fields["changed_files"] = json.dumps(fields["changed_files"], ensure_ascii=False)
        if "conflicts" in fields and isinstance(fields["conflicts"], list):
            fields["conflicts"] = json.dumps(fields["conflicts"], ensure_ascii=False)
        sql = "UPDATE tasks SET {} WHERE id = ?".format(
            ", ".join(f'"{k}" = ?' for k in fields))
        self._q(sql, (*fields.values(), task_id))

    def list_tasks(self, limit: int = 50, status: str | None = None) -> list[dict]:
        if status:
            rows = self._q("SELECT * FROM tasks WHERE status = ? ORDER BY id DESC LIMIT ?",
                           (status, limit))
        else:
            rows = self._q("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
        return [_row_to_task(r) for r in rows]

    def active_tasks(self) -> list[dict]:
        """在途任务（冲突检测用）。

        排除 engine='local' 的影子记录：local 通路只写 local_write_dirs 作用域内的
        文本件、且不做 baseline 快照，其 changed_files 只能由 agent 自报（不可信），
        拿它参与 targets 重叠判定只会产生假信号。
        """
        rows = self._q("SELECT * FROM tasks WHERE status IN"
                       " ('queued', 'running', 'pending_approval')"
                       " AND engine != 'local' ORDER BY id")
        return [_row_to_task(r) for r in rows]

    def tasks_with_file(self, rel_file: str, exclude: int,
                        since: float | None = None) -> list[dict]:
        """改动过同一文件的其它任务（冲突候选）。

        since 给定时只保留该时刻之后仍有活动的任务：时间窗外的先后写入属正常演进
        （后者基于前者的产出继续改），不算冲突。
        """
        rows = self._q("SELECT * FROM tasks WHERE id != ? AND changed_files != '[]'", (exclude,))
        out = []
        for r in rows:
            t = _row_to_task(r)
            if rel_file not in t["changed_files"]:
                continue
            if since is not None:
                last = t.get("finished_at") or t.get("started_at") or t.get("created_at") or 0.0
                if last < since:
                    continue
            out.append(t)
        return out

    def status_counts(self) -> dict[str, int]:
        rows = self._q("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    def period_stats(self, since: float) -> dict:
        rows = self._q("SELECT status, COUNT(*) AS c FROM tasks WHERE created_at >= ? GROUP BY status",
                       (since,))
        counts = {r["status"]: r["c"] for r in rows}
        total = sum(counts.values())
        return {"submitted": total,
                "done_rate": round(counts.get("done", 0) / total, 3) if total else 0.0}

    def recent_task_ids(self, minutes: int = 60, limit: int = 20) -> list[int]:
        rows = self._q("SELECT id FROM tasks WHERE created_at >= ? ORDER BY id DESC LIMIT ?",
                       (time.time() - minutes * 60, limit))
        return [r["id"] for r in rows]

    def stale_tasks(self, states: list[str], cutoff: float) -> list[dict]:
        """created_at 早于 cutoff 且仍处于指定状态的任务（用于超时清扫）。"""
        ph = ",".join("?" * len(states))
        rows = self._q(f"SELECT * FROM tasks WHERE status IN ({ph}) AND created_at < ? ORDER BY created_at",
                       (*states, cutoff))
        return [_row_to_task(r) for r in rows]

    def stale_local_tasks(self, cutoff: float) -> list[dict]:
        """engine='local' 且仍 running、created_at 早于 cutoff 的影子记录。

        avatar 崩溃或被 kill 时来不及回报终态，影子记录会永远停在 running。
        这是唯一能发现它的地方——local 任务没有 workdir、没有 baseline、
        relay 也不执行它，sweep 不兜就是一条永久 running 的僵尸行。
        """
        rows = self._q("SELECT * FROM tasks WHERE engine = 'local'"
                       " AND status = 'running' AND created_at < ?"
                       " ORDER BY created_at", (cutoff,))
        return [_row_to_task(r) for r in rows]

    def mark_stale_done(self, task_id: int, marker: str) -> bool:
        """原子地把仍处于 review/pending_approval 的任务置为 done。返回是否更新。"""
        now = time.time()
        _, rowcount = self._run(
            "UPDATE tasks SET status='done', error=?, finished_at=COALESCE(finished_at, ?)"
            " WHERE id=? AND status IN ('review','pending_approval')",
            (marker, now, task_id))
        return rowcount > 0

    def mark_approval_timeout(self, task_id: int, marker: str) -> bool:
        """原子地把仍处于 pending_approval 的任务置为 **failed**。返回是否更新。

        与 mark_stale_done 分开：待审批＝活干到一半被门禁拦住，超时置 done 等于把
        没办完的任务标成办完了（#299 若无人手工拒批，24h 后就会被标 done）。
        WHERE 带 status 守卫，避免与并发的人工 approve/deny 竞态双写。
        """
        now = time.time()
        _, rowcount = self._run(
            "UPDATE tasks SET status='failed', error=?, finished_at=COALESCE(finished_at, ?)"
            " WHERE id=? AND status='pending_approval'",
            (marker, now, task_id))
        return rowcount > 0

    # ---- plans（定时计划：声明存库，root 侧 relay-plan.timer 扫库触发） ----

    def create_plan(self, *, name: str, owner_name: str, on_calendar: str,
                    action: dict, enabled: bool = True,
                    next_run: float | None = None) -> int:
        now = time.time()
        args = (name, owner_name, on_calendar,
                json.dumps(action, ensure_ascii=False), enabled, next_run, now, now)
        sql = ("INSERT INTO plans(name, owner_name, on_calendar, action, enabled,"
               " next_run, created_at, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?)")
        if self._pg:
            row = self._q(sql + " RETURNING id", args, one=True)
            return row["id"]
        self._q(sql, args)
        rows, _ = self._run("SELECT last_insert_rowid() AS id")
        return rows[0]["id"]

    def plan(self, plan_id: int) -> dict | None:
        row = self._q("SELECT * FROM plans WHERE id = ?", (plan_id,), one=True)
        return _row_to_plan(row) if row else None

    def plan_by_name(self, name: str) -> dict | None:
        row = self._q("SELECT * FROM plans WHERE name = ?", (name,), one=True)
        return _row_to_plan(row) if row else None

    def list_plans(self, limit: int = 500) -> list[dict]:
        rows = self._q("SELECT * FROM plans ORDER BY id DESC LIMIT ?", (limit,))
        return [_row_to_plan(r) for r in rows]

    def update_plan(self, plan_id: int, **fields) -> None:
        fields = {k: v for k, v in fields.items() if k in PLAN_FIELDS}
        if not fields:
            return
        if "action" in fields and not isinstance(fields["action"], str):
            fields["action"] = json.dumps(fields["action"], ensure_ascii=False)
        fields["updated_at"] = time.time()
        sql = "UPDATE plans SET {} WHERE id = ?".format(
            ", ".join(f"{k} = ?" for k in fields))
        self._q(sql, (*fields.values(), plan_id))

    def delete_plan(self, plan_id: int) -> bool:
        _, rowcount = self._run("DELETE FROM plans WHERE id = ?", (plan_id,))
        return rowcount > 0

    def due_plans(self, now: float) -> list[dict]:
        """到点（enabled 且 next_run<=now）的计划，供 tick 触发。"""
        rows = self._q("SELECT * FROM plans WHERE enabled AND next_run IS NOT NULL"
                       " AND next_run <= ? ORDER BY next_run", (now,))
        return [_row_to_plan(r) for r in rows]

    def claim_plan(self, plan_id: int, now: float, new_next_run: float) -> bool:
        """原子认领：仅当计划仍到点时推进 next_run/last_run，防 tick 重入双发。

        返回是否由本次调用认领成功（rowcount>0）。
        """
        _, rowcount = self._run(
            "UPDATE plans SET last_run = ?, next_run = ?, updated_at = ?"
            " WHERE id = ? AND enabled AND next_run IS NOT NULL AND next_run <= ?",
            (now, new_next_run, now, plan_id, now))
        return rowcount > 0

    def report(self, days: int = 7) -> dict:
        rows = [dict(r) for r in self._q(
            'SELECT "user", status, tokens, created_at, blocked_cmd FROM tasks WHERE created_at >= ?',
            (time.time() - days * 86400,))]
        by_day: dict[str, dict[str, int]] = {}
        by_user: dict[str, dict] = {}
        conflicts = blocked = 0
        for r in rows:
            day = time.strftime("%Y-%m-%d", time.localtime(r["created_at"]))
            d = by_day.setdefault(day, {"submitted": 0, "done": 0, "failed": 0,
                                        "conflict": 0, "cancelled": 0, "pending_approval": 0})
            d["submitted"] += 1
            if r["status"] in d:
                d[r["status"]] += 1
            u = by_user.setdefault(r["user"],
                                   {"user": r["user"], "tasks": 0, "done": 0,
                                    "failed": 0, "tokens": 0, "last_active": 0.0})
            u["tasks"] += 1
            u["tokens"] += r["tokens"] or 0
            if r["status"] == "done":
                u["done"] += 1
            elif r["status"] == "failed":
                u["failed"] += 1
            if r["created_at"] > u["last_active"]:
                u["last_active"] = r["created_at"]
            if r["status"] == "conflict":
                conflicts += 1
            if r["blocked_cmd"]:
                blocked += 1
        for u in by_user.values():
            u["done_rate"] = round(u["done"] / u["tasks"], 3) if u["tasks"] else 0.0
        return {
            "days": days,
            "by_day": [{"day": k, **v} for k, v in sorted(by_day.items())],
            "by_user": sorted(by_user.values(), key=lambda x: x["user"]),
            "conflicts": conflicts,
            "blocked": blocked,
        }
