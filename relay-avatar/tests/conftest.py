"""tests/conftest.py —— 公共 fixture：临时状态目录 + 可注入 policy 的 Config。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config as C          # noqa: E402
from app.audit import Audit          # noqa: E402
from app.storage import Storage      # noqa: E402
from app.policy import Policy        # noqa: E402

POLICY_TMPL = """
[identity]
avatar_name = "测试分身"
owner_name = "测试主"
owner_userid = "18434"
self_open_id = "SELFOPENID"
dws_profile = ""

[permissions]
superadmin = ["18434"]
admins = ["100"]
whitelist = ["200"]
blacklist = ["999"]
file_uploaders = ["300"]
grant_valid_days = 90

[security]
bot_blocklist = ["BOTOPENID"]
rate_limit_daily_tasks = 3
media_allowed_ext = [".md", ".txt", ".docx"]

[store]
root = "{root}"
cli = "/nonexistent/store"
least_privilege = true
staging_fallback = true

[knowledge]
local_dir = "{root}/knowledge"

[monitor]
dashboard_enabled = false
alert_to = "18434"

[ingress]
debounce_sec = 20
noise_ignore = ["你好", "收到"]
use_card = false
"""


@pytest.fixture
def state(tmp_path):
    return tmp_path / "avatar"


@pytest.fixture
def shared(tmp_path):
    d = tmp_path / "shared"
    (d / "data").mkdir(parents=True)
    (d / "knowledge" / "工具运维").mkdir(parents=True)
    return d


@pytest.fixture
def cfg(state, shared):
    state.mkdir(parents=True, exist_ok=True)
    (state / "policy.toml").write_text(
        POLICY_TMPL.format(root=str(shared)), encoding="utf-8")
    return C.load(state=state)


@pytest.fixture
def storage(cfg):
    return Storage(cfg)


@pytest.fixture
def audit(cfg):
    return Audit(cfg.state)


@pytest.fixture
def policy(cfg, storage, audit):
    return Policy(cfg, storage, audit)
