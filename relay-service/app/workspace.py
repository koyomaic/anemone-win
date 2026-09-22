from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
import time

SKIP_DIRS = {".baseline", ".result", "home", "data", ".git", "node_modules", "__pycache__",
             "venv", ".venv", "models", ".archive",
             # store 台账与任务中间态：由 store CLI 而非任务写入，纳入基线会把
             # manifest.jsonl 的正常刷新误判成任务产出（进而误报冲突）
             "index", "work"}


def is_runtime_dir(dirpath: str, name: str) -> bool:
    """目录是否属运行时环境或非任务产出区，不应纳入基线快照。

    覆盖三类：虚拟环境/模型缓存（动辄数万文件、数 GB，逐文件 sha256 会让 prepare()
    空耗——实测 shared/ 下 venv+venv_funasr+models 共 5.2 万文件、10.5G，快照 ~76s，
    而业务文件仅 2300 个）；归档与台账/中间态（由 store CLI 而非任务写入）。
    pyvenv.cfg 是虚拟环境的确定标记，据此泛化识别，避免把站点特有的 venv 目录名
    硬编码进机队共用的仓库。
    """
    if name in SKIP_DIRS:
        return True
    return os.path.isfile(os.path.join(dirpath, name, "pyvenv.cfg"))


def is_volatile(rel: str, patterns: list[str]) -> bool:
    """路径是否属工具自动重写的运行时产物（凭据/token 缓存、备份文件等）。

    这类改动不是任务产出：不计入 changed_files，也不参与冲突判定。
    """
    return any(fnmatch.fnmatchcase(rel, p) for p in patterns)


def _sha(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _snap(root: str, base: str) -> dict[str, tuple[int, int, str]]:
    """快照 root 下所有文件：{工作区相对路径: (mtime_ns, size, sha256)}。"""
    out: dict[str, tuple[int, int, str]] = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_runtime_dir(dirpath, d)]
        for fn in filenames:
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, base)
            try:
                st = os.stat(p)
                out[rel] = (st.st_mtime_ns, st.st_size, _sha(p))
            except OSError:
                continue
    return out


class Workspace:
    """每任务独立工作目录 + 共享区基线快照 + 完成扫描 + 冲突备份。"""

    def __init__(self, cfg, db):
        self.cfg = cfg
        self.db = db

    def resolve(self, target: str) -> str:
        t = target.strip().strip("/")
        if t.startswith("/"):
            return t
        return os.path.join(self.cfg.workspace_root, t)

    def prepare(self, task: dict, reuse_workdir: str | None = None) -> str:
        workdir = reuse_workdir or os.path.join(self.cfg.task_root, str(task["id"]))
        os.makedirs(workdir, exist_ok=True)
        os.makedirs(os.path.join(workdir, "home"), exist_ok=True)
        if not reuse_workdir:
            os.makedirs(os.path.join(workdir, "data"), exist_ok=True)
        baseline: dict = {}
        baseline.update(_snap(self.cfg.shared_dir, self.cfg.workspace_root))
        for t in task["targets"]:
            baseline.update(_snap(self.resolve(t), self.cfg.workspace_root))
        with open(os.path.join(workdir, ".baseline.json"), "w", encoding="utf-8") as f:
            json.dump(baseline, f, ensure_ascii=False)
        if not task.get("workdir"):
            self.db.set(task["id"], workdir=workdir)
        return workdir

    def cleanup_terminal(self, task: dict) -> None:
        """任务进入终态后清理 workdir 临时目录：home/（agent 缓存）与 data/（工作数据）。

        保留 .result/（变更文件，供跨任务冲突备份）与 .baseline.json。幂等，目录缺失即跳过。
        """
        if not self.cfg.cleanup_runtime:
            return
        workdir = task.get("workdir") or os.path.join(self.cfg.task_root, str(task["id"]))
        for sub in ("home", "data"):
            p = os.path.join(workdir, sub)
            if os.path.isdir(p):
                try:
                    shutil.rmtree(p)
                except OSError:
                    pass

    def scan(self, task: dict) -> dict:
        """任务结束后的变更扫描（事后兜底）。"""
        workdir = task["workdir"]
        with open(os.path.join(workdir, ".baseline.json"), encoding="utf-8") as f:
            baseline = json.load(f)
        cur: dict = {}
        cur.update(_snap(self.cfg.shared_dir, self.cfg.workspace_root))
        for t in task["targets"]:
            cur.update(_snap(self.resolve(t), self.cfg.workspace_root))

        # dws 凭证种子（shared/secrets/dws-cli-seed/）的 token 轮换是基础设施状态
        # （任务 HOME 软链原地刷新，见 shared_knowledge._symlink_dws_seed），非任务产出：
        # 不排除会让触发刷新的 dws 任务误标 conflict，并引发跨任务伪冲突。
        # 轮换过的文件仍记入 volatile，便于排查「为什么这个任务没有产出」。
        seed_rel = os.path.relpath(
            os.path.join(self.cfg.shared_dir, "secrets", "dws-cli-seed"),
            self.cfg.workspace_root) + os.sep
        seed_changed = sorted(
            k for k in set(baseline) | set(cur)
            if k.startswith(seed_rel)
            and (k not in baseline or k not in cur or baseline[k][2] != cur[k][2]))
        baseline = {k: v for k, v in baseline.items() if not k.startswith(seed_rel)}
        cur = {k: v for k, v in cur.items() if not k.startswith(seed_rel)}

        created, modified, deleted = [], [], []
        for rel, meta in cur.items():
            if rel not in baseline:
                created.append(rel)
            elif baseline[rel][2] != meta[2]:  # 仅按内容哈希判定，避免同内容重写的误报
                modified.append(rel)
        for rel in baseline:
            if rel not in cur:
                deleted.append(rel)

        # 其余工具自动重写的运行时产物（token 缓存、冲突备份自身）单列，不计产出、不参与冲突判定
        pats = getattr(self.cfg, "volatile_patterns", None) or []
        volatile = sorted(set(seed_changed)
                          | {r for r in created + modified + deleted if is_volatile(r, pats)})
        created = [r for r in created if not is_volatile(r, pats)]
        modified = [r for r in modified if not is_volatile(r, pats)]
        deleted = [r for r in deleted if not is_volatile(r, pats)]
        created.sort(); modified.sort(); deleted.sort()

        for rel in created + modified:
            src = os.path.join(self.cfg.workspace_root, rel)
            dst = os.path.join(workdir, ".result", rel)
            if os.path.isfile(src):
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)

        all_changed = sorted(set(created) | set(modified) | set(deleted))
        self.db.set(task["id"], changed_files=all_changed)
        return {"created": created, "modified": modified, "deleted": deleted,
                "all": all_changed, "volatile": volatile}

    def _in_runtime_dir(self, rel_file: str) -> bool:
        """rel_file 的任一目录段是否属运行时/非任务产出区（虚拟环境、模型缓存、台账等）。"""
        parts = [p for p in rel_file.replace(os.sep, "/").split("/") if p]
        acc = self.cfg.workspace_root
        for p in parts[:-1]:  # 最后一段是文件名，只判目录段
            if is_runtime_dir(acc, p):
                return True
            acc = os.path.join(acc, p)
        return False

    def backup_version(self, other_task: dict, rel_file: str) -> tuple[str, str]:
        """把 other_task 产出的 rel_file 版本备份为 <文件>.bak-<用户>-HHMM，绝不静默覆盖。

        返回 (备份路径, 跳过原因)；路径为空即未备份，原因写进 conflicts 供人核查。
        两道护栏——历史事故：无上限的 copy2 让一个声明宽 target 的任务把 venv/models
        整棵树备份成 18972 个 .bak、4.92G（单个 model.bin.bak 1.5G，还拷了两份）：
          1. 单文件超过 cfg.backup_max_mb 不拷（RELAY_BACKUP_MAX_MB 可调）
          2. 位于运行时目录（venv/models/.archive/index/work/data 等）不拷
        """
        src = os.path.join(other_task["workdir"], ".result", rel_file)
        if not os.path.isfile(src):
            return "", ""
        limit = self.cfg.backup_max_mb * 1024 * 1024
        size = os.path.getsize(src)
        if size > limit:
            return "", (f"文件 {size / 2 ** 20:.0f}MB 超过备份上限 "
                        f"{self.cfg.backup_max_mb:g}MB，已跳过拷贝，需人工核查")
        if self._in_runtime_dir(rel_file):
            return "", "位于运行时目录（虚拟环境/模型缓存/台账等），不参与冲突备份"
        dst_file = os.path.join(self.cfg.workspace_root, rel_file)
        os.makedirs(os.path.dirname(dst_file), exist_ok=True)
        hm = time.strftime("%H%M", time.localtime(other_task["finished_at"] or time.time()))
        dst = f"{dst_file}.bak-{other_task['user']}-{hm}"
        shutil.copy2(src, dst)
        return dst, ""
