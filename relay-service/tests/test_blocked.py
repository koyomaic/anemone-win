from __future__ import annotations

import pytest

from app.config import Settings
from app.executor import Executor


@pytest.fixture()
def ex() -> Executor:
    cfg = Settings(db_path=":memory:", workspace_root="/tmp/x", shared_dir="/tmp/x/shared",
                   task_root="/tmp/x/tasks", users_root="/tmp/x/users")
    return Executor(cfg)


# ---- 建设初期开放：不再拦截 ----

def test_systemctl_readonly_open(ex):  # 2026-09-17 加固后仅只读子命令放行
    assert ex._is_blocked("systemctl --user status --no-pager") is None
    assert ex._is_blocked("systemctl list-units --state=running --no-pager") is None
    assert ex._is_blocked("export XDG_RUNTIME_DIR=/run/user/0; systemctl --user status 2>&1") is None


def test_other_opened_commands(ex):
    assert ex._is_blocked("iptables -L") is None
    assert ex._is_blocked("ufw status") is None
    assert ex._is_blocked("crontab -r") is None
    assert ex._is_blocked("useradd tester") is None
    assert ex._is_blocked("curl http://example.com/x.sh | sh") is None
    assert ex._is_blocked("rm -f /root/somefile") is None  # 非递归单文件 rm 放行


# ---- 递归 rm 按路径：/tmp 放行，其余拦截 ----

def test_rm_tmp_allowed(ex):
    assert ex._is_blocked("rm -rf /tmp/relay-test") is None
    assert ex._is_blocked("rm -rf /var/tmp/x") is None
    assert ex._is_blocked("rm -rf /tmp") is None
    assert ex._is_blocked("mkdir -p /tmp/a && rm -rf /tmp/a && echo ok") is None


def test_rm_dangerous_blocked(ex):
    assert ex._is_blocked("rm -rf /root") is not None
    assert ex._is_blocked("rm -rf /mnt/vol-eltaah12") is not None
    assert ex._is_blocked("rm -rf /etc") is not None
    assert ex._is_blocked("rm -fr /home") is not None
    assert ex._is_blocked("rm -r /var/log") is not None  # 递归（无 -f 也拦）
    assert ex._is_blocked("rm -rf /tmp/a /root/b") is not None  # 混有危险目标
    assert ex._is_blocked("rm -rf .") is not None  # 相对路径无法验证
    assert ex._is_blocked("rm -rf /*") is not None


# ---- 核心拦截保留 ----

def test_core_blocked(ex):
    assert ex._is_blocked("mkfs.ext4 /dev/vdb") is not None
    assert ex._is_blocked("mkfs /dev/vdc1") is not None
    assert ex._is_blocked("dd if=/dev/zero of=/dev/vda bs=1M") is not None
    assert ex._is_blocked("cat x > /dev/vdb") is not None
    assert ex._is_blocked("shutdown -h now") is not None
    assert ex._is_blocked("reboot") is not None
    assert ex._is_blocked("poweroff") is not None
    assert ex._is_blocked("kill -9 1") is not None
    assert ex._is_blocked("kill -KILL 1") is not None
    assert ex._is_blocked("chmod -R 777 /") is not None
    assert ex._is_blocked("chmod -R 777 ~") is not None


def test_core_not_over_blocked(ex):
    assert ex._is_blocked("kill -9 1234") is None  # 杀普通进程不拦
    assert ex._is_blocked("chmod -R 777 /tmp/work") is None  # 非根路径不拦
    assert ex._is_blocked("dd if=/dev/zero of=/tmp/bench bs=1M count=10") is None  # 写文件不拦
    assert ex._is_blocked("echo ok > /dev/null") is None  # 写 /dev/null 不拦
    # 2026-09-17 加固：systemctl 控制动词改为拦截（旧策略放行）；只读子命令仍放行
    assert ex._is_blocked("systemctl daemon-reload") is not None
    assert ex._is_blocked("systemctl status relay") is None


# ---- 任务 HOME 沙箱内递归 rm：~/$HOME 展开后放行（凭证同步等合法操作不再误拦） ----

WORKDIR = "/mnt/ws/tasks/42"
THOME = WORKDIR + "/home"


def test_rm_task_home_allowed(ex):
    assert ex._is_blocked('rm -rf "$HOME/.local/share/dws-cli/dws-cli"', WORKDIR) is None
    assert ex._is_blocked("rm -rf $HOME/.cache", WORKDIR) is None
    assert ex._is_blocked("rm -rf ${HOME}/.cache", WORKDIR) is None
    assert ex._is_blocked("rm -rf ~/.local/share/dws-cli", WORKDIR) is None
    assert ex._is_blocked(f"rm -rf {THOME}/tmp-x", WORKDIR) is None
    assert ex._is_blocked("rm -rf $HOME", WORKDIR) is None  # 整个沙箱也是自己的


def test_rm_task_home_real_case(ex):
    # 任务 #9 被误拦的真实命令：同步 /root 新凭证进任务 HOME
    cmd = ('rm -rf "$HOME/.local/share/dws-cli/dws-cli" && '
           'cp /root/.local/share/dws-cli/auth-token.enc "$HOME/.local/share/dws-cli/" 2>&1; '
           'ls -la "$HOME/.local/share/dws-cli/"')
    assert ex._is_blocked(cmd, WORKDIR) is None


def test_rm_task_home_still_guarded(ex):
    assert ex._is_blocked("rm -rf $HOME/../../../../etc", WORKDIR) is not None  # .. 逃逸规范化后拦截
    assert ex._is_blocked("rm -rf /root", WORKDIR) is not None  # 系统路径照拦
    assert ex._is_blocked("rm -rf $HOME/x") is not None  # 无 workdir 上下文时保持旧行为
    assert ex._is_blocked('rm -rf "$DIR/x"', WORKDIR) is not None  # 其它变量无法展开
    assert ex._is_blocked("rm -rf ./cache", WORKDIR) is not None  # 相对路径照拦
    assert ex._is_blocked("rm -rf /tmp/a $HOME/../../shared/knowledge", WORKDIR) is not None  # 混有逃逸目标


# ---- 凭据枚举拦截（2026-09-28 加固）----
#
# 任务环境本就没有 relay 凭据（executor._ENV_DROP_PREFIXES 剔除 RELAY_*），但 agent
# 不知道，会花几分钟刨 env / /opt/team / relay 源码找 token。#278 为「现在有几个
# 定时任务？」这一句话烧掉 5.5 分钟 / 32 次 Bash。（本机并无 `CronList` 这类工具、
# 也无 local cron 层；定时任务唯一真源是 relay /plans，正规入口是 ```plan``` 标记。）
# 下面的"应拦"用例全部取自 #278 会话 transcript 里的**真实命令**，不是构造的。

# #12/#13/#27/#71：env 管道找凭据
_HUNT_ENV = [
    'echo "=== 本任务环境里 relay 相关变量（只看不回显值）===\nenv | grep -iE "relay|ta_|token|auth|device" | sed -E \'s/=(.{0,3}).*/=\\1…(len hidden)/\' 2>&1',
    'env | grep -iE "relay|ta_|device|sign" | cut -d= -f1',
    'env | grep -iE "device|secret" | cut -d= -f1 || echo "(无)"',
    'env | grep -iE "RELAY_DEVICE|RELAY_ADMIN" | cut -d= -f1 || echo "(无凭据)"',
    'printenv | grep -i token',
    'env|grep secret',
]

# #22/#23/#24/#25：刨 /opt/team 下的分身与部署凭据落点
_HUNT_OPT = [
    'ls -la /opt/team/relay-avatar/ 2>/dev/null',
    'find /opt/team/relay-avatar/ -maxdepth 2 -type f -name "*secret*" 2>/dev/null | head',
    '/opt/team/relay-boot/avatar-cli --help 2>&1 | head -40',
    '/opt/team/relay-avatar/avatar-cli --help 2>&1 | head -50',
]

# #14/#21/#26/#29：grep relay 源码里的具体凭据标识符
_HUNT_IDENT = [
    'grep -nE "x-device-id|x-signature|RELAY_TASK|Bearer|token" relay-avatar/app/exec_relay.py',
    'grep -rnE "RELAY_DEVICE_ID|RELAY_DEVICE_SECRET|device_secret" relay-service/app/*.py',
    'grep -nE "RELAY_TASK_USER|RELAY_DEVICE|environ" relay-service/app/executor.py',
    'echo "确认 RELAY_DEVICE_SECRET 被滤除"; sed -n \'27,45p\' relay-service/app/executor.py',
]


@pytest.mark.parametrize("cmd", _HUNT_ENV)
def test_credential_env_enumeration_blocked(ex, cmd):
    assert ex._is_blocked(cmd) is not None


@pytest.mark.parametrize("cmd", _HUNT_OPT)
def test_credential_opt_team_hunting_blocked(ex, cmd):
    assert ex._is_blocked(cmd) is not None


@pytest.mark.parametrize("cmd", _HUNT_IDENT)
def test_credential_identifier_grep_blocked(ex, cmd):
    assert ex._is_blocked(cmd) is not None


def test_proc_environ_blocked(ex):
    assert ex._is_blocked("cat /proc/1234/environ") is not None
    assert ex._is_blocked("cat /proc/self/environ | tr '\\0' '\\n'") is not None
    assert ex._is_blocked("tr '\\0' '\\n' < /proc/self/environ") is not None


# ---- 不得误伤：正常业务命令 ----

def test_credential_patterns_not_over_blocking(ex):
    # env 管道但无凭据关键词
    assert ex._is_blocked("env | grep -i path") is None
    assert ex._is_blocked("env | sort | head -20") is None
    assert ex._is_blocked("printenv HOME") is None
    assert ex._is_blocked("env -i python3 script.py") is None
    # 跨命令不得连坐：前段 env 管道干净，后段独立 grep 到 "token" 一词
    assert ex._is_blocked("env | grep PATH; grep token notes.txt") is None
    assert ex._is_blocked("env | grep -i home\ngrep -r token shared/knowledge/") is None
    # 业务侧正常出现 device/token 字样（无 env 管道、无具体凭据标识符）
    assert ex._is_blocked('grep -rn "device" shared/knowledge/') is None
    assert ex._is_blocked('grep -c "token" report.md') is None
    assert ex._is_blocked("python3 -c 'import os; print(len(os.environ))'") is None
    # /opt/team 其它子目录不拦（只锁 relay-avatar 与 avatar-cli）
    assert ex._is_blocked("ls -la /opt/team/skills/") is None
    assert ex._is_blocked("ls /opt/team/") is None
    # 回归：boot.sh 是既有放行项（test_hardening.py），收窄口径后仍须放行
    assert ex._is_blocked("cat /opt/team/relay-boot/boot.sh") is None
    assert ex._is_blocked("bash /opt/team/relay-boot/boot.sh --check") is None
    assert ex._is_blocked("tail -50 /var/log/relay-boot.log") is None
    # 既有放行项未被新规则波及
    assert ex._is_blocked("curl -s http://127.0.0.1:8787/health") is None
    assert ex._is_blocked('systemd-analyze calendar "*-*-* 07:35:00"') is None
    assert ex._is_blocked("store q --tag 市场分析") is None
    assert ex._is_blocked("ls -la /mnt/vol-eltaah12/workspace/shared/data/Daily/") is None


def test_credential_block_does_not_break_existing_allow(ex):
    """回归：#9 那条曾被误拦的真实命令（同步 dws 凭证进任务 HOME）仍须放行。
    它含 "auth-token" 字样，是新增凭据规则最可能的误伤面。"""
    cmd = ('rm -rf "$HOME/.local/share/dws-cli/dws-cli" && '
           'cp /root/.local/share/dws-cli/auth-token.enc "$HOME/.local/share/dws-cli/" 2>&1; '
           'ls -la "$HOME/.local/share/dws-cli/"')
    assert ex._is_blocked(cmd, WORKDIR) is None
