from app import card_progress as cp


def test_safe_text_redacts_sensitive():
    assert cp.safe_text("RELAY_ADMIN_TOKEN=abc") == ""
    assert cp.safe_text("读取 /mnt/vol-eltaah12/workspace/report.xlsx") == "读取 report.xlsx"
    assert cp.safe_text("a\x01b") == "a b"


def test_safe_basename():
    assert cp.safe_basename("/mnt/vol-eltaah12/workspace/shared/report/x.html") == "x.html"
    assert cp.safe_basename("/etc/relay.env") == ""


def test_bash_labels_are_safe():
    assert cp.bash_label("python render_report.py --in a.json")[0] == "📑 渲染报告"
    assert cp.bash_label("store add --kind report")[0] == "🗂 台账登记/校验"
    assert cp.bash_label("pytest -q")[0] == "✅ 自检"
    assert cp.bash_label("python /tmp/calc.py")[1] == "calc.py"
    assert cp.bash_label("cat /etc/passwd")[0] == "🔧 执行系统命令"
    assert cp.bash_label("cat /etc/passwd")[1] == ""
    assert cp.bash_label("env | grep TOKEN")[0] == "🔧 执行系统命令"
    assert cp.bash_label("env | grep TOKEN")[1] == ""


def test_tool_step():
    blk = {"name": "Read", "input": {"file_path": "/tmp/a/b.xlsx"}}
    assert cp.tool_step(blk) == "📖 读取文件 · b.xlsx"
    blk = {"name": "Task", "input": {"description": "净头寸分析"}}
    assert cp.tool_step(blk) == "🤖 委派子任务 · 净头寸分析"
    blk = {"name": "mcp__server__query"}
    assert cp.tool_step(blk) == "🔌 调用 query"


def test_budget_bar():
    assert cp.budget_bar(30, 60, 10).endswith("50%")
    assert cp.budget_bar(70, 60, 10).endswith("100%")
    assert cp.budget_bar(10, 0) == ""


def test_render_rich_contains_progress():
    ctx = cp.ProgressCtx(
        avatar_name="鲁大师",
        hint="生成净头寸策略报告",
        backend="relay",
        status="running",
        total_elapsed=750,
        seg_elapsed=150,
        budget=1800,
        seg=1,
        max_seg=3,
        actions=["🤖 委派子任务 · 净头寸分析", "📖 读取文件 · d04.xlsx"],
        controls="发「进度」看详情，发「取消」随时中止",
    )
    out = cp.render(ctx)
    assert out.startswith("🍂 **鲁大师正在处理…**")
    assert "已收到：生成净头寸策略报告" in out
    assert "当前阶段" in out
    assert "总用时 12 分 30 秒" in out
    assert "本段 2 分 30 秒" in out
    assert "第 2/3 段" in out
    assert "本段预算 30 分 0 秒" in out
    assert "委派子任务" in out
    assert "取消" in out


def test_render_local_no_cancel_promise():
    ctx = cp.ProgressCtx(
        avatar_name="鲁大师",
        hint="整理报告",
        backend="local",
        status="local",
        total_elapsed=60,
        seg_elapsed=60,
        budget=300,
        actions=["📑 渲染报告 · render_report.py"],
        controls="本机预处理中；超时会自动转后台持久通道",
    )
    out = cp.render(ctx)
    assert "本机预算 5 分 0 秒" in out
    assert "取消" not in out
    assert "报告装配" in out


def test_render_hides_details_in_group_when_disabled():
    ctx = cp.ProgressCtx(
        avatar_name="鲁大师",
        hint="报告",
        backend="relay",
        status="running",
        total_elapsed=10,
        actions=["📖 读取文件 · secret_name.xlsx"],
        detail=False,
    )
    out = cp.render(ctx)
    assert "secret_name.xlsx" not in out
    assert "最近动作" not in out


def test_render_summary():
    ctx = cp.ProgressCtx(
        avatar_name="鲁大师",
        backend="relay",
        status="running",
        total_elapsed=1500,
        stage="报告装配",
        actions=["📑 渲染报告"],
        controls="发「进度」看详情，发「取消」随时中止",
    )
    out = cp.render_summary(ctx)
    assert "仍在后台执行" in out
    assert "25 分 0 秒" in out
    assert "报告装配" in out


def test_basic_mode_fallback():
    ctx = cp.ProgressCtx(
        avatar_name="鲁大师",
        hint="报告",
        total_elapsed=31,
        note="正在为你办",
        mode="basic",
    )
    out = cp.render(ctx)
    assert "正在为你办…（已用 31s）" in out
