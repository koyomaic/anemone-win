from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from conftest import submit, wait_task  # noqa: E402

from app.main import create_app  # noqa: E402


def _start_app(cfg, port: int) -> uvicorn.Server:
    app = create_app(cfg)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1",
                                           port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 20
    while time.time() < deadline:
        try:
            if httpx.get(base + "/health", timeout=1).status_code == 200:
                return server
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError("second relay server did not start")


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.integration
def test_restart_recovery_reruns_orphans(relay_factory):
    """进程重启后，原 queued/running 任务必须被重新排队执行（不再变孤儿）。"""
    r1 = relay_factory()
    base1, cfg = r1["base"], r1["cfg"]
    admin = httpx.Client(base_url=base1, headers={"Authorization": "Bearer admin-test"},
                         timeout=30)
    tok = admin.post("/users", json={"name": "rec"}).json()["token"]
    t1 = submit(base1, tok, "只回复 ok，不要使用任何工具。")["task_id"]
    time.sleep(1.0)
    # 直接停掉第一个服务（模拟崩溃/重启，任务可能停在 running 或 queued）
    r1["server"].should_exit = True
    time.sleep(2.0)

    port2 = _free_port()
    server2 = _start_app(cfg, port2)
    base2 = f"http://127.0.0.1:{port2}"
    try:
        t = wait_task(base2, tok, t1, timeout=300)
        assert t["status"] in ("done", "review"), t
    finally:
        server2.should_exit = True
        admin.close()


# ── 计划内重启 vs 真失败：信号退出码的识别与归类 ──────────────────
# 此前两条路只有硬杀能活：kill -9/OOM 时行留在 running，由 lifespan 开头的启动
# 恢复逻辑接回；而**计划内重启**（systemd stop → SIGTERM）会被 executor 判成
# failed，还回用户一句「❌ 没办成」。#149 实测：已续到第 2/3 段，被部署杀掉，
# 前 30 分钟产出白费。正好是反的。
from app.executor import Executor, signal_name           # noqa: E402
from app.config import Settings                          # noqa: E402
from app.main import _restart_interrupted                # noqa: E402
import asyncio                                           # noqa: E402


@pytest.mark.parametrize("rc,expected", [
    (143, "SIGTERM"),      # shell 口径：128+15（#149 实测拿到的就是这个）
    (137, "SIGKILL"),      # 128+9，OOM killer 常见
    (-15, "SIGTERM"),      # asyncio 直接子进程口径：负数
    (-9, "SIGKILL"),
])
def test_signal_name_recognises_both_conventions(rc, expected):
    assert signal_name(rc) == expected


@pytest.mark.parametrize("rc", [0, 1, 2, 128])
def test_signal_name_empty_for_normal_exit(rc):
    """正常退出/agent 自己报错不得被当成信号终止，否则真失败会被重新排队。"""
    assert signal_name(rc) == ""


def test_signal_name_unknown_signal_degrades_gracefully():
    assert signal_name(200) == "signal 72"


@pytest.mark.parametrize("shutting_down,timed_out,exit_code,expected", [
    (True, False, 143, True),    # 计划内重启 + SIGTERM → 重新排队
    (True, False, 137, True),    # SIGKILL 同样接回
    (False, False, 143, False),  # 不是重启（被别的信号杀）→ 照常 failed
    (True, True, 143, False),    # 超时也是 SIGTERM，但预算用尽＝真失败
    (True, False, 1, False),     # agent 自己报错，与重启无关
    (True, False, 0, False),     # 正常退出
    (False, True, 143, False),
])
def test_restart_interrupted_truth_table(shutting_down, timed_out, exit_code,
                                         expected):
    assert _restart_interrupted(shutting_down, timed_out,
                                exit_code) is expected


def test_signal_exit_error_names_signal_not_stderr_noise(tmp_path):
    """被信号打死时，错误主因必须是信号本身，不是 stderr 末行的无关噪声。

    #149 实测：rc=143（部署重启所杀），但 stderr 末行恰好是
    [claude-code:unrecognized_model]，被顶成错误主因，排查直接跑偏。
    """
    script = tmp_path / "fake_agent.sh"
    script.write_text(
        "#!/bin/sh\n"
        "echo '[claude-code:unrecognized_model] {\"model\":\"qwen3.8-flash\"}' >&2\n"
        "kill -TERM $$\n", encoding="utf-8")
    script.chmod(0o755)
    cfg = Settings(db_path=str(tmp_path / "relay.db"),
                   workspace_root=str(tmp_path),
                   shared_dir=str(tmp_path / "shared"),
                   task_root=str(tmp_path / "tasks"),
                   users_root=str(tmp_path / "users"),
                   engine="claude", agent_bin=str(script), task_timeout=30)
    wd = tmp_path / "wd"
    wd.mkdir()

    async def go():
        return await Executor(cfg).run(
            task_id=1, workdir=str(wd), prompt="hi", resume_session=None,
            data_dir=None, cancel=asyncio.Event(),
            publish=lambda *a, **k: None)

    res = asyncio.run(go())
    # 直接 spawn 走 asyncio 负数口径（-15）；#149 的 143 是 agent 经 shell 包装的
    # 128+signo 口径。两种都要认，故这里断言口径无关的结论（映射本身由上面
    # test_signal_name_recognises_both_conventions 分别锁住）。
    assert signal_name(res.exit_code) == "SIGTERM", res
    assert res.error.startswith(
        f"agent exited with code {res.exit_code}（SIGTERM 终止"), res.error
    # 噪声仍保留（可能藏着真因），只是不再打头阵
    assert "unrecognized_model" in res.error
    assert res.error.index("SIGTERM") < res.error.index("unrecognized_model")
