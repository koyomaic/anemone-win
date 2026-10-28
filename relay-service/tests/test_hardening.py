"""中转自身保护加固（2026-09-17）：环境收敛、共享 venv PATH、git/服务控制拦截。

硬边界（relay.service 挂载命名空间）属 systemd 层，无法在单测覆盖，
由部署后冒烟矩阵验证（攻击任务 EROFS/ENOENT + 正向任务全通）。
"""
from __future__ import annotations

import os

import pytest

from app.config import DEFAULT_BLOCKED_PATTERNS, Settings
from app.executor import Executor, _scrubbed_environ


def _cfg(tmp_path, **kw) -> Settings:
    return Settings(
        db_path=str(tmp_path / "relay.db"),
        workspace_root=str(tmp_path),
        shared_dir=str(tmp_path / "shared"),
        task_root=str(tmp_path / "tasks"),
        users_root=str(tmp_path / "users"),
        **kw,
    )


# ---- 环境继承收敛 ----

def test_scrubbed_environ_drops_relay_and_forge_secrets(monkeypatch):
    monkeypatch.setenv("RELAY_ADMIN_TOKEN", "ta_secret")
    monkeypatch.setenv("RELAY_DB", "postgresql://u:p@h/db")
    monkeypatch.setenv("RELAY_DASHBOARD_PASSWORD", "pw")
    monkeypatch.setenv("RELAY_TLS_DIR", "/x")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_x")
    monkeypatch.setenv("GH_TOKEN", "ghp_y")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:8790")
    env = _scrubbed_environ()
    for k in ("RELAY_ADMIN_TOKEN", "RELAY_DB", "RELAY_DASHBOARD_PASSWORD",
              "RELAY_TLS_DIR", "GITHUB_TOKEN", "GH_TOKEN"):
        assert k not in env
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:8790"
    assert env["PATH"] == os.environ["PATH"]


def test_build_env_no_relay_leak(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_ADMIN_TOKEN", "ta_secret")
    ex = Executor(_cfg(tmp_path, engine="claude"))
    env = ex.build_env(str(tmp_path), user="u1")
    assert "RELAY_ADMIN_TOKEN" not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["IS_SANDBOX"] == "1"  # claude 引擎必需变量不受收敛影响


# ---- 共享 venv PATH 前置（pip install 放开）----

def test_build_env_prepends_shared_venv(tmp_path):
    venv_bin = tmp_path / "shared" / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    ex = Executor(_cfg(tmp_path))
    env = ex.build_env(str(tmp_path))
    assert env["PATH"].startswith(str(venv_bin) + os.pathsep)


def test_build_env_shared_venv_override(tmp_path):
    custom = tmp_path / "custom-venv" / "bin"
    custom.mkdir(parents=True)
    ex = Executor(_cfg(tmp_path, shared_venv=str(tmp_path / "custom-venv")))
    env = ex.build_env(str(tmp_path))
    assert env["PATH"].startswith(str(custom) + os.pathsep)


def test_build_env_path_untouched_without_venv(tmp_path):
    ex = Executor(_cfg(tmp_path))
    env = ex.build_env(str(tmp_path))
    assert env["PATH"] == os.environ["PATH"]


# ---- 拦截清单：git 写操作 / 服务控制 ----

BLOCKED = [
    "git push origin main",
    "git commit -m x",
    "cd /mnt/vol-eltaah12/finaagent && git rebase -i head~2",
    "git reset --hard origin/main",
    "git tag v9.9",
    "git remote add evil https://x",
    "git -c /mnt/vol-eltaah12/finaagent push --dry-run origin main",  # 全局选项间隔绕过（冒烟实测）
    "git -c user.email=a@b commit -m x",
    "git --git-dir=/x/.git push",
    "systemctl restart relay",
    "systemctl stop claude-proxy",
    "systemctl daemon-reload",
    "systemctl disable relay",
    "systemctl --no-pager restart relay",
    "service relay restart",
    "pkill -f app.serve",
    "killall -9 uvicorn",
]
ALLOWED = [
    "git log --oneline -5",
    "git status",
    "git diff head~1",
    "git clone https://example.com/x /tmp/x",
    "git fetch origin",
    "git --no-pager log -3",
    "git -c /mnt/vol-eltaah12/finaagent log --oneline -3",
    "git remote -v",
    "git branch -vv",
    "systemctl status relay",
    "systemctl is-active relay",
    "systemctl list-timers",
    "systemctl --no-pager status relay",
    "journalctl -u relay -n 50",
    "cat /opt/team/relay-boot/boot.sh",
    "pip install cowsay",
    "python3 -m venv /tmp/v && /tmp/v/bin/pip install requests",
    "curl -s http://127.0.0.1:8787/health",
]


@pytest.mark.parametrize("cmd", BLOCKED)
def test_blocked_commands(tmp_path, cmd):
    ex = Executor(_cfg(tmp_path))
    assert ex._is_blocked(cmd, str(tmp_path)), cmd


@pytest.mark.parametrize("cmd", ALLOWED)
def test_allowed_commands(tmp_path, cmd):
    ex = Executor(_cfg(tmp_path))
    assert ex._is_blocked(cmd, str(tmp_path)) is None, cmd


def test_defaults_contain_self_protection():
    joined = "\n".join(DEFAULT_BLOCKED_PATTERNS)
    assert "git" in joined and "systemctl" in joined and "pkill" in joined


# ---- 变更扫描：dws 种子 token 轮换不算任务改动（冒烟实测误标 conflict 修复）----

def test_scan_excludes_dws_seed_rotation(tmp_path):
    from app.workspace import Workspace

    class _DB:
        def set(self, *a, **k):
            pass

    cfg = _cfg(tmp_path)
    seed = tmp_path / "shared" / "secrets" / "dws-cli-seed" / "dws-cli"
    seed.mkdir(parents=True)
    (seed / "auth-token_x.enc").write_text("old")
    know = tmp_path / "shared" / "knowledge"
    know.mkdir(parents=True)
    (know / "a.md").write_text("v1")

    ws = Workspace(cfg, _DB())
    workdir = tmp_path / "tasks" / "1"
    workdir.mkdir(parents=True)
    task = {"id": 1, "workdir": str(workdir), "targets": []}
    ws.prepare(task)

    (seed / "auth-token_x.enc").write_text("rotated")   # 基础设施轮换
    (know / "a.md").write_text("v2")                    # 任务真实产出

    res = ws.scan(task)
    assert "shared/knowledge/a.md" in res["all"]
    assert not any("dws-cli-seed" in r for r in res["all"])
